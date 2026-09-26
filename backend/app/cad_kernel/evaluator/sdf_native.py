"""Native fast-path for SDF grid evaluation (Rust extension `forge_sdf`).

Compiles a feature tree of box/sphere/cylinder + booleans into a JSON program the
Rust sampler evaluates in parallel — the measured hot path (~97% of eval time).
Trees using any other feature return None here and fall back to the numpy SDF.
"""

from __future__ import annotations

import json
from typing import Optional

import logging
import numpy as np

_LOGGER = logging.getLogger(__name__)

from app.cad_kernel.core.topology import Solid
from app.cad_kernel.evaluator.sdf import (
    grid_dims,
    mesh_from_field,
    sdf_from_feature,
    _rotation_matrix,
    _euler_matrix,
)

try:
    import forge_sdf as _native  # built via native/forge_sdf (maturin)

    AVAILABLE = True
except Exception:  # pragma: no cover
    _native = None
    AVAILABLE = False


def _cyl_program(center, radius, height, axis) -> dict:
    m = _rotation_matrix(axis)  # native applies M^T·(p-center)
    return {
        "op": "cylinder",
        "center": [center.x, center.y, center.z],
        "radius": float(radius),
        "half_h": float(height) / 2.0,
        "rot": [float(v) for v in m.flatten()],
    }


def _transform_program(child: dict, translate, rot_matrix, scale: float) -> dict:
    return {
        "op": "transform",
        "translate": [float(translate[0]), float(translate[1]), float(translate[2])],
        "rot": [float(v) for v in np.asarray(rot_matrix).flatten()],
        "scale": float(scale),
        "child": child,
    }


def compile_feature(feature) -> Optional[dict]:
    """Return a JSON-able program for the native sampler, or None if unsupported."""
    from app.cad_kernel.ops.primitives import BoxFeature, SphereFeature, CylinderFeature, ConeFeature
    from app.cad_kernel.ops.booleans import BooleanFeature
    from app.cad_kernel.ops.modifiers import (
        FilletFeature, ChamferFeature, ShellFeature, HoleFeature, PatternFeature, TransformFeature,
    )
    from app.cad_kernel.ops.sketch import ExtrudeFeature

    if isinstance(feature, BoxFeature):
        c, s = feature.center, feature.size
        return {"op": "box", "center": [c.x, c.y, c.z], "half": [s.x / 2.0, s.y / 2.0, s.z / 2.0]}
    if isinstance(feature, SphereFeature):
        c = feature.center
        return {"op": "sphere", "center": [c.x, c.y, c.z], "radius": float(feature.radius)}
    if isinstance(feature, CylinderFeature):
        return _cyl_program(feature.center, feature.radius, feature.height, feature.axis)
    if isinstance(feature, ConeFeature):
        c = feature.center
        m = _rotation_matrix(feature.axis)
        return {
            "op": "cone", "center": [c.x, c.y, c.z], "base_radius": float(feature.base_radius),
            "height": float(feature.height), "rot": [float(v) for v in m.flatten()],
        }
    if isinstance(feature, BooleanFeature):
        children = [compile_feature(ch) for ch in feature.children]
        if any(ch is None for ch in children):
            return None
        return {"op": "boolean", "operation": feature.operation, "children": children}
    if isinstance(feature, (FilletFeature, ChamferFeature)):
        children = [compile_feature(ch) for ch in feature.children]
        if any(ch is None for ch in children):
            return None
        return {
            "op": "smooth", "operation": feature.operation, "radius": float(feature.radius),
            "chamfer": isinstance(feature, ChamferFeature), "children": children,
        }
    if isinstance(feature, ShellFeature):
        child = compile_feature(feature.children[0]) if feature.children else None
        if child is None:
            return None
        return {"op": "shell", "thickness": float(feature.thickness), "child": child}
    if isinstance(feature, HoleFeature):
        from app.cad_kernel.core.types import Point3, Vector3
        from app.cad_kernel.geom import holes

        child = compile_feature(feature.children[0]) if feature.children else None
        if child is None:
            return None
        # The tool comes from the same `geom/holes` list the numpy and B-rep
        # kernels build from. Re-deriving it here would be a third transcription
        # of the arithmetic, and a native path that meshes a counterbore
        # differently from numpy is exactly the correctness hazard the parity
        # test exists to catch.
        parts = []
        for tool in holes.tools_for(feature):
            if isinstance(tool, holes.Cyl):
                parts.append(_cyl_program(Point3(*tool.center), tool.radius,
                                          tool.height, Vector3(*tool.axis)))
            else:
                parts.append({
                    "op": "cone", "center": list(tool.center),
                    "base_radius": float(tool.base_radius),
                    "height": float(tool.height),
                    "rot": [float(v) for v in
                            _rotation_matrix(Vector3(*tool.axis)).flatten()],
                })
        cut = parts[0] if len(parts) == 1 else {
            "op": "boolean", "operation": "union", "children": parts}
        return {"op": "boolean", "operation": "subtract", "children": [child, cut]}
    if isinstance(feature, TransformFeature):
        child = compile_feature(feature.children[0]) if feature.children else None
        if child is None:
            return None
        r = feature.rotate
        R = _euler_matrix(r.x, r.y, r.z)
        t = feature.translate
        return _transform_program(child, [t.x, t.y, t.z], R, feature.scale)
    if isinstance(feature, PatternFeature):
        child = compile_feature(feature.children[0]) if feature.children else None
        if child is None:
            return None
        count = max(1, int(feature.count))
        copies = []
        if feature.kind == "circular":
            axis = feature.axis
            center = np.array([feature.center.x, feature.center.y, feature.center.z])
            for i in range(count):
                ang = 360.0 * i / count
                if abs(axis.z) >= abs(axis.x) and abs(axis.z) >= abs(axis.y):
                    R = _euler_matrix(0, 0, ang)
                elif abs(axis.y) >= abs(axis.x):
                    R = _euler_matrix(0, ang, 0)
                else:
                    R = _euler_matrix(ang, 0, 0)
                t = center - R @ center
                copies.append(_transform_program(child, t, R, 1.0))
        else:
            step = np.array([feature.axis.x, feature.axis.y, feature.axis.z]) * feature.spacing
            for i in range(count):
                copies.append(_transform_program(child, step * i, np.eye(3), 1.0))
        return {"op": "boolean", "operation": "union", "children": copies}
    if isinstance(feature, ExtrudeFeature):
        from app.cad_kernel.evaluator.sdf import _PLANE_NORMAL
        # **The Rust sampler knows three profile shapes and `loops` is not one.**
        # Everything else here declines by returning None, which is the numpy
        # fallback; an unknown profile instead reached the crate and came back as
        # `unknown variant 'loops', expected one of 'rect', 'circle', 'polygon'`
        # — serde's complaint, raised rather than returned, so the document
        # failed to evaluate at all. `loops` is what `sketch_to_loops` emits, so
        # on a deployment without OCP *every sketch-derived extrude* was a dead
        # document, against the promise that everything still works through the
        # SDF. The numpy sampler has built `loops` since sketches became curves.
        if str((feature.profile or {}).get("type", "")) not in NATIVE_PROFILES:
            return None
        return {
            "op": "extrude", "profile": feature.profile,
            "normal": _PLANE_NORMAL.get(feature.plane, "z"),
            "offset": float(feature.offset), "depth": float(feature.depth),
        }
    return None  # OptimizeFeature (GridSDF) and anything else -> numpy fallback


