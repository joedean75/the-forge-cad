"""Ingest converted rows in child processes, because OpenCascade can segfault.

Three builds of the Zero-To-CAD ingest died with no traceback. The cause was not
the data and not memory: **`Segmentation fault (core dumped)`, exit 139** — a
crash inside OCC, which takes the interpreter with it. No `try` reaches that, so
the only defence is that the thing which dies is not the thing holding the
results.

So the rows are ingested a chunk at a time in a subprocess. A chunk that crashes
or hangs is **bisected** down to the single row responsible, which is recorded by
cause and skipped; everything around it survives. That also covers the other
failure this ingest met — one program held a build for 68 minutes at 283% CPU on a
whole-body chamfer over a revolve — because a timeout is a crash as far as this is
concerned, and neither can be caught in-process.

    python -m datasets.ingest --rows data/z2c-imported.jsonl --out data/ft-v6-imported

The output layout is `datasets.build`'s, produced by the same `corpus.assemble`
and `corpus.stats`, so the result is interchangeable with a corpus built the
ordinary way.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from typing import Dict, List, Tuple

#: Rows per child. Small enough that one crash costs little work and the bisect
#: is short; large enough that interpreter and OCC start-up (about eight seconds)
#: is not most of the run.
CHUNK = 25

#: Seconds a chunk may take. At the measured ~1.7 s a row a full chunk is under a
#: minute, so this is loose enough not to fire on slow geometry and tight enough
#: that the hour-long hang is caught.
TIMEOUT = 420


def _slice(rows_path: str, start: int, count: int) -> List[str]:
    out: List[str] = []
    with open(rows_path) as handle:
        for index, line in enumerate(handle):
            if index < start:
                continue
            if index >= start + count:
                break
            if line.strip():
                out.append(line.rstrip("\n"))
    return out


def _run_chunk(rows_path: str, start: int, count: int,
               timeout: int) -> Tuple[bool, List[dict], Dict[str, int], str]:
    """One child process over `rows[start:start+count]`."""
    with tempfile.TemporaryDirectory() as scratch:
        emit = os.path.join(scratch, "samples.jsonl")
        proc = subprocess.run(
            [sys.executable, "-m", "datasets.ingest", "--worker",
             "--rows", rows_path, "--start", str(start), "--count", str(count),
             "--emit", emit],
            capture_output=True, text=True, timeout=None if timeout <= 0 else timeout,
            check=False, env=os.environ.copy())
        if proc.returncode != 0 or not os.path.exists(emit):
            return False, [], {}, f"exit{proc.returncode}"
        rows, drops = [], {}
        with open(emit) as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("_drops") is not None:
                    drops = record["_drops"]
                else:
                    rows.append(record)
        return True, rows, drops, ""


def ingest(rows_path: str, *, chunk: int = CHUNK, timeout: int = TIMEOUT,
           limit: int = 0) -> Tuple[List[dict], Dict[str, int]]:
    total = sum(1 for line in open(rows_path) if line.strip())
    if limit:
        total = min(total, limit)
    collected: List[dict] = []
    drops: Dict[str, int] = {}

    def note(cause: str) -> None:
        drops[cause] = drops.get(cause, 0) + 1

    def attempt(start: int, count: int) -> None:
        """Run a span, halving it when the child dies rather than losing it all."""
        try:
            ok, rows, chunk_drops, why = _run_chunk(rows_path, start, count, timeout)
        except subprocess.TimeoutExpired:
            ok, rows, chunk_drops, why = False, [], {}, "timeout"
        if ok:
            collected.extend(rows)
            for cause, n in chunk_drops.items():
                drops[cause] = drops.get(cause, 0) + n
            return
        if count == 1:
            # The row that kills the process, named. Every other ingest failure is
            # counted by cause; this one used to take the whole build with it.
            note(f"crash:{why}")
            print(f"  row {start} killed the worker ({why}) — skipped", flush=True)
            return
        half = count // 2
        attempt(start, half)
        attempt(start + half, count - half)

    for start in range(0, total, chunk):
        count = min(chunk, total - start)
        attempt(start, count)
        print(f"  {min(start + count, total)}/{total} rows, "
              f"{len(collected)} samples", flush=True)
    return collected, drops


def _worker(args) -> int:
    """One chunk, in its own process, writing samples as JSON lines."""
    from app.services import persistence
    from datasets import external

    persistence.init_db()
    lines = _slice(args.rows, args.start, args.count)
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
        handle.write("\n".join(lines) + "\n")
        slice_path = handle.name
    external.DROPS.clear()
    try:
        samples = external.samples_from_file(slice_path)
    finally:
        os.unlink(slice_path)
    with open(args.emit, "w") as out:
        for sample in samples:
            out.write(json.dumps({"messages": sample.messages, "tools": sample.tools,
                                  "meta": sample.meta}) + "\n")
        out.write(json.dumps({"_drops": dict(external.DROPS)}) + "\n")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", required=True)
    parser.add_argument("--out")
    parser.add_argument("--chunk", type=int, default=CHUNK)
    parser.add_argument("--timeout", type=int, default=TIMEOUT)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--val-fraction", type=float, default=0.1, dest="val_fraction")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--start", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--count", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--emit", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        return _worker(args)
    if not args.out:
        parser.error("--out is required")

    from datasets import build as build_module
    from datasets import corpus

    records, drops = ingest(args.rows, chunk=args.chunk, timeout=args.timeout,
                            limit=args.limit)
    if not records:
        print("no samples; nothing written", file=sys.stderr)
        return 1
    samples = [corpus.Sample(messages=r["messages"], tools=r.get("tools"),
                             meta=r.get("meta") or {}) for r in records]
    built = corpus.assemble(samples, val_fraction=args.val_fraction)
    splits = built["splits"]
    figures = corpus.stats(splits)
    os.makedirs(args.out, exist_ok=True)
    for name, rows in splits.items():
        with open(os.path.join(args.out, f"{name}.jsonl"), "w") as fh:
            for sample in rows:
                fh.write(json.dumps(sample.line()) + "\n")
        with open(os.path.join(args.out, f"{name}.meta.jsonl"), "w") as fh:
            for sample in rows:
                fh.write(json.dumps(sample.meta) + "\n")
    report = {"profile": "finetuned", "figures": figures,
              "duplicates_dropped": built["duplicates_dropped"],
              "dropped_sources": dict(sorted(drops.items())),
              "sources": ["imported"], "val_fraction": args.val_fraction,
              "database": os.environ.get("DATABASE_URL", "(default dev database)")}
    with open(os.path.join(args.out, "stats.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    with open(os.path.join(args.out, "CARD.md"), "w") as fh:
        fh.write(build_module.card(report))
    print(json.dumps(figures, indent=2))
    print(f"written to {args.out}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
