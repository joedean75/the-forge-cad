"""Linear-static FEA: a real solve, on the mesh the optimizer already trusts.

`document_analysis._approximate_simulation` computes closed-form scalars —
`total_force / volume^(2/3)`, clamped to [0.01, 500] MPa — and calls the result
a stress. It does not know where the load is applied, where the part is held, or
what shape it is; a 10 mm cube and a 10 mm cantilever with the same load get the
same answer. That is why `simulate` was deliberately kept out of the operation
catalog: exposing it as a verifiable signal would teach a model to optimise a
number that means nothing.

This is the real thing, and it needed no new dependency. `simp_optimizer` has
been assembling and solving a 3D linear-elastic system on every iteration since
it was written — 8-node hexahedral elements, a sparse direct solve on CPU and a
Jacobi-preconditioned CG on GPU. What it never needed was *stress*, because
compliance is all SIMP optimises. So the element stiffness, the DOF map and the
solver are reused here as-is, and what is added is the recovery step: strain and
then Cauchy stress at each element centroid, then von Mises.

**A voxel mesh is honest about displacement and awkward about peak stress.**
Displacement is an integral of the solution and converges quickly. Peak stress
does not: a stair-stepped boundary puts re-entrant corners where the real part
has a smooth face, and the stress at a re-entrant corner rises without limit as
the mesh refines. So the report gives both the raw maximum *and* a 99th
percentile, says which is which, and computes the safety factor from the
percentile — the one that is stable under refinement. Anything that reports only
a peak voxel stress is quoting a mesh artefact to four significant figures.

CalculiX was the obvious alternative and was rejected: it is an external binary
that a deployment either has or does not, and this repo's optional dependencies
(OCP, an LLM key, Stripe) all degrade to something that still works. An analysis
that silently disappears in production is worse than one with a documented
discretisation caveat.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
from scipy.sparse import coo_matrix, diags
from scipy.sparse.linalg import LinearOperator, cg, spsolve

from app.cad_kernel.evaluator.sdf import Bounds
from app.services.simp_optimizer import _compute_ke, _edof_mat, _hex_shape_deriv

# Void elements keep a token stiffness so the system stays non-singular, exactly
# as the SIMP loop does. Nine orders down is well below anything load-bearing.
_E_VOID = 1e-9

# Element budget. 8k elements resolves a beam's bending to a few percent and
# solves in about a second.
_DEFAULT_ELEMENTS = 8000
_MAX_ELEMENTS = 60000

# Above this many free DOFs the direct solve is abandoned for conjugate
# gradients. A sparse LU of a 3D elasticity system fills in badly: a 98k-DOF
# beam took over five minutes and 5 GB before it was killed, where CG on the
# same system is seconds and O(n) memory. Below the threshold the direct solve
# is faster and exact, so it is kept.
_DIRECT_SOLVE_DOF = 20000
_CG_TOL = 1e-9
_CG_MAX_ITER_FACTOR = 20

_POISSON = 0.3

# Loads and supports are applied over a patch this fraction of the body's
# diagonal, rather than at a single node. See `_patch`.
_PATCH_FRACTION = 0.05

# The percentile the safety factor is computed from. See the module docstring:
# the raw maximum lives on a stair-step corner and does not converge.
_STRESS_PERCENTILE = 99.0


class FeaError(ValueError):
    """The model cannot be solved, with a reason worth showing."""


def _elasticity_matrix(youngs: float, nu: float = _POISSON) -> np.ndarray:
    """Isotropic 3D constitutive matrix (6x6), Voigt order xx yy zz xy yz zx."""
    lam = youngs * nu / ((1.0 + nu) * (1.0 - 2.0 * nu))
    mu = youngs / (2.0 * (1.0 + nu))
    D = np.zeros((6, 6))
    D[:3, :3] = lam
    D[0, 0] = D[1, 1] = D[2, 2] = lam + 2.0 * mu
    D[3, 3] = D[4, 4] = D[5, 5] = mu
    return D


def _centroid_b_matrix(hx: float, hy: float, hz: float) -> np.ndarray:
    """Strain-displacement matrix (6 x 24) at the element centroid.

    A regular hexahedron has a diagonal Jacobian, so mapping the shape-function
    derivatives from the reference element is a scale per axis rather than a
    matrix inverse.
    """
    dN = _hex_shape_deriv(0.0, 0.0, 0.0)          # (8, 3) in reference coords
    dN = dN * np.array([2.0 / hx, 2.0 / hy, 2.0 / hz])

    B = np.zeros((6, 24))
    for i in range(8):
        dx, dy, dz = dN[i]
        c = 3 * i
        B[0, c + 0] = dx
        B[1, c + 1] = dy
        B[2, c + 2] = dz
        B[3, c + 0], B[3, c + 1] = dy, dx      # gamma_xy
        B[4, c + 1], B[4, c + 2] = dz, dy      # gamma_yz
        B[5, c + 0], B[5, c + 2] = dz, dx      # gamma_zx
    return B


def _von_mises(stress: np.ndarray) -> np.ndarray:
    """Von Mises equivalent stress from (n, 6) Voigt components."""
    sxx, syy, szz, sxy, syz, szx = (stress[:, i] for i in range(6))
    return np.sqrt(0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
                   + 3.0 * (sxy ** 2 + syz ** 2 + szx ** 2))


def _grid_for(bounds, target_elements: int):
    """Element counts giving roughly cubic cells and about `target_elements`."""
    span = np.maximum(bounds.max - bounds.min, 1e-6)
    scale = (target_elements / float(span[0] * span[1] * span[2])) ** (1.0 / 3.0)
    dims = np.maximum(np.round(span * scale).astype(int), 2)
    return int(dims[0]), int(dims[1]), int(dims[2]), span


def _node_of(ix: int, iy: int, iz: int, nx: int, ny: int) -> int:
    """Node index in the DOF map's z-major ordering."""
    return iz * nx * ny + iy * nx + ix


