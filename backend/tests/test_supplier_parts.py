"""Purchased parts the user brought in, beside the ones we generate.

The generated catalog is the floor and cannot be the ceiling: it can build an
ISO 4762 M4×16 and it cannot build *this* connector. The CAD portals that could
are, on inspection, the one thing that is not permitted — 3Dfindit disallows
this crawler and publishes no geometry API, and TraceParts' API terms forbid
copying its data into another catalog in four separate clauses. So the library
grows the only lawful way: the person who accepted the portal's terms brings the
file, and it becomes a catalog part *of theirs*.

What has to hold:

* **An imported STEP is exact.** If it arrives as a voxel blob it is a worse
  part than the generated screw it sits next to, and the row has to say which.
* **One list, one shape.** Generated and imported rows share `describe`'s shape
  and both carry `fidelity` and `geometry`, so nothing is inferred from which
  half a row came from.
* **A row is one account's.** It names a document, and `add_component` refuses a
  part its owner cannot open — a supplier part leaking across accounts is a
  broken assembly at best and someone else's geometry at worst.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from app.cad_kernel.io import step_brep, step_import
from app.services import supplier_parts

client = TestClient(app)

pytestmark = pytest.mark.skipif(
    not step_import.available(), reason="exact import needs cadquery-ocp")


def _widget_step() -> bytes:
    """A 30×20×10 mm block with a Ø6 bore — a stand-in for a supplier download."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 0.030, 0.020, 0.010).Shape()
    bore = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0.015, 0.010, -0.001), gp_Dir(0, 0, 1)), 0.003, 0.02).Shape()
    return step_brep.write_step_brep(BRepAlgoAPI_Cut(box, bore).Shape(),
                                     "widget").encode()


