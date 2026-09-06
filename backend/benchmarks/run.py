"""Run the fixture workloads and report where the time and the bytes go.

    python -m benchmarks.run                     # the fast fixtures, 5 repeats
    python -m benchmarks.run --repeat 9 --json out.json
    python -m benchmarks.run --fixture exact_plate --slow

Every scenario is measured **cold and warm**, and the distinction is the whole
point of the exercise: an edit that misses every cache rebuilds an OCC body, and
one that hits `_evaluation_key` returns without meshing anything. Reporting one
mean over both describes the ratio of this harness's calls, not the system's.

What comes out is a JSON document — the environment it ran on, the repeat count,
and per scenario p50/p95, the stage spans underneath, the cache/sampler counters
and the bytes. A budget written against it names the fixture and the hardware,
because a latency number without either cannot be reproduced or refuted.

Nothing here asserts. Turning a measurement into a gate is `CI-02`'s job and it
needs a noise tolerance agreed against repeated samples on a fixed runner; a
threshold invented on the first run fails the next reviewer's laptop.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
from typing import Callable, Dict, List, Optional

# The datastore reads DATABASE_URL at import time, so a throwaway file has to be
# chosen before anything under `app` is imported. A benchmark that wrote into a
# developer's real library would be measuring their parts and growing their db.
if not os.environ.get("FORGE_BENCH_KEEP_DB"):
    import tempfile

    _db = os.path.join(tempfile.mkdtemp(prefix="forge_bench_"), "bench.db")
    os.environ["DATABASE_URL"] = f"sqlite:///{_db}"
    os.environ["FORGE_DATA_DIR"] = tempfile.mkdtemp(prefix="forge_bench_data_")
os.environ.setdefault("FORGE_RATE_LIMIT", "0")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import instrumentation  # noqa: E402
from benchmarks.fixtures import FIXTURES  # noqa: E402


class Scenario:
    """One measured call, with the fixture state it needs already built."""

    def __init__(self, name: str, run: Callable[[], object], *,
                 payload: Optional[Callable[[object], int]] = None):
        self.name = name
        self.run = run
        self.payload = payload


def _time(fn: Callable[[], object]) -> tuple[float, object]:
    started = time.perf_counter()
    value = fn()
    return time.perf_counter() - started, value


def measure(scenario: Scenario, repeat: int) -> dict:
    """Cold once, then `repeat` warm runs, with the spans of the warm ones.

    The cold sample is reported on its own rather than folded in. It is one
    observation by definition — the second call is warm — so a percentile over
    it would be a percentile over n=1 dressed up as a distribution.
    """
    gc.collect()
    rss_before = instrumentation.rss_bytes()

    cold_recorder = instrumentation.Recorder(f"{scenario.name}:cold")
    with instrumentation.recording(recorder=cold_recorder):
        cold_s, value = _time(scenario.run)

    warm: List[float] = []
    warm_recorder = instrumentation.Recorder(f"{scenario.name}:warm")
    with instrumentation.recording(recorder=warm_recorder):
        for _ in range(repeat):
            elapsed, value = _time(scenario.run)
            warm.append(elapsed)

    out = {
        "scenario": scenario.name,
        "cold_ms": round(cold_s * 1000.0, 3),
        "warm": instrumentation.summarize(warm),
        "cold_detail": cold_recorder.report(),
        "warm_detail": warm_recorder.report(),
        "rss_bytes_before": rss_before,
        "rss_bytes_after": instrumentation.rss_bytes(),
        "rss_delta_bytes": instrumentation.rss_bytes() - rss_before,
        "peak_rss_bytes": instrumentation.peak_rss_bytes(),
    }
    if scenario.payload is not None:
        try:
            out["payload_bytes"] = scenario.payload(value)
        except Exception as exc:  # noqa: BLE001 — a size is never worth failing a run for
            out["payload_bytes_error"] = str(exc)
    return out


# ---------------------------------------------------------------------------
# Scenario construction
# ---------------------------------------------------------------------------

def _document_scenarios(name: str, doc_id: str, resolution: int) -> List[Scenario]:
    """Open, edit, re-evaluate, export — the four things a modelling session does."""
    from app.services import artifacts, cad_operations, document_service as docs

    def open_doc():
        return docs.get_document(doc_id)

    def re_evaluate():
        doc = docs.get_document(doc_id)
        return docs.evaluate_document(doc, resolution=resolution)

    flip = {"n": 0}

    def parameter_edit():
        # Two alternating sizes rather than one: setting the value a feature
        # already has is an `_evaluation_key` hit, so a repeat of it would
        # measure the cache and call it an edit.
        flip["n"] += 1
        z = 0.010 if flip["n"] % 2 else 0.011
        env = cad_operations.execute(
            "set_feature_params", doc_id,
            {"feature_id": _first_box(doc_id), "params": {"size": {"x": 0.12, "y": 0.08, "z": z}}},
            include_state=False)
        if not env.get("ok"):
            raise RuntimeError(env.get("error"))
        return env

    def export_step():
        return artifacts.export(doc_id, "step", resolution=resolution)

    def mesh_bytes(_value=None):
        doc = docs.get_document(doc_id)
        payload = docs.cached_mesh(doc.id, doc.derived.mesh_hash or "")
        return len(payload) if payload else 0

    return [
        Scenario(f"{name}.open", open_doc),
        Scenario(f"{name}.evaluate", re_evaluate, payload=lambda _v: mesh_bytes()),
        Scenario(f"{name}.edit", parameter_edit, payload=lambda _v: mesh_bytes()),
        Scenario(f"{name}.export_step", export_step,
                 payload=lambda v: int(v.get("bytes", 0)) if isinstance(v, dict) else 0),
    ]


def _first_box(doc_id: str) -> str:
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services import document_service as docs

    for node in walk_nodes(docs.get_document(doc_id).feature_tree):
        if node.get("type") == "BoxFeature":
            return node["id"]
    raise RuntimeError("fixture has no box to edit")


def build_scenarios(names: List[str], owner_id: Optional[str]) -> List[Scenario]:
    from app.services import document_service as docs

    scenarios: List[Scenario] = []
    for name in names:
        fixture = FIXTURES[name]
        state = fixture.build(owner_id=owner_id, resolution=fixture.resolution,
                              **fixture.kwargs)
        if name == "library":
            def listing(_owner=owner_id):
                return docs.list_documents(owner_id=_owner)

            scenarios.append(Scenario("library.list", listing,
                                      payload=lambda v: len(json.dumps(
                                          [s.model_dump() for s in v]).encode())))
            continue
        if name == "optimize":
            from app.models.schemas import OptimizationSetup

            doc_id = str(state["document_id"])

            def solve(_doc_id=doc_id, _res=fixture.resolution):
                return docs.run_document_optimize(
                    _doc_id, OptimizationSetup(resolution=_res), resolution=_res)

            scenarios.append(Scenario("optimize.solve", solve))
            continue
        scenarios.extend(_document_scenarios(name, str(state["document_id"]),
                                             fixture.resolution))
    return scenarios


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeat", type=int, default=5,
                        help="warm repeats per scenario (default 5)")
    parser.add_argument("--fixture", action="append", default=None,
                        help="run only this fixture; repeatable")
    parser.add_argument("--slow", action="store_true",
                        help="include the fixtures excluded by default (the SIMP solve)")
    parser.add_argument("--json", default=None, help="write the full report here")
    args = parser.parse_args(argv)

    from app.db import init_db
    from app.services import rollout

    init_db()

    names = args.fixture or [n for n, f in FIXTURES.items() if args.slow or not f.slow]
    unknown = [n for n in names if n not in FIXTURES]
    if unknown:
        parser.error(f"unknown fixture(s): {', '.join(unknown)}; "
                     f"known: {', '.join(sorted(FIXTURES))}")

    report = {
        "environment": instrumentation.environment(),
        "repeat": args.repeat,
        "fixtures": {n: FIXTURES[n].description for n in names},
        "scenarios": [],
    }

    # Metering off and resolution pinned: `capped_resolution` otherwise clamps to
    # whichever plan the account happens to hold, so the same fixture would
    # evaluate to different geometry for different runners.
    with rollout.rollout():
        for scenario in build_scenarios(names, owner_id=None):
            result = measure(scenario, args.repeat)
            report["scenarios"].append(result)
            print(f"{result['scenario']:<32} "
                  f"cold {result['cold_ms']:>9.2f} ms   "
                  f"p50 {result['warm']['p50_ms']:>8.2f} ms   "
                  f"p95 {result['warm']['p95_ms']:>8.2f} ms   "
                  f"{result.get('payload_bytes', 0) / 1024:>8.1f} KiB",
                  flush=True)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
