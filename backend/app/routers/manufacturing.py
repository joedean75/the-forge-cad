import math
import numpy as np
from fastapi import APIRouter, HTTPException

from app.models.schemas import ManufacturingResult, Vector3
from app.services.cad_service import kernel_manufacturing_analysis
from app.services.mesh_generator import CANDIDATE_DATA
from app.cad_kernel.kernel import Kernel

router = APIRouter(prefix="/api/manufacturing", tags=["manufacturing"])

# LPBF process constants (rough averages for aluminum)
LAYER_THICKNESS_M = 30e-6       # 30 µm
HATCH_SPACING_M = 100e-6        # 100 µm
SCAN_SPEED_MPS = 1.0            # 1 m/s
MIN_FEATURE_SIZE_M = 1e-3       # 1 mm
OVERHANG_THRESHOLD_DEG = 45.0


def _auto_build_axis(solid):
    """Pick the build direction (+X, +Y, or +Z) that minimizes overhang area."""
    axes = {
        "+X": np.array([1.0, 0.0, 0.0]),
        "+Y": np.array([0.0, 1.0, 0.0]),
        "+Z": np.array([0.0, 0.0, 1.0]),
    }
    best_axis = "+Z"
    best_area = float("inf")
    for name, axis in axes.items():
        stats = kernel_manufacturing_analysis(solid, build_axis=axis)
        if stats["overhang_area"] < best_area:
            best_area = stats["overhang_area"]
            best_axis = name
    return best_axis, axes[best_axis]


def _compute_manufacturing_result(candidate_id: str, build_axis: str = "auto") -> ManufacturingResult:
    data = CANDIDATE_DATA.get(candidate_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Candidate not found")

    solid = data.get("kernel_solid")
    if solid is None and data.get("candidate_mesh") is not None:
        mesh = data["candidate_mesh"]
        solid = Kernel.from_mesh(mesh.vertices, mesh.faces)
    if solid is None:
        raise HTTPException(status_code=404, detail="Candidate geometry not available")

    axes = {
        "x": np.array([1.0, 0.0, 0.0]),
        "y": np.array([0.0, 1.0, 0.0]),
        "z": np.array([0.0, 0.0, 1.0]),
    }
    if build_axis in axes:
        axis_name = build_axis.upper()
        axis_vec = axes[build_axis]
    else:
        axis_name, axis_vec = _auto_build_axis(solid)

    stats = kernel_manufacturing_analysis(solid, build_axis=axis_vec)
    overhang_area = stats["overhang_area"]
    overhang_angle = stats["max_overhang_angle"]
    total_area = stats["total_area"]
    total_volume = stats["total_volume"]

    # Rough support volume: overhang area times a nominal support height
    support_volume = overhang_area * 5e-3  # 5 mm average support height

    # Printability score: penalize overhang area and support volume
    overhang_penalty = 0.0
    support_penalty = 0.0
    if total_area > 0:
        overhang_penalty = 60.0 * (overhang_area / total_area)
    if total_volume > 0:
        support_penalty = 20.0 * (support_volume / total_volume)
    printability = int(round(np.clip(100.0 - overhang_penalty - support_penalty, 0.0, 100.0)))

    # Print time: volume deposition rate
    deposition_rate = SCAN_SPEED_MPS * HATCH_SPACING_M * LAYER_THICKNESS_M  # m^3/s
    total_print_volume = total_volume + support_volume
    print_time_seconds = total_print_volume / max(deposition_rate, 1e-15)
    print_time_minutes = int(round(print_time_seconds / 60.0))

    # Feature size check
    lengths = edge_lengths_from_solid(solid)
    min_edge = float(lengths.min()) if lengths.size else 0.0
    feature_size_ok = min_edge >= MIN_FEATURE_SIZE_M

    # Enclosed cavities: conservative hint based on mesh watertightness proxy
    enclosed_cavities = bool(total_volume > 0 and support_volume > 0)

    return ManufacturingResult(
        build_orientation=axis_name,
        build_orientation_vector=Vector3(x=float(axis_vec[0]), y=float(axis_vec[1]), z=float(axis_vec[2])),
        overhang_angle=round(overhang_angle, 1),
        overhang_area=round(overhang_area, 6),
        support_volume=round(support_volume, 9),
        printability_score=printability,
        print_time_minutes=max(1, print_time_minutes),
        feature_size_ok=bool(feature_size_ok),
        enclosed_cavities=bool(enclosed_cavities),
    )


def edge_lengths_from_solid(solid):
    from app.cad_kernel.geom.meshing import edge_lengths, solid_to_mesh
    mesh = solid_to_mesh(solid)
    return edge_lengths(mesh.vertices, mesh.faces)


@router.post("", response_model=ManufacturingResult)
def manufacture(candidate_id: str = "", build_axis: str = "auto"):
    return _compute_manufacturing_result(candidate_id, build_axis)