def _node_grid(lo, span, nelx, nely, nelz):
    """World coordinates of every node, in the DOF map's own ordering."""
    xs = lo[0] + np.linspace(0.0, span[0], nelx + 1)
    ys = lo[1] + np.linspace(0.0, span[1], nely + 1)
    zs = lo[2] + np.linspace(0.0, span[2], nelz + 1)
    gz, gy, gx = np.meshgrid(np.arange(nelz + 1), np.arange(nely + 1),
                             np.arange(nelx + 1), indexing="ij")
    return np.stack([xs[gx.ravel()], ys[gy.ravel()], zs[gz.ravel()]], axis=1)


def _patch(nodes: np.ndarray, point: np.ndarray, radius: float) -> np.ndarray:
    """Node indices within `radius` of `point`, never fewer than the nearest one.

    A point force on a continuum is a singularity: the displacement under it
    grows without bound as the mesh refines, so a "converged" answer never
    arrives. Measured on a 200x20x20 cantilever the tip deflection went
    1.40 -> 1.72 -> 2.11 mm across three refinements, away from the 1.0 mm the
    beam formula gives, and support points did the same in reverse by getting
    relatively softer. Spreading both over a patch of fixed *physical* size
    fixes the geometry of the boundary condition, so refinement converges.
    """
    distance = np.linalg.norm(nodes - point, axis=1)
    inside = np.flatnonzero(distance <= radius)
    return inside if inside.size else np.array([int(distance.argmin())])


def _as_point(value) -> np.ndarray:
    if hasattr(value, "x"):
        return np.array([value.x, value.y, value.z], dtype=float)
    if isinstance(value, dict):
        return np.array([value.get("x", 0.0), value.get("y", 0.0), value.get("z", 0.0)])
    return np.asarray(value, dtype=float)


def _solve_system(stiffness, load):
    """KU = F, direct for small systems and Jacobi-preconditioned CG for large.

    Returns the displacement and the name of the method, so the report can say
    how the number was obtained rather than leaving it to be inferred.
    """
    n = stiffness.shape[0]
    if n <= _DIRECT_SOLVE_DOF:
        return spsolve(stiffness.tocsc(), load), "direct"

    matrix = stiffness.tocsr()
    diagonal = matrix.diagonal().copy()
    diagonal[np.abs(diagonal) < 1e-30] = 1.0
    preconditioner = LinearOperator(
        (n, n), matvec=lambda v: v / diagonal, dtype=float)
    solution, info = cg(matrix, load, rtol=_CG_TOL, atol=0.0,
                        maxiter=_CG_MAX_ITER_FACTOR * n, M=preconditioner)
    if info != 0:
        raise FeaError(
            f"conjugate gradients did not converge on {n} degrees of freedom "
            f"(info={info}); the model may be under-constrained")
    return solution, "conjugate-gradient"


