"""Deterministic budgets, gated on every pull request (CI-02).

Two kinds of number come out of a benchmark and only one of them can be a gate.

**Wall-clock timings cannot.** They move with the runner, the neighbour on the
box and the CPU governor, and a threshold picked from one run fails on the next
reviewer's laptop — which does not catch regressions, it teaches the team to
press "re-run CI" until it goes green. Those stay reported (`benchmarks/run.py`
uploads its JSON) and a real timing gate needs an agreed noise tolerance from
repeated samples on a fixed runner, which is a decision to make with data rather
than a number to invent here.

**Sizes can.** How many bytes a document response carries, how many a mesh
encodes to, how large the entry bundle is — these are deterministic functions of
the code. They are also the ones that actually regressed historically: the mesh
was a base64 data URI inlined in the document, 99% of a 1164 KB response,
re-downloaded on every edit; the bundle was 1435 KB in one chunk. Both were
noticed by a person looking, months later.

So the budgets here are byte counts with headroom, and the failure message says
what grew and by how much.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

if not os.environ.get("FORGE_BENCH_KEEP_DB"):
    import tempfile

    os.environ["DATABASE_URL"] = (
        f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='forge_budget_'), 'b.db')}")
    os.environ["FORGE_DATA_DIR"] = tempfile.mkdtemp(prefix="forge_budget_data_")
os.environ.setdefault("FORGE_RATE_LIMIT", "0")
os.environ.setdefault("FORGE_JOB_WORKERS", "0")

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "backend"))

KIB = 1024


class Budget:
    """One measured size and the ceiling it must stay under."""

    def __init__(self, name: str, limit_bytes: int, why: str):
        self.name = name
        self.limit_bytes = limit_bytes
        self.why = why


#: Ceilings, calibrated against what the code measures today (the figures in
#: each rationale). Roughly an order of magnitude of headroom, which sounds
#: generous and is the right size for what this catches: the failure mode is a
#: payload's *shape* changing — something inlined that should be a URL — and
#: those are 50-100x, not 20%. A ceiling close to the current value would fail
#: on a fixture with a slightly larger sketch and teach everyone to raise it.
BUDGETS = {
    "document_response_bytes": Budget(
        "document_response_bytes", 32 * KIB,
        "measures ~1.9 KiB: a document is a tree and some metadata, and the "
        "mesh is a hash-keyed URL. It was once 1164 KiB, 99% of it a base64 "
        "STL re-sent on every edit. This ceiling is far above any real tree and "
        "far below an inlined mesh."),
    "document_response_with_parameters_bytes": Budget(
        "document_response_with_parameters_bytes", 48 * KIB,
        "measures ~2.8 KiB. The same, plus `derived.numeric_tree` — the "
        "expression-resolved copy the viewport reads, which is a second whole "
        "tree and so legitimately larger."),
    "mesh_payload_bytes": Budget(
        "mesh_payload_bytes", 64 * KIB,
        "measures ~21 KiB: the indexed, normal-free wire form of a plate with a "
        "bore at resolution 96. STL of the same triangles is ~2.8x, so this "
        "fails if the transport ever reverts to triangle soup."),
    "operation_envelope_bytes": Budget(
        "operation_envelope_bytes", 48 * KIB,
        "measures ~3.3 KiB: what an agent gets back from one edit and puts in a "
        "prompt. It carries the whole document, so it moves with the first "
        "budget and fails for the same reasons."),
    "job_poll_bytes": Budget(
        "job_poll_bytes", 4 * KIB,
        "measures ~0.4 KiB. Small on purpose: a poll does not echo the job's "
        "inputs back, because the caller that submitted them already has them. "
        "This is the budget a polling loop multiplies."),
    "frontend_entry_gzip_bytes": Budget(
        "frontend_entry_gzip_bytes", 96 * KIB,
        "measures ~60 KiB gzipped. The first paint; everything else is split at "
        "the screen and the panel. It was 388 KiB in one chunk before the "
        "split, so this fails when something stops being lazy."),
}


def measure_backend() -> dict:
    """Every payload budget, measured through the real routes."""
    from fastapi.testclient import TestClient

    from app.db import init_db
    from app.services import document_analysis, document_service as docs, rollout

    init_db()
    sys.path.insert(0, str(_ROOT / "backend" / "tests"))
    from conftest import http_part  # noqa: E402 — the same bootstrap the suite uses

    from main import app

    client = TestClient(app)
    measured = {}
    with rollout.rollout():
        doc_id = http_part(client, (0.12, 0.08, 0.01), resolution=96)
        client.post(f"/api/documents/{doc_id}/op", json={
            "op": "add_hole",
            "args": {"radius": 0.006, "depth": 0.01, "through": True,
                     "axis": {"x": 0, "y": 0, "z": 1}}})

        measured["document_response_bytes"] = len(
            client.get(f"/api/documents/{doc_id}").content)

        envelope = client.post(f"/api/documents/{doc_id}/op", json={
            "op": "set_feature_params",
            "args": {"feature_id": _box(doc_id),
                     "params": {"size": {"x": 0.13, "y": 0.08, "z": 0.01}}}})
        measured["operation_envelope_bytes"] = len(envelope.content)

        doc = docs.get_document(doc_id)
        payload = document_analysis.mesh_payload(doc_id, doc.derived.mesh_hash)
        measured["mesh_payload_bytes"] = len(payload or b"")

        # With parameters, so `derived.numeric_tree` is present — the case that
        # actually sets the ceiling.
        client.post(f"/api/documents/{doc_id}/op", json={
            "op": "set_parameters",
            "args": {"parameters": [{"name": "width", "expr": "0.13"}]}})
        measured["document_response_with_parameters_bytes"] = len(
            client.get(f"/api/documents/{doc_id}").content)

        started = client.post(f"/api/documents/{doc_id}/package", json={})
        job_id = started.json()["id"]
        measured["job_poll_bytes"] = len(
            client.get(f"/api/documents/{doc_id}/jobs/{job_id}").content)
    return measured


def measure_frontend() -> dict:
    """The gzipped entry chunk, from a build that has already run.

    Read off `dist/` rather than built here: CI builds the frontend in its own
    job, and building it twice to measure it is a minute of runner time for a
    number the first build already produced.
    """
    import gzip

    dist = _ROOT / "frontend" / "dist" / "assets"
    if not dist.is_dir():
        return {}
    entries = sorted(dist.glob("main-*.js"))
    if not entries:
        return {}
    return {"frontend_entry_gzip_bytes":
            len(gzip.compress(entries[0].read_bytes(), 9))}


def check(measured: dict) -> Tuple[List[str], List[str]]:
    passes, failures = [], []
    for name, value in sorted(measured.items()):
        budget = BUDGETS.get(name)
        if budget is None:
            continue
        line = (f"{name}: {value / KIB:.1f} KiB "
                f"(budget {budget.limit_bytes / KIB:.0f} KiB, "
                f"{100 * value / budget.limit_bytes:.0f}% used)")
        (failures if value > budget.limit_bytes else passes).append(
            line if value <= budget.limit_bytes else f"{line}\n    {budget.why}")
    return passes, failures


def _box(doc_id: str) -> str:
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services import document_service as docs

    for node in walk_nodes(docs.get_document(doc_id).feature_tree):
        if node.get("type") == "BoxFeature":
            return node["id"]
    raise RuntimeError("no box")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", default=None)
    parser.add_argument("--frontend-only", action="store_true")
    args = parser.parse_args(argv)

    measured = {} if args.frontend_only else measure_backend()
    measured.update(measure_frontend())

    passes, failures = check(measured)
    for line in passes:
        print(f"  ok   {line}")
    for line in failures:
        print(f"  OVER {line}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"measured": measured,
                       "budgets": {n: b.limit_bytes for n, b in BUDGETS.items()}},
                      fh, indent=2, sort_keys=True)
    if failures:
        print(f"\n{len(failures)} payload budget(s) exceeded.")
        return 1
    print(f"\n{len(passes)} payload budget(s) within limits.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
