"""Analysis and export helpers for DesignDocuments.

These wrap the CAD kernel so the documents router stays thin. Everything here
consumes a document's evaluated solid via `document_service.get_solid`.
"""

from __future__ import annotations

import logging
import math
import os
from typing import List, Optional

import numpy as np
from fastapi import HTTPException

from app.cad_kernel.geom.meshing import mesh_volume, solid_to_mesh
from app.cad_kernel.kernel import Kernel
from app.models.schemas import (
    DesignSpec,
    DfmCheck,
    DfmReport,
    Material,
    OptimizationSetup,
    Quote,
    SimulationResult,
    Vector3,
)
from app.services import document_service as docs
from app.services.cad_service import (
    MeshAnalysis,
    kernel_manufacturing_analysis,
    solid_to_step_string,
)
from app.services import materials as materials_catalog


# Default LPBF process constants (rough averages for aluminum)
_LAYER_THICKNESS_M = 30e-6
_HATCH_SPACING_M = 100e-6
_SCAN_SPEED_MPS = 1.0
_MIN_FEATURE_SIZE_M = 1e-3
_OVERHANG_THRESHOLD_DEG = 45.0
_DEFAULT_DENSITY = 2700.0  # kg/m^3

_LOGGER = logging.getLogger(__name__)


def _default_spec() -> DesignSpec:
    """Fallback spec when a document has no optimization overlay."""
    return DesignSpec(
        name="Default",
        design_space={"size": {"x": 1.0, "y": 1.0, "z": 1.0}, "center": {"x": 0.0, "y": 0.0, "z": 0.0}},
        preserve_regions=[],
        obstacle_regions=[],
        load_cases=[
            {"id": "l1", "name": "Vertical Load", "point": {"x": 0.0, "y": 0.5, "z": 0.0}, "force": {"x": 0.0, "y": -1000.0, "z": 0.0}, "load_type": "static"},
        ],
        constraints=[
            {"id": "c1", "name": "Fixed Base", "type": "fixed", "point": {"x": 0.0, "y": -0.5, "z": 0.0}},
        ],
        material_id="al-7075",
        objective={"target": "minimize_mass", "weight": 1.0},
    )


def _spec_from_optimization(doc_name: str, opt: OptimizationSetup) -> DesignSpec:
    return DesignSpec(
        name=doc_name,
        design_space={"size": {"x": 1.0, "y": 1.0, "z": 1.0}, "center": {"x": 0.0, "y": 0.0, "z": 0.0}},
        preserve_regions=opt.keep_in,
        obstacle_regions=opt.keep_out,
        load_cases=opt.load_cases,
        constraints=opt.constraints,
        material_id=opt.material_id,
        objective=opt.objective,
    )


def _material_for_spec(spec: DesignSpec) -> Material:
    # Solver-schema view (MPa/GPa) of the catalog entry; unknown ids fall back to
    # the catalog default rather than to whichever material happened to be first.
    return materials_catalog.spec_material_for(spec.material_id)


def _approximate_simulation(solid, spec: DesignSpec, mat: Material) -> SimulationResult:
    """Coarse simulation estimate for an arbitrary kernel solid."""
    mesh = solid_to_mesh(solid)
    volume = abs(mesh_volume(mesh.vertices, mesh.faces))
    mass = volume * mat.density

    verts = mesh.vertices
    size = verts.max(axis=0) - verts.min(axis=0)
    char_len = float(np.mean(size)) + 1e-6
    area_scale = (volume ** (2.0 / 3.0)) + 1e-6

    E_pa = mat.youngs_modulus * 1e9

    total_force = 0.0
    for load in spec.load_cases:
        f = np.array([load.force.x, load.force.y, load.force.z])
        total_force += float(np.linalg.norm(f))

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


_BUILD_AXES = {
    "+X": np.array([1.0, 0.0, 0.0]),
    "+Y": np.array([0.0, 1.0, 0.0]),
    "+Z": np.array([0.0, 0.0, 1.0]),
}

_DFM_PROCESSES = ("additive", "milling", "molding", "casting")


def _pull_axis(build_axis: str) -> str:
    """The mould pull / tool approach direction, defaulting to +Z.

    "auto" means *search the orientations* on the additive path. There is no
    equivalent search here — which way a mould opens is a decision, not
    something to infer from the geometry it has to release — so it resolves to
    the conventional +Z rather than silently picking a winner.
    """
    named = {"x": "+X", "y": "+Y", "z": "+Z"}
    key = (build_axis or "").lower()
    if key in named:
        return named[key]
    return build_axis if build_axis in _BUILD_AXES else "+Z"


