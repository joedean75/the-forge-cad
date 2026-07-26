"""Sheet metal: bend geometry, flat-pattern maths, and the operation surface."""

import math

import pytest

from app.cad_kernel.core.feature_tree import Feature, FeatureTree
from app.cad_kernel.geom.meshing import mesh_volume
from app.cad_kernel.kernel import Kernel
from app.cad_kernel.ops.sheet_metal import (
    DEFAULT_K_FACTOR,
    SheetMetalFeature,
    bend_allowance,
    bend_deduction,
)
from app.services import cad_operations as ops
from app.services import document_service as ds


T, L, W, R = 0.002, 0.10, 0.06, 0.003


def _extent(feature):
    """Bounds of the feature's signed distance field."""
    from app.cad_kernel.evaluator.sdf import sdf_from_feature

    return sdf_from_feature(feature).bounds()


def _volume(feature, resolution=140):
    solid = Kernel.evaluate_tree(FeatureTree(feature), resolution=resolution)
    mesh = Kernel.tessellate(solid)
    return abs(mesh_volume(mesh.vertices, mesh.faces))


# --------------------------------------------------------------- bend maths --

def test_bend_allowance_follows_the_k_factor_formula():
    # BA = angle * (r + K*t); a 90 deg bend is a quarter of that circumference.
    ba = bend_allowance(90.0, R, T, DEFAULT_K_FACTOR)
    assert ba == pytest.approx((math.pi / 2) * (R + DEFAULT_K_FACTOR * T))


def test_bend_allowance_scales_with_angle_and_is_sign_agnostic():
    assert bend_allowance(180.0, R, T, 0.44) == pytest.approx(2 * bend_allowance(90.0, R, T, 0.44))
    assert bend_allowance(-90.0, R, T, 0.44) == bend_allowance(90.0, R, T, 0.44)


def test_a_larger_k_factor_moves_the_neutral_axis_out_and_lengthens_the_blank():
    assert bend_allowance(90.0, R, T, 0.50) > bend_allowance(90.0, R, T, 0.33)


def test_bend_deduction_is_positive_for_a_typical_90_degree_bend():
    assert bend_deduction(90.0, R, T, DEFAULT_K_FACTOR) > 0


# ------------------------------------------------------------- flat pattern --

def test_flat_pattern_of_an_unbent_plate_is_the_plate():
    f = SheetMetalFeature(thickness=T, length=L, width=W)
    fp = f.flat_pattern()
    assert fp["blank_size"]["x"] == pytest.approx(L)
    assert fp["blank_size"]["y"] == pytest.approx(W)
    assert fp["bend_count"] == 0


def test_flat_pattern_adds_allowance_plus_leg_per_bend():
    leg = 0.03
    f = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R,
                          flanges=[{"edge": "x+", "length": leg, "angle": 90}])
    fp = f.flat_pattern()
    expected = L + bend_allowance(90, R, T, DEFAULT_K_FACTOR) + leg
    assert fp["blank_size"]["x"] == pytest.approx(expected, abs=1e-6)
    assert fp["blank_size"]["y"] == pytest.approx(W)


def test_flanges_on_opposite_edges_both_grow_the_blank():
    f = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R, flanges=[
        {"edge": "x+", "length": 0.02, "angle": 90},
        {"edge": "x-", "length": 0.02, "angle": 90},
    ])
    fp = f.flat_pattern()
    growth = 2 * (bend_allowance(90, R, T, DEFAULT_K_FACTOR) + 0.02)
    assert fp["blank_size"]["x"] == pytest.approx(L + growth, abs=1e-6)
    assert fp["bend_count"] == 2


def test_flanges_on_different_axes_grow_their_own_axis():
    f = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R, flanges=[
        {"edge": "x+", "length": 0.02, "angle": 90},
        {"edge": "y-", "length": 0.01, "angle": -90},
    ])
    fp = f.flat_pattern()
    assert fp["blank_size"]["x"] > L
    assert fp["blank_size"]["y"] > W


def test_bend_line_position_is_reported_from_the_blank_edge():
    f = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R,
                          flanges=[{"edge": "x+", "length": 0.03, "angle": 90}])
    bend = f.flat_pattern()["bends"][0]
    # Nothing was added on the -x side, so the bend sits at the base length.
    assert bend["distance_from_edge"] == pytest.approx(L)


# ----------------------------------------------------------------- geometry --

def test_bent_flange_volume_matches_the_analytic_solid():
    """Base plate + quarter annulus + straight leg, all one width."""
    leg = 0.03
    f = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R,
                          flanges=[{"edge": "x+", "length": leg, "angle": 90}])
    expected = (
        L * T * W                                              # base plate
        + (math.pi / 2) * ((R + T) ** 2 - R ** 2) / 2 * W      # bend
        + leg * T * W                                          # leg
    )
    assert _volume(f) == pytest.approx(expected, rel=0.02)


def test_flat_plate_volume_is_exact():
    f = SheetMetalFeature(thickness=T, length=L, width=W)
    assert _volume(f, resolution=120) == pytest.approx(L * W * T, rel=0.02)


def test_flange_direction_controls_which_side_the_material_goes():
    up = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R,
                           flanges=[{"edge": "x+", "length": 0.03, "angle": 90}])
    down = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R,
                             flanges=[{"edge": "x+", "length": 0.03, "angle": -90}])
    assert _extent(up).max[2] > T * 2, "an up bend must add height above the plate"
    assert _extent(down).min[2] < 0.0, "a down bend must add material below the plate"


def test_bend_radius_shows_up_in_the_bounding_box():
    small = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=0.001,
                              flanges=[{"edge": "x+", "length": 0.02, "angle": 90}])
    large = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=0.008,
                              flanges=[{"edge": "x+", "length": 0.02, "angle": 90}])
    assert _extent(large).max[0] > _extent(small).max[0]


# ------------------------------------------------------------- serialization --

def test_feature_round_trips_through_the_tree_format():
    f = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R, k_factor=0.33,
                          flanges=[{"edge": "y+", "length": 0.02, "angle": 45}])
    restored = Feature.from_dict(f.to_dict())
    assert isinstance(restored, SheetMetalFeature)
    assert restored.thickness == T
    assert restored.k_factor == 0.33
    assert restored.flanges == f.flanges
    assert restored.flat_pattern() == f.flat_pattern()


# -------------------------------------------------------------- validation --

@pytest.mark.parametrize("bad", [
    {"edge": "z+", "length": 0.02, "angle": 90},
    {"edge": "x+", "length": -0.01, "angle": 90},
    {"edge": "x+", "length": 0.02, "angle": 0},
    {"edge": "x+", "length": 0.02, "angle": 200},
])
def test_invalid_flanges_are_rejected(bad):
    f = SheetMetalFeature(flanges=[bad])
    with pytest.raises(ValueError):
        f.normalized_flanges()


# -------------------------------------------------------------- operations --

def test_operations_are_published_in_the_catalog():
    names = {o["name"] for o in ops.catalog()}
    assert {"add_sheet_metal", "add_flange", "flat_pattern"} <= names


def test_agent_can_build_and_unfold_a_bracket():
    doc = ds.create_document(name="SM ops", units="mm", resolution=64, owner_id=None)
    ops.dispatch("add_sheet_metal", doc.id,
                 {"thickness": 0.002, "length": 0.12, "width": 0.05, "bend_radius": 0.003})
    flat_box = ops.dispatch("bounding_box", doc.id, {})["size_mm"]

    ops.dispatch("add_flange", doc.id, {"edge": "x+", "length": 0.025, "angle": 90})
    bent_box = ops.dispatch("bounding_box", doc.id, {})["size_mm"]
    assert bent_box["z"] > flat_box["z"], "bending a flange must change the solid"

    fp = ops.dispatch("flat_pattern", doc.id, {})
    assert fp["bend_count"] == 1
    assert fp["blank_size_mm"]["x"] > 120.0
    assert fp["bends"][0]["bend_allowance_mm"] > 0


def test_flat_pattern_without_a_sheet_metal_body_is_an_error():
    doc = ds.create_document(name="not sheet", units="mm", resolution=32, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    with pytest.raises(ValueError):
        ops.dispatch("flat_pattern", doc.id, {})


def test_flange_before_a_base_flange_is_an_error():
    doc = ds.create_document(name="no base", units="mm", resolution=32, owner_id=None)
    with pytest.raises(ValueError):
        ops.dispatch("add_flange", doc.id, {"edge": "x+", "length": 0.02, "angle": 90})
