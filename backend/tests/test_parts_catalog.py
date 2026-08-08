"""The standard purchased parts library.

Every real assembly needs screws and there was no way to get one except
modelling it. Three things have to hold, and each is a way a parts library goes
wrong:

* **The dimensions are the standard's.** A screw that is nearly M4 is worse than
  no screw, because it will fit in CAD and not on the bench. Checked against the
  published tables and against the masses those tables imply.
* **The part is an editable tree, not a blob.** An `ImportFeature` would have no
  parameters, no history and nothing the decompiler can recover.
* **The fidelity is declared.** A screw here has no thread helix. That is the
  right body for clearance and interference and the wrong one for a thread
  study, and the part has to say so rather than let you find out by measuring.
"""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import base_box
from main import app
from app.cad_kernel.core.feature_tree import FeatureTree, walk_nodes
from app.cad_kernel.geom.meshing import mesh_volume
from app.cad_kernel.kernel import Kernel
from app.services import materials, parts_catalog as pc

client = TestClient(app)


def _measure(entry):
    """(bbox mm, volume mm³, mass g) of a catalog part, actually evaluated."""
    solid = Kernel.evaluate_tree(FeatureTree.from_dict(entry.build()), resolution=96)
    mesh = Kernel.tessellate(solid)
    volume = abs(mesh_volume(mesh.vertices, mesh.faces))
    size = (mesh.vertices.max(axis=0) - mesh.vertices.min(axis=0)) * 1000
    return size, volume * 1e9, volume * materials.density_of(entry.material) * 1000


def _op(doc_id, op, **args):
    resp = client.post(f"/api/documents/{doc_id}/op?envelope=1",
                       json={"op": op, "args": args})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _aop(asm_id, op, **args):
    resp = client.post(f"/api/assemblies/{asm_id}/op?envelope=1",
                       json={"op": op, "args": args})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- the geometry is the standard's -----------------------------------------

def test_a_socket_head_screw_matches_iso_4762():
    size, _, mass_g = _measure(pc.by_key("iso4762-m4-16"))
    # dk = 7.0 across the head, and length is measured under the head, so the
    # overall is 16 + k(4.0) = 20.
    assert size == pytest.approx([7.0, 7.0, 20.0], abs=0.05)
    # A steel M4×16 cap screw is about 2.5 g. Off by a factor here would mean
    # the shank or the head is the wrong size.
    assert 2.0 < mass_g < 3.2, mass_g


def test_a_hex_nut_is_a_real_hexagon_of_the_right_width():
    """Across flats is the standard's dimension; across corners is 2/√3 of it.
    A nut built from a cylinder would pass a width check and fail a spanner."""
    size, volume, mass_g = _measure(pc.by_key("iso4032-m4"))
    across_flats = 7.0
    assert size[1] == pytest.approx(across_flats, abs=0.02)
    assert size[0] == pytest.approx(across_flats * 2 / np.sqrt(3), abs=0.05)
    assert size[2] == pytest.approx(3.2, abs=0.02)
    assert 0.7 < mass_g < 1.0, mass_g


def test_a_washer_matches_iso_7089():
    size, volume, _ = _measure(pc.by_key("iso7089-m4"))
    assert size == pytest.approx([9.0, 9.0, 0.8], abs=0.05)
    # π/4 × (9² − 4.3²) × 0.8
    assert volume == pytest.approx(np.pi / 4 * (9 ** 2 - 4.3 ** 2) * 0.8, rel=0.02)


def test_a_bearing_matches_its_iso_15_boundary():
    for designation, expected in (("608", (22, 22, 7)), ("6203", (40, 40, 12))):
        size, _, _ = _measure(pc.by_key(f"bearing-{designation}"))
        assert size == pytest.approx(expected, abs=0.05), designation


def test_a_stepper_has_the_frame_face_bolt_pattern_and_shaft():
    entry = pc.by_key("stepper-nema17")
    size, _, _ = _measure(entry)
    assert size[0] == pytest.approx(42.3, abs=0.05)
    assert size[1] == pytest.approx(42.3, abs=0.05)
    # Four mounting holes at 31 mm, taking M3 clearance.
    holes = [n for n in walk_nodes(entry.build()) if n.get("name") == "Mount hole"]
    assert len(holes) == 4
    centres = {(round(h["params"]["center"]["x"] * 1000, 2),
                round(h["params"]["center"]["y"] * 1000, 2)) for h in holes}
    assert centres == {(15.5, 15.5), (15.5, -15.5), (-15.5, 15.5), (-15.5, -15.5)}
    assert holes[0]["params"]["radius"] * 2000 == pytest.approx(3.4, abs=0.01)


