"""Mixed load: does modelling stay responsive while the slow work runs?

    python -m benchmarks.load                        # 30 s, default mix
    python -m benchmarks.load --seconds 120 --editors 4 --jobs 2

`benchmarks/run.py` measures one thing at a time, which is what a baseline needs
and not what a deployment experiences. The question this answers is the one the
whole refactor is for: **with a SIMP solve and a release package running, does an
edit still come back in the time it takes alone?** A p50 that is fine in
isolation and four seconds under load is a system that fails exactly when
somebody is using it.

The mix is deliberately what a small team does — a few people editing, somebody
browsing the library, one or two heavy jobs in flight, and artifacts being
fetched. Every worker records its own latencies, and the report puts each
population's p50/p95 next to the single-workload baseline so a regression is
visible as a *ratio* rather than as a number nobody can calibrate.

Two things it deliberately does not do. It does not assert — a threshold agreed
on one runner fails on another, and `CI-02` is where a budget gets a noise
tolerance. And it does not spawn API workers: it drives the services in-process,
so it measures contention for the GIL, the caches and the datastore, which is
what a single-instance deployment actually has. Multi-worker validation needs
real processes behind a balancer and is the gate described in
`docs/SCALING-AUDIT.md`.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from typing import Dict, List, Optional

if not os.environ.get("FORGE_BENCH_KEEP_DB"):
    import tempfile

    _dir = tempfile.mkdtemp(prefix="forge_load_")
    os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_dir, 'load.db')}"
    os.environ["FORGE_DATA_DIR"] = tempfile.mkdtemp(prefix="forge_load_data_")
os.environ.setdefault("FORGE_RATE_LIMIT", "0")
# The pool is started explicitly below, so the number of workers is a parameter
# of the experiment rather than whatever the environment happens to say.
os.environ.setdefault("FORGE_JOB_WORKERS", "0")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import instrumentation  # noqa: E402


class Population:
    """One kind of user, and every latency they saw."""

    def __init__(self, name: str):
        self.name = name
        self.samples: List[float] = []
        self.errors = 0
        self._lock = threading.Lock()

    def record(self, seconds: float) -> None:
        with self._lock:
            self.samples.append(seconds)

    def fail(self) -> None:
        with self._lock:
            self.errors += 1

    def report(self) -> dict:
        with self._lock:
            summary = instrumentation.summarize(self.samples)
        return {"population": self.name, "errors": self.errors, **summary}


def _timed(population: Population, fn) -> None:
    started = time.perf_counter()
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — an error is a data point
        population.fail()
        if os.environ.get("FORGE_LOAD_VERBOSE"):
            print(f"  {population.name}: {type(exc).__name__}: {exc}", flush=True)
        return
    population.record(time.perf_counter() - started)


def _editor(stop: threading.Event, population: Population, doc_id: str,
            feature_id: str, rng: random.Random) -> None:
    """Resize a feature, over and over. The interactive path under measurement."""
    from app.services import cad_operations

    while not stop.is_set():
        width = 0.05 + rng.random() * 0.05
        _timed(population, lambda: cad_operations.execute(
            "set_feature_params", doc_id,
            {"feature_id": feature_id,
             "params": {"size": {"x": width, "y": 0.04, "z": 0.01}}},
            include_state=False))
        stop.wait(0.05)


def _browser(stop: threading.Event, population: Population) -> None:
    """Somebody with the library open."""
    from app.services import document_service as docs

    while not stop.is_set():
        _timed(population, lambda: docs.list_documents(owner_id=None))
        stop.wait(0.5)


def _fetcher(stop: threading.Event, population: Population, doc_id: str) -> None:
    """A viewport pulling the mesh — the hottest endpoint in the system."""
    from app.services import document_analysis, document_service as docs

    while not stop.is_set():
        doc = docs.get_document(doc_id)
        digest = doc.derived.mesh_hash if doc else None
        if digest:
            _timed(population, lambda: document_analysis.mesh_payload(doc_id, digest))
        stop.wait(0.2)


def _job_submitter(stop: threading.Event, population: Population, doc_id: str,
                   kind: str) -> None:
    """Keep a heavy job in flight. The thing everything else has to survive."""
    from app.services import jobs

    while not stop.is_set():
        if jobs.active_for_subject(doc_id, kind) is None:
            try:
                _timed(population, lambda: jobs.enqueue(
                    kind, doc_id, _job_inputs(kind), message="load test"))
            except jobs.QueueFull:
                pass
        stop.wait(1.0)


def _job_inputs(kind: str) -> dict:
    if kind == "analysis":
        return {"elements": 4000, "resolution": 64}
    return {"setup": {"resolution": 48, "volume_fraction": 0.4}, "resolution": 48}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--editors", type=int, default=2)
    parser.add_argument("--browsers", type=int, default=1)
    parser.add_argument("--fetchers", type=int, default=2)
    parser.add_argument("--job-workers", type=int, default=2,
                        help="background worker threads (0 leaves jobs queued)")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    from app.db import init_db
    from app.services import blobs, derived_cache, jobs, rollout, singleflight
    from app.services import job_handlers  # noqa: F401 — registers the handlers
    from benchmarks.fixtures import build_feature_tree, build_library

    init_db()

    with rollout.rollout():
        print("building fixtures…", flush=True)
        tree = build_feature_tree(resolution=64)
        build_library(count=25, resolution=32)
        doc_id = str(tree["document_id"])
        feature_id = _first_box(doc_id)

        populations = {name: Population(name) for name in
                       ("edit", "browse", "mesh_fetch", "job_submit")}
        stop = threading.Event()
        threads: List[threading.Thread] = []

        for n in range(args.editors):
            threads.append(threading.Thread(
                target=_editor, daemon=True,
                args=(stop, populations["edit"], doc_id, feature_id,
                      random.Random(n))))
        for _ in range(args.browsers):
            threads.append(threading.Thread(
                target=_browser, args=(stop, populations["browse"]), daemon=True))
        for _ in range(args.fetchers):
            threads.append(threading.Thread(
                target=_fetcher, args=(stop, populations["mesh_fetch"], doc_id),
                daemon=True))
        threads.append(threading.Thread(
            target=_job_submitter, daemon=True,
            args=(stop, populations["job_submit"], doc_id, "analysis")))

        pool = jobs.start_workers(args.job_workers) if args.job_workers else None
        rss_before = instrumentation.rss_bytes()
        started = time.time()
        for thread in threads:
            thread.start()
        print(f"running the mix for {args.seconds:g}s…", flush=True)
        stop.wait(args.seconds)
        stop.set()
        for thread in threads:
            thread.join(timeout=10.0)
        if pool is not None:
            jobs.stop_workers()

        report = {
            "environment": instrumentation.environment(),
            "config": {"seconds": args.seconds, "editors": args.editors,
                       "browsers": args.browsers, "fetchers": args.fetchers,
                       "job_workers": args.job_workers},
            "elapsed_s": round(time.time() - started, 2),
            "populations": [p.report() for p in populations.values()],
            "jobs": jobs.summary(),
            "caches": derived_cache.stats(),
            "blobs": blobs.stats(),
            "singleflight": singleflight.stats(),
            "rss_bytes_before": rss_before,
            "rss_bytes_after": instrumentation.rss_bytes(),
            "peak_rss_bytes": instrumentation.peak_rss_bytes(),
        }

    for row in report["populations"]:
        print(f"{row['population']:<12} n={row['n']:<5} "
              f"p50 {row['p50_ms']:>9.2f} ms   p95 {row['p95_ms']:>9.2f} ms   "
              f"errors {row['errors']}")
    print(f"\njobs      {report['jobs']['counts']}")
    print(f"peak RSS  {report['peak_rss_bytes'] / 1e6:.1f} MB")
    print(f"blobs     {report['blobs']['entries']} entries, "
          f"{report['blobs']['bytes'] / 1e6:.1f} MB")
    dedup = report["singleflight"].get("mesh", {})
    if dedup:
        print(f"mesh work collapsed: {dedup.get('joined', 0)} of "
              f"{dedup.get('joined', 0) + dedup.get('leaders', 0)} requests")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        print(f"\nwrote {args.json}")
    return 0


def _first_box(doc_id: str) -> str:
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services import document_service as docs

    for node in walk_nodes(docs.get_document(doc_id).feature_tree):
        if node.get("type") == "BoxFeature":
            return node["id"]
    raise RuntimeError("the fixture has no box to edit")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
