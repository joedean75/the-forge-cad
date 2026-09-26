"""What an external dataset would actually contribute, before training on it.

`docs/CAD-DATASET-SOURCING.md` says to download first and widen the converter
second, "by whichever unsupported call actually dominates `DROPS`, not by
whichever looks easiest from the card". This is how that number gets measured.

**Two columns, and the second is the one to act on.** `convert_cadquery` drops a
program at its *first* unsupported call, so a histogram of causes says which call
is met earliest — not which call is worth adding. A program using `threePointArc`
*and* `revolve` is not unblocked by adding `revolve`. So every program is also
scanned for the *whole set* of calls this converter cannot express, and
`unblocks` counts the programs whose set is exactly one call: those are the
programs that supporting it would actually win. It is the same distinction
`decompiler.corpus_report` draws with `blocking`, where a `BoxFeature` beside an
`OptimizeFeature` shows a 0% rate and sends you off to fix boxes.

Reads the JSONL a column projection produces — nothing here downloads anything,
and the whole 349 GB Zero-To-CAD repo is 58 MB when only the program column is
fetched. Usage:

    python -m datasets.yield_report /tmp/z2c-test.jsonl
    python -m datasets.yield_report sketches.jsonl --kind sketchgraphs
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from typing import Dict, List


def _calls(source: str) -> List[str]:
    """Every method name called in the program, in the order they appear."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]


def _program_of(row: dict) -> str:
    for key in ("program", "cadquery_file", "source", "code"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def cadquery_yield(rows: List[dict]) -> Dict:
    from datasets import external

    external.DROPS.clear()
    converted = 0
    ops_emitted: Counter = Counter()
    seen: Counter = Counter()
    blocked_by: Counter = Counter()
    unblocks: Counter = Counter()

    for row in rows:
        source = _program_of(row)
        if not source:
            external._drop("row:no_program")
            continue

        calls = _calls(source)
        seen.update(set(calls))
        # `_PLACEMENT` calls position the next one rather than adding material and
        # are skipped by the converter, so they are not what blocks a program.
        unsupported = {c for c in calls
                       if c not in external.SUPPORTED and c not in external.PLACEMENT}
        for call in unsupported:
            blocked_by[call] += 1
        if len(unsupported) == 1:
            unblocks[next(iter(unsupported))] += 1

        converted_row = external.convert_cadquery(source, name=str(row.get("uuid") or "model"))
        if converted_row:
            converted += 1
            for op in converted_row["ops"]:
                ops_emitted[op["op"]] += 1

    return {"rows": len(rows), "converted": converted,
            "rate": round(converted / max(1, len(rows)), 4),
            "by_cause": dict(sorted(external.DROPS.items(), key=lambda kv: -kv[1])),
            "calls_seen": dict(seen.most_common()),
            "blocked_by": dict(blocked_by.most_common()),
            "unblocks": dict(unblocks.most_common()),
            "ops_emitted": dict(ops_emitted.most_common())}


def sketchgraphs_yield(rows: List[dict]) -> Dict:
    from datasets import external

    external.DROPS.clear()
    converted = 0
    for row in rows:
        if external.convert_sketchgraphs(row):
            converted += 1
    return {"rows": len(rows), "converted": converted,
            "rate": round(converted / max(1, len(rows)), 4),
            "by_cause": dict(sorted(external.DROPS.items(), key=lambda kv: -kv[1]))}


def _table(title: str, counts: Dict[str, int], total: int, limit: int = 18) -> str:
    lines = [f"\n{title}"]
    for name, count in list(counts.items())[:limit]:
        lines.append(f"  {name:32} {count:6,}  {100 * count / max(1, total):5.1f}%")
    if len(counts) > limit:
        lines.append(f"  … {len(counts) - limit} more")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", help="JSONL of rows (one program or sketch each)")
    parser.add_argument("--kind", default="cadquery", choices=("cadquery", "sketchgraphs"))
    parser.add_argument("--limit", type=int, default=0, help="0 = every row")
    parser.add_argument("--json", action="store_true", help="the report as JSON")
    args = parser.parse_args(argv)

    rows: List[dict] = []
    with open(args.path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
            if args.limit and len(rows) >= args.limit:
                break

    report = (cadquery_yield(rows) if args.kind == "cadquery"
              else sketchgraphs_yield(rows))
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    total = report["rows"]
    print(f"{report['converted']:,} of {total:,} converted ({100 * report['rate']:.1f}%)")
    print(_table("dropped by cause (the first refusal met):",
                 report["by_cause"], total))
    if report.get("unblocks"):
        print(_table("programs ONE call away — supporting it wins these:",
                     report["unblocks"], total))
        print(_table("programs mentioning an unsupported call (they need all of theirs):",
                     report["blocked_by"], total))
    if report.get("ops_emitted"):
        print(_table("operations the conversions emitted:", report["ops_emitted"],
                     max(1, sum(report["ops_emitted"].values()))))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