def test_extrusion_is_the_right_section_and_the_length_asked_for():
    size, _, _ = _measure(pc.by_key("extrusion-2020-200"))
    assert size == pytest.approx([20, 200, 20], abs=0.1)


def test_every_catalog_part_builds_and_has_volume():
    """Not a sample: a listing row that cannot be built is a row someone will
    click. Coarse resolution — this asserts existence, not dimensions."""
    for entry in pc.catalog():
        tree = FeatureTree.from_dict(entry.build())
        solid = Kernel.evaluate_tree(tree, resolution=32)
        mesh = Kernel.tessellate(solid)
        assert len(mesh.faces) > 0, entry.key
        assert abs(mesh_volume(mesh.vertices, mesh.faces)) > 0, entry.key


# --- what the listing says ---------------------------------------------------

def test_every_part_declares_its_fidelity_and_its_standard():
    for entry in pc.catalog():
        row = pc.describe(entry)
        assert row["fidelity"] in ("as-standard", "envelope"), entry.key
        assert row["standard"], entry.key
        assert row["notes"], entry.key
        assert row["material_id"] in {m.id for m in materials.list_materials()}, entry.key


def test_the_listing_carries_a_search_not_an_invented_part_number():
    """A part number is what an order is placed against and none of these were
    verified against a supplier's data. A search on the designation is honest."""
    row = pc.describe(pc.by_key("bearing-608"))
    assert "supplier_search" in row
    assert row["supplier_search"].startswith("https://")
    assert "part_number" not in row and "sku" not in row


def test_the_declared_envelope_matches_the_body_it_describes():
    """The listing's size is what someone picking a part checks, so it cannot be
    a different number from the one the body has."""
    for key in ("iso4762-m6-20", "iso4032-m6", "iso7089-m6", "bearing-6203",
                "stepper-nema23", "extrusion-3030-300", "iso2338-6-20"):
        entry = pc.by_key(key)
        measured, _, _ = _measure(entry)
        declared = pc.describe(entry)["size_mm"]
        assert [declared["x"], declared["y"], declared["z"]] == pytest.approx(
            list(measured), abs=0.1), key


def test_a_motor_says_what_it_really_weighs():
    """A solid steel envelope of a NEMA 17 is 670 g against a real 350 g,
    because a motor is laminations, windings and air. The document keeps
    reporting its geometric mass — that is what its geometry is — and the
    catalog says what the real thing weighs so a BOM is not quietly doubled."""
    row = pc.describe(pc.by_key("stepper-nema17"))
    assert row["typical_mass_g"] == 350
    _, _, geometric = _measure(pc.by_key("stepper-nema17"))
    assert geometric > row["typical_mass_g"]
    assert "over-states mass" in row["notes"]


# --- reaching it from the catalog of operations ------------------------------

def test_the_parts_are_browsable_as_an_operation():
    doc_id = client.post("/api/documents", json={"resolution": 48}).json()["id"]
    result = _op(doc_id, "list_standard_parts", category="bearing")["result"]
    assert result["count"] == len(pc.BEARINGS)
    assert all(row["category"] == "bearing" for row in result["parts"])

    narrowed = _op(doc_id, "list_standard_parts", search="NEMA 17")["result"]
    assert narrowed["count"] == 1
    assert narrowed["parts"][0]["key"] == "stepper-nema17"


def test_eight_screws_are_one_document_and_one_bom_line():
    """The BOM groups by part id, so eight documents would print eight lines of
    quantity one where purchasing wants one line of quantity eight."""
    asm_id = client.post("/api/assemblies", json={"name": "frame"}).json()["id"]
    env = _aop(asm_id, "insert_standard_part", key="iso4762-m4-16", quantity=8)
    assert env["ok"], env["error"]
    assert len(env["result"]["component_ids"]) == 8

    lines = _aop(asm_id, "bill_of_materials")["result"]["lines"]
    assert len(lines) == 1
    assert lines[0]["quantity"] == 8
    assert "ISO 4762 M4×16" in lines[0]["name"]