class _BrepOccupancy:
    """Inside/outside for a body that has no analytic field, from the B-rep itself.

    Not a distance field and not pretending to be: `solve` only ever asks
    `field(centres) <= 0`, so what is needed is an inside test — and a body that
    has no field has one *because* it is exact geometry, which can answer that
    question exactly. `BRepClass3d_SolidClassifier` costs about 30 µs a point,
    so a 60k-element grid is a couple of seconds beside a CG solve.

    Voxelising the tessellation was tried first, following `drawing._field`'s
    precedent, and is wrong for this. `trimesh.voxelized().fill()` marks every
    voxel the surface passes through, so the body comes back dilated by one voxel
    all over: a 200×20×20 beam solved 4% heavy at the element pitch and 11% heavy
    at four times finer, because the shell stays one voxel thick however small
    the voxel is. Drawing can absorb that — it only traces an outline — but mass
    feeds straight into stress here. Ray-casting the mesh with `trimesh.contains`
    is exact but wants `rtree`, and a new hard dependency to answer a question
    OCC already answers is the wrong trade.
    """

    def __init__(self, shape):
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier

        self._classifier = BRepClass3d_SolidClassifier(shape)
        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box)
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        self._bounds = Bounds(np.array([xmin, ymin, zmin], dtype=float),
                              np.array([xmax, ymax, zmax], dtype=float))

    def bounds(self) -> "Bounds":
        return self._bounds

    def __call__(self, points: np.ndarray) -> np.ndarray:
        from OCP.gp import gp_Pnt
        from OCP.TopAbs import TopAbs_State

        # Metres throughout, so Precision::Confusion (1e-7) is 0.1 µm here —
        # four orders below the thinnest feature the kernel deals with.
        classifier, inward = self._classifier, TopAbs_State.TopAbs_IN
        points = np.asarray(points, dtype=float)
        out = np.ones(len(points), dtype=float)
        for i, (x, y, z) in enumerate(points):
            classifier.Perform(gp_Pnt(float(x), float(y), float(z)), 1e-7)
            if classifier.State() == inward:
                out[i] = -1.0
        return out


def _occupancy_field(body_feature):
    """Something that answers "is this point inside the body?" for any body.

    Analytic where there is a field, and the exact kernel otherwise. Without the
    second path every part carrying a real edge fillet, chamfer, face shell or
    imported STEP solid — the whole point of the exact kernel — fell through to
    `document_analysis`'s closed-form estimate, which cannot tell a cube from a
    cantilever. A meaningless number reaching a caller that asked for an analysis
    is the one failure this module exists to prevent.
    """
    from app.cad_kernel.evaluator.sdf import sdf_from_feature

    try:
        return sdf_from_feature(body_feature)
    except ValueError as exc:
        reason = str(exc)

    from app.cad_kernel.evaluator import brep

    if not brep.AVAILABLE:
        # Unreachable in practice — the features with no field are exactly the
        # ones OCC has to build — but a clear reason beats an AttributeError.
        raise FeaError(reason)
    shape = brep.build_strict(body_feature)
    return _BrepOccupancy(shape)


