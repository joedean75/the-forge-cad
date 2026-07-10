"""Signed-distance-field evaluator for the DAPS CAD kernel.

The kernel exposes a B-rep feature tree, but for robust boolean evaluation of
primitives we compose signed distance functions on a regular grid and extract
the zero-isosurface with marching cubes.  This avoids the robustness pitfalls
of self-written B-rep trimming/booleans while still producing a fully custom,
controllable geometry pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, List, Tuple

import numpy as np

from app.cad_kernel.core.topology import Face, FaceMesh, Shell, Solid
from app.cad_kernel.core.types import Point3, Vector3


@dataclass
class Bounds:
    min: np.ndarray
    max: np.ndarray

    def expanded(self, margin: float = 0.05) -> "Bounds":
        size = self.max - self.min
        delta = size * margin + 1e-3
        return Bounds(self.min - delta, self.max + delta)

    def to_arrays(self) -> Tuple[np.ndarray, np.ndarray]:
        return self.min, self.max


def _rotation_matrix(axis: Vector3) -> np.ndarray:
    """Return a rotation matrix that maps the +Z axis to `axis`."""
    axis = axis.normalized()
    if np.allclose(axis.to_array(), [0.0, 0.0, 1.0]):
        return np.eye(3)
    if np.allclose(axis.to_array(), [0.0, 0.0, -1.0]):
        return np.diag([1.0, 1.0, -1.0])
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(z, axis.to_array())
    s = np.linalg.norm(v)
    c = np.dot(z, axis.to_array())
    vx = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
    R = np.eye(3) + vx + vx @ vx * ((1.0 - c) / (s * s))
    return R


class SDF(ABC):
    """Base class for signed distance functions."""

    @abstractmethod
    def __call__(self, points: np.ndarray) -> np.ndarray:
        """Return signed distances for an (n, 3) array of points."""
        raise NotImplementedError

    @abstractmethod
    def bounds(self) -> Bounds:
        raise NotImplementedError


class BoxSDF(SDF):
    def __init__(self, size: Vector3, center: Point3 = Point3()):
        self.half = np.array([size.x, size.y, size.z]) / 2.0
        self.center = center.to_array()

    def __call__(self, points: np.ndarray) -> np.ndarray:
        p = points - self.center
        q = np.abs(p) - self.half
        return np.linalg.norm(np.maximum(q, 0.0), axis=1) + np.minimum(np.max(q, axis=1), 0.0)

    def bounds(self) -> Bounds:
        return Bounds(self.center - self.half, self.center + self.half)


class SphereSDF(SDF):
    def __init__(self, radius: float, center: Point3 = Point3()):
        self.radius = radius
        self.center = center.to_array()

    def __call__(self, points: np.ndarray) -> np.ndarray:
        return np.linalg.norm(points - self.center, axis=1) - self.radius

    def bounds(self) -> Bounds:
        r = np.array([self.radius, self.radius, self.radius])
        return Bounds(self.center - r, self.center + r)


class CylinderSDF(SDF):
    """Finite cylinder aligned with the given axis."""

    def __init__(self, radius: float, height: float, center: Point3 = Point3(), axis: Vector3 = Vector3(0.0, 0.0, 1.0)):
        self.radius = radius
        self.height = height
        self.center = center.to_array()
        self.axis = axis.normalized()
        self.R = _rotation_matrix(self.axis).T

    def __call__(self, points: np.ndarray) -> np.ndarray:
        p = (points - self.center) @ self.R.T
        d = np.stack([np.linalg.norm(p[:, :2], axis=1) - self.radius, np.abs(p[:, 2]) - self.height / 2.0], axis=1)
        return np.linalg.norm(np.maximum(d, 0.0), axis=1) + np.minimum(np.max(d, axis=1), 0.0)

    def bounds(self) -> Bounds:
        # Bounding box oriented to axis
        axis_arr = self.axis.to_array()
        extent_along = np.abs(axis_arr) * (self.height / 2.0)
        extent_rad = np.sqrt(1.0 - axis_arr * axis_arr) * self.radius
        extent = extent_along + extent_rad
        return Bounds(self.center - extent, self.center + extent)


class ConeSDF(SDF):
    """Finite cone with apex at +height/2 along axis, base at -height/2."""

    def __init__(self, base_radius: float, height: float, center: Point3 = Point3(), axis: Vector3 = Vector3(0.0, 0.0, 1.0)):
        self.base_radius = base_radius
        self.height = height
        self.center = center.to_array()
        self.axis = axis.normalized()
        self.R = _rotation_matrix(self.axis).T

    def __call__(self, points: np.ndarray) -> np.ndarray:
        p = (points - self.center) @ self.R.T
        # Cone axis is +Z: apex at z = +h/2, base radius at z = -h/2
        r = np.linalg.norm(p[:, :2], axis=1)
        z = p[:, 2]
        cone_radius = self.base_radius * (0.5 - z / self.height)
        # Solid cone: inside if r <= cone_radius and -h/2 <= z <= h/2
        outside_cone = r - cone_radius
        outside_slab = np.maximum(-z - self.height / 2.0, z - self.height / 2.0)
        return np.maximum(outside_cone, outside_slab)

    def bounds(self) -> Bounds:
        axis_arr = self.axis.to_array()
        extent_along = np.abs(axis_arr) * (self.height / 2.0)
        extent_rad = np.sqrt(1.0 - axis_arr * axis_arr) * self.base_radius
        extent = extent_along + extent_rad
        return Bounds(self.center - extent, self.center + extent)


class BooleanSDF(SDF):
    def __init__(self, operation: str, children: List[SDF]):
        self.operation = operation
        self.children = children

    def __call__(self, points: np.ndarray) -> np.ndarray:
        if not self.children:
            return np.full(points.shape[0], 1e9)
        result = self.children[0](points)
        for child in self.children[1:]:
            d = child(points)
            if self.operation == "union":
                result = np.minimum(result, d)
            elif self.operation == "subtract":
                result = np.maximum(result, -d)
            else:  # intersect
                result = np.maximum(result, d)
        return result

    def bounds(self) -> Bounds:
        child_bounds = [c.bounds() for c in self.children]
        if self.operation == "subtract" and child_bounds:
            # A - B - ... is always contained within A's bounds.
            return child_bounds[0]
        mins = np.stack([b.min for b in child_bounds], axis=0)
        maxs = np.stack([b.max for b in child_bounds], axis=0)
        if self.operation == "intersect":
            return Bounds(mins.max(axis=0), maxs.min(axis=0))
        return Bounds(mins.min(axis=0), maxs.max(axis=0))


# ---------------------------------------------------------------------------
# Modifier / smooth-boolean SDFs (Phase 2 standard CAD ops)
# ---------------------------------------------------------------------------


def _euler_matrix(rx: float, ry: float, rz: float) -> np.ndarray:
    """Rotation matrix (local -> world) from XYZ euler angles in degrees."""
    ax, ay, az = np.radians([rx, ry, rz])
    cx, sx = np.cos(ax), np.sin(ax)
    cy, sy = np.cos(ay), np.sin(ay)
    cz, sz = np.cos(az), np.sin(az)
    rot_x = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    rot_y = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rot_z = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return rot_z @ rot_y @ rot_x


def _smooth_min(a: np.ndarray, b: np.ndarray, k: float, chamfer: bool) -> np.ndarray:
    if k <= 1e-9:
        return np.minimum(a, b)
    if chamfer:
        return np.minimum(np.minimum(a, b), (a - k + b) * 0.70710678)
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0.0, 1.0)
    return b * (1.0 - h) + a * h - k * h * (1.0 - h)


def _smooth_max(a: np.ndarray, b: np.ndarray, k: float, chamfer: bool) -> np.ndarray:
    return -_smooth_min(-a, -b, k, chamfer)


class TransformSDF(SDF):
    """Rigid transform + uniform scale applied to a child SDF."""

    def __init__(self, child: SDF, translate: np.ndarray, R: np.ndarray, scale: float = 1.0):
        self.child = child
        self.t = np.asarray(translate, dtype=float)
        self.R = np.asarray(R, dtype=float)  # local -> world
        self.scale = max(float(scale), 1e-6)

    def __call__(self, points: np.ndarray) -> np.ndarray:
        local = ((points - self.t) @ self.R) / self.scale  # (p - t) @ R == R^T (p - t)
        return self.child(local) * self.scale

    def bounds(self) -> Bounds:
        b = self.child.bounds()
        corners = np.array([[x, y, z] for x in (b.min[0], b.max[0]) for y in (b.min[1], b.max[1]) for z in (b.min[2], b.max[2])])
        world = (corners * self.scale) @ self.R.T + self.t
        return Bounds(world.min(axis=0), world.max(axis=0))


class ShellSDF(SDF):
    """Hollow a child to a wall of the given thickness."""

    def __init__(self, child: SDF, thickness: float):
        self.child = child
        self.thickness = float(thickness)

    def __call__(self, points: np.ndarray) -> np.ndarray:
        return np.abs(self.child(points)) - self.thickness / 2.0

    def bounds(self) -> Bounds:
        b = self.child.bounds()
        pad = self.thickness / 2.0
        return Bounds(b.min - pad, b.max + pad)


class SmoothBooleanSDF(SDF):
    """Boolean of children with a fillet (polynomial) or chamfer blend radius."""

    def __init__(self, operation: str, radius: float, children: List[SDF], chamfer: bool = False):
        self.operation = operation
        self.radius = float(radius)
        self.children = children
        self.chamfer = chamfer

    def __call__(self, points: np.ndarray) -> np.ndarray:
        if not self.children:
            return np.full(points.shape[0], 1e9)
        result = self.children[0](points)
        for child in self.children[1:]:
            d = child(points)
            if self.operation == "union":
                result = _smooth_min(result, d, self.radius, self.chamfer)
            elif self.operation == "subtract":
                result = _smooth_max(result, -d, self.radius, self.chamfer)
            else:  # intersect
                result = _smooth_max(result, d, self.radius, self.chamfer)
        return result

    def bounds(self) -> Bounds:
        mins = np.stack([c.bounds().min for c in self.children], axis=0)
        maxs = np.stack([c.bounds().max for c in self.children], axis=0)
        pad = self.radius
        return Bounds(mins.min(axis=0) - pad, maxs.max(axis=0) + pad)


class GridSDF(SDF):
    """Signed distance sampled from a voxel density field (generative results).

    Built from a SIMP density grid: occupancy = density > threshold, then a
    discrete signed distance via EDT, trilinearly interpolated at query points.
    """

    def __init__(self, density, origin, size, threshold: float = 0.5):
        from scipy.ndimage import distance_transform_edt

        self.density = np.asarray(density, dtype=float)  # (nx, ny, nz)
        self.dims = np.array(self.density.shape)
        self.center = np.asarray(origin, dtype=float)
        self.size = np.asarray(size, dtype=float)
        self.cell = self.size / np.maximum(self.dims, 1)
        self.mean_cell = float(self.cell.mean())
        self._lo = self.center - self.size / 2.0

        occ = self.density > threshold
        if not occ.any():
            self.sdf_vol = np.full(self.density.shape, 1e6)
        else:
            d_in = distance_transform_edt(occ)
            d_out = distance_transform_edt(~occ)
            self.sdf_vol = (d_out - d_in) * self.mean_cell

    def __call__(self, points: np.ndarray) -> np.ndarray:
        from scipy.ndimage import map_coordinates

        rel = (points - self._lo) / self.cell - 0.5  # -> continuous grid indices
        coords = np.vstack([rel[:, 0], rel[:, 1], rel[:, 2]])
        vals = map_coordinates(self.sdf_vol, coords, order=1, mode="nearest")
        outside = np.any((points < self._lo - 1e-9) | (points > self._lo + self.size + 1e-9), axis=1)
        if np.any(outside):
            vals = vals.copy()
            vals[outside] = np.maximum(vals[outside], self.mean_cell)
        return vals

    def bounds(self) -> Bounds:
        return Bounds(self._lo.copy(), self._lo + self.size)


_PLANE_NORMAL = {"xy": "z", "xz": "y", "yz": "x"}


def _poly_sdf_2d(uv: np.ndarray, verts: np.ndarray) -> np.ndarray:
    """Signed distance from points to a 2D polygon (iq's algorithm, vectorized)."""
    n = len(verts)
    d = np.sum((uv - verts[0]) ** 2, axis=1)
    s = np.ones(len(uv))
    for i in range(n):
        j = (i - 1) % n
        e = verts[j] - verts[i]
        w = uv - verts[i]
        t = np.clip((w @ e) / (e @ e + 1e-12), 0.0, 1.0)
        b = w - np.outer(t, e)
        d = np.minimum(d, np.sum(b * b, axis=1))
        c1 = uv[:, 1] >= verts[i, 1]
        c2 = uv[:, 1] < verts[j, 1]
        c3 = (e[0] * w[:, 1] - e[1] * w[:, 0]) > 0
        cond = (c1 & c2 & c3) | (~c1 & ~c2 & ~c3)
        s = np.where(cond, -s, s)
    return s * np.sqrt(d)


class ExtrudeSDF(SDF):
    """A 2D profile (rect/circle/polygon) on a base plane, extruded to a depth."""

    def __init__(self, profile: dict, plane: str, offset: float, depth: float):
        self.profile = profile or {}
        self.normal = _PLANE_NORMAL.get(plane, "z")
        self.offset = float(offset)
        self.depth = float(depth)

    def _local(self, points: np.ndarray):
        if self.normal == "z":
            return points[:, 0], points[:, 1], points[:, 2]
        if self.normal == "y":
            return points[:, 0], points[:, 2], points[:, 1]
        return points[:, 1], points[:, 2], points[:, 0]

    def _profile_sdf(self, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        p = self.profile
        cx, cy = p.get("cx", 0.0), p.get("cy", 0.0)
        du, dv = u - cx, v - cy
        kind = p.get("type", "rect")
        if kind == "circle":
            return np.sqrt(du * du + dv * dv) - p.get("radius", 0.02)
        if kind == "polygon":
            verts = np.asarray(p.get("points", [[0, 0]]), dtype=float)
            return _poly_sdf_2d(np.stack([u, v], axis=1), verts)
        hw, hh = p.get("width", 0.05) / 2.0, p.get("height", 0.03) / 2.0
        qx, qy = np.abs(du) - hw, np.abs(dv) - hh
        return np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2) + np.minimum(np.maximum(qx, qy), 0.0)

    def __call__(self, points: np.ndarray) -> np.ndarray:
        u, v, w = self._local(points)
        slab = np.abs(w - self.offset) - self.depth / 2.0
        return np.maximum(self._profile_sdf(u, v), slab)

    def bounds(self) -> Bounds:
        p = self.profile
        cx, cy = p.get("cx", 0.0), p.get("cy", 0.0)
        kind = p.get("type", "rect")
        if kind == "circle":
            r = p.get("radius", 0.02)
            umin, umax, vmin, vmax = cx - r, cx + r, cy - r, cy + r
        elif kind == "polygon":
            verts = np.asarray(p.get("points", [[0, 0]]), dtype=float)
            umin, vmin = verts.min(axis=0)
            umax, vmax = verts.max(axis=0)
        else:
            hw, hh = p.get("width", 0.05) / 2.0, p.get("height", 0.03) / 2.0
            umin, umax, vmin, vmax = cx - hw, cx + hw, cy - hh, cy + hh
        lo, hi = self.offset - self.depth / 2.0, self.offset + self.depth / 2.0
        if self.normal == "z":
            return Bounds(np.array([umin, vmin, lo]), np.array([umax, vmax, hi]))
        if self.normal == "y":
            return Bounds(np.array([umin, lo, vmin]), np.array([umax, hi, vmax]))
        return Bounds(np.array([lo, umin, vmin]), np.array([hi, umax, vmax]))


def sdf_from_feature(feature) -> SDF:
    """Convert a feature-tree feature into an SDF expression."""
    from app.cad_kernel.ops.primitives import (
        BoxFeature,
        SphereFeature,
        CylinderFeature,
        ConeFeature,
    )
    from app.cad_kernel.ops.booleans import BooleanFeature
    from app.cad_kernel.ops.modifiers import (
        FilletFeature,
        ChamferFeature,
        ShellFeature,
        HoleFeature,
        PatternFeature,
        TransformFeature,
    )
    from app.cad_kernel.ops.generative import OptimizeFeature
    from app.cad_kernel.ops.sketch import ExtrudeFeature

    if isinstance(feature, BoxFeature):
        return BoxSDF(feature.size, feature.center)
    if isinstance(feature, SphereFeature):
        return SphereSDF(feature.radius, feature.center)
    if isinstance(feature, CylinderFeature):
        return CylinderSDF(feature.radius, feature.height, feature.center, feature.axis)
    if isinstance(feature, ConeFeature):
        return ConeSDF(feature.base_radius, feature.height, feature.center, feature.axis)
    if isinstance(feature, BooleanFeature):
        return BooleanSDF(feature.operation, [sdf_from_feature(c) for c in feature.children])
    if isinstance(feature, (FilletFeature, ChamferFeature)):
        return SmoothBooleanSDF(
            feature.operation,
            feature.radius,
            [sdf_from_feature(c) for c in feature.children],
            chamfer=isinstance(feature, ChamferFeature),
        )
    if isinstance(feature, ShellFeature):
        return ShellSDF(sdf_from_feature(feature.children[0]), feature.thickness)
    if isinstance(feature, HoleFeature):
        child = sdf_from_feature(feature.children[0])
        c = feature.center
        hole = CylinderSDF(feature.radius, feature.depth, c, feature.axis)
        return BooleanSDF("subtract", [child, hole])
    if isinstance(feature, TransformFeature):
        child = sdf_from_feature(feature.children[0])
        R = _euler_matrix(feature.rotate.x, feature.rotate.y, feature.rotate.z)
        t = np.array([feature.translate.x, feature.translate.y, feature.translate.z])
        return TransformSDF(child, t, R, feature.scale)
    if isinstance(feature, PatternFeature):
        child = sdf_from_feature(feature.children[0])
        copies = _pattern_transforms(child, feature)
        return BooleanSDF("union", copies)
    if isinstance(feature, ExtrudeFeature):
        return ExtrudeSDF(feature.profile, feature.plane, feature.offset, feature.depth)
    if isinstance(feature, OptimizeFeature):
        cache = feature.cache
        if cache and cache.get("density") is not None:
            origin = cache["origin"]
            size = cache["size"]
            return GridSDF(
                cache["density"],
                [origin["x"], origin["y"], origin["z"]],
                [size["x"], size["y"], size["z"]],
                cache.get("threshold", 0.5),
            )
        # Not solved yet — fall back to the un-optimized input body.
        if feature.children:
            return sdf_from_feature(feature.children[0])
        raise ValueError("OptimizeFeature has no cache and no input body")
    raise ValueError(f"Feature {type(feature).__name__} cannot be converted to SDF")


def _pattern_transforms(child: SDF, feature) -> List["SDF"]:
    """Build the transformed copies for a linear or circular pattern."""
    copies: List[SDF] = []
    count = max(1, int(feature.count))
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
            # rotate about center: world = R (p - center) + center  ->  translate = center - R@center
            t = center - R @ center
            copies.append(TransformSDF(child, t, R, 1.0))
    else:  # linear
        step = np.array([feature.axis.x, feature.axis.y, feature.axis.z]) * feature.spacing
        R = np.eye(3)
        for i in range(count):
            copies.append(TransformSDF(child, step * i, R, 1.0))
    return copies


def grid_dims(bounds: Bounds, resolution: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray, int, int, int]:
    """Grid framing shared by the numpy and native samplers (identical results)."""
    min_b, max_b = bounds.to_arrays()
    size = max_b - min_b
    scale = (resolution**3 / (size[0] * size[1] * size[2])) ** (1.0 / 3.0)
    dims = np.maximum(4, np.round(size * scale)).astype(int)
    return min_b, max_b, size, int(dims[0]), int(dims[1]), int(dims[2])


def mesh_from_field(values: np.ndarray, min_b, size, nx: int, ny: int, nz: int, level: float) -> Solid:
    """Marching-cubes a scalar field into a Solid (shared by both sampling paths)."""
    from skimage.measure import marching_cubes

    try:
        verts, faces, _, _ = marching_cubes(values, level=float(level))
    except Exception as exc:
        raise RuntimeError(f"SDF marching cubes failed: {exc}")

    world = np.zeros_like(verts)
    world[:, 0] = min_b[0] + verts[:, 0] * (size[0] / (nx - 1))
    world[:, 1] = min_b[1] + verts[:, 1] * (size[1] / (ny - 1))
    world[:, 2] = min_b[2] + verts[:, 2] * (size[2] / (nz - 1))

    mesh = FaceMesh(world, faces)
    face = Face(surface=None, mesh=mesh)
    return Solid(Shell([face]))


def evaluate_sdf(
    sdf: SDF,
    resolution: int = 128,
    margin: float = 0.05,
    level: float = 0.0,
) -> Solid:
    """Evaluate an SDF on a grid and return the zero-isosurface as a Solid."""
    min_b, max_b, size, nx, ny, nz = grid_dims(sdf.bounds().expanded(margin), resolution)
    x = np.linspace(min_b[0], max_b[0], nx)
    y = np.linspace(min_b[1], max_b[1], ny)
    z = np.linspace(min_b[2], max_b[2], nz)
    grid = np.stack(np.meshgrid(x, y, z, indexing="ij"), axis=-1).reshape(-1, 3)
    values = sdf(grid).reshape(nx, ny, nz)
    return mesh_from_field(values, min_b, size, nx, ny, nz, level)
