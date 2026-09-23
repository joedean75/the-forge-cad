"""Build the copilot fine-tuning dataset.

    python -m datasets.build --out data/ft
    python -m datasets.build --out data/ft --sources recorded,catalog --val-fraction 0.1
    python -m datasets.build --out data/ft --sources imported --imported path/to/converted.jsonl

Writes `train.jsonl` / `val.jsonl` (training lines only — `messages`, `tools`,
`parallel_tool_calls`), `train.meta.jsonl` / `val.meta.jsonl` (provenance, one
line per sample, same order), `stats.json` and `CARD.md`.

Nothing here invents geometry. A synthetic sequence is kept only when the
decompiler's replay rebuilds the same body, and each sample says where it came
from and under what licence, so a set that later turns out to be unusable can be
filtered out by `source` rather than rebuilt from memory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

# The datastore reads DATABASE_URL at import time. A build against a developer's
# real library is a legitimate thing to want (`--sources library`), so this does
# not force a throwaway database the way the benchmarks do — it just says which
# one it used, in the card.
from datasets import corpus  # noqa: E402

SOURCES = ("recorded", "catalog", "template", "library", "imported")


def _gather(names: List[str], args) -> List[corpus.Sample]:
    samples: List[corpus.Sample] = []
    for name in names:
        if name == "recorded":
            samples += corpus.recorded_samples(include_unfinished=args.include_unfinished,
                                               profile=args.profile)
        elif name == "catalog":
            samples += corpus.catalog_samples(limit=args.catalog_limit, profile=args.profile)
        elif name == "template":
            samples += corpus.template_samples(variations=args.variations,
                                               profile=args.profile)
        elif name == "library":
            samples += corpus.library_samples(args.owner, limit=args.library_limit,
                                              profile=args.profile)
        elif name == "imported":
            samples += _imported(args.imported)
        else:
            raise SystemExit(f"unknown source {name!r}; one of {', '.join(SOURCES)}")
        print(f"  {name:10} -> {len(samples):6,} samples so far", flush=True)
    return samples


def _imported(path: str) -> List[corpus.Sample]:
    """Samples from an external dataset already converted to operation sequences.

    The converter is `datasets/external.py`, which reads a local copy the user
    downloaded under whatever terms they accepted — nothing here fetches a
    dataset, for the reason `supplier_parts` does not call a portal. Each line
    is `{"goal", "ops", "source", "license"}`.
    """
    if not path:
        raise SystemExit("--sources imported needs --imported <file.jsonl>")
    from datasets import external

    return external.samples_from_file(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="directory to write into")
    parser.add_argument("--sources", default="recorded,catalog,template",
                        help=f"comma-separated: {', '.join(SOURCES)}")
    parser.add_argument("--val-fraction", type=float, default=0.1,
                        help="share of *sources* held out (never of samples)")
    parser.add_argument("--catalog-limit", type=int, default=None)
    parser.add_argument("--profile", default="full", choices=("full", "finetuned"),
                        help="which prompt a sample carries. 'finetuned' drops the "
                             "schemas and the operation directory — for a model that "
                             "has the API in its weights, and the profile that run "
                             "must then be served with (FORGE_COPILOT_PROMPT)")
    parser.add_argument("--variations", type=int, default=3,
                        help="how many re-dimensioned copies of each template to build; "
                             "each is a verified sequence with a goal stating its sizes")
    parser.add_argument("--library-limit", type=int, default=None)
    parser.add_argument("--owner", default=None, help="owner id for --sources library")
    parser.add_argument("--imported", default=None, help="converted external JSONL")
    parser.add_argument("--include-unfinished", action="store_true",
                        help="also learn from runs that stopped short (off by default: "
                             "a truncated run's last action is not evidence)")
    args = parser.parse_args(argv)

    names = [n.strip() for n in args.sources.split(",") if n.strip()]
    print(f"building from: {', '.join(names)}", flush=True)
    samples = _gather(names, args)
    if not samples:
        print("no samples; nothing written", file=sys.stderr)
        return 1

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

    report = {"profile": args.profile,
              "figures": figures, "duplicates_dropped": built["duplicates_dropped"],
              "dropped_sources": dict(sorted(corpus.DROPS.items())),
              "sources": names, "val_fraction": args.val_fraction,
              "database": os.environ.get("DATABASE_URL", "(default dev database)")}
    with open(os.path.join(args.out, "stats.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    with open(os.path.join(args.out, "CARD.md"), "w") as fh:
        fh.write(card(report))
    print(json.dumps(figures, indent=2))
    print(f"written to {args.out}", flush=True)
    return 0


def card(report: dict) -> str:
    """The dataset card: what is in it, how it was made, and what it is not."""
    figures = report["figures"]
    train, val = figures.get("train", {}), figures.get("val", {})
    lines = [
        "# Forge copilot fine-tuning set",
        "",
        "One sample is **one planner turn**: the system prompt and tool schemas a run",
        "was offered, a user turn carrying the goal, the transcript so far and the state",
        "as it stood, and the single tool call as the label. Built by",
        "`copilot.planner_request` — the same function a live turn uses — so the format",
        "cannot drift from inference.",
        "",
        f"**Prompt profile: `{report.get('profile', 'full')}`.** Serve the trained model "
        f"with `FORGE_COPILOT_PROMPT={report.get('profile', 'full')}`; the `finetuned` "
        "profile sends no tool schemas and expects the JSON answer the rules describe.",
        "",
        "| | train | val |",
        "|---|---|---|",
        f"| samples | {train.get('samples', 0):,} | {val.get('samples', 0):,} |",
        f"| finishing turns | {train.get('finish_samples', 0):,} | {val.get('finish_samples', 0):,} |",
        f"| turns that recover from a failure | {train.get('recovery_samples', 0):,} | {val.get('recovery_samples', 0):,} |",
        f"| geometry verified by replay | {train.get('verified', 0):,} | {val.get('verified', 0):,} |",
        f"| prompt exact (state as sent, not as recorded) | {train.get('state_exact', 0):,} | {val.get('state_exact', 0):,} |",
        f"| approx tokens | {train.get('approx_tokens', 0):,} | {val.get('approx_tokens', 0):,} |",
        f"| approx tokens per sample | {train.get('mean_tokens', 0):,} | {val.get('mean_tokens', 0):,} |",
        "",
        f"Sources: {', '.join(report['sources'])}. Duplicates dropped: "
        f"{report['duplicates_dropped']:,}. Held out: {report['val_fraction']:.0%} of "
        "*sources*, so every turn of one run or one document is in the same split — "
        "splitting by sample would measure memorisation of a run's own prefix.",
        "",
        "### What produced nothing",
        "",
        "| cause | count |",
        "|---|---|",
        *[f"| {cause} | {count:,} |" for cause, count in (report.get("dropped_sources") or {}).items()],
        "" if report.get("dropped_sources") else "Nothing was dropped.",
        "",
        "## By source",
        "",
        "| source | train | val |",
        "|---|---|---|",
    ]
    for source in sorted(set(train.get("by_source", {})) | set(val.get("by_source", {}))):
        lines.append(f"| {source} | {train.get('by_source', {}).get(source, 0):,} | "
                     f"{val.get('by_source', {}).get(source, 0):,} |")
    lines += [
        "",
        "## Most common labels",
        "",
        "| operation | train |",
        "|---|---|",
    ]
    for op, count in list(train.get("by_op", {}).items())[:15]:
        lines.append(f"| `{op}` | {count:,} |")
    lines += [
        "",
        "## How each source is verified",
        "",
        "* **recorded** — real copilot runs that ended in a good outcome. A failed call",
        "  stays in the transcript, because recovering from one is most of what an agent",
        "  does, but a failed call is never the label.",
        "* **catalog / template / library** — the body is decompiled into an operation",
        "  sequence, replayed, and the mesh hashes compared; a sequence that rebuilds a",
        "  different part is dropped, not taught.",
        "* **imported** — an external dataset converted to operation sequences and",
        "  replayed through `execute`; only sequences that build a valid solid are kept.",
        "  Every sample carries its `license` in the meta file.",
        "",
        "## What this set is not",
        "",
        "* Not a measure of whether a model is any good: that is `evals/` (the",
        "  adversarial suite runs with no key; the goal suite needs one).",
        "* Not a source of reference images. Runs whose prompt carried images are",
        "  excluded, because the bytes are never kept and the sample would be missing",
        "  what the model was looking at.",
        "* Not free of the recorded models' mistakes. Every sample's meta names the",
        "  model that produced it, so a later filter can exclude one.",
        "",
        f"Database: `{report['database']}`.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