def test_inserting_the_same_part_into_a_second_assembly_reuses_the_document():
    first = client.post("/api/assemblies", json={"name": "a"}).json()["id"]
    second = client.post("/api/assemblies", json={"name": "b"}).json()["id"]
    a = _aop(first, "insert_standard_part", key="bearing-625")["result"]["part_id"]
    b = _aop(second, "insert_standard_part", key="bearing-625")["result"]["part_id"]
    assert a == b


def test_an_unknown_key_points_at_the_listing():
    asm_id = client.post("/api/assemblies", json={"name": "c"}).json()["id"]
    env = _aop(asm_id, "insert_standard_part", key="iso4762-m4-17")
    assert env["error"]["code"] == "BAD_ARG"
    assert env["error"]["arg"] == "key"
    assert "list_standard_parts" in env["error"]["message"]


def test_an_inserted_part_is_an_editable_tree_not_a_blob():
    """An ImportFeature would have no parameters, no history, and would be
    undecompilable — it would drag corpus health down while looking like content."""
    from app.services import decompiler, document_service as docs

    asm_id = client.post("/api/assemblies", json={"name": "d"}).json()["id"]
    part_id = _aop(asm_id, "insert_standard_part", key="iso7089-m5")["result"]["part_id"]
    tree = docs.get_document(part_id).feature_tree
    types = {n["type"] for n in walk_nodes(tree)}
    assert types <= {"BooleanFeature", "CylinderFeature", "BoxFeature",
                     "TransformFeature", "DatumPlaneFeature"}
    assert decompiler.decompile(tree), "a catalog part must decompile"


# --- clearance holes ---------------------------------------------------------

def test_a_clearance_hole_is_iso_273_not_the_screw_diameter():
    doc_id = client.post("/api/documents", json={"resolution": 48}).json()["id"]
    base_box(client, doc_id, size=(0.06, 0.04, 0.006))
    result = _op(doc_id, "clearance_hole", screw="M4",
                 center={"x": 0.02, "y": 0.0, "z": 0.0})["result"]
    assert result["diameter_mm"] == 4.5          # not 4.0
    assert _op(doc_id, "list_edges", kind="circle")["result"]["count"] >= 2


def test_the_three_iso_273_series_differ_in_the_right_direction():
    fine = pc.clearance_hole("M6", "fine")["diameter_mm"]
    medium = pc.clearance_hole("M6", "medium")["diameter_mm"]
    coarse = pc.clearance_hole("M6", "coarse")["diameter_mm"]
    assert 6.0 < fine < medium < coarse
    assert medium == 6.6


def test_a_counterbore_takes_the_head_of_the_screw_it_is_for():
    doc_id = client.post("/api/documents", json={"resolution": 48}).json()["id"]
    base_box(client, doc_id, size=(0.06, 0.04, 0.01))
    before = _op(doc_id, "bounding_box")["result"]["mass_kg"]
    result = _op(doc_id, "clearance_hole", screw="M5", counterbore=True)["result"]
    # ISO 4762 M5 head is 8.5 across; the bore clears it.
    assert result["counterbore_dia_mm"] >= pc.METRIC["M5"].head_dia
    assert result["counterbored"] is True
    assert _op(doc_id, "bounding_box")["result"]["mass_kg"] < before


def test_drilling_a_document_with_no_body_is_refused():
    doc_id = client.post("/api/documents", json={"resolution": 48}).json()["id"]
    env = _op(doc_id, "clearance_hole", screw="M4")
    assert env["error"]["code"] == "PRECONDITION"


def test_the_screw_argument_is_not_published_as_a_length():
    """`size` is a length everywhere else in this catalog and `units` folds that
    into the description at build time, so a thread designation under that name
    would be published as "in metres"."""
    from app.services import cad_operations as ops

    params = {o["name"]: o for o in ops.catalog()}["clearance_hole"]["params"]
    assert "size" not in params
    assert "metres" not in params["screw"]["description"]
    assert "metres" in params["depth"]["description"]
