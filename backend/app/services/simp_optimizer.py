"""
3D GPU-accelerated SIMP topology optimizer.

Implements the classic SIMP (Solid Isotropic Material with Penalization)
method for compliance minimization under a volume constraint on a regular
hexahedral mesh.  Density-field operations (filtering, OC update) are run
on the GPU via PyTorch when a CUDA device is available; the sparse linear
system KU=F is solved on the host with scipy for stability.

Adapted from the educational MATLAB code:
  Liu & Tovar, "An efficient 3D topology optimization code written in Matlab",
  Structural and Multidisciplinary Optimization, 2014.
"""

import time
import math
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import trimesh
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.linalg import spsolve
import torch

from app.models.schemas import DesignSpec, PreserveRegion, ObstacleRegion, LoadCase, Constraint, Vector3


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class SimpResult:
    """Output of a SIMP optimization run, including everything needed for
    downstream simulation and manufacturing analysis."""

    mesh: trimesh.Trimesh
    info: dict
    spec: DesignSpec
    rho: np.ndarray
    U: np.ndarray
    edofMat: np.ndarray
    Ke: np.ndarray
    nelx: int
    nely: int
    nelz: int
    size: Vector3
    origin: Vector3
    has_user_constraints: bool
    has_user_loads: bool


# ---------------------------------------------------------------------------
# Progress reporting for long-running SIMP jobs
# ---------------------------------------------------------------------------


class ProgressState:
    """Thread-safe progress state shared between the optimizer and the API."""

    def __init__(self):
        self._lock = threading.Lock()
        self.stage = "initializing"
        self.iteration = 0
        self.max_iter = 0
        self.compliance = 0.0
        self.change = 1.0
        self.message = "Initializing topology optimizer"

    def update(
        self,
        stage: Optional[str] = None,
        iteration: Optional[int] = None,
        max_iter: Optional[int] = None,
        compliance: Optional[float] = None,
        change: Optional[float] = None,
        message: Optional[str] = None,
    ):
        with self._lock:
            if stage is not None:
                self.stage = stage
            if iteration is not None:
                self.iteration = iteration
            if max_iter is not None:
                self.max_iter = max_iter
            if compliance is not None:
                self.compliance = compliance
            if change is not None:
                self.change = change
            if message is not None:
                self.message = message

    def read(self) -> dict:
        with self._lock:
            return {
                "stage": self.stage,
                "iteration": self.iteration,
                "max_iter": self.max_iter,
                "compliance": self.compliance,
                "change": self.change,
                "message": self.message,
            }


# ---------------------------------------------------------------------------
# Element stiffness matrix for an 8-node hexahedral element (24 x 24)
# ---------------------------------------------------------------------------


