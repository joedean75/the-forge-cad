import math
import numpy as np
from fastapi import APIRouter, HTTPException
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import spsolve

from app.models.schemas import SimulationResult, Material
from app.services.mesh_generator import CANDIDATE_DATA
from app.services.simp_optimizer import _hex_shape_deriv, _world_to_grid, _node_index
from app.cad_kernel.kernel import Kernel
from app.cad_kernel.geom.meshing import mesh_volume, solid_to_mesh
from app.routers.materials import _MATERIALS

router = APIRouter(prefix="/api/simulate", tags=["simulate"])


def _elastic_matrix(nu: float, E: float) -> np.ndarray:
    """3D isotropic elasticity D matrix for given E (Pa) and nu."""
    k = E * (1.0 - nu) / ((1.0 + nu) * (1.0 - 2.0 * nu))
    c11 = k
    c12 = k * nu / (1.0 - nu)
    c44 = E * (1.0 - 2.0 * nu) / (2.0 * (1.0 - nu))
    return np.array([
        [c11, c12, c12, 0,   0,   0],
        [c12, c11, c12, 0,   0,   0],
        [c12, c12, c11, 0,   0,   0],
        [0,   0,   0,   c44, 0,   0],
        [0,   0,   0,   0,   c44, 0],
        [0,   0,   0,   0,   0,   c44],
    ])


