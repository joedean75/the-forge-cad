"""Linear-static FEA — a solve that knows what shape the part is.

`_approximate_simulation` computed `total_force / volume^(2/3)`, clamped. It has
no idea where the load acts, where the part is held, or what it looks like, and
that is why `simulate` was kept out of the operation catalog: a verifiable
signal that means nothing is worse than none, because a model will optimise it.

These tests check the two things that make the replacement worth having — that
it agrees with a closed-form answer where one exists, and that it converges.
"""

import numpy as np
import pytest

from app.cad_kernel.core.feature_tree import Feature
from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services import fea

_E = 200e9          # steel, Pa
_YIELD = 250.0      # MPa


class _P:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class _Load:
    def __init__(self, point, force):
        self.point, self.force = point, force


class _Fix:
    def __init__(self, point):
        self.point = point


def _beam(length=0.200, width=0.020, depth=0.020):
    return Feature.from_dict({
        "type": "BoxFeature", "id": "f1", "name": "Beam",
        "params": {"size": {"x": length, "y": width, "z": depth},
                   "center": {"x": length / 2, "y": 0.0, "z": 0.0}}})


def _cantilever(elements=4000, load_n=1000.0, length=0.200):
    return fea.solve(
        _beam(length), youngs_pa=_E, yield_mpa=_YIELD, density=7800.0,
        loads=[_Load(_P(length, 0.0, 0.0), _P(0.0, 0.0, -load_n))],
        constraints=[_Fix(_P(0.0, y, z)) for y in (-0.01, 0, 0.01)
                     for z in (-0.01, 0, 0.01)],
        target_elements=elements)


def test_a_cantilever_matches_the_beam_formula():
    """PL^3/3EI, corrected for the length the supports actually clamp.

    The support patch is 5% of the body diagonal — about 10 mm on a 200 mm beam
    — so the free length is ~190 mm, not 200. Deflection goes as L^3, so that is
    a 14% difference and the test would be meaningless without it. Getting the
    *corrected* figure right is what shows the solver is correct rather than
    accidentally close.
    """
    length, width, depth, load = 0.200, 0.020, 0.020, 1000.0
    second_moment = width * depth ** 3 / 12.0
    patch = 0.05 * float(np.linalg.norm([length, width, depth]))
    effective = length - patch
    expected_mm = load * effective ** 3 / (3 * _E * second_moment) * 1000.0

    result = _cantilever(elements=8000)

    assert result["max_displacement"] == pytest.approx(expected_mm, rel=0.10)


def test_the_answer_converges_as_the_mesh_refines():
    """The property a point load destroys.

    Applied at a single node, tip deflection went 1.40 -> 1.72 -> 2.11 mm across
    three refinements — diverging, because a point force on a continuum has no
    finite solution. Spread over a patch of fixed physical size it settles.
    """
    coarse = _cantilever(elements=2000)["max_displacement"]
    medium = _cantilever(elements=8000)["max_displacement"]
    fine = _cantilever(elements=20000)["max_displacement"]

    assert abs(fine - medium) < abs(medium - coarse)      # the steps shrink
    assert abs(fine - medium) / fine < 0.02               # and are small


def test_stress_scales_with_load():
    single = _cantilever(load_n=1000.0)
    double = _cantilever(load_n=2000.0)

    # Linear elasticity: double the load, double the stress, exactly.
    assert double["max_stress"] == pytest.approx(2 * single["max_stress"], rel=1e-6)
    # The factor is reported to three decimals, so halving it cannot survive
    # the rounding any tighter than the last digit.
    assert double["safety_factor"] == pytest.approx(single["safety_factor"] / 2, abs=1e-3)


def test_a_longer_lever_arm_bends_more():
    """The estimate cannot see this: both beams have the same volume^(2/3)."""
    short = _cantilever(length=0.10)["max_displacement"]
    long = _cantilever(length=0.20)["max_displacement"]

    assert long > 4 * short          # deflection goes as L^3


def test_the_safety_factor_is_taken_from_the_peak_not_the_percentile():
    """Conservative by construction. The percentile is the prettier, more stable
    number, and it sits below the real stress — which pushes the safety factor
    *up*. An optimistic safety factor is the one failure mode this must not have.
    """
    result = _cantilever()

    assert result["stress_p99"] < result["max_stress"]
    assert result["safety_factor"] == pytest.approx(
        _YIELD / result["max_stress"], rel=1e-3)


def test_a_model_with_no_load_is_refused_rather_than_answered_with_zero():
    with pytest.raises(fea.FeaError):
        fea.solve(_beam(), youngs_pa=_E, yield_mpa=_YIELD, density=7800.0,
                  loads=[], constraints=[])


def test_an_ungrounded_model_is_grounded_rather_than_left_singular():
    result = fea.solve(
        _beam(), youngs_pa=_E, yield_mpa=_YIELD, density=7800.0,
        loads=[_Load(_P(0.2, 0, 0), _P(0, 0, -500.0))], constraints=[],
        target_elements=2000)

    assert "bottom face" in result["grounded_on"]
    assert np.isfinite(result["max_displacement"])


def test_large_models_switch_to_conjugate_gradients():
    """A direct sparse LU fills in badly in 3D: a 98k-DOF beam took over five
    minutes and 5 GB before it was killed. CG is seconds and O(n) memory."""
    small = _cantilever(elements=2000)
    large = _cantilever(elements=20000)

    assert "direct" in small["solver"]
    assert "conjugate-gradient" in large["solver"]


# ── through the operation catalog ──

def _doc(size):
    doc = docs.create_document(name="Part", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box", "params": {"size": size}})
    return doc.id


def test_simulate_is_now_an_operation_and_reports_a_real_solve():
    """It was deliberately absent while it was a closed-form guess."""
    env = ops.execute("simulate", _doc({"x": .2, "y": .02, "z": .02}), {"elements": 4000})

    assert env["ok"], env.get("error")
    result = env["result"]
    assert "hex8-linear-static" in result["solver"]
    assert result.get("estimate_only") is not True
    assert result["mesh"]["elements"] > 0


def test_the_result_distinguishes_parts_the_estimate_could_not():
    """A slender beam and a solid block, same load, same supports. The old
    estimate depends only on force and volume^(2/3)."""
    beam = ops.dispatch("simulate", _doc({"x": .2, "y": .02, "z": .02}), {"elements": 4000})
    block = ops.dispatch("simulate", _doc({"x": .2, "y": .2, "z": .2}), {"elements": 4000})

    assert beam["max_displacement"] > 3 * block["max_displacement"]


def test_a_fallback_says_plainly_that_it_is_not_an_analysis():
    """If the solve cannot run, the caller has to be able to tell — otherwise a
    meaningless number gets scored as if it were a measurement."""
    from app.services import document_analysis

    doc_id = _doc({"x": .05, "y": .05, "z": .05})
    original = fea.solve
    try:
        fea.solve = lambda *a, **k: (_ for _ in ()).throw(fea.FeaError("no solver"))
        report = document_analysis.analyse_document(doc_id)
    finally:
        fea.solve = original

    assert report["estimate_only"] is True
    assert report["solver"] == "closed-form estimate"
    assert "no solver" in report["reason"]
    assert any("NOT an analysis" in note for note in report["notes"])


def test_the_rest_simulation_endpoint_still_returns_its_own_shape():
    from app.services import document_analysis

    result = document_analysis.simulate_document(_doc({"x": .2, "y": .02, "z": .02}))

    assert result.max_stress > 0
    assert result.safety_factor > 0


# ------------------------------------------- bodies with no distance field --

# An edge fillet, a face shell and an imported STEP solid have no SDF form at
# all — they are the features the exact kernel exists to provide. Until the
# occupancy field could come from the B-rep, every one of them fell straight
# through to the closed-form estimate: the parts most worth analysing were
# exactly the parts that could not be.

exact_only = pytest.mark.skipif(
    not __import__("app.cad_kernel.evaluator.brep", fromlist=["brep"]).AVAILABLE,
    reason="OCP (OpenCascade) not installed")


def _filleted(size, radius=0.005):
    doc_id = _doc(size)
    edges = ops.execute("list_edges", doc_id, {"kind": "line"})["result"]["edges"]
    height = size["z"] * 1000
    picked = [e["id"] for e in edges if abs(e["length_mm"] - height) < .01]
    env = ops.execute("fillet_edges", doc_id, {"edges": picked, "radius": radius})
    assert env["ok"], env.get("error")
    return doc_id


@exact_only
def test_a_filleted_part_gets_a_real_solve_not_the_estimate():
    result = ops.dispatch("simulate", _filleted({"x": .1, "y": .06, "z": .02}), {"elements": 4000})

    assert "hex8-linear-static" in result["solver"]
    assert result.get("estimate_only") is not True


@exact_only
def test_the_brep_occupancy_agrees_with_the_distance_field_it_replaces():
    """The two paths have to describe the same body, or the fallback is a lie.

    Solved on a plain beam, which has both an analytic field and an exact form,
    with the field path suppressed for the second run. They differ only where an
    element centre lands within OCC's confusion tolerance of the surface.
    """
    from app.cad_kernel.evaluator import sdf as sdf_module

    beam = _beam()
    kwargs = dict(youngs_pa=_E, yield_mpa=_YIELD, density=7850.0,
                  loads=[_Load(_P(.1, 0, 0), _P(0, 0, -500))],
                  constraints=[_Fix(_P(-.1, 0, 0))], target_elements=4000)

    analytic = fea.solve(beam, **kwargs)
    original = sdf_module.sdf_from_feature
    try:
        sdf_module.sdf_from_feature = lambda f: (_ for _ in ()).throw(ValueError("no field"))
        exact = fea.solve(beam, **kwargs)
    finally:
        sdf_module.sdf_from_feature = original

    for key in ("mass", "max_displacement", "stress_p99", "max_stress"):
        assert exact[key] == pytest.approx(analytic[key], rel=1e-3), key


@exact_only
def test_hollowing_a_part_moves_the_answer_the_way_physics_does():
    """The point of solving these bodies at all: the shell has to change the result.

    A 3 mm wall through the top face removes about seventy per cent of the
    material, so the section is far less stiff and the stress rises several-fold.
    The old estimate could not see the difference — `volume^(2/3)` moves the
    wrong way, making the *lighter* part look stronger per unit load.
    """
    size = {"x": .1, "y": .06, "z": .03}
    solid_id = _doc(size)
    shell_id = _doc(size)
    faces = ops.execute("list_faces", shell_id, {})["result"]["faces"]
    top = max((f for f in faces if f.get("normal") == [0., 0., 1.]),
              key=lambda f: f["center_mm"][2])
    assert ops.execute("shell_faces", shell_id, {"faces": [top["id"]], "thickness": .003})["ok"]

    solid = ops.dispatch("simulate", solid_id, {"elements": 4000})
    shell = ops.dispatch("simulate", shell_id, {"elements": 4000})

    assert shell.get("estimate_only") is not True
    assert shell["mass"] < 0.5 * solid["mass"]
    assert shell["stress_p99"] > 2 * solid["stress_p99"]
    assert shell["safety_factor"] < solid["safety_factor"]