def _auto_build_axis(analysis) -> tuple[str, np.ndarray]:
    """Pick +X/+Y/+Z to minimize overhang area.

    Takes a prepared MeshAnalysis so the three candidates share one tessellation
    and one face-normal pass; only the overhang test runs per axis.
    """
    best_name = min(_BUILD_AXES, key=lambda n: analysis.overhang(_BUILD_AXES[n])[0])
    return best_name, _BUILD_AXES[best_name]


def simulate_document(doc_id: str, resolution: int = 128) -> SimulationResult:
    """The REST surface's simulation, now a real solve where one is possible."""
    report = analyse_document(doc_id, resolution=resolution)
    return SimulationResult(
        max_stress=report["max_stress"],
        max_displacement=report["max_displacement"],
        safety_factor=report["safety_factor"],
        compliance=report["compliance"],
        mass=report["mass"],
    )


def analyse_document(doc_id: str, resolution: int = 128,
                     elements: Optional[int] = None) -> dict:
    """Linear-static FEA of the document, with the closed-form estimate as a last resort.

    Which one ran is in `solver`, and that distinction is the whole point: the
    estimate cannot tell a cube from a cantilever, so nothing should score
    against it without knowing it is what produced the number.
    """
    from app.services import fea

    doc = docs.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    spec = (_spec_from_optimization(doc.name, doc.optimization) if doc.optimization
            else _default_spec())
    material = _material_for_spec(spec)

    try:
        from app.cad_kernel.core.feature_tree import FeatureTree

        report = fea.solve(
            FeatureTree.from_dict(docs.resolved_tree(doc)).root,
            youngs_pa=material.youngs_modulus * 1e9,
            yield_mpa=material.yield_strength,
            density=material.density,
            loads=list(spec.load_cases),
            constraints=list(spec.constraints),
            **({"target_elements": int(elements)} if elements else {}),
        )
        report["material"] = material.name
        return report
    except Exception as exc:
        # Python clears the exception name at the end of the block, so the
        # reason has to be carried out rather than referenced below.
        reason = str(exc)
        _LOGGER.info("FEA fell back to the closed-form estimate for %s: %s", doc_id, reason)

    solid = docs.get_solid(doc_id, resolution=resolution)
    estimate = _approximate_simulation(solid, spec, material)
    return {
        "solver": "closed-form estimate",
        "estimate_only": True,
        "reason": reason,
        "material": material.name,
        **estimate.model_dump(),
        "notes": [
            "this is NOT an analysis: max_stress is total_force / volume^(2/3) "
            "clamped to [0.01, 500] MPa, which does not depend on the shape, the "
            "load's position or where the part is held. Two different parts under "
            "the same load get the same answer.",
        ],
    }


def _topology_checks(doc_id: str, process: str, axis: str) -> List[DfmCheck]:
    """Manufacturability measured on the exact body, when there is one.

    Silently empty rather than raising: a shelled or optimized part has no exact
    form, and losing the whole DFM report over that would be worse than losing
    the checks that need topology. The mesh checks still run either way.
    """
    from app.services import dfm_topology

    from app.services.cad_operations import _exact_shape

    if not dfm_topology.available():
        return []
    try:
        shape = _exact_shape(doc_id)
    except Exception:
        return []
    try:
        return dfm_topology.checks(shape, process=process, axis=axis)
    except Exception:
        _LOGGER.exception("topology DFM failed for %s", doc_id)
        return []


