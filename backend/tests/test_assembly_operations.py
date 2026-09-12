"""Assemblies as an action space.

An agent could model a part and could not compose two. The mate solver, the
interference check and the BOM were implemented, routed and tested — and
unreachable from the catalog every model in this system drives.

These tests are about the *surface*, not about the mate maths (which is
`test_assembly_mates`): does an operation exist, is the id it takes the right
kind of id, does a failure say which, and does the caller learn what changed.
"""

from fastapi.testclient import TestClient

from conftest import base_box
from main import app
from app.services import cad_operations

client = TestClient(app)


def _part(size=(0.1, 0.06, 0.02), center=(0.0, 0.0, 0.0), name=None) -> str:
    doc_id = client.post("/api/documents", json={"resolution": 48}).json()["id"]
    base_box(client, doc_id, size=size, center=center)
    if name:
        client.patch(f"/api/documents/{doc_id}", json={"name": name})
    return doc_id


def _assembly() -> str:
    return client.post("/api/assemblies", json={"name": "test"}).json()["id"]


def _op(asm_id: str, op: str, **args) -> dict:
    resp = client.post(f"/api/assemblies/{asm_id}/op?envelope=1",
                       json={"op": op, "args": args})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ok(asm_id: str, op: str, **args) -> dict:
    env = _op(asm_id, op, **args)
    assert env["ok"], env["error"]
    return env["result"]


# --- the surface -----------------------------------------------------------

def test_the_catalog_declares_which_kind_of_id_each_operation_takes():
    """A caller should know before it calls, not after a 404."""
    catalog = {op["name"]: op for op in cad_operations.catalog()}
    assert catalog["insert_part"]["subject"] == "assembly"
    assert catalog["add_primitive"]["subject"] == "document"
    assert {op["name"] for op in cad_operations.catalog() if op["subject"] == "assembly"} == {
        "list_components", "insert_part", "insert_standard_part", "build_part",
        "remove_component", "place_component", "list_mates", "add_mate",
        "remove_mate", "drive_mate", "check_interference",
        "bill_of_materials", "dof",
        "mass_budget", "export_urdf", "prop_clearance",
    }


def test_an_assembly_operation_on_the_document_route_points_at_the_right_door():
    doc_id = _part()
    resp = client.post(f"/api/documents/{doc_id}/op",
                       json={"op": "insert_part", "args": {"part_id": doc_id}})
    assert resp.status_code == 400
    assert "assemblies" in resp.json()["detail"]["message"]


def test_a_document_operation_on_the_assembly_route_does_the_same():
    asm_id = _assembly()
    resp = client.post(f"/api/assemblies/{asm_id}/op",
                       json={"op": "bounding_box", "args": {}})
    assert resp.status_code == 400
    assert "part document" in resp.json()["detail"]["message"]


def test_an_unknown_assembly_is_a_typed_failure_with_a_remedy():
    resp = client.post("/api/assemblies/nope/op?envelope=1",
                       json={"op": "list_components", "args": {}})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "NO_SUCH_DOCUMENT"


# --- composing -------------------------------------------------------------

def test_inserting_a_part_reports_the_component_id_it_created():
    """Saves a `list_components` round-trip to learn the id of the thing you
    just made — the same contract `add_primitive` has on a document."""
    asm_id, part_id = _assembly(), _part()
    result = _ok(asm_id, "insert_part", part_id=part_id)
    assert result["component_id"]
    assert result["component_count"] == 1
    listed = _ok(asm_id, "list_components")["components"]
    assert [c["id"] for c in listed] == [result["component_id"]]
    assert listed[0]["part_id"] == part_id


def test_inserting_a_part_that_does_not_exist_names_the_argument():
    asm_id = _assembly()
    env = _op(asm_id, "insert_part", part_id="not-a-part")
    assert env["error"]["code"] == "BAD_ARG"
    assert env["error"]["arg"] == "part_id"


def test_a_component_id_from_another_assembly_lists_the_ones_that_do_exist():
    asm_id = _assembly()
    _ok(asm_id, "insert_part", part_id=_part())
    env = _op(asm_id, "remove_component", component_id="c-nope")
    assert env["error"]["code"] == "BAD_ARG"
    assert "it has:" in env["error"]["message"]


def test_a_mate_moves_the_part_and_the_state_says_where_it_went():
    asm_id = _assembly()
    base = _ok(asm_id, "insert_part", part_id=_part(size=(0.1, 0.1, 0.02)))["component_id"]
    moved = _ok(asm_id, "insert_part", part_id=_part(size=(0.04, 0.04, 0.03)))["component_id"]

    env = _op(asm_id, "add_mate", type="coincident", base=base, moved=moved, axis="z")
    assert env["ok"], env["error"]
    assert env["result"]["mate_id"]
    # Both parts are centred on the origin, so stacking max-to-min puts the
    # moved part's centre at half of each thickness: 10 + 15 = 25 mm.
    placed = {c["id"]: c for c in _ok(asm_id, "list_components")["components"]}
    assert placed[moved]["position_mm"]["z"] == 25.0