#: The profile shapes the Rust crate can deserialize. Kept here as the gate
#: rather than discovered at the boundary, because the crate answers an unknown
#: one by raising, and this module's whole contract is that it returns None when
#: it cannot help. `tests/test_sdf_native_parity.py` compares mesh hashes across
#: the trees it *does* take; a shape it does not take has to reach numpy to be
#: compared at all.
NATIVE_PROFILES = ("rect", "circle", "polygon")


def evaluate_feature_native(feature, resolution: int = 128, margin: float = 0.05, level: float = 0.0) -> Optional[Solid]:
    """Evaluate a feature via the Rust sampler, or None to signal a numpy fallback."""
    if not AVAILABLE:
        return None
    program = compile_feature(feature)
    if program is None:
        return None

    bounds = sdf_from_feature(feature).bounds().expanded(margin)
    min_b, max_b, size, nx, ny, nz = grid_dims(bounds, resolution)
    try:
        flat = _native.sample_grid(
            json.dumps(program),
            [float(min_b[0]), float(min_b[1]), float(min_b[2])],
            [float(max_b[0]), float(max_b[1]), float(max_b[2])],
            (nx, ny, nz),
        )
    except Exception as exc:  # noqa: BLE001 — the crate refusing is not a failure
        # Belt and braces behind `NATIVE_PROFILES`: whatever the crate declines,
        # numpy is the reference implementation and the parity test exists to say
        # they agree. Failing the document instead would make a *speedup* into a
        # correctness hazard, which is the one thing two evaluators must not be.
        _LOGGER.debug("native sampler declined, falling back to numpy: %s", exc)
        return None
    values = np.asarray(flat).reshape(nx, ny, nz)
    return mesh_from_field(values, min_b, size, nx, ny, nz, level)