def dfm_document(doc_id: str, build_axis: str = "auto", resolution: int = 128,
                 process: str = "additive") -> DfmReport:
    doc = docs.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    process = (process or "additive").lower()
    if process not in _DFM_PROCESSES:
        raise ValueError(
            f"unknown process {process!r}; one of {', '.join(_DFM_PROCESSES)}")

    if process != "additive":
        # Overhang, support volume and print time are questions about a powder-bed
        # build. Asked of a milled or moulded part they have no answer, and an
        # invented one would be worse than none — so a subtractive/formative
        # report is the topology checks and nothing else. No quote either: this
        # module has no cost model for those processes, and a made-up number is
        # exactly the sort of signal a model would learn to optimise.
        checks = _topology_checks(doc_id, process, _pull_axis(build_axis))
        if not checks:
            raise ValueError(
                f"a {process} report needs the exact kernel and a body it can "
                "build; this part evaluates through the distance field only")
        met = sum(1 for c in checks if c.status == "pass")
        return DfmReport(
            process=process.upper(),
            checks=checks,
            printability_score=int(round(100.0 * met / len(checks))),
        )

    solid = docs.get_solid(doc_id, resolution=resolution)

    # One tessellation and one set of face normals for the whole report, however
    # many orientations are tried.
    analysis = MeshAnalysis(solid)

    named = {"x": "+X", "y": "+Y", "z": "+Z"}
    if build_axis.lower() in named:
        axis_name = named[build_axis.lower()]
        axis_vec = _BUILD_AXES[axis_name]
    else:
        axis_name, axis_vec = _auto_build_axis(analysis)

    stats = analysis.stats_for(axis_vec)
    overhang_area = stats["overhang_area"]
    overhang_angle = stats["max_overhang_angle"]
    total_area = stats["total_area"]
    total_volume = stats["total_volume"]
    support_volume = stats["support_volume"]
    min_edge = stats["min_edge"]

    overhang_penalty = 60.0 * (overhang_area / total_area) if total_area > 0 else 0.0
    support_penalty = 20.0 * (support_volume / total_volume) if total_volume > 0 else 0.0
    printability = int(round(np.clip(100.0 - overhang_penalty - support_penalty, 0.0, 100.0)))

    deposition_rate = _SCAN_SPEED_MPS * _HATCH_SPACING_M * _LAYER_THICKNESS_M
    total_print_volume = total_volume + support_volume
    print_time_minutes = int(round((total_print_volume / max(deposition_rate, 1e-15)) / 60.0))

    feature_size_ok = min_edge >= _MIN_FEATURE_SIZE_M
    enclosed_cavities = bool(total_volume > 0 and support_volume > 0)

    checks = [
        DfmCheck(id="overhang", label="Overhang angle", status="fail" if overhang_angle > _OVERHANG_THRESHOLD_DEG else "pass", detail=f"{overhang_angle:.1f}° max", value=overhang_angle),
        DfmCheck(id="feature_size", label="Feature size", status="pass" if feature_size_ok else "fail", detail=f"{min_edge*1000:.2f} mm min edge", value=min_edge),
        DfmCheck(id="support", label="Support volume", status="warn" if support_volume > 0 else "pass", detail=f"{support_volume*1e6:.2f} mm³", value=support_volume),
    ]

    # Overhang and support are honest tessellation questions — a face normal
    # against a build direction survives meshing fine. `feature_size` is not: it
    # reports the shortest edge of a marching-cubes mesh, which is a property of
    # `resolution`. When the exact kernel can build the body, the real wall
    # thickness measured on it replaces that row rather than sitting beside it,
    # because two "minimum feature" numbers that disagree is worse than one.
    exact = _topology_checks(doc_id, "additive", _pull_axis(build_axis))
    if exact:
        replaces = {c.id for c in exact}
        if "wall_thickness" in replaces:
            checks = [c for c in checks if c.id != "feature_size"]
        checks.extend(exact)

    return DfmReport(
        process="LPBF",
        checks=checks,
        printability_score=printability,
        quote=Quote(
            process="LPBF",
            material_cost=round(total_volume * 50.0, 2),
            machine_cost=round(print_time_minutes * 2.0, 2),
            setup_cost=50.0,
            total=round(total_volume * 50.0 + print_time_minutes * 2.0 + 50.0, 2),
            lead_time_days=max(1, int(math.ceil(print_time_minutes / 720))),
        ),
        build_orientation=axis_name,
        build_orientation_vector=Vector3(x=float(axis_vec[0]), y=float(axis_vec[1]), z=float(axis_vec[2])),
        overhang_angle=round(overhang_angle, 1),
        overhang_area=round(overhang_area, 6),
        support_volume=round(support_volume, 9),
        print_time_minutes=max(1, print_time_minutes),
    )


def exact_step_document(doc_id: str) -> Optional[str]:
    """STEP with analytic surfaces, when the tree is exactly representable.

    Returns ``None`` when any feature has no exact form (a smooth-boolean
    "fillet", a shell, an imported voxel body) or when OCC is not installed, so
    the caller falls back to the faceted writer rather than exporting something
    that differs from the body the rest of the system computed.
    """
    from app.cad_kernel.io.step_brep import write_step_brep

    doc = docs.get_document(doc_id)
    if doc is None:
        return None
    try:
        # `get_exact_shape` applies the same resolution pipeline the SDF path
        # uses — suppression, datums, driving parameters — minus the sampling,
        # and caches the result on the resolved tree's hash.
        shape = docs.get_exact_shape(doc_id)
        if shape is None:
            return None
        return write_step_brep(shape, name=(doc.name or "part")[:40])
    except Exception:  # noqa: BLE001 — exactness is an optimisation, never a hard failure
        _LOGGER.info("exact STEP export unavailable for %s; using the faceted writer",
                     doc_id, exc_info=True)
        return None


def export_step_document(doc_id: str, resolution: int = 128) -> str:
    doc = docs.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    exact = exact_step_document(doc_id)
    if exact is not None:
        return exact
    solid = docs.get_solid(doc_id, resolution=resolution)
    return solid_to_step_string(solid, name=f"doc_{doc_id}")