def solve(body_feature, *, youngs_pa: float, yield_mpa: float, density: float,
          loads: List[Any], constraints: List[Any],
          target_elements: int = _DEFAULT_ELEMENTS) -> Dict[str, Any]:
    """Linear-static analysis of an evaluated body. SI in, mm/MPa out.

    `loads` carry a point and a force vector in newtons; `constraints` carry a
    point that is fully fixed. With no constraints the whole bottom face is
    grounded, which is `run_simp`'s convention and the only assumption that
    makes an under-specified model solvable rather than singular.
    """
    if not loads:
        raise FeaError(
            "a static analysis needs at least one load case — with no force "
            "applied, every stress and displacement is zero by definition")

    target_elements = int(np.clip(target_elements, 200, _MAX_ELEMENTS))
    field = _occupancy_field(body_feature)
    bounds = field.bounds()
    nelx, nely, nelz, span = _grid_for(bounds, target_elements)
    lo = bounds.min
    hx, hy, hz = span[0] / nelx, span[1] / nely, span[2] / nelz

    # Element-centre occupancy, ordered z-major to match the DOF map.
    ex = lo[0] + (np.arange(nelx) + 0.5) * hx
    ey = lo[1] + (np.arange(nely) + 0.5) * hy
    ez = lo[2] + (np.arange(nelz) + 0.5) * hz
    gk, gj, gi = np.meshgrid(np.arange(nelz), np.arange(nely), np.arange(nelx),
                             indexing="ij")
    centres = np.stack([ex[gi.ravel()], ey[gj.ravel()], ez[gk.ravel()]], axis=1)
    solid = field(centres) <= 0.0
    if not solid.any():
        raise FeaError("the body did not intersect the analysis grid")

    n_nodes = (nelx + 1) * (nely + 1) * (nelz + 1)
    n_dof = 3 * n_nodes
    nelem = nelx * nely * nelz

    Ke = _compute_ke(_POISSON, hx=hx, hy=hy, hz=hz)
    edof = _edof_mat(nelx, nely, nelz)
    rows = np.kron(edof, np.ones((1, 24), dtype=int)).flatten()
    cols = np.kron(edof, np.ones((24, 1), dtype=int)).flatten()

    modulus = np.where(solid, youngs_pa, youngs_pa * _E_VOID)
    data = (Ke.flatten()[None, :] * modulus[:, None]).flatten()
    K = coo_matrix((data, (rows, cols)), shape=(n_dof, n_dof)).tocsc()

    nodes = _node_grid(lo, span, nelx, nely, nelz)
    radius = _PATCH_FRACTION * float(np.linalg.norm(span))

    fixed: set = set()
    for constraint in constraints or []:
        for node in _patch(nodes, _as_point(getattr(constraint, "point", constraint)),
                           radius):
            fixed.update((3 * node, 3 * node + 1, 3 * node + 2))
    grounded = f"{len(constraints or [])} support patch(es) of radius " \
               f"{radius * 1000:.1f} mm"
    if len(fixed) < 9:
        # Fewer than three fixed nodes leaves rigid-body modes and a singular
        # system. Ground the bottom face rather than return a solution that is
        # arbitrary up to a translation.
        for iy in range(nely + 1):
            for ix in range(nelx + 1):
                node = _node_of(ix, iy, 0, nelx + 1, nely + 1)
                fixed.update((3 * node, 3 * node + 1, 3 * node + 2))
        grounded = "bottom face (no usable supports were given)"

    force = np.zeros(n_dof)
    total_force = 0.0
    for load in loads:
        vector = _as_point(getattr(load, "force", (0.0, 0.0, 0.0)))
        patch = _patch(nodes, _as_point(getattr(load, "point", load)), radius)
        share = vector / float(patch.size)          # the total force is preserved
        for node in patch:
            force[3 * node:3 * node + 3] += share
        total_force += float(np.linalg.norm(vector))
    if total_force <= 0.0:
        raise FeaError("every load case has zero force")

    free = np.setdiff1d(np.arange(n_dof), np.array(sorted(fixed), dtype=int))
    displacement = np.zeros(n_dof)
    try:
        displacement[free], method = _solve_system(K[free][:, free], force[free])
    except FeaError:
        raise
    except Exception as exc:                       # pragma: no cover - solver blowup
        raise FeaError(f"the linear system could not be solved: {exc}") from exc
    if not np.all(np.isfinite(displacement)):
        raise FeaError(
            "the solve did not converge — the model is probably under-constrained")

    # Stress recovery, solid elements only: a void element's stress is a
    # property of its 1e-9 stiffness, not of the part.
    B = _centroid_b_matrix(hx, hy, hz)
    D = _elasticity_matrix(youngs_pa)
    solid_idx = np.flatnonzero(solid)
    element_u = displacement[edof[solid_idx]]                  # (n_solid, 24)
    strain = element_u @ B.T                                   # (n_solid, 6)
    stress = strain @ D.T
    mises_pa = _von_mises(stress)

    magnitude = np.linalg.norm(displacement.reshape(-1, 3), axis=1)
    peak_mpa = float(mises_pa.max()) / 1e6
    typical_mpa = float(np.percentile(mises_pa, _STRESS_PERCENTILE)) / 1e6
    volume = float(solid.sum()) * hx * hy * hz
    compliance = float(force @ displacement)

    # The safety factor is computed from the peak, not the percentile. The
    # percentile is the more stable number and the tempting one to headline, but
    # it sits *below* the real stress, which pushes the safety factor up — and a
    # safety factor that errs optimistic is the one failure mode this must not
    # have. On a voxel mesh the peak is an upper bound, so the factor is
    # conservative, and both numbers are reported so the gap is visible.
    return {
        "solver": f"hex8-linear-static ({method})",
        "max_stress": round(peak_mpa, 4),
        "stress_p99": round(typical_mpa, 4),
        "max_displacement": round(float(magnitude.max()) * 1000.0, 6),
        "safety_factor": round(min(yield_mpa / max(peak_mpa, 1e-9), 999.0), 3),
        "compliance": round(compliance, 9),
        "mass": round(volume * density, 6),
        "mesh": {
            "elements": int(solid.sum()),
            "grid": [nelx, nely, nelz],
            "element_size_mm": round(float(min(hx, hy, hz)) * 1000.0, 4),
        },
        "grounded_on": grounded,
        "notes": [
            "max_stress is the highest element von Mises stress and the safety "
            f"factor is against {yield_mpa:g} MPa yield from it, so the factor is "
            "conservative: on a stair-stepped boundary the peak element overstates "
            "the real surface stress and does not converge under refinement.",
            f"stress_p99 is the {_STRESS_PERCENTILE:g}th percentile — stable under "
            "refinement, but below the true peak, so it is context and not the "
            "number to size a part from.",
            "loads and supports act over a patch of "
            f"{_PATCH_FRACTION:.0%} of the body diagonal, not a single node; a point "
            "force on a continuum has no converged solution.",
        ],
    }


def available() -> bool:
    """Always true — the solver is in-process, with no external binary."""
    return True
