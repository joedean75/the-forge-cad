"""Universal import — turn an uploaded file into a live, editable document.

Roadmap feature 02. There are two paths, and which one runs decides what you get.

**STEP and IGES go through OpenCascade** (`cad_kernel/io/step_import.py`) and
stay exact: the body arrives as a `BrepBodyFeature` carrying real analytic
surfaces, so its faces and edges are addressable, `mass_properties` is computed
rather than sampled, and it re-exports as an editable solid. This is the only
path on which an imported part is the same kind of object as a modelled one.

**Meshes are voxelized**, because an STL has nothing better to recover. The
occupancy grid is stored on an `ImportFeature` in the same cache framing
`OptimizeFeature` uses, so the body evaluates through `GridSDF` and composes
with booleans, modifiers, generative optimization, FEA, DFM, CAM and assemblies.

STEP falls back to the mesh path when OCC is unavailable or the file defeats it,
via the faceted reader below — which handles triangulated STEP including The
Forge's own legacy exports. IGES has no such fallback: it is a surface format
with no faceted convention to parse.
"""

from __future__ import annotations

import base64
import io
import re
from typing import Optional, Tuple

import numpy as np
import trimesh

from app.cad_kernel.io import step_import
from app.models.schemas import DesignDocument
from app.services import document_service

MESH_FORMATS = {"stl", "obj", "ply", "glb", "off"}
STEP_FORMATS = step_import.STEP_FORMATS
IGES_FORMATS = step_import.IGES_FORMATS
EXACT_FORMATS = step_import.EXACT_FORMATS
SUPPORTED_FORMATS = MESH_FORMATS | EXACT_FORMATS

MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_RESOLUTION = 96  # voxels along the longest axis
MAX_RESOLUTION = 160

# Files with extents larger than this (metres) are assumed to be millimetres —
# engineering parts are rarely >5 m across, but are routinely >5 mm.
_MM_HEURISTIC_THRESHOLD = 5.0

_UNIT_SCALE = {"m": 1.0, "mm": 0.001, "cm": 0.01, "inch": 0.0254, "in": 0.0254}


class ImportError_(ValueError):
    """Import failed for a user-explainable reason (bad format/geometry)."""


def _parse_faceted_step(text: str) -> Tuple[np.ndarray, np.ndarray]:
    """Extract triangles from a faceted (triangulated) STEP file.

    Walks EDGE_LOOP → ORIENTED_EDGE → EDGE_CURVE → VERTEX_POINT →
    CARTESIAN_POINT. Polygonal loops are fan-triangulated. Raises
    ``ImportError_`` when no faceted geometry is present (NURBS-surface STEP).
    """
    points: dict[int, tuple[float, float, float]] = {}
    for m in re.finditer(r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)", text):
        try:
            coords = [float(v) for v in m.group(2).split(",")]
        except ValueError:
            continue
        if len(coords) == 3:
            points[int(m.group(1))] = (coords[0], coords[1], coords[2])

    vertex_pt = {
        int(m.group(1)): int(m.group(2))
        for m in re.finditer(r"#(\d+)\s*=\s*VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)", text)
    }
    edge_verts = {
        int(m.group(1)): (int(m.group(2)), int(m.group(3)))
        for m in re.finditer(r"#(\d+)\s*=\s*EDGE_CURVE\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*#(\d+)", text)
    }
    oriented = {
        int(m.group(1)): (int(m.group(2)), m.group(3) == "T")
        for m in re.finditer(r"#(\d+)\s*=\s*ORIENTED_EDGE\s*\(\s*'[^']*'\s*,\s*\*\s*,\s*\*\s*,\s*#(\d+)\s*,\s*\.([TF])\.", text)
    }

    verts: list[tuple[float, float, float]] = []
    vert_index: dict[tuple[float, float, float], int] = {}
    faces: list[list[int]] = []

    def vid(coords: tuple[float, float, float]) -> int:
        key = (round(coords[0], 9), round(coords[1], 9), round(coords[2], 9))
        if key not in vert_index:
            vert_index[key] = len(verts)
            verts.append(coords)
        return vert_index[key]

    for m in re.finditer(r"#\d+\s*=\s*EDGE_LOOP\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)", text):
        loop_vertex_ids: list[int] = []
        ok = True
        for ref in re.findall(r"#(\d+)", m.group(1)):
            oe = oriented.get(int(ref))
            if oe is None:
                ok = False
                break
            edge, forward = oe
            ev = edge_verts.get(edge)
            if ev is None:
                ok = False
                break
            start = ev[0] if forward else ev[1]
            pt_id = vertex_pt.get(start)
            if pt_id is None or pt_id not in points:
                ok = False
                break
            loop_vertex_ids.append(vid(points[pt_id]))
        if ok and len(loop_vertex_ids) >= 3:
            for i in range(1, len(loop_vertex_ids) - 1):
                faces.append([loop_vertex_ids[0], loop_vertex_ids[i], loop_vertex_ids[i + 1]])

    if not faces:
        raise ImportError_(
            "This STEP file has no faceted (triangulated) geometry. "
            "NURBS-surface STEP isn't supported yet — export a tessellated STEP or STL instead."
        )
    return np.asarray(verts, dtype=float), np.asarray(faces, dtype=np.int64)