def flat_pattern_dxf(doc_id: str) -> str:
    """The sheet-metal blank as a DXF: real cut contours plus bend lines.

    The outline is contoured from the *unfolded* body rather than assumed
    rectangular, so holes, notches and non-rectangular blanks all come through —
    each closed contour becomes its own loop on the CUT layer. Kerf is
    deliberately zero: a DXF carries nominal geometry and the cutter applies its
    own compensation, unlike the G-code path which bakes it in.

    Falls back to the analytic rectangle if the body cannot be sectioned, so a
    valid file always comes back for a part that has a computable blank.
    """
    from app.cad_kernel.io.dxf import write_dxf
    from app.services import cad_operations
    from app.services.cam import profile_cut as pc

    feature = cad_operations._sheet_metal_feature(docs.get_document(doc_id))
    pattern = feature.flat_pattern_outline()

    try:
        cut = pc.profile_cut(doc_id, kerf_mm=0.0)
        outlines = [p for p in cut["paths"] if len(p) >= 3]
    except Exception:  # degenerate section — fall back to the computed rectangle
        _LOGGER.warning("Falling back to the analytic blank outline for %s", doc_id, exc_info=True)
        outlines = []

    if outlines:
        # Contours come out in the body's own frame; move the blank to the origin
        # so the DXF lands in the positive quadrant like the analytic outline.
        xs = [p[0] for path in outlines for p in path]
        ys = [p[1] for path in outlines for p in path]
        dx, dy = -min(xs), -min(ys)
        outlines = [[(round(x + dx, 4), round(y + dy, 4)) for x, y in path] for path in outlines]
    else:
        outlines = [pattern["outline_mm"]]

    return write_dxf(outlines, pattern["bend_lines_mm"])


class ExportTooLargeError(Exception):
    """A STEP export exceeded the triangle budget and was not built."""

    def __init__(self, triangles: int, limit: int):
        super().__init__(f"{triangles} triangles exceeds the {limit} budget")
        self.triangles = triangles
        self.limit = limit


def _step_triangle_budget() -> int:
    """Max triangles a single STEP export may emit (~1.3 kB of file each)."""
    try:
        return max(1000, int(os.environ.get("FORGE_STEP_MAX_TRIANGLES", "100000")))
    except ValueError:
        return 100000


def mesh_payload(doc_id: str, mesh_hash: str) -> Optional[bytes]:
    """The document's triangles in transport form, if they hash to `mesh_hash`.

    Returns ``None`` when they do not. That is the whole safety of serving a
    cacheable URL: the hash is checked against the geometry actually produced,
    so a stale link can only 404 — never quietly return a *different* body under
    a hash that browsers and proxies have been told to keep forever.

    The solid is re-derived on a cache miss the way `get_solid` already does, so
    a process restart costs one evaluation rather than a broken viewport.
    """
    from app.cad_kernel.geom.meshing import mesh_hash as hash_of
    from app.cad_kernel.io import mesh_binary
    from app.cad_kernel.kernel import Kernel

    cached = docs.cached_mesh(doc_id, mesh_hash)
    if cached is not None:
        return cached

    doc = docs.get_document(doc_id)
    if doc is None:
        return None
    try:
        solid = docs.get_solid(doc_id, resolution=doc.derived.resolution or 128)
    except (KeyError, ValueError):
        return None
    mesh = Kernel.tessellate(solid)
    digest = hash_of(mesh)
    payload = mesh_binary.write_mesh_binary(mesh)
    docs.cache_mesh(doc_id, digest, payload)
    return payload if digest == mesh_hash else None


def stream_step_document(doc_id: str, resolution: int = 128):
    """Return ``(chunk_iterator, triangle_count)`` for a document's STEP export.

    The triangle count is checked *before* any entity is built, so an oversized
    request costs a meshing pass rather than gigabytes of string building.
    """
    from app.cad_kernel.io.step import stream_step, triangle_count

    doc = docs.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    # An exact export has no triangles to budget: analytic surfaces are a handful
    # of entities however curved the part is, which is the whole point.
    exact = exact_step_document(doc_id)
    if exact is not None:
        return iter([exact]), 0

    solid = docs.get_solid(doc_id, resolution=resolution)

    triangles = triangle_count(solid)
    limit = _step_triangle_budget()
    if triangles > limit:
        raise ExportTooLargeError(triangles, limit)
    return stream_step(solid, name=f"doc_{doc_id}"), triangles


def cam_document(doc_id: str, tool) -> "object":
    """Generate a 2.5D profiling program for the document body and cache it."""
    from app.cad_kernel.core.feature_tree import FeatureTree
    from app.models.schemas import CamResult
    from app.services.cam.toolpath import generate_toolpaths

    doc = docs.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    root = FeatureTree.from_dict(doc.feature_tree).root
    try:
        result = generate_toolpaths(root, tool)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"CAM generation failed: {exc}")

    cam = CamResult(**result)
    doc.derived.cam = cam
    docs._persist(doc)
    return cam