def _import(**form) -> dict:
    fields = {"designation": "ACME W-30 widget", "source": "3dfindit",
              "manufacturer": "ACME", "part_number": "W-30",
              "category": "hardware"}
    fields.update(form)
    resp = client.post("/api/documents/meta/supplier-parts",
                       files={"file": ("widget.step", _widget_step(), "application/step")},
                       data=fields)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _listing(**params) -> dict:
    resp = client.get("/api/documents/meta/standard-parts", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------ import --

def test_an_imported_step_becomes_an_exact_catalog_part():
    row = _import()
    assert row["key"].startswith(supplier_parts.PREFIX)
    assert row["fidelity"] == "supplier"
    # The whole reason to import a STEP rather than an STL: faces and edges are
    # addressable, so the part can be filleted, measured and re-exported.
    assert row["geometry"] == "exact"
    assert row["source"] == "3dfindit"
    assert row["manufacturer"] == "ACME" and row["part_number"] == "W-30"

    doc = client.get(f"/api/documents/{row['document_id']}").json()
    kinds = {n["type"] for n in _walk(doc["feature_tree"])}
    assert "BrepBodyFeature" in kinds
    assert "ImportFeature" not in kinds


def test_the_row_records_the_measured_size_not_a_typed_one():
    row = _import()
    assert row["size_mm"]["x"] == pytest.approx(30.0, abs=0.2)
    assert row["size_mm"]["y"] == pytest.approx(20.0, abs=0.2)
    assert row["size_mm"]["z"] == pytest.approx(10.0, abs=0.2)


def test_a_mesh_import_says_so_rather_than_passing_as_exact():
    """An STL of a screw places and interferes fine and cannot be filleted. Both
    are legitimate; a row that does not distinguish them is not."""
    stl = _ascii_stl_tetra()
    resp = client.post("/api/documents/meta/supplier-parts",
                       files={"file": ("blob.stl", stl, "model/stl")},
                       data={"designation": "traced blob", "source": "grabcad",
                             "category": "other"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["geometry"] == "mesh"
    assert resp.json()["fidelity"] == "supplier"


def test_an_unknown_source_is_refused_by_name():
    resp = client.post("/api/documents/meta/supplier-parts",
                       files={"file": ("widget.step", _widget_step(), "application/step")},
                       data={"designation": "x", "source": "mcmaster"})
    assert resp.status_code == 422
    assert "mcmaster" in resp.json()["detail"]


def test_importing_the_same_part_number_twice_does_not_replace_the_first():
    """The first one may already be placed in an assembly, and swapping the
    document under it would move geometry nobody touched."""
    first, second = _import(), _import()
    assert first["key"] != second["key"]
    assert first["document_id"] != second["document_id"]


# ----------------------------------------------------------------- listing --

def test_one_listing_holds_both_halves_and_every_row_states_both_axes():
    _import()
    page = _listing(limit=250)
    assert page["imported"] >= 1
    keys = {r["key"] for r in page["parts"]}
    assert any(k.startswith(supplier_parts.PREFIX) for k in keys)
    assert "iso4762-m4-16" in keys
    for row in page["parts"]:
        assert row["fidelity"] in ("as-standard", "envelope", "supplier")
        assert row["geometry"] in ("exact", "mesh")


def test_the_imported_parts_come_first():
    _import()
    parts = _listing(limit=250)["parts"]
    assert parts[0]["key"].startswith(supplier_parts.PREFIX)


def test_the_source_filter_returns_only_imports():
    _import()
    page = _listing(source="3dfindit", limit=250)
    assert page["parts"]
    assert all(r["source"] == "3dfindit" for r in page["parts"])


def test_the_listing_offers_the_portals_as_searches_never_as_fetches():
    """A hyperlink is not a copy. This is how you get to a file; there is no
    path here that reaches into a portal on the user's behalf."""
    sources = {s["id"]: s for s in _listing()["sources"]}
    assert {"3dfindit", "traceparts"} <= set(sources)
    assert "search?q=" in sources["3dfindit"]["search"]
    url = supplier_parts.search_url("traceparts", "SKF 608 bearing")
    assert url and "SKF+608" in url


def test_search_matches_the_manufacturer_and_part_number():
    _import()
    assert any(r["key"].startswith(supplier_parts.PREFIX)
               for r in _listing(search="ACME", limit=250)["parts"])
    assert any(r["key"].startswith(supplier_parts.PREFIX)
               for r in _listing(search="W-30", limit=250)["parts"])


# ------------------------------------------------------------- placing them --

def test_an_imported_part_places_into_an_assembly_like_a_generated_one():
    row = _import()
    asm = client.post("/api/assemblies", json={"name": "supplier test"}).json()
    resp = client.post(f"/api/assemblies/{asm['id']}/op?envelope=1",
                       json={"op": "insert_standard_part",
                             "args": {"key": row["key"], "quantity": 3}})
    env = resp.json()
    assert env["ok"], env
    assert env["result"]["component_count"] == 3
    assert env["result"]["part_id"] == row["document_id"]

    # Three components, one BOM line — the same reuse the generated parts get.
    bom = client.post(f"/api/assemblies/{asm['id']}/op?envelope=1",
                      json={"op": "bill_of_materials", "args": {}}).json()
    assert bom["ok"], bom
    lines = [l for l in bom["result"]["lines"] if l["part_id"] == row["document_id"]]
    assert len(lines) == 1 and lines[0]["quantity"] == 3


def test_placing_a_key_this_account_never_imported_is_refused_by_name():
    asm = client.post("/api/assemblies", json={"name": "supplier test"}).json()
    env = client.post(f"/api/assemblies/{asm['id']}/op?envelope=1",
                      json={"op": "insert_standard_part",
                            "args": {"key": "supplier:acme-nothing"}}).json()
    assert not env["ok"]
    assert env["error"]["code"] == "BAD_ARG"
    assert env["error"]["arg"] == "key"


def test_forgetting_a_part_leaves_its_document_alone():
    """It may be sitting in three assemblies. A catalog row is a shortcut to
    placing it again, not the thing keeping it alive."""
    row = _import()
    assert client.delete(f"/api/documents/meta/supplier-parts/{row['key']}").status_code == 200
    assert client.get(f"/api/documents/{row['document_id']}").status_code == 200
    assert not any(r["key"] == row["key"] for r in _listing(limit=250)["parts"])
    assert client.delete(f"/api/documents/meta/supplier-parts/{row['key']}").status_code == 404


# ------------------------------------------------------------------ helpers --

def _walk(tree):
    from app.cad_kernel.core.feature_tree import walk_nodes

    return walk_nodes(tree)


def _ascii_stl_tetra() -> bytes:
    facets = [((0, 0, 0), (0.02, 0, 0), (0, 0.02, 0)),
              ((0, 0, 0), (0, 0.02, 0), (0, 0, 0.02)),
              ((0, 0, 0), (0, 0, 0.02), (0.02, 0, 0)),
              ((0.02, 0, 0), (0, 0.02, 0), (0, 0, 0.02))]
    out = ["solid blob"]
    for tri in facets:
        out.append("facet normal 0 0 0\n outer loop")
        out += [f"  vertex {v[0]} {v[1]} {v[2]}" for v in tri]
        out.append(" endloop\nendfacet")
    out.append("endsolid blob")
    return "\n".join(out).encode()