def parse_mesh(filename: str, data: bytes) -> trimesh.Trimesh:
    """Parse an uploaded file into a triangle mesh (original file units)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED_FORMATS:
        raise ImportError_(
            f"Unsupported format '.{ext}'. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}."
        )
    if ext in IGES_FORMATS:
        raise ImportError_(
            "IGES import needs the exact kernel (pip install cadquery-ocp). "
            "There is no faceted fallback for a surface format."
        )
    if ext in STEP_FORMATS:
        verts, faces = _parse_faceted_step(data.decode("utf-8", errors="replace"))
        mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
    else:
        loaded = trimesh.load(io.BytesIO(data), file_type=ext, force="mesh")
        if isinstance(loaded, trimesh.Scene):
            geoms = list(loaded.geometry.values())
            if not geoms:
                raise ImportError_("The file contains no mesh geometry.")
            mesh = trimesh.util.concatenate(geoms)
        else:
            mesh = loaded
    if mesh.vertices is None or len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        raise ImportError_("The file contains no triangles.")
    return mesh


def _unit_scale(mesh: trimesh.Trimesh, units: str) -> Tuple[float, str]:
    if units != "auto":
        scale = _UNIT_SCALE.get(units)
        if scale is None:
            raise ImportError_(f"Unknown units '{units}'. Use auto, m, mm, cm or inch.")
        return scale, units
    extent = float(np.max(mesh.extents)) if mesh.extents is not None else 0.0
    if extent > _MM_HEURISTIC_THRESHOLD:
        return 0.001, "mm (auto-detected)"
    return 1.0, "m (auto-detected)"


def voxelize(mesh: trimesh.Trimesh, resolution: int) -> dict:
    """Voxelize a mesh (metres) into an ImportFeature cache dict.

    The occupancy grid is bit-packed to base64 so the persisted feature tree
    stays compact (a 96³ grid is ~110 KB instead of ~2 MB of JSON floats).
    """
    resolution = max(16, min(int(resolution), MAX_RESOLUTION))
    extents = np.maximum(mesh.extents, 1e-6)
    pitch = float(np.max(extents)) / resolution

    vg = mesh.voxelized(pitch=pitch)
    try:
        vg = vg.fill()
    except BaseException:
        pass  # unfillable (open shell) — surface voxels still give a usable body
    matrix = np.asarray(vg.matrix, dtype=bool)
    if not matrix.any():
        raise ImportError_("Voxelization produced an empty body — is the mesh degenerate?")

    # Grid framing: trimesh voxel indices map to cell-centre points.
    p0 = vg.indices_to_points(np.array([[0, 0, 0]]))[0]
    dims = np.array(matrix.shape)
    lo = np.asarray(p0, dtype=float) - pitch / 2.0
    size = dims * pitch
    center = lo + size / 2.0

    packed = base64.b64encode(np.packbits(matrix.ravel())).decode("ascii")
    return {
        "density_packed": packed,
        "dims": [int(d) for d in dims],
        "origin": {"x": float(center[0]), "y": float(center[1]), "z": float(center[2])},
        "size": {"x": float(size[0]), "y": float(size[1]), "z": float(size[2])},
        "threshold": 0.5,
    }


def unpack_density(cache: dict) -> np.ndarray:
    """Rebuild the (nx, ny, nz) occupancy array from a bit-packed cache."""
    dims = cache["dims"]
    n = int(dims[0]) * int(dims[1]) * int(dims[2])
    bits = np.unpackbits(np.frombuffer(base64.b64decode(cache["density_packed"]), dtype=np.uint8))
    return bits[:n].astype(float).reshape(dims)


def _exact_tree(data: bytes, base: str, stem: str, ext: str, units: str) -> dict:
    """Read a STEP/IGES file as exact B-rep and wrap it in a feature tree."""
    from app.cad_kernel.evaluator import brep

    shape, info = step_import.read_exact(data, ext, units=units)
    return {
        "version": "brep-v1",
        "root": {
            "type": "BrepBodyFeature",
            "id": "f1",
            "name": f"Import · {stem}",
            "params": {
                "payload": brep.serialize(shape),
                "source": dict(info, filename=base, format=ext, exact=True),
            },
        },
    }


def import_document(
    filename: str,
    data: bytes,
    name: Optional[str] = None,
    resolution: int = DEFAULT_RESOLUTION,
    units: str = "auto",
    owner_id: Optional[str] = None,
) -> DesignDocument:
    """Create a new document whose body is the imported file.

    STEP/IGES import exactly when OpenCascade is present; everything else — and
    a STEP file OCC cannot read — goes through the voxel path.
    """
    if len(data) > MAX_FILE_BYTES:
        raise ImportError_(f"File too large ({len(data) // (1024 * 1024)} MB > {MAX_FILE_BYTES // (1024 * 1024)} MB).")

    base = filename.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""

    tree: Optional[dict] = None
    if ext in EXACT_FORMATS and step_import.available():
        try:
            tree = _exact_tree(data, base, stem, ext, units)
        except step_import.ExactImportError as exc:
            # STEP has a faceted fallback worth trying; IGES has none, so the
            # reason the exact reader gave is the one the user needs to see.
            if ext in IGES_FORMATS:
                raise ImportError_(str(exc))
        except Exception as exc:  # OCC can fail in ways it does not name well
            if ext in IGES_FORMATS:
                raise ImportError_(f"IGES import failed: {exc}")

    if tree is None:
        mesh = parse_mesh(filename, data)
        scale, detected_units = _unit_scale(mesh, units)
        if scale != 1.0:
            mesh = mesh.copy()
            mesh.apply_scale(scale)
        tree = {
            "version": "brep-v1",
            "root": {
                "type": "ImportFeature",
                "id": "f1",
                "name": f"Import · {stem}",
                "params": {
                    "cache": voxelize(mesh, resolution),
                    "source": {
                        "filename": base,
                        "format": ext,
                        "triangles": int(len(mesh.faces)),
                        "units": detected_units,
                        "exact": False,
                    },
                },
            },
        }

    doc = document_service.create_document(name=name or stem, owner_id=owner_id)
    updated = document_service.update_document(
        doc.id, feature_tree=tree, resolution=min(128, max(64, resolution))
    )
    if updated is None:  # pragma: no cover — we just created it
        raise ImportError_("Document creation failed.")
    updated.provenance = "imported"
    document_service._persist(updated)
    return updated
