"""A new document is three reference planes and no body.

It used to be a 100 mm box flagged `placeholder`, quietly replaced by the first
real feature. That was wrong twice: the viewport opened on a part nobody asked
for, and predicting what `add_primitive` would do meant knowing about a flag.

What replaces it has to hold three properties, and each of them is a way the
first attempt could have gone wrong: the planes are *ordinary features* (so
delete and add back are the operations that already exist), they contribute *no
geometry* (so nothing downstream has to have an opinion about them), and the
document reports *no body* rather than the 1 mm stand-in the evaluator needs.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import base_box
from main import app
from app.cad_kernel.ops.datum_plane import DatumHasNoBody, DatumPlaneFeature
from app.services import cad_operations as ops
from app.services import decompiler, document_service as docs, feature_ops, thumbnail

client = TestClient(app)


def _blank() -> str:
    return client.post("/api/documents", json={"resolution": 48}).json()["id"]


def _op(doc_id: str, op: str, **args) -> dict:
    resp = client.post(f"/api/documents/{doc_id}/op?envelope=1",
                       json={"op": op, "args": args})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ok(doc_id: str, op: str, **args) -> dict:
    env = _op(doc_id, op, **args)
    assert env["ok"], env["error"]
    return env["result"]


# --- what a new document is ------------------------------------------------

def test_a_new_document_has_three_planes_and_no_block():
    doc = client.post("/api/documents", json={"resolution": 48}).json()
    planes = _ok(doc["id"], "list_planes")
    assert [p["plane"] for p in planes["planes"]] == ["xy", "xz", "yz"]
    assert [p["name"] for p in planes["planes"]] == ["Top (XY)", "Front (XZ)", "Right (YZ)"]
    assert not any(n["type"] == "BoxFeature"
                   for n in _ok(doc["id"], "list_features")["features"])


def test_a_document_with_no_body_reports_no_body():
    """The 1 mm stand-in the evaluator needs must not be reported as geometry —
    scaled to fit it renders as a confident little cube, which is the starting
    block this whole change removes."""
    derived = client.post("/api/documents", json={"resolution": 48}).json()["derived"]
    assert derived["mesh_url"] is None
    assert derived["mesh_hash"] is None
    assert derived["bbox"] is None
    assert derived["volume"] == 0.0
    # And no error: having no body yet is a state, not a failure.
    assert derived["error"] is None


def test_a_blank_document_gets_no_thumbnail_rather_than_a_picture_of_nothing():
    doc = client.post("/api/documents", json={"resolution": 48}).json()
    assert thumbnail.for_document(doc["id"], "anything") is None


def test_every_blank_document_starts_from_the_same_tree():
    """Deterministic ids are what make trajectory replay and the decompiler
    work; a template that numbered its planes differently each time would
    break both."""
    first = client.post("/api/documents", json={"resolution": 48}).json()["feature_tree"]
    second = client.post("/api/documents", json={"resolution": 48}).json()["feature_tree"]
    assert first == second
    assert [n["id"] for n in feature_ops.flatten(first)] == ["f4", "f1", "f2", "f3"]


# --- they are ordinary features --------------------------------------------

def test_a_plane_can_be_deleted_and_added_back():
    doc_id = _blank()
    assert _ok(doc_id, "delete_feature", feature_id="f2")
    assert [p["plane"] for p in _ok(doc_id, "list_planes")["planes"]] == ["xy", "yz"]

    added = _ok(doc_id, "add_plane", plane="xz", name="Front again")
    assert added["normal_axis"] == "y"
    assert [p["name"] for p in _ok(doc_id, "list_planes")["planes"]][-1] == "Front again"


def test_an_offset_plane_records_where_it_sits():
    """The point of adding one: sketching at a height nothing has a face at."""
    doc_id = _blank()
    result = _ok(doc_id, "add_plane", plane="xy", offset=0.03)
    assert result["offset_mm"] == 30.0
    assert _ok(doc_id, "list_planes")["planes"][-1]["offset_mm"] == 30.0


def test_deleting_every_plane_leaves_the_body_alone():
    doc_id = _blank()
    base_box(client, doc_id, size=(0.08, 0.05, 0.02))
    for feature_id in ("f1", "f2", "f3"):
        _ok(doc_id, "delete_feature", feature_id=feature_id)
    assert _ok(doc_id, "list_planes")["count"] == 0
    assert _ok(doc_id, "bounding_box")["size_mm"] == {"x": 80.0, "y": 50.0, "z": 20.0}


def test_planes_do_not_change_the_body_they_sit_beside():
    """The whole "contributes no geometry" claim, measured."""
    plain = _blank()
    base_box(client, plain, size=(0.06, 0.04, 0.02))
    for feature_id in ("f1", "f2", "f3"):
        _ok(plain, "delete_feature", feature_id=feature_id)

    with_planes = _blank()
    base_box(client, with_planes, size=(0.06, 0.04, 0.02))
    _ok(with_planes, "add_plane", plane="yz", offset=0.01)

    assert (docs.get_document(plain).derived.mesh_hash
            == docs.get_document(with_planes).derived.mesh_hash)


# --- they never reach an evaluator -----------------------------------------

def test_a_plane_is_pruned_before_evaluation():
    doc_id = _blank()
    base_box(client, doc_id)
    resolved = feature_ops.resolve_for_evaluation(docs.get_document(doc_id).feature_tree)
    assert not any(n["type"] == feature_ops.DATUM_PLANE_TYPE
                   for n in feature_ops.flatten(resolved))


def test_a_plane_that_reaches_an_evaluator_says_so_rather_than_being_empty():
    """Registered in `_SDF_BUILDERS` for the same reason `SketchFeature` is: an
    empty solid here would silently delete whatever the plane sits beside."""
    with pytest.raises(DatumHasNoBody):
        DatumPlaneFeature(id="f9", name="p").evaluate()


def test_a_document_of_only_planes_evaluates_to_nothing_not_an_error():
    doc = docs.get_document(_blank())
    assert doc.derived.error is None
    assert doc.derived.volume == 0.0


# --- cutting nothing ---------------------------------------------------------

def test_subtracting_from_an_empty_document_is_refused():
    """Without the guard the boolean collapses to its one surviving child once
    the planes are pruned, so a cut would return the *tool* as a solid and
    report success — the worst thing this action space can contain."""
    env = _op(_blank(), "add_primitive", shape="cylinder", operation="subtract")
    assert env["error"]["code"] == "PRECONDITION"
    assert "no material to cut" in env["error"]["message"]


def test_subtracting_works_once_there_is_something_to_cut():
    doc_id = _blank()
    base_box(client, doc_id, size=(0.06, 0.06, 0.06))
    before = _ok(doc_id, "bounding_box")["mass_kg"]
    assert _op(doc_id, "add_primitive", shape="cylinder", operation="subtract",
               params={"radius": 0.01, "height": 0.2})["ok"]
    assert _ok(doc_id, "bounding_box")["mass_kg"] < before


def test_a_legacy_placeholder_document_still_behaves_as_it_did():
    """Nothing creates one any more, but they are persisted. Without the branch
    that still honours the flag, their first edit unions with a stray block."""
    doc = docs.create_document(name="legacy", owner_id=None)
    doc.feature_tree = {"version": "brep-v1", "root": {
        "type": "BoxFeature", "id": "f1", "name": "Base Box", "placeholder": True,
        "params": {"size": {"x": 0.1, "y": 0.1, "z": 0.1}, "center": {"x": 0, "y": 0, "z": 0}}}}
    docs.update_document(doc.id, feature_tree=doc.feature_tree)

    _ok(doc.id, "add_primitive", shape="box", params={"size": {"x": 0.02, "y": 0.02, "z": 0.02}})
    # Replaced, not unioned: the part is the 20 mm box, not a 100 mm block.
    assert _ok(doc.id, "bounding_box")["size_mm"] == {"x": 20.0, "y": 20.0, "z": 20.0}


# --- the decompiler ----------------------------------------------------------

def test_the_default_planes_are_the_starting_state_not_three_steps():
    """Replay runs into a *new* document, which already has them. Emitting
    `add_plane` for the three would rebuild the source with six — and the mesh
    hashes would still match, because planes carry no geometry, so the
    verification could never catch it."""
    doc_id = _blank()
    base_box(client, doc_id, size=(0.05, 0.05, 0.05))
    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)
    assert [s["op"] for s in steps] == ["add_primitive"]


def test_a_plane_somebody_added_is_a_step():
    doc_id = _blank()
    base_box(client, doc_id, size=(0.05, 0.05, 0.05))
    _ok(doc_id, "add_plane", plane="xy", offset=0.04, name="Lift")
    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)
    added = [s for s in steps if s["op"] == "add_plane"]
    assert len(added) == 1
    assert added[0]["args"]["offset"] == pytest.approx(0.04)
    assert added[0]["args"]["name"] == "Lift"


def test_a_blank_document_decompiles_and_replays():
    """A document with no body is a legitimate thing to recover, and the
    verification path must not treat "no operations" as a failure."""
    outcome = decompiler.decompile_document(_blank(), resolution=32, record=False)
    assert outcome["ok"] is False
    # Nothing to rebuild is `empty`, not `unsupported` — the distinction is what
    # `corpus_report.by_cause` is for.
    assert outcome["cause"] == "empty"


def test_the_operations_are_in_the_catalog():
    catalog = {op["name"] for op in ops.catalog()}
    assert {"add_plane", "list_planes"} <= catalog
