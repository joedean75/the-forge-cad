"""Revision-safe writes and input fingerprints (STATE-01/02, CACHE-01/02).

The property under test throughout is the same one: **a result is only ever
shown as current when the inputs that produced it still describe the document**,
and **a write only lands on the version its author read**. Both used to be
handled by the browser tab that happened to make the change, which is no help
after a reload, for a second viewer, or for an agent.
"""

from __future__ import annotations

import pytest
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


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------

def test_every_write_moves_the_revision():
    doc_id = _part()
    before = docs.get_document(doc_id).revision
    cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id),
        "params": {"size": {"x": 0.09, "y": 0.05, "z": 0.01}}})
    assert docs.get_document(doc_id).revision > before


def test_a_rename_moves_the_revision_but_not_the_geometry_fingerprint():
    """The distinction the two counters exist for.

    A revision counts writes, so a rename is one. A fingerprint hashes the
    inputs an evaluation reads, so a rename is not — otherwise renaming a part
    would rebuild its body and re-send its mesh.
    """
    doc_id = _part()
    doc = docs.get_document(doc_id)
    before_rev, before_fp = doc.revision, doc.derived.eval_key

    resp = client.put(f"/api/documents/{doc_id}", json={"name": "renamed", "resolution": 32})
    assert resp.status_code == 200

    doc = docs.get_document(doc_id)
    assert doc.revision > before_rev
    assert doc.derived.eval_key == before_fp


def test_a_read_that_ensures_geometry_does_not_count_as_a_write():
    """Otherwise merely looking at a document invalidates somebody's write."""
    doc_id = _part()
    resolution = docs.get_document(doc_id).derived.resolution
    before = docs.get_document(doc_id).revision
    for _ in range(3):
        docs.evaluate_document(docs.get_document(doc_id), resolution=resolution)
    assert docs.get_document(doc_id).revision == before


def test_a_write_against_a_stale_revision_is_refused():
    doc_id = _part()
    stale = docs.get_document(doc_id).revision
    cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id),
        "params": {"size": {"x": 0.09, "y": 0.05, "z": 0.01}}})

    env = cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id),
        "params": {"size": {"x": 0.10, "y": 0.05, "z": 0.01}}},
        expected_revision=stale)
    assert env["ok"] is False
    assert env["error"]["code"] == "PRECONDITION"
    # Both numbers, so the caller learns how far behind it is without a re-read.
    assert str(stale) in env["error"]["message"]
    assert env["revision"] == docs.get_document(doc_id).revision


def test_the_revision_a_call_returns_can_be_chained_into_the_next_one():
    doc_id = _part()
    feature = _box_id(doc_id)
    revision = docs.get_document(doc_id).revision
    for width in (0.09, 0.10, 0.11):
        env = cad_operations.execute("set_feature_params", doc_id, {
            "feature_id": feature, "params": {"size": {"x": width, "y": 0.05, "z": 0.01}}},
            expected_revision=revision)
        assert env["ok"] is True, env.get("error")
        revision = env["revision"]


def test_omitting_the_revision_keeps_the_old_behaviour():
    """Optimistic concurrency is opt-in: one editor in one tab pays no ceremony."""
    doc_id = _part()
    cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id), "params": {"size": {"x": 0.09, "y": 0.05, "z": 0.01}}})
    env = cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id), "params": {"size": {"x": 0.10, "y": 0.05, "z": 0.01}}})
    assert env["ok"] is True


def test_the_op_route_reports_the_revision_and_honours_an_expected_one():
    doc_id = _part()
    resp = client.post(f"/api/documents/{doc_id}/op", json={
        "op": "set_feature_params",
        "args": {"feature_id": _box_id(doc_id),
                 "params": {"size": {"x": 0.09, "y": 0.05, "z": 0.01}}}})
    assert resp.status_code == 200
    revision = resp.json()["revision"]
    assert revision is not None

    stale = client.post(f"/api/documents/{doc_id}/op", json={
        "op": "set_feature_params",
        "expected_revision": revision - 1,
        "args": {"feature_id": _box_id(doc_id),
                 "params": {"size": {"x": 0.10, "y": 0.05, "z": 0.01}}}})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "PRECONDITION"


def test_a_whole_tree_put_against_a_stale_revision_is_a_409():
    """The PUT is the write most able to clobber, because it replaces everything."""
    doc_id = _part()
    tree = client.get(f"/api/documents/{doc_id}").json()["feature_tree"]
    stale = docs.get_document(doc_id).revision
    client.put(f"/api/documents/{doc_id}", json={"name": "moved on", "resolution": 32})

    resp = client.put(f"/api/documents/{doc_id}",
                      json={"feature_tree": tree, "resolution": 32,
                            "expected_revision": stale})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "REVISION_CONFLICT"
    assert detail["expected"] == stale
    assert detail["actual"] > stale


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def test_the_geometry_fingerprint_ignores_what_cannot_move_the_body():
    doc_id = _part()
    doc = docs.get_document(doc_id)
    before = derived_cache.geometry_fingerprint(doc, 32)
    doc.name = "something else"
    doc.spec = {"requirements": []}
    assert derived_cache.geometry_fingerprint(doc, 32) == before


def test_the_geometry_fingerprint_moves_with_the_resolution():
    doc = docs.get_document(_part())
    assert (derived_cache.geometry_fingerprint(doc, 32)
            != derived_cache.geometry_fingerprint(doc, 64))


