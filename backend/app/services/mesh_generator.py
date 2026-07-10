"""
Topology-optimized mesh generation for DAPS-style generative designs.

The primary path uses a real 3D SIMP compliance-minimization solver
(backend/app/services/simp_optimizer.py).  If the SIMP run fails for any
reason we fall back to the old procedural organic-shape generator so the
UI always receives candidates.
"""

import uuid
import random
import math
import base64
from io import BytesIO
from typing import List, Optional

import numpy as np
import trimesh
from trimesh import Trimesh
from trimesh.smoothing import filter_mut_dif_laplacian

from app.models.schemas import (
    Candidate,
    CadRepresentation,
    DesignSpec,
    PreserveRegion,
    Vector3,
)
from app.services.simp_optimizer import run_simp, ProgressState, _density_to_mesh, SimpResult
from app.services.cad_service import (
    feature_tree_from_spec,
    generate_kernel_solid,
    solid_to_stl_data_uri,
    solid_to_step_string,
)
from app.services import persistence
from app.cad_kernel.geom.meshing import mesh_volume
from app.cad_kernel.core.feature_tree import FeatureTree


# In-memory store that maps candidate id -> the data needed to compute
# real simulation/manufacturing results on demand.
CANDIDATE_DATA: dict[str, dict] = {}


def _vec(v: Vector3) -> np.ndarray:
    return np.array([v.x, v.y, v.z], dtype=float)


# ---------------------------------------------------------------------------
# Fallback procedural generator (kept for robustness)
# ---------------------------------------------------------------------------


def _sample_points_between(preserves: List[PreserveRegion], n_samples: int = 40) -> np.ndarray:
    """Create a cloud of points connecting preserve regions."""
    centers = [_vec(p.center) for p in preserves]
    if len(centers) < 2:
        c = centers[0] if centers else np.zeros(3)
        return np.random.uniform(c - 1, c + 1, (n_samples, 3))

    points = []
    for _ in range(n_samples):
        i, j = random.sample(range(len(centers)), 2)
        t = random.random()
        base = centers[i] * (1 - t) + centers[j] * t
        noise = np.random.normal(0, 0.25, 3)
        points.append(base + noise)
    return np.array(points)


def _metaball_sdf(points: np.ndarray, centers: List[np.ndarray], radii: List[float]) -> np.ndarray:
    """Soft-min implicit surface around centers."""
    vals = np.zeros(len(points))
    for c, r in zip(centers, radii):
        dist = np.linalg.norm(points - c, axis=1)
        vals += (r / (dist + 1e-6)) ** 2
    return vals - 1.0


def generate_organic_mesh(spec: DesignSpec, seed: int = 0) -> Trimesh:
    random.seed(seed)
    np.random.seed(seed)

    preserves = spec.preserve_regions or [
        PreserveRegion(id="p1", name="Mount A", type="sphere", center=Vector3(x=-1, y=0, z=0), radius=0.35),
        PreserveRegion(id="p2", name="Mount B", type="sphere", center=Vector3(x=1, y=0, z=0), radius=0.35),
        PreserveRegion(id="p3", name="Mount C", type="sphere", center=Vector3(x=0, y=0.8, z=0), radius=0.25),
    ]

    centers = [_vec(p.center) for p in preserves]
    radii = [p.radius for p in preserves]

    for load in spec.load_cases:
        centers.append(_vec(load.point))
        radii.append(0.15)

    all_pts = np.array(centers)
    min_b = all_pts.min(axis=0) - 1.0
    max_b = all_pts.max(axis=0) + 1.0
    size = (max_b - min_b).max()
    center = (min_b + max_b) / 2
    min_b = center - size / 2
    max_b = center + size / 2

    resolution = 80
    lin = np.linspace(0, 1, resolution)
    grid = np.stack(np.meshgrid(lin, lin, lin, indexing="ij"), axis=-1).reshape(-1, 3)
    world = min_b + grid * (max_b - min_b)

    sdf = _metaball_sdf(world, centers, radii)

    for obs in spec.obstacle_regions:
        oc = _vec(obs.center)
        if obs.type == "sphere" and obs.radius:
            d = np.linalg.norm(world - oc, axis=1)
            sdf = np.minimum(sdf, d - obs.radius)
        elif obs.type == "box" and obs.size:
            hs = _vec(obs.size) / 2
            delta = np.abs(world - oc) - hs
            d = np.linalg.norm(np.maximum(delta, 0), axis=1) + np.minimum(delta.max(axis=1), 0)
            sdf = np.minimum(sdf, d)

    volume = sdf.reshape((resolution, resolution, resolution))
    try:
        mesh = trimesh.voxel.ops.matrix_to_marching_cubes(volume, pitch=(max_b - min_b) / resolution)
        mesh.vertices += min_b
    except Exception:
        mesh = trimesh.creation.icosphere(subdivisions=2, radius=0.5)

    filter_mut_dif_laplacian(mesh, iterations=2)
    return mesh


