"""Where the numerics run, and that moving them did not change the answer.

`services/compute` holds the device decision and one Jacobi-preconditioned CG.
Both large systems in this codebase — SIMP's per-iteration `KU = F` and
`fea.solve`'s single one — now go through it. Before, SIMP had a torch solver
that could use a GPU and FEA had a scipy one that could not, so the analysis a
user waits for was the half pinned to the CPU.

**What is and is not verified here.** The CUDA path is the CPU path with a
different `torch.device`, which is why it is written this way rather than as a
second implementation. Everything below runs on CPU. No GPU was available where
this was written, so the CUDA branch is not verified by execution — the claim is
that it is device-agnostic torch, and `FORGE_DEVICE=cuda` is how someone with a
GPU makes these same tests prove it there.
"""

import os

import numpy as np
import pytest
from scipy.sparse import eye, random as sparse_random
from scipy.sparse.linalg import spsolve

from app.services import compute


def _spd(n: int = 800, seed: int = 0):
    """A sparse symmetric-positive-definite system, like a stiffness matrix."""
    a = sparse_random(n, n, density=0.004, random_state=seed)
    a = (a + a.T) + eye(n) * 20.0
    rng = np.random.default_rng(seed)
    return a.tocsr(), rng.standard_normal(n)


# ------------------------------------------------------------- the solver --

def test_the_shared_cg_matches_a_direct_solve():
    """The whole point of moving it is that nothing changed but the device."""
    a, b = _spd()
    assert np.abs(compute.pcg_solve(a, b, tol=1e-12) - spsolve(a.tocsc(), b)).max() < 1e-9


def test_a_zero_load_is_solved_rather_than_iterated_to_nothing():
    """`b_norm` is the convergence denominator, so a zero right-hand side would
    divide by ~0 forever and then be reported as non-convergent."""
    a, _ = _spd(200)
    assert np.abs(compute.pcg_solve(a, np.zeros(200))).max() == 0.0


def test_non_convergence_raises_instead_of_returning_a_plausible_vector():
    """An unconverged displacement field becomes a stress and then a safety
    factor. A wrong safety factor is the one output here that must never be
    quietly optimistic, so this is a raise and not a best effort."""
    a, b = _spd()
    with pytest.raises(RuntimeError, match="did not converge"):
        compute.pcg_solve(a, b, tol=1e-30, max_iter=2)


# ------------------------------------------------------- the device choice --

def test_the_device_is_cpu_or_cuda_and_is_describable():
    dev = compute.device()
    assert dev.type in ("cpu", "cuda")
    assert compute.describe_device(dev)


def test_forge_device_pins_it(monkeypatch):
    """The override is how a deployment forces CPU, and how someone with a GPU
    makes the CUDA branch run these same tests rather than hoping."""
    monkeypatch.setenv("FORGE_DEVICE", "cpu")
    assert compute.device().type == "cpu"


def test_a_nonsense_device_falls_back_rather_than_crashing_the_solve(monkeypatch):
    monkeypatch.setenv("FORGE_DEVICE", "quantum")
    assert compute.device().type == "cpu"


def test_prefer_gpu_false_is_honoured_when_no_override_is_set(monkeypatch):
    monkeypatch.delenv("FORGE_DEVICE", raising=False)
    assert compute.device(prefer_gpu=False).type == "cpu"


# --------------------------------------------------- both callers share it --

def test_simp_and_fea_use_the_same_solver():
    """One system assembled in two places should not be two solvers."""
    import inspect

    from app.services import simp_optimizer

    assert "compute.pcg_solve" in inspect.getsource(simp_optimizer._pcg_solve)

    from app.services import fea

    assert "compute.pcg_solve" in inspect.getsource(fea._solve_system)


def test_fea_reports_the_device_it_solved_on():
    """`solver` already said *how* the number was obtained; on the iterative
    path it now says where, so two runs that disagree can be told apart."""
    import inspect

    from app.services import fea

    source = inspect.getsource(fea._solve_system)
    assert "describe_device" in source
    # And the small-system path stays exact rather than being routed to CG.
    assert "spsolve" in source
    assert fea._DIRECT_SOLVE_DOF == compute.ITERATIVE_ABOVE_DOF


def test_a_real_cantilever_solves_through_the_shared_path():
    """End to end, not just the solver in isolation: a 200x20x20 mm steel
    cantilever with a 200 N tip load. The figure is checked loosely — the point
    is that the solve ran, converged, and reported how — because
    `test_fea` already pins the physics against the beam formula."""
    from app.cad_kernel.core.feature_tree import Feature
    from app.services import fea

    class _P:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = x, y, z

    class _Load:
        def __init__(self, point, force):
            self.point, self.force = point, force

    class _Fix:
        def __init__(self, point):
            self.point = point

    beam = Feature.from_dict({
        "type": "BoxFeature",
        "params": {"size": {"x": 0.200, "y": 0.020, "z": 0.020},
                   "center": {"x": 0.0, "y": 0.0, "z": 0.0}}})
    result = fea.solve(
        beam, youngs_pa=200e9, yield_mpa=250.0, density=7800.0,
        loads=[_Load(_P(0.100, 0.0, 0.0), _P(0.0, 0.0, -200.0))],
        constraints=[_Fix(_P(-0.100, 0.0, 0.0))],
        target_elements=4000)

    # 4k elements is under `ITERATIVE_ABOVE_DOF`, so this is the *direct* path —
    # deliberately, because it is the one that has to keep working unchanged.
    assert result["solver"] == "hex8-linear-static (direct)"
    assert result["max_displacement"] > 0
    assert result["safety_factor"] > 0


def test_a_large_model_takes_the_shared_iterative_path_and_names_the_device():
    """Above the threshold the direct factorisation is abandoned for CG — the
    path that now runs on `services/compute` and can use a GPU. It has to say
    so, so two runs on different hardware can be told apart."""
    from app.cad_kernel.core.feature_tree import Feature
    from app.services import fea

    class _P:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = x, y, z

    class _Load:
        def __init__(self, point, force):
            self.point, self.force = point, force

    class _Fix:
        def __init__(self, point):
            self.point = point

    beam = Feature.from_dict({
        "type": "BoxFeature",
        "params": {"size": {"x": 0.200, "y": 0.020, "z": 0.020},
                   "center": {"x": 0.0, "y": 0.0, "z": 0.0}}})
    result = fea.solve(
        beam, youngs_pa=200e9, yield_mpa=250.0, density=7800.0,
        loads=[_Load(_P(0.100, 0.0, 0.0), _P(0.0, 0.0, -200.0))],
        constraints=[_Fix(_P(-0.100, 0.0, 0.0))],
        target_elements=20000)

    assert "conjugate-gradient" in result["solver"], result["solver"]
    assert compute.describe_device() in result["solver"], result["solver"]
    assert result["max_displacement"] > 0