def test_a_mate_that_moves_nothing_says_so_rather_than_reporting_a_bare_success():
    """The assembly-side version of an edit that returns `ok` and changes
    nothing — the worst thing this action space can contain."""
    asm_id = _assembly()
    base = _ok(asm_id, "insert_part", part_id=_part(size=(0.1, 0.1, 0.02)))["component_id"]
    moved = _ok(asm_id, "insert_part", part_id=_part(size=(0.04, 0.04, 0.03)))["component_id"]
    _ok(asm_id, "add_mate", type="coincident", base=base, moved=moved, axis="z")
    again = _ok(asm_id, "add_mate", type="coincident", base=base, moved=moved, axis="z")
    assert "nothing moved" in again.get("note", "")


def test_mating_a_component_to_itself_is_refused():
    asm_id = _assembly()
    one = _ok(asm_id, "insert_part", part_id=_part())["component_id"]
    env = _op(asm_id, "add_mate", type="coincident", base=one, moved=one)
    assert env["error"]["code"] == "BAD_ARG"


def test_hand_placing_a_mated_component_is_a_precondition_not_a_silent_overwrite():
    """Writing a position the next solve discards is an edit that reports
    success and does not survive — so it is refused, naming the fix."""
    asm_id = _assembly()
    base = _ok(asm_id, "insert_part", part_id=_part())["component_id"]
    moved = _ok(asm_id, "insert_part", part_id=_part())["component_id"]
    _ok(asm_id, "add_mate", type="coincident", base=base, moved=moved)

    env = _op(asm_id, "place_component", component_id=moved, translate={"z": 0.5})
    assert env["error"]["code"] == "PRECONDITION"
    assert "mate" in env["error"]["message"]
    # The base is not positioned by the mate, so it may still be placed.
    assert _op(asm_id, "place_component", component_id=base, translate={"z": 0.5})["ok"]


def test_removing_a_mate_hands_the_component_back_to_place_component():
    asm_id = _assembly()
    base = _ok(asm_id, "insert_part", part_id=_part())["component_id"]
    moved = _ok(asm_id, "insert_part", part_id=_part())["component_id"]
    mate_id = _ok(asm_id, "add_mate", type="coincident", base=base, moved=moved)["mate_id"]
    assert _ok(asm_id, "remove_mate", mate_id=mate_id)["mate_count"] == 0
    assert _op(asm_id, "place_component", component_id=moved, translate={"z": 0.5})["ok"]


def test_a_partial_placement_keeps_the_axes_it_was_not_given():
    asm_id = _assembly()
    cid = _ok(asm_id, "insert_part", part_id=_part())["component_id"]
    _ok(asm_id, "place_component", component_id=cid, translate={"x": 0.05, "y": 0.02})
    _ok(asm_id, "place_component", component_id=cid, translate={"z": 0.01})
    at = _ok(asm_id, "list_components")["components"][0]["position_mm"]
    assert (at["x"], at["y"], at["z"]) == (50.0, 20.0, 10.0)


# --- reading it back -------------------------------------------------------

def test_interference_is_reachable_from_the_catalog():
    asm_id = _assembly()
    # A small part inside a big one, both centred on the origin: every vertex of
    # the small one is strictly inside the large one, which is an interference
    # rather than the zero gap two *identical* coincident parts would report.
    _ok(asm_id, "insert_part", part_id=_part(size=(0.05, 0.05, 0.05)))
    _ok(asm_id, "insert_part", part_id=_part(size=(0.02, 0.02, 0.02)))
    report = _ok(asm_id, "check_interference", clearance_mm=1.0)
    assert report["components_checked"] == 2
    assert report["interferences"] >= 1
    assert report["clear"] is False


def test_the_bill_of_materials_is_reachable_from_the_catalog():
    asm_id = _assembly()
    shared = _part()
    _ok(asm_id, "insert_part", part_id=shared)
    _ok(asm_id, "insert_part", part_id=shared)
    _ok(asm_id, "insert_part", part_id=_part(size=(0.02, 0.02, 0.02)))
    lines = _ok(asm_id, "bill_of_materials")["lines"]
    # One line per distinct part, not per placement.
    assert len(lines) == 2
    assert sorted(line["quantity"] for line in lines) == [1, 2]


def test_the_observation_describes_an_assembly_rather_than_a_feature_tree():
    """`state` on a part is features and params. On an assembly the useful
    compact form is components, mates and where things sit."""
    asm_id = _assembly()
    _ok(asm_id, "insert_part", part_id=_part())
    env = _op(asm_id, "insert_part", part_id=_part())
    state = env["state"]
    assert state["kind"] == "assembly"
    assert len(state["components"]) == 2
    components = env["delta"]["components"]
    assert (components["count_before"], components["count_after"]) == (1, 2)
    assert components["added"] == [state["components"][-1]["id"]]