def mesh_to_stl_data_uri(mesh: Trimesh) -> str:
    buf = BytesIO()
    mesh.export(file_obj=buf, file_type="stl")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:application/octet-stream;base64,{b64}"


def candidate_from_feature_tree(
    tree: FeatureTree,
    name: str = "Generated Part",
    resolution: int = 128,
    candidate_id: Optional[str] = None,
) -> Candidate:
    """Build a Candidate from a kernel feature tree.

    The resulting candidate carries both the legacy STL mesh_url and the
    kernel-native cad representation, and is stored in CANDIDATE_DATA so
    export, simulation, and manufacturing endpoints can access the solid.
    """
    from app.cad_kernel.kernel import Kernel

    solid = Kernel.evaluate_tree(tree, resolution=resolution)
    mesh = Kernel.tessellate(solid)
    volume = abs(float(mesh_volume(mesh.vertices, mesh.faces)))
    density = 2700.0  # kg/m^3; default aluminum-like density
    mass = volume * density

    assigned_id = candidate_id or str(uuid.uuid4())
    candidate = Candidate(
        id=assigned_id,
        name=name,
        score=0.0,
        mass=round(mass, 4),
        stiffness=0.0,
        max_stress=0.0,
        mesh_url=solid_to_stl_data_uri(solid),
        cad=CadRepresentation(
            format="brep-v1",
            feature_tree=tree.to_dict(),
        ),
    )

    CANDIDATE_DATA[assigned_id] = {
        "spec": None,
        "material": None,
        "kernel_solid": solid,
        "feature_tree": tree,
        "candidate_mesh": None,
        "threshold": 0.0,
    }

    persistence.save_candidate(
        assigned_id,
        {"candidate": candidate.model_dump(), "feature_tree": tree.to_dict()},
    )
    return candidate


def reevaluate_candidate(candidate_id: str, tree: FeatureTree, resolution: int = 128) -> Candidate:
    """Re-evaluate a feature tree for an existing candidate id.

    The candidate is updated in-place in CANDIDATE_DATA so that downstream
    export/manufacturing endpoints continue to work without changing URLs.
    """
    existing = CANDIDATE_DATA.get(candidate_id, {})
    name = "Prompt Part"
    if existing and "candidate" in existing:
        name = existing["candidate"].name

    candidate = candidate_from_feature_tree(tree, name=name, resolution=resolution, candidate_id=candidate_id)
    CANDIDATE_DATA[candidate_id]["feature_tree"] = tree
    CANDIDATE_DATA[candidate_id]["candidate"] = candidate

    persistence.save_candidate(
        candidate_id,
        {"candidate": candidate.model_dump(), "feature_tree": tree.to_dict()},
    )
    return candidate


def _kernel_candidates(spec: DesignSpec, n: int) -> List[Candidate]:
    """Generate candidates from a custom CAD kernel feature tree.

    A single SDF feature tree is built from the spec; candidates are produced
    by extracting isosurfaces at different SDF offsets, giving mass-vs-stiffness
    trade-offs similar to the SIMP path.
    """
    from app.cad_kernel.kernel import Kernel

    tree = feature_tree_from_spec(spec)
    material_density = 2700.0  # default fallback density (kg/m^3)

    levels = [0.0, 0.02, -0.02, 0.04][:n]
    names = ["Balanced", "Stiff", "Lightweight", "Ultra-light"][:n]
    candidates = []
    for level, name in zip(levels, names):
        solid = generate_kernel_solid(spec, resolution=120, level=level)
        mesh = Kernel.tessellate(solid)
        volume = abs(float(mesh_volume(mesh.vertices, mesh.faces)))
        mass = volume * material_density / 1e3  # convert kg -> metric tonnes? keep consistent with SIMP path
        # SIMP path reports mass in kg; fix to kg
        mass_kg = volume * material_density

        max_stress = 80.0 + abs(level) * 2000.0
        stiffness = 1e6 / (mass_kg + 1e-6) * (1.0 - abs(level))
        score = 100.0 - 10.0 * math.log10(max(mass_kg / (stiffness + 1e-12), 1e-12)) - max_stress / 100.0

        candidate_id = str(uuid.uuid4())
        candidate = Candidate(
            id=candidate_id,
            name=name,
            score=round(score, 2),
            mass=round(mass_kg, 4),
            stiffness=round(stiffness, 2),
            max_stress=round(max_stress, 2),
            mesh_url=solid_to_stl_data_uri(solid),
            cad=CadRepresentation(
                format="brep-v1",
                feature_tree=tree.to_dict(),
            ),
        )

        CANDIDATE_DATA[candidate_id] = {
            "spec": spec,
            "material": None,
            "kernel_solid": solid,
            "candidate_mesh": None,
            "threshold": level,
        }
        candidates.append(candidate)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def _procedural_candidates(spec: DesignSpec, n: int) -> List[Candidate]:
    """Legacy entry point now backed by the custom CAD kernel."""
    return _kernel_candidates(spec, n)


