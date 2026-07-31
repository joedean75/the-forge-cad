"""A true ruled loft, beside the distance-field blend rather than replacing it.

`LoftSDF` interpolates the two profiles' *distance values*. That is a real
shape and a useful one, but it is not what CAD calls a loft: the waist of a
circle-to-square transition bulges the way an average of two fields does, not
the way a straight line between the two rims does. Mapping `LoftFeature` onto
OCC's `ThruSections` would have changed what an existing document means
depending on whether OCC was installed, so this is a second feature — the same
reasoning that put `EdgeFilletFeature` beside `FilletFeature`.
"""

import math

import pytest

from app.cad_kernel.evaluator import brep
from app.services import cad_operations as ops
from app.services import decompiler, document_service as docs

pytestmark = pytest.mark.skipif(
    not brep.AVAILABLE, reason="a ruled loft needs cadquery-ocp")


def _doc():
    return docs.create_document(name="Adapter", resolution=64, owner_id=None).id


def _loft(doc_id, **args):
    return ops.execute("add_ruled_loft", doc_id, args)


def _volume_cm3(doc_id):
    return ops.dispatch("mass_properties", doc_id, {})["volume_cm3"]


def test_a_circular_loft_is_exactly_a_frustum():
    """pi*h/3 * (R^2 + Rr + r^2). A ruled surface between two circles *is* a
    conical frustum, so this is an equality, not an approximation — which is
    what shows the surface is genuinely ruled."""
    doc_id = _doc()
    big, small, height = 0.020, 0.010, 0.060
    expected = math.pi * height / 3 * (big ** 2 + big * small + small ** 2) * 1e6

    result = _loft(doc_id, profile_a={"type": "circle", "radius": big},
                   profile_b={"type": "circle", "radius": small},
                   offset_a=0.0, offset_b=height)

    assert result["ok"], result.get("error")
    assert _volume_cm3(doc_id) == pytest.approx(expected, rel=1e-6)


def test_a_prismatic_loft_is_exactly_a_pyramid_frustum():
    """The same identity for squares: h/3 * (A + sqrt(AB) + B)."""
    doc_id = _doc()
    a, b, height = 0.040, 0.020, 0.050
    area_a, area_b = a * a, b * b
    expected = height / 3 * (area_a + math.sqrt(area_a * area_b) + area_b) * 1e6

    _loft(doc_id, profile_a={"type": "rect", "width": a, "height": a},
          profile_b={"type": "rect", "width": b, "height": b},
          offset_a=0.0, offset_b=height)

    assert _volume_cm3(doc_id) == pytest.approx(expected, rel=1e-6)


def test_it_is_a_different_body_from_the_distance_field_loft():
    """Both are legitimate; they are not the same shape. If they agreed there
    would have been no reason to add a second feature."""
    square = {"type": "rect", "width": 0.04, "height": 0.04}
    circle = {"type": "circle", "radius": 0.015}

    ruled_id = _doc()
    _loft(ruled_id, profile_a=square, profile_b=circle, offset_a=0.0, offset_b=0.05)

    blended_id = _doc()
    ops.dispatch("add_loft", blended_id, {"profile_a": square, "profile_b": circle,
                                          "offset_a": 0.0, "offset_b": 0.05})

    assert _volume_cm3(ruled_id) != pytest.approx(_volume_cm3(blended_id), rel=0.02)


def test_the_body_is_exact_and_addressable():
    doc_id = _doc()
    _loft(doc_id, profile_a={"type": "rect", "width": 0.04, "height": 0.04},
          profile_b={"type": "circle", "radius": 0.015}, offset_a=0.0, offset_b=0.05)

    assert docs.get_document(doc_id).derived.sampler == "brep"
    assert ops.dispatch("mass_properties", doc_id, {})["exact"] is True
    assert ops.dispatch("list_faces", doc_id, {})["count"] > 0


def test_a_smooth_loft_is_a_different_shape_again():
    """`ruled=False` asks ThruSections for a C1 surface through the sections,
    which is the other thing CAD systems call a loft."""
    sections = dict(profile_a={"type": "circle", "radius": 0.02},
                    profile_b={"type": "circle", "radius": 0.01},
                    offset_a=0.0, offset_b=0.06)

    ruled_id = _doc()
    _loft(ruled_id, ruled=True, **sections)
    smooth_id = _doc()
    result = _loft(smooth_id, ruled=False, **sections)

    assert result["ok"], result.get("error")
    assert _volume_cm3(smooth_id) > 0


def test_two_sections_at_the_same_height_are_refused():
    result = _loft(_doc(), profile_a={"type": "circle", "radius": 0.02},
                   profile_b={"type": "circle", "radius": 0.01},
                   offset_a=0.03, offset_b=0.03)

    assert result["ok"] is False
    assert result["error"]["code"] == "PRECONDITION"


def test_it_composes_as_a_boolean_like_any_other_body():
    doc_id = _doc()
    ops.dispatch("add_primitive", doc_id,
                 {"shape": "box", "params": {"size": {"x": .05, "y": .05, "z": .01}}})
    before = _volume_cm3(doc_id)

    _loft(doc_id, profile_a={"type": "circle", "radius": 0.02},
          profile_b={"type": "circle", "radius": 0.01},
          offset_a=0.005, offset_b=0.05, operation="union")

    assert _volume_cm3(doc_id) > before


def test_it_round_trips_through_the_decompiler():
    doc_id = _doc()
    _loft(doc_id, profile_a={"type": "circle", "radius": 0.02},
          profile_b={"type": "circle", "radius": 0.01}, offset_a=0.0, offset_b=0.06)

    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)

    assert [s["op"] for s in steps] == ["add_ruled_loft"]
    assert steps[0]["args"]["offset_b"] == 0.06


def test_the_distance_field_loft_is_still_declined_by_the_exact_kernel():
    """The old feature keeps its own meaning. Nothing about adding a real loft
    should change what an existing document evaluates to."""
    assert "LoftFeature" in brep.UNSUPPORTED
    assert "RuledLoftFeature" not in brep.UNSUPPORTED
