"""The workflow scenarios the refactor must not break (FLOW-01).

Not unit tests of a service — sequences a person actually performs, written down
so that moving compute off the request path, splitting the store, or changing who
owns a cache has something to fail against. Each one is a race or an ordering
that the individual pieces all handle correctly on their own:

- edit -> evaluate -> analyse -> export, the spine of a session;
- an edit while a long job is running;
- an undo landing while an evaluation of the newer tree is still in flight;
- switching documents with a request still open against the first.

The last two are the ones that used to have no test at all, because neither is
reproducible by clicking carefully — they need two requests deliberately
overlapped, which is what a thread pool here is for.
"""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from conftest import http_part
from app.services import cad_operations, derived_cache, document_service as docs
from main import app

client = TestClient(app)


def _part(size=(0.08, 0.05, 0.01)) -> str:
    return http_part(client, size, resolution=32)


def _box_id(doc_id: str) -> str:
    from app.cad_kernel.core.feature_tree import walk_nodes

    for node in walk_nodes(docs.get_document(doc_id).feature_tree):
        if node.get("type") == "BoxFeature":
            return node["id"]
    raise AssertionError("no box in that tree")


def _resize(doc_id: str, x: float, **kwargs):
    return cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id),
        "params": {"size": {"x": x, "y": 0.05, "z": 0.01}}}, **kwargs)


def test_edit_evaluate_analyse_export_end_to_end():
    """The spine: every stage sees the body the stage before it produced."""
    doc_id = _part()
    assert _resize(doc_id, 0.12)["ok"] is True

    doc = docs.get_document(doc_id)
    assert doc.derived.error is None
    assert doc.derived.bbox is not None
    width = doc.derived.bbox.max.x - doc.derived.bbox.min.x
    assert abs(width - 0.12) < 1e-3

    dfm = cad_operations.execute("dfm", doc_id, {"process": "additive", "resolution": 32})
    assert dfm["ok"] is True, dfm.get("error")

    export = cad_operations.execute("export", doc_id, {"format": "stl"})
    assert export["ok"] is True, export.get("error")
    fetched = client.get(export["result"]["url"])
    assert fetched.status_code == 200
    assert len(fetched.content) == export["result"]["bytes"]


def test_an_edit_during_a_running_job_is_not_blocked_by_it():
    """Modelling stays responsive while something expensive runs.

    The job here is a real evaluation of a *different* document, run on another
    thread — the point is that an edit does not wait behind it, not that the
    evaluation is fast.
    """
    editing = _part()
    busy = _part((0.1, 0.1, 0.04))

    with ThreadPoolExecutor(max_workers=2) as pool:
        job = pool.submit(cad_operations.execute, "dfm", busy,
                          {"process": "additive", "resolution": 48})
        edit = pool.submit(_resize, editing, 0.15)
        assert edit.result()["ok"] is True
        assert job.result()["ok"] is True

    doc = docs.get_document(editing)
    assert abs((doc.derived.bbox.max.x - doc.derived.bbox.min.x) - 0.15) < 1e-3


def test_an_analysis_that_finishes_after_an_edit_is_not_published_as_current():
    """The undo-during-evaluation shape, from the result's point of view.

    A slow reading is taken against one body and lands against another. Nothing
    stops the caller showing it — it is a correct reading of *something* — but
    it may not be filed as the document's current answer, and `freshness` is
    what makes the difference visible after a reload.
    """
    doc_id = _part()
    doc = docs.get_document(doc_id)
    taken = derived_cache.analysis_fingerprint(doc, "analysis", {})

    _resize(doc_id, 0.2)

    doc = docs.get_document(doc_id)
    kept = derived_cache.publish(doc, "analysis", {"max_stress": 42.0},
                                 settings={}, fingerprint=taken)
    assert kept is False
    assert derived_cache.freshness(doc, "analysis") == "missing"


def test_an_undo_landing_after_a_newer_edit_is_refused_when_it_carries_a_revision():
    """Undo writes the *older* tree, so ordering is the whole question.

    The client serialises these itself (`historySyncSettled`), and this is the
    server-side backstop for when it cannot: with a revision attached, the late
    write is refused instead of quietly resurrecting the undone geometry.
    """
    doc_id = _part()
    original_tree = copy.deepcopy(docs.get_document(doc_id).feature_tree)
    revision_when_undo_was_issued = docs.get_document(doc_id).revision

    _resize(doc_id, 0.2)  # a newer edit lands first

    resp = client.put(f"/api/documents/{doc_id}", json={
        "feature_tree": original_tree, "resolution": 32,
        "expected_revision": revision_when_undo_was_issued})
    assert resp.status_code == 409

    doc = docs.get_document(doc_id)
    assert abs((doc.derived.bbox.max.x - doc.derived.bbox.min.x) - 0.2) < 1e-3


def test_switching_documents_leaves_each_ones_geometry_its_own():
    """A response arriving for the document you just left must not describe it.

    Server-side the guarantee is narrower than the UI's but it is the one the UI
    is built on: every derived cache is keyed by document id, so an evaluation
    of A can never write B's mesh — even when the two overlap in time.
    """
    a, b = _part((0.08, 0.05, 0.01)), _part((0.2, 0.05, 0.01))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_resize, a, 0.09)
        second = pool.submit(_resize, b, 0.21)
        assert first.result()["ok"] is True
        assert second.result()["ok"] is True

    doc_a, doc_b = docs.get_document(a), docs.get_document(b)
    assert doc_a.derived.mesh_hash != doc_b.derived.mesh_hash
    assert abs((doc_a.derived.bbox.max.x - doc_a.derived.bbox.min.x) - 0.09) < 1e-3
    assert abs((doc_b.derived.bbox.max.x - doc_b.derived.bbox.min.x) - 0.21) < 1e-3
    # And the mesh URL each carries resolves to that document's own triangles.
    assert derived_cache.cached_mesh(a, doc_b.derived.mesh_hash) is None


def test_two_concurrent_edits_to_one_document_both_report_a_revision():
    """Whichever lands second, the revision it reports is the one it produced.

    Without a revision passed in, last-write-wins is the documented behaviour —
    what must not happen is two writes reporting the *same* revision, which
    would make the counter useless for the callers that do pass one.
    """
    doc_id = _part()

    with ThreadPoolExecutor(max_workers=2) as pool:
        envs = [f.result() for f in
                [pool.submit(_resize, doc_id, 0.11), pool.submit(_resize, doc_id, 0.12)]]

    assert all(e["ok"] for e in envs)
    assert envs[0]["revision"] != envs[1]["revision"]
    assert docs.get_document(doc_id).revision == max(e["revision"] for e in envs)


def test_a_failed_evaluation_leaves_the_document_readable_and_says_why():
    """A tree that does not evaluate is a warning, not a wedged document."""
    doc_id = _part()
    env = cad_operations.execute("set_parameters", doc_id, {
        "parameters": [{"name": "width", "expr": "undefined_thing * 2"}]})
    doc = docs.get_document(doc_id)
    assert doc is not None
    # Either the parameter is refused outright or the document carries the error
    # — both are honest; silently evaluating to something else is not.
    assert (env["ok"] is False) or doc.derived.error or env.get("warnings")
    assert client.get(f"/api/documents/{doc_id}").status_code == 200