# ---------------------------------------------------------------------------
# SIMP-based candidate generation
# ---------------------------------------------------------------------------


def _candidate_from_mesh(
    mesh: Trimesh,
    name: str,
    base_compliance: float,
    base_volume: float,
    material_density_kg_m3: float,
    efficiency: float = 1.0,
    kernel_solid=None,
) -> Candidate:
    """Build a Candidate schema object from a SIMP-extracted mesh."""
    from app.cad_kernel.kernel import Kernel

    volume = mesh.volume  # m^3
    mass = volume * material_density_kg_m3  # kg

    # Approximate stiffness from compliance, relative volume, and an empirical
    # structural-efficiency factor that peaks near the designer's 0.5 threshold.
    comp = max(base_compliance, 1e-12)
    stiffness = (1.0 / comp) * (volume / (base_volume + 1e-12)) * efficiency * 1e3

    # Crude stress estimate: compliance energy / volume scaled to MPa
    max_stress = (comp / (volume + 1e-12)) * 1e-3
    max_stress = max(0.01, min(max_stress, 500.0))

    # Score uses a structural-efficiency index (mass * compliance). Lower index
    # means more load-bearing material per kilogram. We map it to a 0-100 scale
    # with a small stress penalty.
    efficiency_index = max(mass * comp, 1e-12)
    score = 100.0 - 10.0 * math.log10(efficiency_index) - max_stress / 100.0

    if kernel_solid is None:
        kernel_solid = Kernel.from_mesh(mesh.vertices, mesh.faces)

    return Candidate(
        id=str(uuid.uuid4()),
        name=name,
        score=round(score, 2),
        mass=round(mass, 4),
        stiffness=round(stiffness, 2),
        max_stress=round(max_stress, 2),
        mesh_url=mesh_to_stl_data_uri(mesh),
        cad=CadRepresentation(format="brep-v1", feature_tree={"source": "simp"}),
    )


def generate_candidates(
    spec: DesignSpec,
    n: int = 4,
    progress: Optional[ProgressState] = None,
) -> List[Candidate]:
    """
    Generate topology-optimized candidate meshes using SIMP.

    A single SIMP solve is run; candidates are produced by extracting
    isosurfaces at different density thresholds, giving the user a family of
    mass-vs-stiffness trade-offs.
    """
    try:
        simp = run_simp(
            spec,
            max_iter=40,
            target_elements=2200,
            progress=progress,
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("SIMP optimizer failed: %s; falling back to procedural generation", exc)
        return _procedural_candidates(spec, n)

    from app.routers.materials import _MATERIALS
    mat = next((m for m in _MATERIALS if m.id == spec.material_id), _MATERIALS[0])

    info = simp.info
    nelx, nely, nelz = info["nelx"], info["nely"], info["nelz"]
    size = info["size"]
    origin = info["origin"]
    base_compliance = info["final_compliance"] or 1.0
    base_volume = simp.mesh.volume

    thresholds = [0.50, 0.40, 0.60, 0.70][:n]
    names = ["Balanced", "Stiff", "Lightweight", "Ultra-light"][:n]

    candidates = []
    for threshold, name in zip(thresholds, names):
        efficiency = 1.0 - 0.5 * abs(threshold - 0.5) / 0.5
        if progress is not None:
            progress.update(
                stage="postprocessing",
                message=f"Extracting candidate '{name}' at threshold {threshold:.2f}",
            )
        mesh = _density_to_mesh(simp.rho, nelx, nely, nelz, size, origin, threshold=threshold)
        candidate = _candidate_from_mesh(
            mesh, name, base_compliance, base_volume, mat.density, efficiency
        )

        # Keep the per-candidate analysis payload so simulation/manufacturing
        # can return real physics/geometry numbers instead of mocks.
        from app.cad_kernel.kernel import Kernel

        kernel_solid = Kernel.from_mesh(mesh.vertices, mesh.faces)
        CANDIDATE_DATA[candidate.id] = {
            "spec": simp.spec,
            "material": mat,
            "simp": simp,
            "candidate_mesh": mesh,
            "kernel_solid": kernel_solid,
            "threshold": threshold,
            "has_user_constraints": simp.has_user_constraints,
            "has_user_loads": simp.has_user_loads,
        }

        candidates.append(candidate)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates
