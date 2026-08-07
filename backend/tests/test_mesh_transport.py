"""The mesh as a cacheable resource rather than part of the document body.

The triangles used to be base64-inlined into `derived.mesh_url`, which made them
99% of the document JSON — 528 KB gzipped, re-downloaded on every edit including
edits that could not move the geometry. Behind a URL keyed by the hash of the
triangles themselves they are immutable by construction, so the browser keeps
them and an edit that leaves the body alone costs nothing.

The tests that matter here are the cache-safety ones: a hash must never name two
different bodies, and a hash the document no longer has must 404 rather than
quietly resolve to the current one.
"""

import numpy as np
import pytest

from app.cad_kernel.io.mesh_binary import read_mesh_binary, write_mesh_binary
from app.cad_kernel.kernel import Kernel
from conftest import body_id
from app.services import cad_operations as ops
from app.services import document_analysis
from app.services import document_service as docs


def _part(name="mesh transport"):
    doc_id = docs.create_document(name=name).id
    ops.execute("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.08, "y": 0.06, "z": 0.02}}})
    return doc_id


# ------------------------------------------------------------ the format --

def test_a_mesh_round_trips_through_the_binary_form():
    solid = docs.get_solid(_part())
    mesh = Kernel.tessellate(solid)
    vertices, faces = read_mesh_binary(write_mesh_binary(mesh))
    assert np.allclose(vertices, np.asarray(mesh.vertices, dtype=np.float32), atol=1e-6)
    assert np.array_equal(faces, np.asarray(mesh.faces, dtype=np.uint32))


def test_the_indexed_form_is_much_smaller_than_the_stl_it_replaced():
    """Not a micro-optimisation: STL repeats every shared vertex and carries a
    per-face normal three.js recomputes anyway."""
    from app.cad_kernel.io.stl import write_stl_binary

    mesh = Kernel.tessellate(docs.get_solid(_part()))
    indexed = len(write_mesh_binary(mesh))
    stl = len(write_stl_binary(mesh))
    assert indexed < stl * 0.55, f"indexed {indexed} vs stl {stl}"


class _EmptyMesh:
    """A body that meshed to nothing — an all-suppressed tree reaches this."""

    vertices: list = []
    faces: list = []


def test_an_empty_mesh_is_a_valid_payload_not_a_crash():
    vertices, faces = read_mesh_binary(write_mesh_binary(_EmptyMesh()))
    assert len(vertices) == 0 and len(faces) == 0


@pytest.mark.parametrize("payload", [b"", b"NOPE", b"FMSH"])
def test_a_payload_that_is_not_a_mesh_is_refused(payload):
    with pytest.raises(ValueError):
        read_mesh_binary(payload)


def test_a_truncated_payload_is_refused_rather_than_read_past_the_end():
    good = write_mesh_binary(Kernel.tessellate(docs.get_solid(_part())))
    with pytest.raises(ValueError, match="shorter than"):
        read_mesh_binary(good[:len(good) // 2])


# ------------------------------------------------------------ addressing --

def test_the_document_carries_a_url_not_a_payload():
    doc = docs.get_document(_part())
    assert doc.derived.mesh_url == f"/api/documents/{doc.id}/mesh/{doc.derived.mesh_hash}"
    # The body is the feature tree and some numbers, not a megabyte of base64.
    assert len(doc.model_dump_json()) < 8_000


def test_one_hash_always_serves_the_same_bytes():
    """What a cache needs is not that the payload re-hashes to the key — the wire
    form is float32 and `mesh_hash` reads the float64 mesh — but that the key
    identifies the bytes. Same hash, same triangles, forever."""
    doc_id = _part()
    digest = docs.get_document(doc_id).derived.mesh_hash
    first = document_analysis.mesh_payload(doc_id, digest)
    docs._MESH_CACHE.clear()          # force the re-derivation path
    second = document_analysis.mesh_payload(doc_id, digest)
    assert first is not None and first == second


def test_different_geometry_gets_a_different_hash():
    doc_id = _part()
    before = docs.get_document(doc_id).derived.mesh_hash
    ops.execute("set_feature_params", doc_id, {
        "feature_id": body_id(docs.get_document(doc_id).feature_tree),
        "params": {"size": {"x": 0.081, "y": 0.06, "z": 0.02}}})
    assert docs.get_document(doc_id).derived.mesh_hash != before


def test_a_hash_this_document_does_not_have_is_refused():
    """404, never "here is the current mesh instead" — the response is cached
    forever under that hash, so answering the wrong one poisons every cache
    that sees it."""
    assert document_analysis.mesh_payload(_part(), "deadbeefdeadbeef") is None


def test_a_superseded_hash_still_resolves_while_it_is_remembered():
    """The client is handed a URL for the geometry it was told about, and another
    edit may land before it asks. Serving the mesh it named is correct — the hash
    still identifies exactly those triangles."""
    doc_id = _part()
    first = docs.get_document(doc_id).derived.mesh_hash
    ops.execute("add_primitive", doc_id, {"shape": "sphere", "params": {"radius": 0.03}})
    second = docs.get_document(doc_id).derived.mesh_hash
    assert first != second

    assert document_analysis.mesh_payload(doc_id, second) is not None
    assert document_analysis.mesh_payload(doc_id, first) is not None, "the old one too"


# --------------------------------------------- not doing needless work --

def test_an_edit_that_cannot_move_the_body_does_not_rebuild_one():
    """`set_material` and `set_spec` used to pay a full evaluate-and-tessellate
    (85-90 ms) and re-send the whole mesh."""
    doc_id = _part()
    # A copy: `get_document` hands back the live document, so reading fields off
    # it after the edit would be comparing the object to itself.
    before = docs.get_document(doc_id).derived.model_copy(deep=True)
    ops.execute("set_material", doc_id, {"material_id": "steel-1018"})
    after = docs.get_document(doc_id).derived

    assert after.mesh_url == before.mesh_url, "same geometry, same URL, no refetch"
    assert after.mesh_hash == before.mesh_hash
    assert after.volume == before.volume
    # Mass is the one derived number a material *does* move.
    assert after.mass != before.mass


@pytest.mark.parametrize("op,args", [
    ("set_spec", {"requirements": []}),
    ("set_material", {"material_id": "al-6061"}),
], ids=["set_spec", "set_material"])
def test_non_geometric_operations_keep_the_mesh(op, args):
    doc_id = _part()
    url = docs.get_document(doc_id).derived.mesh_url
    assert ops.execute(op, doc_id, args)["ok"]
    assert docs.get_document(doc_id).derived.mesh_url == url


def test_a_geometric_edit_does_move_it():
    doc_id = _part()
    url = docs.get_document(doc_id).derived.mesh_url
    ops.execute("set_feature_params", doc_id, {
        "feature_id": body_id(docs.get_document(doc_id).feature_tree),
        "params": {"size": {"x": 0.2, "y": 0.06, "z": 0.02}}})
    assert docs.get_document(doc_id).derived.mesh_url != url


def test_a_parameter_change_is_part_of_the_evaluation_key():
    """The trap the cache exists to avoid: a body that moved without a single
    feature node changing."""
    doc_id = _part()
    root = body_id(docs.get_document(doc_id).feature_tree)
    ops.execute("set_parameters", doc_id, {"parameters": [{"name": "w", "expr": "0.08"}]})
    ops.execute("set_feature_params", doc_id, {
        "feature_id": root, "params": {"size": {"x": "=w", "y": 0.06, "z": 0.02}}})
    url = docs.get_document(doc_id).derived.mesh_url

    ops.execute("set_parameters", doc_id, {"parameters": [{"name": "w", "expr": "0.16"}]})
    assert docs.get_document(doc_id).derived.mesh_url != url, "the plate doubled in width"
