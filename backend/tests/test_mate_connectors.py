"""Mate connectors, the kinematic mate types, and degrees of freedom.

Assembly mates were four keywords reading a component's *bounding-box extremes*.
That places a bolted stack and nothing else, and the reason is structural rather
than a missing count: every mate was a relationship between two silhouettes, so
there was nothing to place, nothing to pattern, and no way to say "this hole,
not that one". Adding eight more types to that scheme would have been adding
rows to the wrong table.

Two things carry most of the weight here. **The legacy four must not move** —
assemblies are persisted, and a stack that shifts because the solver got
cleverer is a regression however much better the new scheme is. And **degrees of
freedom are a property of the constraint, not of the placement**: the solver
still picks a value for a revolute's free rotation, and reporting DOF from what
it picked would report zero for everything.
"""

import math

import numpy as np
import pytest

from app.cad_kernel.ops.mate_connector import MateConnectorFeature
from app.services import assembly_mates
from app.services import assembly_service
from app.services import cad_operations as ops
from app.services import document_service as docs

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731


def _part(size=(.04, .04, .01), name="part"):
    doc = docs.create_document(name=name, owner_id="dev")
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": V(*size), "center": V(0, 0, 0)}})
    return doc.id


def _assembly(*part_ids):
    asm = assembly_service.create_assembly(name="rig", owner_id="dev")
    ids = []
    for part_id in part_ids:
        result = ops.dispatch("insert_part", asm.id, {"part_id": part_id})
        ids.append(result["component_id"])
    return asm.id, ids


# ------------------------------------------------------------- the frame --

def test_a_frame_is_orthonormalised_rather_than_trusted_as_given():
    """`z_axis` is the mate axis and wins; a skewed `x_axis` is projected off
    it. Accepting the pair as given would make the third axis non-orthogonal
    and every rotation derived from it a shear."""
    connector = MateConnectorFeature.from_dict({
        "type": "MateConnectorFeature", "id": "m1",
        "params": {"origin": V(0, 0, 0), "z_axis": V(0, 0, 1), "x_axis": V(1, 0, .5)}})
    _, rotation = connector.frame()
    assert np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-12)
    assert np.allclose(rotation[:, 2], [0, 0, 1])
    assert rotation[2, 0] == pytest.approx(0.0, abs=1e-12)


def test_an_x_axis_parallel_to_the_mate_axis_still_gives_a_frame():
    """It says nothing about rotation, so any perpendicular will do — but it has
    to be the *same* perpendicular every rebuild, or the part turns on reload."""
    spec = {"type": "MateConnectorFeature", "id": "m1",
            "params": {"origin": V(0, 0, 0), "z_axis": V(0, 0, 1), "x_axis": V(0, 0, 1)}}
    first = MateConnectorFeature.from_dict(spec).frame()[1]
    second = MateConnectorFeature.from_dict(spec).frame()[1]
    assert np.allclose(first, second)
    assert np.allclose(first.T @ first, np.eye(3), atol=1e-12)


def test_a_connector_contributes_no_geometry():
    from app.cad_kernel.ops.mate_connector import ConnectorHasNoBody
    from app.services import feature_ops

    assert "MateConnectorFeature" in feature_ops.BODYLESS_TYPES
    with pytest.raises(ConnectorHasNoBody):
        MateConnectorFeature().evaluate()


def test_adding_a_connector_leaves_the_body_alone():
    part_id = _part()
    before = ops.dispatch("mass_properties", part_id, {})["volume_cm3"]
    ops.dispatch("add_mate_connector", part_id,
                 {"origin": V(.02, 0, 0), "name": "right edge"})
    assert ops.dispatch("mass_properties", part_id, {})["volume_cm3"] == pytest.approx(before)


def test_list_mate_connectors_reports_the_orthonormalised_frame_in_mm():
    """A caller that supplied a skewed axis should see what it became, and
    lengths read back in millimetres like every other observation."""
    part_id = _part()
    ops.dispatch("add_mate_connector", part_id,
                 {"origin": V(.02, 0, .005), "z_axis": V(0, 0, 1), "x_axis": V(1, 0, .5),
                  "name": "top right"})
    rows = ops.dispatch("list_mate_connectors", part_id, {})["connectors"]
    assert len(rows) == 1
    assert rows[0]["name"] == "top right"
    assert rows[0]["origin_mm"] == [20.0, 0.0, 5.0]
    assert rows[0]["z_axis"] == [0.0, 0.0, 1.0]
    assert rows[0]["x_axis"][2] == pytest.approx(0.0, abs=1e-6)


# ------------------------------------------------- the legacy four, intact --