def _b_matrix_centroid() -> np.ndarray:
    """Strain-displacement matrix B (6 x 24) at the centroid of a unit hexa."""
    nodes = np.array([
        [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
        [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
    ], dtype=float)
    dN = _hex_shape_deriv(0.0, 0.0, 0.0)
    J = dN.T @ nodes
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
    return B


def _von_mises_3d(stress: np.ndarray) -> float:
    sx, sy, sz, sxy, syz, sxz = stress
    return math.sqrt(
        0.5 * (
            (sx - sy)**2 + (sy - sz)**2 + (sz - sx)**2
            + 6.0 * (sxy**2 + syz**2 + sxz**2)
        )
    )


def _approximate_simulation(solid, spec: dict, mat: Material) -> SimulationResult:
    """Rough simulation estimate for kernel-generated fallback geometry."""
    mesh = solid_to_mesh(solid)
    volume = abs(mesh_volume(mesh.vertices, mesh.faces))
    mass = volume * mat.density

    # Characteristic length from bounding box
    verts = mesh.vertices
    size = verts.max(axis=0) - verts.min(axis=0)
    char_len = float(np.mean(size)) + 1e-6
    area_scale = (volume ** (2.0 / 3.0)) + 1e-6

    E_pa = mat.youngs_modulus * 1e9

    total_force = 0.0
    for load in spec.load_cases:
        f = np.array([load.force.x, load.force.y, load.force.z])
        total_force += float(np.linalg.norm(f))

    # Very coarse estimates
    max_stress_mpa = (total_force / area_scale) / 1e6
    max_stress_mpa = max(0.01, min(max_stress_mpa, 500.0))
    max_displacement_mm = (total_force * char_len) / (E_pa * area_scale) * 1000.0
    compliance_j = total_force * (max_displacement_mm / 1000.0)
    safety_factor = mat.yield_strength / max(max_stress_mpa, 1e-6)
    safety_factor = min(safety_factor, 999.0)

    return SimulationResult(
        max_stress=round(max_stress_mpa, 2),
        max_displacement=round(max_displacement_mm, 4),
        safety_factor=round(safety_factor, 2),
        compliance=round(compliance_j, 6),
        mass=round(mass, 4),
    )


def _compute_simulation_result(candidate_id: str) -> SimulationResult:
    data = CANDIDATE_DATA.get(candidate_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Candidate not found")

    simp = data.get("simp")
    spec = data.get("spec")
    mat = data.get("material")

    if simp is None:
        # Kernel fallback path
        solid = data.get("kernel_solid")
        if solid is None and data.get("candidate_mesh") is not None:
            mesh = data["candidate_mesh"]
            solid = Kernel.from_mesh(mesh.vertices, mesh.faces)
        if solid is None or spec is None:
            raise HTTPException(status_code=404, detail="Candidate geometry not available")
        if mat is None:
            mat = next((m for m in _MATERIALS if m.id == spec.material_id), _MATERIALS[0])
        return _approximate_simulation(solid, spec, mat)

    mesh = data["candidate_mesh"]
    has_user_constraints = data.get("has_user_constraints", False)

    E_pa = mat.youngs_modulus * 1e9
    nu = mat.poissons_ratio
    rho = simp.rho
    penal = 3.0

    nelx, nely, nelz = simp.nelx, simp.nely, simp.nelz
    n_nodes_x, n_nodes_y, n_nodes_z = nelx + 1, nely + 1, nelz + 1
    n_nodes = n_nodes_x * n_nodes_y * n_nodes_z
    n_dof = 3 * n_nodes
    nelem = nelx * nely * nelz

    size = simp.size
    origin = simp.origin
    L = size.x / nelx  # voxel size in meters

    # Dimensional element stiffness: Ke_unit was for E=1, L=1.
    Ke_dim = simp.Ke * E_pa * L

    edofMat = simp.edofMat
    iK = np.kron(edofMat, np.ones((1, 24), dtype=int)).flatten()
    jK = np.kron(edofMat, np.ones((24, 1), dtype=int)).flatten()

    # Global stiffness with real material and the optimized density field.
    # Ke_dim already carries E and L; the density factor is dimensionless.
    e_factors = 1e-9 + rho ** penal * (1.0 - 1e-9)
    sK = (Ke_dim.flatten()[None, :] * e_factors[:, None]).flatten()
    K = coo_matrix((sK, (iK, jK)), shape=(n_dof, n_dof)).tocsc()

    # Fixed DOFs
    fixed_dofs = set()
    if not has_user_constraints:
        for iy in range(n_nodes_y):
            for ix in range(n_nodes_x):
                nid = _node_index(ix, iy, 0, n_nodes_x, n_nodes_y)
                fixed_dofs.update([3*nid, 3*nid+1, 3*nid+2])
    else:
        for constraint in spec.constraints:
            ix, iy, iz = _world_to_grid(constraint.point, origin, size, nelx, nely, nelz)
            nid = _node_index(ix, iy, iz, n_nodes_x, n_nodes_y)
            fixed_dofs.update([3*nid, 3*nid+1, 3*nid+2])

    for region in spec.preserve_regions:
        ix, iy, iz = _world_to_grid(region.center, origin, size, nelx, nely, nelz)
        nid = _node_index(ix, iy, iz, n_nodes_x, n_nodes_y)
        fixed_dofs.update([3*nid, 3*nid+1, 3*nid+2])

    fixed = np.array(sorted(fixed_dofs), dtype=int)
    free = np.setdiff1d(np.arange(n_dof), fixed)

    # Load vector (uses normalized/default loads from spec)
    F = np.zeros(n_dof)
    for load in spec.load_cases:
        ix, iy, iz = _world_to_grid(load.point, origin, size, nelx, nely, nelz)
        nid = _node_index(ix, iy, iz, n_nodes_x, n_nodes_y)
        F[3*nid] += load.force.x
        F[3*nid + 1] += load.force.y
        F[3*nid + 2] += load.force.z

    # Solve
    K_free = K[free][:, free]
    F_free = F[free]
    U_free = spsolve(K_free, F_free)
    U = np.zeros(n_dof)
    U[free] = U_free

    # Stresses at element centroids
    D = _elastic_matrix(nu, E_pa)
    B_unit = _b_matrix_centroid()
    B = B_unit / L

    Ue = U[edofMat]
    strains = (B @ Ue.T).T
    stresses = (D @ strains.T).T
    vm = np.array([_von_mises_3d(s) for s in stresses])
    max_stress_pa = float(vm.max()) if vm.size else 0.0

    # Max nodal displacement
    ux = U[0::3]
    uy = U[1::3]
    uz = U[2::3]
    nodal_disp = np.sqrt(ux**2 + uy**2 + uz**2)
    max_displacement_m = float(nodal_disp.max())

    # Compliance
    compliance_j = float(F @ U)

    max_stress_mpa = max_stress_pa / 1e6
    safety_factor = mat.yield_strength / max(max_stress_mpa, 1e-6)
    safety_factor = min(safety_factor, 999.0)

    mass = mesh.volume * mat.density

    return SimulationResult(
        max_stress=round(max_stress_mpa, 2),
        max_displacement=round(max_displacement_m * 1000, 4),
        safety_factor=round(safety_factor, 2),
        compliance=round(compliance_j, 6),
        mass=round(mass, 4),
    )


@router.post("", response_model=SimulationResult)
def simulate(candidate_id: str = ""):
    return _compute_simulation_result(candidate_id)
