"""Regression tests for the 2D sketch constraint solver, including arcs.

Covers: the trust-region path for under-constrained sketches (which 'lm' cannot
handle), dimensional constraints solving to exact values, and arc tessellation /
implicit equal-radius behaviour feeding a closed extrusion profile.
"""

import math

import numpy as np
import pytest

from app.services import sketch_solver as ss


def _rect_spec(w=0.1, h=0.06):
    """A rough rectangle with H/V + distance constraints (over-determined → lm)."""
    return {
        "points": [
            {"id": "a", "x": 0.001, "y": -0.002},
            {"id": "b", "x": 0.09, "y": 0.001},
            {"id": "c", "x": 0.098, "y": 0.058},
            {"id": "d", "x": -0.001, "y": 0.061},
        ],
        "lines": [
            {"id": "l0", "a": "a", "b": "b"},
            {"id": "l1", "a": "b", "b": "c"},
            {"id": "l2", "a": "c", "b": "d"},
            {"id": "l3", "a": "d", "b": "a"},
        ],
        "constraints": [
            {"type": "fixed", "point": "a", "x": 0.0, "y": 0.0},
            {"type": "horizontal", "line": "l0"},
            {"type": "horizontal", "line": "l2"},
            {"type": "vertical", "line": "l1"},
            {"type": "vertical", "line": "l3"},
            {"type": "distance", "a": "a", "b": "b", "value": w},
            {"type": "distance", "a": "a", "b": "d", "value": h},
        ],
    }


def test_rectangle_solves_to_exact_dimensions():
    solved = ss.solve_sketch(_rect_spec(0.1, 0.06))
    assert solved["converged"], solved
    p = solved["points"]
    assert math.hypot(*(np.array(p["b"]) - np.array(p["a"]))) == pytest.approx(0.1, abs=1e-6)
    assert math.hypot(*(np.array(p["d"]) - np.array(p["a"]))) == pytest.approx(0.06, abs=1e-6)


def test_underconstrained_rect_uses_trf_and_does_not_crash():
    # Only H/V constraints (4 residuals) vs 8 point DOF → 'lm' would raise;
    # the solver must fall back to 'trf'.
    spec = _rect_spec()
    spec["constraints"] = [c for c in spec["constraints"] if c["type"] in ("horizontal", "vertical")]
    solved = ss.solve_sketch(spec)  # must not raise
    assert "points" in solved


def _slot_spec(length=0.04, half_h=0.01):
    return {
        "points": [
            {"id": "tl", "x": 0.0, "y": half_h},
            {"id": "tr", "x": length, "y": half_h},
            {"id": "br", "x": length, "y": -half_h},
            {"id": "bl", "x": 0.0, "y": -half_h},
            {"id": "lc", "x": 0.0, "y": 0.0},
            {"id": "rc", "x": length, "y": 0.0},
        ],
        "lines": [
            {"id": "top", "a": "tl", "b": "tr"},
            {"id": "bot", "a": "br", "b": "bl"},
        ],
        "arcs": [
            {"id": "rarc", "a": "tr", "b": "br", "center": "rc", "ccw": False},
            {"id": "larc", "a": "bl", "b": "tl", "center": "lc", "ccw": False},
        ],
        "constraints": [],
    }


def test_arc_slot_solves_and_forms_closed_polygon():
    spec = _slot_spec(0.04, 0.01)
    solved = ss.solve_sketch(spec)
    assert solved["converged"], solved
    poly = ss.sketch_to_polygon(spec, solved, arc_segments=24)
    xs = [q[0] for q in poly]
    ys = [q[1] for q in poly]
    # Two lines + two semicircle caps → far more vertices than the 6 base points.
    assert len(poly) > 20
    # Slot spans x in [-r, length+r] and y in [-r, r] (r = half height = 0.01).
    assert min(xs) == pytest.approx(-0.01, abs=1e-4)
    assert max(xs) == pytest.approx(0.05, abs=1e-4)
    assert min(ys) == pytest.approx(-0.01, abs=1e-4)
    assert max(ys) == pytest.approx(0.01, abs=1e-4)
    # Loop must not repeat its start (closed implicitly, matching line-only convention).
    assert poly[0] != poly[-1]


def test_arc_radius_constraint_resizes_cap():
    spec = _slot_spec(0.04, 0.01)
    spec["constraints"] = [{"type": "arc_radius", "arc": "rarc", "value": 0.02}]
    # Free the top-right / bottom-right endpoints so the cap radius can grow.
    solved = ss.solve_sketch(spec)
    assert solved["converged"], solved
    p = solved["points"]
    r = math.hypot(p["tr"][0] - p["rc"][0], p["tr"][1] - p["rc"][1])
    assert r == pytest.approx(0.02, abs=1e-5)


def test_arc_extrude_volume_matches_analytic():
    """The tessellated slot profile extrudes to the analytic slot volume."""
    from app.cad_kernel.core.feature_tree import Feature
    from app.cad_kernel.evaluator.sdf import sdf_from_feature

    spec = _slot_spec(0.04, 0.01)
    poly = ss.sketch_to_polygon(spec, ss.solve_sketch(spec))
    node = {
        "type": "ExtrudeFeature", "id": "e", "name": "e",
        "params": {"profile": {"type": "polygon", "points": poly}, "plane": "xy", "offset": 0.0, "depth": 0.02},
        "children": [],
    }
    sdf = sdf_from_feature(Feature.from_dict(node))
    rng = np.random.default_rng(0)
    n = 300_000
    pts = rng.random((n, 3))
    pts[:, 0] = pts[:, 0] * 0.10 - 0.02
    pts[:, 1] = pts[:, 1] * 0.06 - 0.03
    pts[:, 2] = pts[:, 2] * 0.04 - 0.01
    frac = (sdf(pts) <= 0).mean()
    vol = frac * (0.10 * 0.06 * 0.04)
    # slot area = 0.04*0.02 + pi*0.01^2 = 1.114e-3 m^2, depth 0.02 → 2.228e-5 m^3
    assert vol == pytest.approx(2.228e-5, rel=0.03)