def test_the_bounding_box_mates_place_parts_exactly_where_they_always_did():
    """The regression that matters most: assemblies are persisted, and a stack
    that moves because the solver grew a second scheme is a wrong answer for
    every document already saved."""
    base, top = _part(size=(.04, .04, .01)), _part(size=(.02, .02, .01))
    asm_id, (base_c, top_c) = _assembly(base, top)
    ops.dispatch("add_mate", asm_id, {"type": "coincident", "base": base_c,
                                      "moved": top_c, "axis": "z",
                                      "base_face": "max", "moved_face": "min"})
    placed = assembly_service.get_assembly(asm_id)
    moved = next(c for c in placed.components if c.id == top_c)
    # Base top face at +5 mm, the 10 mm part's bottom lands on it -> centre +10.
    assert moved.transform.translate.z == pytest.approx(.010)
    assert moved.transform.rotate_deg.x == pytest.approx(0.0)


def test_a_bounding_box_mate_needs_no_connectors():
    base, top = _part(), _part()
    asm_id, (base_c, top_c) = _assembly(base, top)
    envelope = ops.execute("add_mate", asm_id, {"type": "fasten", "base": base_c,
                                                "moved": top_c})
    assert envelope["ok"], envelope.get("error")


# --------------------------------------------------------- connector mates --

def test_a_connector_mate_lands_the_two_frames_on_each_other():
    base, moved = _part(name="base"), _part(name="moved")
    b = ops.dispatch("add_mate_connector", base,
                     {"origin": V(.015, 0, .005), "name": "boss"})["connector_id"]
    m = ops.dispatch("add_mate_connector", moved,
                     {"origin": V(-.015, 0, -.005), "name": "spigot"})["connector_id"]
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    ops.dispatch("add_mate", asm_id, {"type": "fasten", "base": base_c, "moved": moved_c,
                                      "base_connector": b, "moved_connector": m})

    placed = assembly_service.get_assembly(asm_id)
    component = next(c for c in placed.components if c.id == moved_c)
    t = component.transform.translate
    # The moved connector sits at (-15, 0, -5) in its part; landing it on the
    # base's (15, 0, 5) puts the part origin at (30, 0, 10) mm.
    assert (t.x, t.y, t.z) == pytest.approx((.030, 0.0, .010))


def test_a_connector_mate_rotates_the_part_which_no_bbox_mate_can():
    """The whole point of the frame. A bounding-box extreme cannot express a
    rotation, so every legacy mate leaves the part axis-aligned."""
    base, moved = _part(name="base"), _part(name="moved")
    b = ops.dispatch("add_mate_connector", base,
                     {"origin": V(0, 0, .005), "z_axis": V(1, 0, 0)})["connector_id"]
    m = ops.dispatch("add_mate_connector", moved,
                     {"origin": V(0, 0, 0), "z_axis": V(0, 0, 1)})["connector_id"]
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    ops.dispatch("add_mate", asm_id, {"type": "fasten", "base": base_c, "moved": moved_c,
                                      "base_connector": b, "moved_connector": m})

    component = next(c for c in assembly_service.get_assembly(asm_id).components
                     if c.id == moved_c)
    rotation = component.transform.rotate_deg
    assert any(abs(v) > 1.0 for v in (rotation.x, rotation.y, rotation.z)), \
        "the part should have been turned to line the frames up"


def test_the_offset_runs_along_the_connectors_shared_axis():
    base, moved = _part(name="base"), _part(name="moved")
    b = ops.dispatch("add_mate_connector", base,
                     {"origin": V(0, 0, .005), "z_axis": V(0, 1, 0)})["connector_id"]
    m = ops.dispatch("add_mate_connector", moved, {"origin": V(0, 0, 0)})["connector_id"]
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    ops.dispatch("add_mate", asm_id, {"type": "fasten", "base": base_c, "moved": moved_c,
                                      "base_connector": b, "moved_connector": m,
                                      "offset_mm": 8.0})
    t = next(c for c in assembly_service.get_assembly(asm_id).components
             if c.id == moved_c).transform.translate
    # Base connector's Z points along world +Y, so the offset goes that way.
    assert t.y == pytest.approx(.008)


def test_a_connector_frame_is_re_read_after_the_part_is_edited():
    """Copied into the mate it would be a stale placement that looks right
    until somebody moves the connector."""
    base, moved = _part(name="base"), _part(name="moved")
    b = ops.dispatch("add_mate_connector", base, {"origin": V(.010, 0, 0)})["connector_id"]
    m = ops.dispatch("add_mate_connector", moved, {"origin": V(0, 0, 0)})["connector_id"]
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    ops.dispatch("add_mate", asm_id, {"type": "fasten", "base": base_c, "moved": moved_c,
                                      "base_connector": b, "moved_connector": m})
    assert next(c for c in assembly_service.get_assembly(asm_id).components
                if c.id == moved_c).transform.translate.x == pytest.approx(.010)

    ops.dispatch("set_feature_params", base, {"feature_id": b,
                                              "params": {"origin": V(.025, 0, 0)}})
    assert next(c for c in assembly_service.get_assembly(asm_id).components
                if c.id == moved_c).transform.translate.x == pytest.approx(.025)