def test_a_material_change_invalidates_a_solve_but_not_a_dfm_report():
    """The invalidation matrix, asserted rather than described.

    Stress is a property of the material as much as of the shape. None of the
    topology measurements DFM makes — wall thickness, draft, tool access —
    depends on what the part is made of, and calling that report stale on a
    material change would train people to ignore the staleness marker.
    """
    doc = docs.get_document(_part())
    analysis_before = derived_cache.analysis_fingerprint(doc, "analysis", {})
    dfm_before = derived_cache.analysis_fingerprint(doc, "dfm", {"process": "milling"})

    doc.material_id = "ti-6al-4v"

    assert derived_cache.analysis_fingerprint(doc, "analysis", {}) != analysis_before
    assert derived_cache.analysis_fingerprint(doc, "dfm", {"process": "milling"}) == dfm_before


def test_a_products_own_settings_are_part_of_its_fingerprint():
    doc = docs.get_document(_part())
    milling = derived_cache.analysis_fingerprint(doc, "dfm", {"process": "milling"})
    additive = derived_cache.analysis_fingerprint(doc, "dfm", {"process": "additive"})
    assert milling != additive


def test_an_unknown_product_kind_raises_rather_than_defaulting():
    """Defaulting to the geometry fingerprint would make it look current forever."""
    doc = docs.get_document(_part())
    with pytest.raises(KeyError):
        derived_cache.analysis_fingerprint(doc, "vibes", {})


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------

def test_a_published_result_reads_back_as_current_and_goes_stale_on_an_edit():
    doc_id = _part()
    doc = docs.get_document(doc_id)

    assert derived_cache.freshness(doc, "spec") == "missing"
    derived_cache.publish(doc, "spec", {"score": 1.0})
    assert derived_cache.freshness(doc, "spec") == "current"

    cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id), "params": {"size": {"x": 0.2, "y": 0.05, "z": 0.01}}})
    doc = docs.get_document(doc_id)
    assert derived_cache.freshness(doc, "spec") == "stale"


def test_a_result_computed_against_a_document_that_has_since_moved_is_refused():
    """STATE-02. The case a slow job hits every time and no caller handles."""
    doc_id = _part()
    doc = docs.get_document(doc_id)
    taken_before = derived_cache.analysis_fingerprint(doc, "analysis", {})

    cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id), "params": {"size": {"x": 0.3, "y": 0.05, "z": 0.01}}})
    doc = docs.get_document(doc_id)

    kept = derived_cache.publish(doc, "analysis", {"max_stress": 1.0},
                                 settings={}, fingerprint=taken_before)
    assert kept is False
    assert derived_cache.freshness(doc, "analysis") == "missing"


def test_running_dfm_files_a_product_the_document_carries():
    doc_id = _part()
    env = cad_operations.execute("dfm", doc_id, {"process": "additive", "resolution": 32})
    assert env["ok"] is True, env.get("error")
    # The result is stamped with what it was a reading of.
    assert env["result"]["fingerprint"]
    doc = docs.get_document(doc_id)
    assert derived_cache.freshness(
        doc, "dfm", {"process": "additive", "build_axis": "auto", "resolution": 32}
    ) == "current"


def test_a_query_that_files_a_product_is_not_a_write():
    """Otherwise every analysis mints a 409 for whoever is editing."""
    doc_id = _part()
    before = docs.get_document(doc_id).revision
    cad_operations.execute("verify_against_spec", doc_id, {})
    assert docs.get_document(doc_id).revision == before


def test_an_oversized_result_is_recorded_as_truncated_rather_than_inlined():
    """A drawing sheet is an artifact with a URL, not a field on the document."""
    doc = docs.get_document(_part())
    huge = {"svg": "x" * (derived_cache.MAX_RESULT_BYTES + 1)}
    derived_cache.publish(doc, "drawing", huge, settings={"spec": None})
    held = derived_cache.held(doc, "drawing")
    assert held.result == {}
    assert held.truncated is True


# ---------------------------------------------------------------------------
# Cache ownership
# ---------------------------------------------------------------------------

def test_document_service_and_derived_cache_share_one_set_of_caches():
    """Aliases, not copies — a caller reaching into `docs._SOLID_CACHE` must hit
    the live cache, or an eviction in one place is invisible in the other."""
    assert docs._SOLID_CACHE is derived_cache.SOLIDS
    assert docs._BREP_CACHE is derived_cache.BREPS


def test_deleting_a_document_forgets_its_mesh_and_thumbnail():
    doc_id = _part()
    doc = docs.get_document(doc_id)
    digest = doc.derived.mesh_hash
    assert derived_cache.cached_mesh(doc_id, digest) is not None
    docs.delete_document(doc_id)
    assert derived_cache.cached_mesh(doc_id, digest) is None


def test_the_lru_reports_what_it_evicted():
    cache = derived_cache.LruCache(2)
    cache["a"], cache["b"], cache["c"] = 1, 2, 3
    assert set(cache) == {"b", "c"}
    assert cache.evictions == 1
    # Counted, not just bounded: an entry count alone cannot say whether a
    # workload is thrashing a cache or comfortably inside it.
    assert derived_cache.stats()["solids"]["capacity"] == derived_cache.SOLID_CAPACITY