def _hex_shape_deriv(xi: float, eta: float, zeta: float) -> np.ndarray:
    """Return dN/dxi (8 x 3) for the 8-node hexahedral element."""
    dN = np.zeros((8, 3))
    # Node ordering matches the MATLAB top3d convention
    coeffs = [
        (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
        (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
    ]
    for i, (cx, cy, cz) in enumerate(coeffs):
        dN[i, 0] = 0.125 * cx * (1 + cy * eta) * (1 + cz * zeta)
        dN[i, 1] = 0.125 * cy * (1 + cx * xi) * (1 + cz * zeta)
        dN[i, 2] = 0.125 * cz * (1 + cx * xi) * (1 + cy * eta)
    return dN


def _compute_ke(nu: float = 0.3, hx: float = 1.0, hy: float = 1.0, hz: float = 1.0) -> np.ndarray:
    """Compute the 24x24 element stiffness matrix for a hexahedral element."""
    # Constitutive matrix for isotropic linear elasticity (3D)
    k = (1.0 - nu) / ((1.0 + nu) * (1.0 - 2.0 * nu))
    c11 = k
    c12 = k * nu / (1.0 - nu)
    c44 = (1.0 - 2.0 * nu) / (2.0 * (1.0 - nu))
    D = np.array([
        [c11, c12, c12, 0,   0,   0],
        [c12, c11, c12, 0,   0,   0],
        [c12, c12, c11, 0,   0,   0],
        [0,   0,   0,   c44, 0,   0],
        [0,   0,   0,   0,   c44, 0],
        [0,   0,   0,   0,   0,   c44],
    ])

    # Node coordinates for a physical element [0,hx] x [0,hy] x [0,hz]
    nodes = np.array([
        [0, 0, 0], [hx, 0, 0], [hx, hy, 0], [0, hy, 0],
        [0, 0, hz], [hx, 0, hz], [hx, hy, hz], [0, hy, hz],
    ], dtype=float)

    Ke = np.zeros((24, 24))
    gp = 1.0 / math.sqrt(3.0)
    gauss_points = [(-gp, -gp, -gp), (gp, -gp, -gp), (gp, gp, -gp), (-gp, gp, -gp),
                    (-gp, -gp, gp),  (gp, -gp, gp),  (gp, gp, gp),  (-gp, gp, gp)]

    for xi, eta, zeta in gauss_points:
        dN = _hex_shape_deriv(xi, eta, zeta)
        J = dN.T @ nodes  # 3x3 Jacobian
        detJ = np.linalg.det(J)
        invJ = np.linalg.inv(J)
        dNdx = dN @ invJ

        B = np.zeros((6, 24))
        for i in range(8):
            B[0, 3*i]     = dNdx[i, 0]
            B[1, 3*i + 1] = dNdx[i, 1]
            B[2, 3*i + 2] = dNdx[i, 2]
            B[3, 3*i]     = dNdx[i, 1]
            B[3, 3*i + 1] = dNdx[i, 0]
            B[4, 3*i + 1] = dNdx[i, 2]
            B[4, 3*i + 2] = dNdx[i, 1]
            B[5, 3*i]     = dNdx[i, 2]
            B[5, 3*i + 2] = dNdx[i, 0]

        Ke += B.T @ D @ B * detJ

    return Ke


# ---------------------------------------------------------------------------
# Mesh bookkeeping
# ---------------------------------------------------------------------------


def _edof_mat(nelx: int, nely: int, nelz: int) -> np.ndarray:
    """Element -> global DOF mapping (nelem x 24)."""
    n_nodes_x, n_nodes_y, n_nodes_z = nelx + 1, nely + 1, nelz + 1

    def node_id(ix, iy, iz):
        return iz * n_nodes_x * n_nodes_y + iy * n_nodes_x + ix

    nelem = nelx * nely * nelz
    edofMat = np.zeros((nelem, 24), dtype=int)
    e = 0
    for k in range(nelz):
        for j in range(nely):
            for i in range(nelx):
                n = [node_id(i,   j,   k),   node_id(i+1, j,   k),
                     node_id(i+1, j+1, k),   node_id(i,   j+1, k),
                     node_id(i,   j,   k+1), node_id(i+1, j,   k+1),
                     node_id(i+1, j+1, k+1), node_id(i,   j+1, k+1)]
                edof = []
                for nid in n:
                    edof.extend([3*nid, 3*nid+1, 3*nid+2])
                edofMat[e] = edof
                e += 1
    return edofMat


# ---------------------------------------------------------------------------
# Density filter
# ---------------------------------------------------------------------------


def _build_filter(nelx: int, nely: int, nelz: int, rmin: float) -> csr_matrix:
    """Build the density-sensitivity filter matrix H."""
    nelem = nelx * nely * nelz
    centers = np.zeros((nelem, 3))
    e = 0
    for k in range(nelz):
        for j in range(nely):
            for i in range(nelx):
                centers[e] = [i + 0.5, j + 0.5, k + 0.5]
                e += 1

    iH = []
    jH = []
    sH = []
    for e1 in range(nelem):
        c1 = centers[e1]
        for e2 in range(nelem):
            dist = np.linalg.norm(centers[e2] - c1)
            if dist < rmin:
                iH.append(e1)
                jH.append(e2)
                sH.append(rmin - dist)

    H = coo_matrix((sH, (iH, jH)), shape=(nelem, nelem)).tocsr()
    Hs = np.array(H.sum(axis=1)).flatten()
    H = H.tocsr()
    # Normalize rows
    H = H.multiply(1.0 / Hs[:, None])
    return H


# ---------------------------------------------------------------------------
# Optimality Criteria update
# ---------------------------------------------------------------------------


def _pcg_solve(K_csr, F, device, tol: float = 1e-8, max_iter: int = None) -> np.ndarray:
    """Jacobi-preconditioned conjugate gradient for KU=F on a torch device (GPU-capable).

    K is SPD (stiffness with fixed DOFs removed). Runs entirely on `device`; used
    in place of scipy's direct solve when a CUDA GPU is available.
    """
    n = K_csr.shape[0]
    if max_iter is None:
        max_iter = max(500, 2 * n)
    coo = K_csr.tocoo()
    idx = torch.tensor(np.vstack([coo.row, coo.col]), dtype=torch.long, device=device)
    val = torch.tensor(coo.data, dtype=torch.float64, device=device)
    A = torch.sparse_coo_tensor(idx, val, (n, n)).coalesce()
    b = torch.tensor(np.asarray(F), dtype=torch.float64, device=device)

    diag = torch.tensor(K_csr.diagonal().copy(), dtype=torch.float64, device=device)
    diag = torch.where(diag.abs() < 1e-30, torch.ones_like(diag), diag)
    m_inv = 1.0 / diag

    x = torch.zeros(n, dtype=torch.float64, device=device)
    r = b.clone()  # x0 = 0 -> r0 = b - A x0 = b
    z = m_inv * r
    p = z.clone()
    rz = torch.dot(r, z)
    b_norm = torch.linalg.norm(b) + 1e-30

    for _ in range(max_iter):
        ap = torch.sparse.mm(A, p.unsqueeze(1)).squeeze(1)
        p_ap = torch.dot(p, ap)
        if p_ap.abs() < 1e-30:
            break
        alpha = rz / p_ap
        x = x + alpha * p
        r = r - alpha * ap
        if torch.linalg.norm(r) / b_norm < tol:
            break
        z = m_inv * r
        rz_new = torch.dot(r, z)
        p = z + (rz_new / rz) * p
        rz = rz_new

    return x.detach().cpu().numpy()


def _oc_update(
    x: np.ndarray,
    dc: np.ndarray,
    volfrac: float,
    passive_void: np.ndarray,
    passive_solid: np.ndarray,
) -> np.ndarray:
    """Optimality criteria update with bisection on Lagrange multiplier."""
    nelem = x.size
    move = 0.2
    xnew = np.zeros(nelem)

    l1, l2 = 0.0, 1e9
    while (l2 - l1) / (l1 + 1e-10) > 1e-4:
        lmid = 0.5 * (l1 + l2)
        # OC update rule
        x_cand = x * np.sqrt(-dc / lmid)
        xnew = np.maximum(0.001, np.maximum(x - move, np.minimum(1.0, np.minimum(x + move, x_cand))))
        # Enforce passive regions
        xnew[passive_void] = 0.001
        xnew[passive_solid] = 1.0
        if xnew.mean() > volfrac:
            l1 = lmid
        else:
            l2 = lmid

    return xnew


# ---------------------------------------------------------------------------
# Boundary condition mapping from DesignSpec
# ---------------------------------------------------------------------------


def _grid_resolution(design_space, target_elements: int = 6000) -> Tuple[int, int, int]:
    """Pick a regular-grid resolution with roughly target_elements total cells."""
    sx, sy, sz = design_space.size.x, design_space.size.y, design_space.size.z
    aspect = np.array([sx, sy, sz])
    aspect = aspect / aspect.max()
    # cube root scaling
    scale = (target_elements / (aspect[0] * aspect[1] * aspect[2])) ** (1.0 / 3.0)
    nel = np.maximum(4, np.round(aspect * scale)).astype(int)
    return int(nel[0]), int(nel[1]), int(nel[2])


def _normalize_spec(spec: DesignSpec) -> DesignSpec:
    """Fill in missing loads / constraints with physically meaningful defaults."""
    import copy

    spec = copy.deepcopy(spec)
    size = spec.design_space.size
    cx, cy, cz = spec.design_space.center.x, spec.design_space.center.y, spec.design_space.center.z
    half_z = size.z / 2.0

    if not spec.load_cases:
        # Downward point load at the top, offset to induce bending.
        spec.load_cases.append(LoadCase(
            id="default-load",
            name="Default vertical load",
            point=Vector3(x=cx, y=cy + size.y * 0.2, z=cz + half_z),
            force=Vector3(x=0.0, y=0.0, z=-1000.0),
        ))

    if not spec.constraints:
        # Fix the entire bottom face so the part behaves like a grounded bracket.
        spec.constraints.append(Constraint(
            id="fixed-bottom",
            name="Fixed bottom face",
            type="fixed",
            point=Vector3(x=cx, y=cy, z=cz - half_z),
        ))

    return spec


def _world_to_grid(point, origin, size, nelx, nely, nelz):
    """Map a world point to the closest grid node index."""
    rel = np.array([point.x, point.y, point.z]) - np.array([origin.x, origin.y, origin.z])
    rel = rel + np.array([size.x, size.y, size.z]) / 2.0
    ratios = rel / np.array([size.x, size.y, size.z])
    ix = int(np.clip(round(ratios[0] * nelx), 0, nelx))
    iy = int(np.clip(round(ratios[1] * nely), 0, nely))
    iz = int(np.clip(round(ratios[2] * nelz), 0, nelz))
    return ix, iy, iz


def _node_index(ix, iy, iz, nx, ny):
    return iz * nx * ny + iy * nx + ix


def _sphere_element_mask(
    nelx: int, nely: int, nelz: int, center, radius: float, size
) -> np.ndarray:
    """Return boolean mask of elements inside a sphere."""
    nelem = nelx * nely * nelz
    mask = np.zeros(nelem, dtype=bool)
    cx, cy, cz = center.x, center.y, center.z
    # element centers in world coords
    ex = np.arange(nelx) + 0.5
    ey = np.arange(nely) + 0.5
    ez = np.arange(nelz) + 0.5
    sx, sy, sz = size.x, size.y, size.z
    # map normalized coords to world
    Wx = (ex / nelx - 0.5) * sx
    Wy = (ey / nely - 0.5) * sy
    Wz = (ez / nelz - 0.5) * sz
    e = 0
    for k in range(nelz):
        for j in range(nely):
            for i in range(nelx):
                dx, dy, dz = Wx[i] - cx, Wy[j] - cy, Wz[k] - cz
                if dx*dx + dy*dy + dz*dz <= radius*radius:
                    mask[e] = True
                e += 1
    return mask


def _box_element_mask(
    nelx: int, nely: int, nelz: int, center, bsize, size
) -> np.ndarray:
    """Return boolean mask of elements inside a box."""
    nelem = nelx * nely * nelz
    mask = np.zeros(nelem, dtype=bool)
    cx, cy, cz = center.x, center.y, center.z
    hx, hy, hz = bsize.x / 2.0, bsize.y / 2.0, bsize.z / 2.0
    ex = np.arange(nelx) + 0.5
    ey = np.arange(nely) + 0.5
    ez = np.arange(nelz) + 0.5
    sx, sy, sz = size.x, size.y, size.z
    Wx = (ex / nelx - 0.5) * sx
    Wy = (ey / nely - 0.5) * sy
    Wz = (ez / nelz - 0.5) * sz
    e = 0
    for k in range(nelz):
        for j in range(nely):
            for i in range(nelx):
                if (cx - hx <= Wx[i] <= cx + hx and
                    cy - hy <= Wy[j] <= cy + hy and
                    cz - hz <= Wz[k] <= cz + hz):
                    mask[e] = True
                e += 1
    return mask


# ---------------------------------------------------------------------------
# Main optimization routine
# ---------------------------------------------------------------------------


def run_simp(
    spec: DesignSpec,
    max_iter: int = 40,
    target_elements: int = 2200,
    progress: Optional[ProgressState] = None,
    design_mask: Optional[np.ndarray] = None,
    volfrac_override: Optional[float] = None,
    preserve_mask: Optional[np.ndarray] = None,
) -> SimpResult:
    """
    Run 3D SIMP topology optimization from a DesignSpec.

    Returns
    -------
    mesh : trimesh.Trimesh
        Optimized geometry extracted at density threshold 0.5.
    info : dict
        Metrics including compliance, volume fraction, mass estimate.
    """
    t0 = time.time()
    if progress is not None:
        progress.update(stage="meshing", message="Building design-space voxel mesh")

    # Remember whether the user supplied constraints/loads so defaults can be
    # applied appropriately after the grid resolution is known.
    user_constraints = list(spec.constraints)
    user_loads = list(spec.load_cases)

    # Provide sensible defaults if the spec is incomplete
    spec = _normalize_spec(spec)

    # Grid resolution
    nelx, nely, nelz = _grid_resolution(spec.design_space, target_elements)
    nelem = nelx * nely * nelz
    n_nodes_x, n_nodes_y, n_nodes_z = nelx + 1, nely + 1, nelz + 1
    n_nodes = n_nodes_x * n_nodes_y * n_nodes_z
    n_dof = 3 * n_nodes

    # Material properties (non-dimensional units, scaled later)
    E0 = 1.0
    Emin = 1e-9
    nu = 0.3
    penal = 3.0
    volfrac = 0.35  # default volume fraction
    if spec.objective.target == "minimize_mass":
        volfrac = 0.25
    elif spec.objective.target == "maximize_stiffness":
        volfrac = 0.45
    if volfrac_override is not None:
        volfrac = float(np.clip(volfrac_override, 1e-3, 1.0))

    rmin = max(1.5, min(nelx, nely, nelz) / 8.0)

    size = spec.design_space.size
    origin = spec.design_space.center

    # Element stiffness matrix and mapping (unit voxel element)
    Ke = _compute_ke(nu, hx=1.0, hy=1.0, hz=1.0)
    edofMat = _edof_mat(nelx, nely, nelz)
    # Row-major element-block indices for scipy COO assembly
    iK = np.kron(edofMat, np.ones((1, 24), dtype=int)).flatten()
    jK = np.kron(edofMat, np.ones((24, 1), dtype=int)).flatten()

    # Filter
    if progress is not None:
        progress.update(stage="meshing", message="Building density filter")
    H = _build_filter(nelx, nely, nelz, rmin)

    # Initialize densities
    x = np.full(nelem, volfrac)

    # Passive regions
    passive_void = np.zeros(nelem, dtype=bool)
    passive_solid = np.zeros(nelem, dtype=bool)

    for region in spec.preserve_regions:
        mask = _sphere_element_mask(nelx, nely, nelz, region.center, region.radius, size)
        passive_solid |= mask

    # Feature-derived keeps: an exact element mask rasterized from the flagged
    # features' own geometry, rather than a sphere the user had to guess. Unlike
    # `preserve_regions` these do not also become fixed supports — keeping a
    # mounting boss says nothing about how the part is grounded.
    if preserve_mask is not None:
        if preserve_mask.shape[0] != nelem:
            raise ValueError(
                f"preserve_mask has {preserve_mask.shape[0]} elements, expected {nelem}"
            )
        passive_solid |= preserve_mask.astype(bool)

    for region in spec.obstacle_regions:
        if region.type == "sphere":
            mask = _sphere_element_mask(nelx, nely, nelz, region.center, region.radius or 0.1, size)
        else:
            bsize = region.size if region.size else type("V", (), {"x": 0.1, "y": 0.1, "z": 0.1})()
            mask = _box_element_mask(nelx, nely, nelz, region.center, bsize, size)
        passive_void |= mask

    # Restrict the design domain to the seeded body: elements outside it can
    # hold no material (used by run_simp_from_solid to optimize an existing part).
    if design_mask is not None:
        if design_mask.shape[0] != nelem:
            raise ValueError(
                f"design_mask has {design_mask.shape[0]} elements, expected {nelem}"
            )
        passive_void |= ~design_mask.astype(bool)
        passive_solid &= design_mask.astype(bool)

    x[passive_void] = 0.001
    x[passive_solid] = 1.0

    # Fixed DOFs from constraints
    fixed_dofs = set()
    if not user_constraints:
        # If the user did not specify constraints, ground the entire bottom face.
        for iy in range(n_nodes_y):
            for ix in range(n_nodes_x):
                nid = _node_index(ix, iy, 0, n_nodes_x, n_nodes_y)
                fixed_dofs.update([3*nid, 3*nid+1, 3*nid+2])
    else:
        for constraint in spec.constraints:
            ix, iy, iz = _world_to_grid(constraint.point, origin, size, nelx, nely, nelz)
            nid = _node_index(ix, iy, iz, n_nodes_x, n_nodes_y)
            fixed_dofs.update([3*nid, 3*nid+1, 3*nid+2])

    # Also fix any preserve region centers as solid supports
    for region in spec.preserve_regions:
        ix, iy, iz = _world_to_grid(region.center, origin, size, nelx, nely, nelz)
        nid = _node_index(ix, iy, iz, n_nodes_x, n_nodes_y)
        fixed_dofs.update([3*nid, 3*nid+1, 3*nid+2])

    fixed = np.array(sorted(fixed_dofs), dtype=int)
    free = np.setdiff1d(np.arange(n_dof), fixed)

    # Load vector
    F = np.zeros(n_dof)
    for load in spec.load_cases:
        ix, iy, iz = _world_to_grid(load.point, origin, size, nelx, nely, nelz)
        nid = _node_index(ix, iy, iz, n_nodes_x, n_nodes_y)
        F[3*nid] += load.force.x
        F[3*nid + 1] += load.force.y
        F[3*nid + 2] += load.force.z

    # SIMP loop
    x_phys = x.copy()
    loop = 0
    change = 1.0
    compliance_history = []

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if progress is not None:
        progress.update(
            stage="optimizing",
            max_iter=max_iter,
            message=f"Running SIMP iterations on {device}",
        )

    while loop < max_iter and change > 0.01:
        loop += 1

        # Tensor density operations (run on GPU if available)
        x_t = torch.from_numpy(x_phys).to(device)
        e_t = Emin + torch.pow(x_t, penal).cpu().numpy() * (E0 - Emin)

        # Build global stiffness matrix: E_e * Ke for each element
        sK = (Ke.flatten()[None, :] * e_t[:, None]).flatten()
        K = coo_matrix((sK, (iK, jK)), shape=(n_dof, n_dof)).tocsc()

        # Apply boundary conditions by removing fixed DOFs
        K_free = K[free][:, free]
        F_free = F[free]

        # Solve KU=F. Use the GPU conjugate-gradient solver when a CUDA device is
        # available; otherwise scipy's direct sparse solve on the host.
        if device.type == "cuda":
            U_free = _pcg_solve(K_free.tocsr(), F_free, device)
        else:
            U_free = spsolve(K_free, F_free)
        U = np.zeros(n_dof)
        U[free] = U_free

        # Compliance and sensitivities
        Ue = U[edofMat]
        ce = np.sum((Ue @ Ke) * Ue, axis=1)
        c = float(np.sum(e_t * ce))
        dc = -penal * (E0 - Emin) * torch.pow(x_t, penal - 1).cpu().numpy() * ce

        # Filter sensitivities
        dc = H @ dc

        # OC update
        x_new = _oc_update(x, dc, volfrac, passive_void, passive_solid)

        change = np.max(np.abs(x_new - x))
        x = x_new
        x_phys = x.copy()
        compliance_history.append(c)

        if progress is not None and loop % 2 == 0:
            progress.update(
                iteration=loop,
                compliance=c,
                change=change,
                message=f"SIMP iteration {loop}/{max_iter}, compliance {c:.4f}",
            )

    if progress is not None:
        progress.update(stage="postprocessing", message="Extracting isosurface mesh")

    # Convert density field to mesh
    mesh = _density_to_mesh(x_phys, nelx, nely, nelz, size, origin)

    # Estimate mass using real material density
    from app.services import materials as materials_catalog
    mat = materials_catalog.get_material(spec.material_id)
    solid_volume_frac = float((x_phys > 0.5).mean())
    total_domain_volume = size.x * size.y * size.z
    # scale to mm^3 then g (density in kg/m^3 -> g/mm^3)
    mass_g = solid_volume_frac * total_domain_volume * 1e6 * (mat.density * 1e-6)

    info = {
        "nelx": nelx,
        "nely": nely,
        "nelz": nelz,
        "size": size,
        "origin": origin,
        "iterations": loop,
        "final_compliance": compliance_history[-1] if compliance_history else None,
        "volume_fraction": float(x_phys.mean()),
        "solid_volume_fraction": solid_volume_frac,
        "mass_g": mass_g,
        "time_seconds": time.time() - t0,
    }
    return SimpResult(
        mesh=mesh,
        info=info,
        spec=spec,
        rho=x_phys,
        U=U,
        edofMat=edofMat,
        Ke=Ke,
        nelx=nelx,
        nely=nely,
        nelz=nelz,
        size=size,
        origin=origin,
        has_user_constraints=len(user_constraints) > 0,
        has_user_loads=len(user_loads) > 0,
    )


# ---------------------------------------------------------------------------
# Seeding SIMP from an existing body (Phase 3 generative unification)
# ---------------------------------------------------------------------------


def _shift_region(region, dx: float, dy: float, dz: float):
    """Return a copy of a region/load/constraint translated by (dx,dy,dz)."""
    data = region.model_dump()
    for key in ("center", "point"):
        if key in data and data[key] is not None:
            data[key]["x"] += dx
            data[key]["y"] += dy
            data[key]["z"] += dz
    return type(region)(**data)


def run_simp_from_solid(
    body_feature,
    setup,
    max_iter: int = 40,
    target_elements: int = 2600,
    progress: Optional[ProgressState] = None,
    keep_features: Optional[list] = None,
    audit_features: Optional[list] = None,
) -> dict:
    """Optimize an existing part: voxelize its solid as the SIMP design domain.

    The body's interior becomes the design region (material may be removed to hit
    the volume target); keep-in regions are held solid, keep-out subtracted.

    ``keep_features`` are evaluated features whose material is pinned solid — the
    optimizer routes load around them instead of deleting them. ``audit_features``
    are checked *after* the solve and reported in ``info["removed_features"]``, so
    a part that lost something it needed says so instead of silently coming back
    wrong.

    Returns a dict with the optimized ``mesh`` (in the part's world frame) plus a
    ``density`` grid + framing so an OptimizeFeature can rebuild it as a GridSDF.
    """
    from app.cad_kernel.evaluator.sdf import sdf_from_feature

    body_sdf = sdf_from_feature(body_feature)
    b = body_sdf.bounds().expanded(0.06)
    lo, hi = b.min, b.max
    center = (lo + hi) / 2.0
    span = np.maximum(hi - lo, 1e-4)
    cx, cy, cz = float(center[0]), float(center[1]), float(center[2])

    # Domain centred at origin (mask helpers assume origin=0); we translate the
    # body and all setup regions into that frame, then shift results back.
    design_space = {
        "size": {"x": float(span[0]), "y": float(span[1]), "z": float(span[2])},
        "center": {"x": 0.0, "y": 0.0, "z": 0.0},
    }

    nelx, nely, nelz = _grid_resolution(
        type("DS", (), {"size": type("V", (), {"x": span[0], "y": span[1], "z": span[2]})()})(),
        target_elements,
    )
    nelem = nelx * nely * nelz

    # Element-centre occupancy of the body (z-major order to match mask helpers).
    ex = ((np.arange(nelx) + 0.5) / nelx - 0.5) * span[0]
    ey = ((np.arange(nely) + 0.5) / nely - 0.5) * span[1]
    ez = ((np.arange(nelz) + 0.5) / nelz - 0.5) * span[2]
    gk, gj, gi = np.meshgrid(np.arange(nelz), np.arange(nely), np.arange(nelx), indexing="ij")
    pts = np.stack([ex[gi.ravel()] + cx, ey[gj.ravel()] + cy, ez[gk.ravel()] + cz], axis=1)
    occupancy = body_sdf(pts) <= 0.0

    design_count = int(occupancy.sum())
    if design_count == 0:
        raise ValueError("Body did not intersect the optimization grid")

    # Rasterize each flagged feature onto the same element centres. Doing it here
    # (rather than approximating with a sphere) means a keep covers exactly the
    # geometry the user pointed at.
    def _feature_mask(feature) -> np.ndarray:
        return sdf_from_feature(feature)(pts) <= 0.0

    preserve_mask = None
    if keep_features:
        preserve_mask = np.zeros(pts.shape[0], dtype=bool)
        for feat in keep_features:
            preserve_mask |= _feature_mask(feat)
        # A keep outside the body can't be honoured — it isn't in the design domain.
        preserve_mask &= occupancy

    volume_fraction = float(getattr(setup, "volume_fraction", 0.4))
    volfrac_override = volume_fraction * design_count / nelem

    keep_in = [_shift_region(r, -cx, -cy, -cz) for r in getattr(setup, "keep_in", [])]
    keep_out = [_shift_region(r, -cx, -cy, -cz) for r in getattr(setup, "keep_out", [])]
    loads = [_shift_region(l, -cx, -cy, -cz) for l in getattr(setup, "load_cases", [])]
    user_constraints = [_shift_region(c, -cx, -cy, -cz) for c in getattr(setup, "constraints", [])]
    objective = setup.objective if getattr(setup, "objective", None) else {"target": "minimize_mass", "weight": 1.0}

    # A single fixed point (or none) cannot ground a 3D body — it leaves rigid-body
    # modes that make the FEA singular. Fall back to auto bottom-face grounding
    # (run_simp's default when constraints are empty) unless the caller supplied
    # a well-posed set, and retry fully-auto if the solve still degenerates.
    def _solve(constraints):
        spec = DesignSpec(
            name="optimize",
            design_space=design_space,
            preserve_regions=keep_in,
            obstacle_regions=keep_out,
            load_cases=loads,
            constraints=constraints,
            material_id=getattr(setup, "material_id", "al-7075"),
            objective=objective,
        )
        return run_simp(
            spec,
            max_iter=max_iter,
            target_elements=target_elements,
            progress=progress,
            design_mask=occupancy,
            volfrac_override=volfrac_override,
            preserve_mask=preserve_mask,
        )

    constraints = user_constraints if len(user_constraints) >= 3 else []
    simp = _solve(constraints)
    if not np.all(np.isfinite(simp.rho)) and constraints:
        simp = _solve([])  # retry with auto grounding
    if not np.all(np.isfinite(simp.rho)):
        raise ValueError("Optimization did not converge (singular system)")

    # Shift optimized mesh back into the part's world frame.
    mesh = simp.mesh
    mesh.vertices = mesh.vertices + np.array([cx, cy, cz])

    density = simp.rho.reshape(nelz, nely, nelx).transpose(2, 1, 0)  # -> [i, j, k]

    info = dict(simp.info)
    info["removed_features"] = _audit_removed_features(
        audit_features or [], _feature_mask, simp.rho, threshold=0.5
    )
    info["kept_feature_count"] = len(keep_features or [])

    return {
        "mesh": mesh,
        "density": density,
        "origin": {"x": cx, "y": cy, "z": cz},
        "size": {"x": float(span[0]), "y": float(span[1]), "z": float(span[2])},
        "threshold": 0.5,
        "info": info,
    }


# How much of a feature has to survive before we stop calling it removed. Below
# this the geometry is gone in any practical sense — a boss eaten to a third of
# its volume no longer accepts a fastener.
_FEATURE_INTACT_FRACTION = 0.5


def _audit_removed_features(features, mask_fn, rho: np.ndarray, threshold: float = 0.5) -> list:
    """Report which features the solve ate through.

    For each feature, the fraction of its own elements that still hold material.
    Optimization legitimately removes unloaded material, so this is advisory —
    but it is the difference between a part that quietly comes back missing its
    mounting bosses and one that says so.
    """
    report = []
    for entry in features:
        feature = entry["feature"] if isinstance(entry, dict) else entry
        mask = mask_fn(feature)
        total = int(mask.sum())
        if total == 0:
            continue  # outside the grid; nothing meaningful to say
        surviving = int((rho[mask] > threshold).sum())
        fraction = surviving / total
        if fraction >= _FEATURE_INTACT_FRACTION:
            continue
        meta = entry if isinstance(entry, dict) else {}
        report.append({
            "feature_id": meta.get("id") or getattr(feature, "id", None),
            "name": meta.get("name") or getattr(feature, "name", "feature"),
            "type": meta.get("type") or type(feature).__name__,
            "remaining_fraction": round(fraction, 3),
            "kept": bool(meta.get("kept")),
        })
    return report


def _density_to_mesh(
    rho: np.ndarray,
    nelx: int, nely: int, nelz: int,
    size, origin,
    threshold: float = 0.5,
) -> trimesh.Trimesh:
    """Extract an isosurface mesh from the element density field."""
    # Reshape to 3D scalar field
    field = rho.reshape((nelz, nely, nelx)).astype(float)
    # Pad with zeros so the surface is closed at the boundaries
    padded = np.pad(field, 1, mode="constant", constant_values=0.0)

    try:
        from skimage.measure import marching_cubes
        verts, faces, _, _ = marching_cubes(padded, level=threshold)
    except Exception:
        # Fallback to trimesh marching cubes
        mesh = trimesh.voxel.ops.matrix_to_marching_cubes(padded, pitch=1.0)
        verts = mesh.vertices
        faces = mesh.faces

    # Scale vertices to world coordinates
    sx, sy, sz = size.x, size.y, size.z
    # skimage marching_cubes returns vertices in (z, y, x) index order because
    # the input field is indexed field[z, y, x]. Map back to (x, y, z).
    world_verts = np.zeros_like(verts)
    world_verts[:, 0] = (verts[:, 2] / (nelx + 1) - 0.5) * sx
    world_verts[:, 1] = (verts[:, 1] / (nely + 1) - 0.5) * sy
    world_verts[:, 2] = (verts[:, 0] / (nelz + 1) - 0.5) * sz
    world_verts += np.array([origin.x, origin.y, origin.z])

    mesh = trimesh.Trimesh(vertices=world_verts, faces=faces)
    try:
        trimesh.smoothing.filter_mut_dif_laplacian(mesh, iterations=2)
    except Exception:
        pass
    return mesh