@pytest.mark.parametrize("kind", sorted(assembly_mates.CONNECTOR_TYPES))
def test_a_kinematic_mate_without_connectors_is_refused_by_name(kind):
    """It has no frames to align. Falling through to the bounding-box path would
    place the part plausibly and wrongly, which is harder to notice than a
    refusal that names the missing argument."""
    base, moved = _part(), _part()
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    envelope = ops.execute("add_mate", asm_id,
                           {"type": kind, "base": base_c, "moved": moved_c})
    assert envelope["ok"] is False
    assert envelope["error"]["arg"] == "base_connector"
    assert "add_mate_connector" in envelope["error"]["message"]


def test_a_connector_id_that_names_nothing_is_refused_at_the_operation():
    """The one moment it is cheap to catch. At solve time it becomes a silent
    fallback to the bounding box."""
    base, moved = _part(), _part()
    m = ops.dispatch("add_mate_connector", moved, {"origin": V(0, 0, 0)})["connector_id"]
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    envelope = ops.execute("add_mate", asm_id,
                           {"type": "revolute", "base": base_c, "moved": moved_c,
                            "base_connector": "nope", "moved_connector": m})
    assert envelope["ok"] is False
    assert envelope["error"]["arg"] == "base_connector"


# ---------------------------------------------------------------------- dof --

def test_every_mate_type_declares_its_freedoms():
    """A type the schema accepts and the freedom table does not know about is a
    mate that silently removes zero degrees of freedom."""
    from app.services import assembly_operations

    catalog = {op["name"]: op for op in ops.catalog()}
    declared = set(catalog["add_mate"]["params"]["type"]["enum"])
    assert declared == set(assembly_mates.FREEDOMS)
    assert assembly_mates.CONNECTOR_TYPES <= declared
    assert set(assembly_operations._MATE_TYPES) == declared


@pytest.mark.parametrize("kind, expected", [
    ("fasten", 0), ("revolute", 1), ("slider", 1), ("cylindrical", 2),
    ("planar", 3), ("ball", 3), ("parallel", 4),
])
def test_each_mate_type_leaves_the_right_number_of_freedoms(kind, expected):
    rotational, translational = assembly_mates.FREEDOMS[kind]
    assert rotational + translational == expected
    assert assembly_mates.removed(kind) == 6 - expected


def test_a_revolute_leaves_exactly_one_rotation():
    base, moved = _part(name="base"), _part(name="moved")
    b = ops.dispatch("add_mate_connector", base, {"origin": V(0, 0, .005)})["connector_id"]
    m = ops.dispatch("add_mate_connector", moved, {"origin": V(0, 0, -.005)})["connector_id"]
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    result = ops.dispatch("add_mate", asm_id,
                          {"type": "revolute", "base": base_c, "moved": moved_c,
                           "base_connector": b, "moved_connector": m})
    assert result["removes_dof"] == 5
    assert result["leaves_free"] == {"rotational": 1, "translational": 0}

    report = ops.dispatch("dof", asm_id, {})
    row = next(r for r in report["components"] if r["component_id"] == moved_c)
    assert row["dof"] == 1
    assert row["over_constrained"] is False


def test_an_unmated_component_has_all_six_degrees_of_freedom():
    part = _part()
    asm_id, (component,) = _assembly(part)
    report = ops.dispatch("dof", asm_id, {})
    assert report["components"][0]["dof"] == 6
    assert report["dof"] == 6
    assert report["grounded"] == []


def test_over_constraining_is_reported_rather_than_clamped():
    """Two mates can be individually valid and jointly impossible, and the count
    is the cheapest signal that they are."""
    base, moved = _part(name="base"), _part(name="moved")
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    for _ in range(2):
        ops.dispatch("add_mate", asm_id, {"type": "fasten", "base": base_c,
                                          "moved": moved_c})
    report = ops.dispatch("dof", asm_id, {})
    row = next(r for r in report["components"] if r["component_id"] == moved_c)
    assert row["over_constrained"] is True
    assert row["dof"] == 0                       # floored, not negative
    assert moved_c in report["over_constrained"]


def test_a_fastened_component_is_reported_grounded():
    base, moved = _part(name="base"), _part(name="moved")
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    ops.dispatch("add_mate", asm_id, {"type": "fasten", "base": base_c, "moved": moved_c})
    report = ops.dispatch("dof", asm_id, {})
    assert moved_c in report["grounded"]
    assert base_c not in report["grounded"]      # nothing constrains the base


# ------------------------------------------------------- the euler inverse --

@pytest.mark.parametrize("angles", [
    (0, 0, 0), (30, 0, 0), (0, 45, 0), (0, 0, 90),
    (15, 25, 35), (-40, 10, 170), (0, 90, 0), (0, -90, 0),
])
def test_a_rotation_survives_the_trip_to_euler_and_back(angles):
    """Getting the convention wrong here is silent — the part lands somewhere
    plausible and only a non-axis-aligned mate shows it. The two gimbal-lock
    cases are in the list because that is where the general formula divides by
    a cosine that has gone to zero."""
    from app.cad_kernel.evaluator.sdf import _euler_matrix

    original = _euler_matrix(*angles)
    rebuilt = _euler_matrix(*assembly_mates._euler_degrees(original))
    assert np.allclose(rebuilt, original, atol=1e-9)
