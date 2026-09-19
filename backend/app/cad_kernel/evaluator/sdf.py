"""Signed-distance-field evaluator for the Forge CAD kernel.

The kernel exposes a B-rep feature tree, but for robust boolean evaluation of
primitives we compose signed distance functions on a regular grid and extract
the zero-isosurface with marching cubes.  This avoids the robustness pitfalls
of self-written B-rep trimming/booleans while still producing a fully custom,
controllable geometry pipeline.
"""

from __future__ import annotations

import math
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


class ConvexPrismSDF(SDF):
    """The intersection of half-spaces — what a rib's trapezoidal prism is.

    Exact on the boundary, which is the only place marching cubes reads, and an
    underestimate of the true distance outside near a convex edge. That is the
    same trade `ConeSDF` above makes, and the zero set — the surface — is
    unaffected by it.
    """

    def __init__(self, planes, corners):
        self.points = np.asarray([p for p, _ in planes], dtype=float)
        self.normals = np.asarray([n for _, n in planes], dtype=float)
        self.offsets = np.einsum("ij,ij->i", self.points, self.normals)
        # Taken separately, not read off the planes: a plane's reference point
        # is *a* point on it, not an extreme of the solid, so bounding by them
        # would clip the far cap and mesh half a rib.
        self.corners = np.asarray(corners, dtype=float)

    def __call__(self, points: np.ndarray) -> np.ndarray:
        return (points @ self.normals.T - self.offsets).max(axis=1)

    def bounds(self) -> Bounds:
        return Bounds(self.corners.min(axis=0), self.corners.max(axis=0))


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


#: The most surface samples one voxelisation may generate. Each is a few dozen
#: bytes in flight, but they are produced in chunks of `_SAMPLE_CHUNK`, so the
#: bound is on time; memory stays at one chunk plus the grid.
MAX_SURFACE_SAMPLES = 40_000_000
_SAMPLE_CHUNK = 2_000_000


class MeshTooDetailed(ValueError):
    """Voxelising at this pitch would take more samples than the budget allows."""


def _barycentric(n: int) -> np.ndarray:
    """The (i/n, j/n, k/n) lattice on a triangle, n+1 points along each edge."""
    i, j = np.meshgrid(np.arange(n + 1), np.arange(n + 1), indexing="ij")
    keep = (i + j) <= n
    i, j = i[keep], j[keep]
    return np.stack([i, j, n - i - j], axis=1).astype(float) / n


def _surface_voxels(vertices: np.ndarray, faces: np.ndarray, pitch: float):
    """The voxels a closed mesh's surface passes through, and the grid's corner.

    Every triangle is sampled on a lattice spaced under half a pitch, and each
    sample marks the voxel it rounds into, which keeps the shell 6-connected so
    the fill cannot leak — the same rule as trimesh's `voxelized`. Two things
    differ, and the second is why this exists.

    Only triangles longer than half a pitch are sampled at all: an imported
    motor meshes to 640k triangles almost all of which are already far smaller
    than a voxel, and trimesh spent seventy seconds re-examining them.

    **Memory is bounded, not proportional to the mesh.** The version before this
    subdivided the big triangles all at once, and the motor's long thin spline
    triangles asked for millions of points each — 15 GB, an OOM kill, and the
    WSL distro it was running in torn down with it. Samples are now generated in
    chunks and written straight into the grid, and a mesh that would need more
    than `MAX_SURFACE_SAMPLES` raises `MeshTooDetailed` so the caller can choose
    a coarser pitch rather than the machine choosing for it.
    """
    max_edge = pitch / 2.0
    lo = np.round(vertices.min(axis=0) / pitch).astype(np.int64)
    hi = np.round(vertices.max(axis=0) / pitch).astype(np.int64)
    shell = np.zeros(hi - lo + 1, dtype=bool)

    def mark(points: np.ndarray) -> None:
        hit = np.round(points / pitch).astype(np.int64) - lo
        np.clip(hit, 0, np.array(shell.shape) - 1, out=hit)
        shell[hit[:, 0], hit[:, 1], hit[:, 2]] = True

    mark(vertices)
    tri = vertices[faces]
    longest = np.max(np.linalg.norm(tri - np.roll(tri, 1, axis=1), axis=2), axis=1)
    steps = np.ceil(longest / max_edge).astype(np.int64)
    big = steps > 1
    if big.any():
        steps_big = steps[big]
        needed = int(((steps_big + 1) * (steps_big + 2) // 2).sum())
        if needed > MAX_SURFACE_SAMPLES:
            raise MeshTooDetailed(
                f"{needed:,} surface samples at a {pitch * 1000:.3f} mm pitch "
                f"(budget {MAX_SURFACE_SAMPLES:,})")
        tri_big = tri[big]
        for n in np.unique(steps_big):
            weights = _barycentric(int(n))
            group = tri_big[steps_big == n]
            per_chunk = max(1, _SAMPLE_CHUNK // len(weights))
            for start in range(0, len(group), per_chunk):
                chunk = group[start:start + per_chunk]
                mark(np.einsum("kv,tvc->tkc", weights, chunk).reshape(-1, 3))
    # Voxel i is centred on (lo + i) * pitch; the grid's corner is half a cell
    # below the first centre.
    return shell, (lo - 0.5) * pitch


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

    #: How far inside the true surface a voxelised mesh's zero level sits, in
    #: cells, measured over randomly rotated and offset boxes and cylinders:
    #: -0.63 on average, between -0.93 and -0.06. `fill()` keeps every voxel the
    #: surface crosses, so the body always comes back fat.
    MESH_BIAS_CELLS = 0.63
    #: What is left after the bias is taken off: surface points then read
    #: between -0.38 and +0.61 cells over the same set. The band a reading from
    #: `from_mesh` can be trusted to, in either direction.
    MESH_TOLERANCE_CELLS = 0.65

    @classmethod
    def from_mesh(cls, vertices, faces, pitch: float, pad_cells: int = 2) -> "GridSDF":
        """A signed distance field for a closed triangle mesh, by voxelising it.

        For a body with no analytic field — an imported STEP solid, an exact edge
        fillet. Two things differ from the density-grid constructor, and both
        were wrong in the first version of this, which lived in `drawing`:

        * **The grid is padded.** Voxelising leaves the body touching the grid
          boundary on every side, so the complement has no voxels there and the
          distance transform of the inside has nothing to measure to — the
          surface read 31 mm deep on a 40 mm box.
        * **The distance is half-cell centred and de-biased.** The density
          constructor's `d_out - d_in` jumps by two cells across one cell at the
          surface; `d - 0.5` is continuous with unit slope, and the measured
          dilation (`MESH_BIAS_CELLS`) is subtracted, so a coincident face reads
          near zero rather than as half a cell of penetration.
        """
        from scipy.ndimage import binary_fill_holes, distance_transform_edt

        pitch = float(pitch)
        pad = max(int(pad_cells), 2)
        shell, corner = _surface_voxels(np.asarray(vertices, dtype=float),
                                        np.asarray(faces, dtype=np.int64), pitch)
        occ = np.pad(binary_fill_holes(shell), pad)
        if occ.any():
            vol = np.where(occ, -(distance_transform_edt(occ) - 0.5),
                           distance_transform_edt(~occ) - 0.5)
        else:
            vol = np.full(occ.shape, 1e6)
        field = cls.__new__(cls)
        field.density = occ.astype(float)
        field.dims = np.array(occ.shape)
        field.cell = np.full(3, float(pitch))
        field.mean_cell = float(pitch)
        field.size = field.dims * float(pitch)
        field._lo = corner - pad * pitch
        field.center = field._lo + field.size / 2.0
        field.sdf_vol = (vol + cls.MESH_BIAS_CELLS) * float(pitch)
        field.tolerance = cls.MESH_TOLERANCE_CELLS * float(pitch)
        return field


# Building a GridSDF runs a full EDT over the voxel grid (expensive at 160³),
# and an ImportFeature's grid never changes between edits to the rest of the
# tree — cache a few recent grids keyed on the packed-density payload.
from collections import OrderedDict  # noqa: E402

_GRID_SDF_CACHE: "OrderedDict[tuple, GridSDF]" = OrderedDict()
_GRID_SDF_CACHE_MAX = 4


def _cached_import_grid_sdf(cache: dict) -> "GridSDF":
    origin = cache["origin"]
    size = cache["size"]
    key = (
        cache["density_packed"],
        tuple(cache.get("dims") or ()),
        origin["x"], origin["y"], origin["z"],
        size["x"], size["y"], size["z"],
        cache.get("threshold", 0.5),
    )
    hit = _GRID_SDF_CACHE.get(key)
    if hit is not None:
        _GRID_SDF_CACHE.move_to_end(key)
        return hit
    from app.services.import_service import unpack_density

    sdf = GridSDF(
        unpack_density(cache),
        [origin["x"], origin["y"], origin["z"]],
        [size["x"], size["y"], size["z"]],
        cache.get("threshold", 0.5),
    )
    _GRID_SDF_CACHE[key] = sdf
    while len(_GRID_SDF_CACHE) > _GRID_SDF_CACHE_MAX:
        _GRID_SDF_CACHE.popitem(last=False)
    return sdf


from app.cad_kernel.core.planes import PLANE_NORMAL as _PLANE_NORMAL  # noqa: E402  (re-exported for sdf_native)


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


# A 3D sample grid asks the same 2D question over and over: an extrude at
# resolution 128 evaluates its profile at 128^3 points, of which only 128^2 are
# distinct (u, v) pairs — every one is repeated once per slice up the extrusion.
# For a rectangle that repetition is free; for a 300-curve traced outline it is
# the whole cost of the document. `_grid_plan` spots it and evaluates the
# distinct pairs.
#
# The probe is what keeps this from being a tax on the cases it cannot help.
# `RevolveSDF` passes a *radius*, which is different at nearly every point, and
# a full `np.unique` over two million of those would cost half a second to
# learn nothing. Counting the distinct values in a strided sample answers the
# same question for a thousandth of the work.
_DEDUP_MIN_POINTS = 65_536
_DEDUP_PROBE = 4096


def _grid_plan(u: np.ndarray, v: np.ndarray):
    """``(uu, ui, vv, vi)`` when (u, v) is a small product set, else ``None``."""
    n = u.size
    if n < _DEDUP_MIN_POINTS:
        return None
    stride = max(1, n // _DEDUP_PROBE)
    if np.unique(u[::stride]).size * np.unique(v[::stride]).size > 16 * _DEDUP_PROBE:
        return None
    uu, ui = np.unique(u, return_inverse=True)
    vv, vi = np.unique(v, return_inverse=True)
    if uu.size * vv.size * 4 > n:
        return None
    return uu, ui.reshape(-1), vv, vi.reshape(-1)


def profile_sdf_2d(profile: dict, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Signed distance to a 2D profile at points (u, v).

    ``rect`` / ``circle`` / ``polygon`` are the original shapes and mean exactly
    what they always did — persisted documents carry them. ``loops`` is the
    sketch profile that keeps arcs as arcs and supports holes; the geometry for
    it lives in `geom.profile2d` so the B-rep path reads the same definition.
    """
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    plan = _grid_plan(u.reshape(-1), v.reshape(-1))
    if plan is None:
        return _profile_sdf_at(profile, u, v)
    uu, ui, vv, vi = plan
    field = _profile_sdf_at(profile,
                            np.repeat(uu, vv.size), np.tile(vv, uu.size))
    return field.reshape(uu.size, vv.size)[ui, vi].reshape(u.shape)


def _profile_sdf_at(profile: dict, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    from app.cad_kernel.geom import profile2d

    p = profile or {}
    if profile2d.is_loops(p):
        return profile2d.loops_sdf(p, u, v)
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


class SheetMetalSDF(SDF):
    """A flat base flange plus bent edge flanges, all at one thickness.

    Each flange is two pieces welded to the base: a circular *bend* and a
    straight *leg* tangent to the end of that bend. Both are solved in the 2D
    plane normal to the bend line and then extruded across the part width, so
    the whole thing stays an exact distance field that composes with booleans,
    fillets and shells like any other body.

    Geometry of one bend, for a flange on the +X edge bending up:
    the base plate occupies ``z in [0, t]`` and ends at ``x = x_e``. The centre
    of curvature sits at ``(x_e, t + r)`` — directly above the plate end — so
    the plate's top surface is the inside of the bend (radius ``r``) and its
    bottom surface the outside (radius ``r + t``). Sweeping from straight-down
    through the bend angle carries the material round to the leg.
    """

    def __init__(self, feature):
        self.t = float(feature.thickness)
        self.length = float(feature.length)
        self.width = float(feature.width)
        self.flanges = feature.normalized_flanges()

    # -- 2D helpers, all operating on (a, z) where `a` is the in-plane axis ---

    @staticmethod
    def _box2(a, z, a_min, a_max, z_min, z_max):
        ca, cz = (a_min + a_max) / 2.0, (z_min + z_max) / 2.0
        ha, hz = (a_max - a_min) / 2.0, (z_max - z_min) / 2.0
        qa, qz = np.abs(a - ca) - ha, np.abs(z - cz) - hz
        return (np.sqrt(np.maximum(qa, 0.0) ** 2 + np.maximum(qz, 0.0) ** 2)
                + np.minimum(np.maximum(qa, qz), 0.0))

    def _bend2(self, a, z, ca, cz, r_in, sweep, out_sign, up_sign):
        """Annular sector: radii [r_in, r_in+t], swept `sweep` radians."""
        qa = (a - ca) * out_sign
        qz = (z - cz) * up_sign
        rho = np.sqrt(qa * qa + qz * qz)
        # Distance to the annulus wall.
        mid = r_in + self.t / 2.0
        d = np.abs(rho - mid) - self.t / 2.0
        # Restrict to the sector: it starts pointing straight "down" (-qz) and
        # rotates toward +qa. For sweeps up to 180 deg that is the intersection
        # of two half planes through the centre.
        d = np.maximum(d, -qa)
        n_a, n_z = -math.cos(sweep), -math.sin(sweep)
        d = np.maximum(d, -(qa * n_a + qz * n_z))
        return d

    def _leg2(self, a, z, ca, cz, r_in, sweep, leg_len, out_sign, up_sign):
        """Straight leg tangent to the end of the bend."""
        qa = (a - ca) * out_sign
        qz = (z - cz) * up_sign
        # Frame at the end of the bend: radial points outward from the centre,
        # tangent is the direction the sheet continues travelling.
        rad_a, rad_z = math.sin(sweep), -math.cos(sweep)
        tan_a, tan_z = math.cos(sweep), math.sin(sweep)
        s = qa * tan_a + qz * tan_z          # along the leg
        rdist = qa * rad_a + qz * rad_z      # across the thickness
        qs = np.abs(s - leg_len / 2.0) - leg_len / 2.0
        qr = np.abs(rdist - (r_in + self.t / 2.0)) - self.t / 2.0
        return (np.sqrt(np.maximum(qs, 0.0) ** 2 + np.maximum(qr, 0.0) ** 2)
                + np.minimum(np.maximum(qs, qr), 0.0))

    def __call__(self, points: np.ndarray) -> np.ndarray:
        x, y, z = points[:, 0], points[:, 1], points[:, 2]
        hx, hy = self.length / 2.0, self.width / 2.0

        # Base flange.
        base = self._box2(x, z, -hx, hx, 0.0, self.t)
        base = np.maximum(base, np.abs(y) - hy)
        d = base

        for f in self.flanges:
            axis, sign = f["edge"][0], f["edge"][1]
            out_sign = 1.0 if sign == "+" else -1.0
            up_sign = 1.0 if f["angle"] > 0 else -1.0
            sweep = math.radians(abs(f["angle"]))
            r_in = f["radius"]

            if axis == "x":
                a, across, edge, half_across = x, y, hx, hy
            else:
                a, across, edge, half_across = y, x, hy, hx

            ca = edge * out_sign
            cz = self.t + r_in if up_sign > 0 else -r_in

            bend = self._bend2(a, z, ca, cz, r_in, sweep, out_sign, up_sign)
            leg = self._leg2(a, z, ca, cz, r_in, sweep, f["length"], out_sign, up_sign)
            piece = np.minimum(bend, leg)
            # Flanges span the full width of the edge they sit on.
            piece = np.maximum(piece, np.abs(across) - half_across)
            d = np.minimum(d, piece)

        return d

    def bounds(self) -> Bounds:
        hx, hy = self.length / 2.0, self.width / 2.0
        lo = np.array([-hx, -hy, 0.0])
        hi = np.array([hx, hy, self.t])
        for f in self.flanges:
            axis, sign = f["edge"][0], f["edge"][1]
            out_sign = 1.0 if sign == "+" else -1.0
            up_sign = 1.0 if f["angle"] > 0 else -1.0
            sweep = math.radians(abs(f["angle"]))
            r_out = f["radius"] + self.t
            edge = hx if axis == "x" else hy
            ca = edge * out_sign
            cz = self.t + f["radius"] if up_sign > 0 else -f["radius"]

            # Furthest reach: end of the leg, plus the bend's own swept extent.
            rad_a, rad_z = math.sin(sweep), -math.cos(sweep)
            tan_a, tan_z = math.cos(sweep), math.sin(sweep)
            tip_a = ca + out_sign * (rad_a * r_out + tan_a * f["length"])
            tip_z = cz + up_sign * (rad_z * r_out + tan_z * f["length"])
            arc_a = ca + out_sign * r_out * max(math.sin(sweep), 0.0)
            arc_z = cz + up_sign * r_out * max(-math.cos(sweep), -1.0)

            idx = 0 if axis == "x" else 1
            for va in (tip_a, arc_a, ca):
                lo[idx] = min(lo[idx], va)
                hi[idx] = max(hi[idx], va)
            for vz in (tip_z, arc_z, cz):
                lo[2] = min(lo[2], vz)
                hi[2] = max(hi[2], vz)
        return Bounds(lo, hi)


class ExtrudeSDF(SDF):
    """A 2D profile (rect/circle/polygon) on a base plane, extruded to a depth.

    ``plane_origin`` shifts the profile's in-plane origin so a sketch placed on a
    feature's face is centered on that face rather than the world origin.
    """

    def __init__(self, profile: dict, plane: str, offset: float, depth: float,
                 plane_origin=None):
        self.profile = profile or {}
        self.normal = _PLANE_NORMAL.get(plane, "z")
        self.offset = float(offset)
        self.depth = float(depth)
        po = plane_origin or (0.0, 0.0)
        self.u0, self.v0 = float(po[0]), float(po[1])

    def _local(self, points: np.ndarray):
        if self.normal == "z":
            return points[:, 0], points[:, 1], points[:, 2]
        if self.normal == "y":
            return points[:, 0], points[:, 2], points[:, 1]
        return points[:, 1], points[:, 2], points[:, 0]

    def _profile_sdf(self, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        # Profile coords are relative to the plane origin (face centre on-face).
        return profile_sdf_2d(self.profile, u - self.u0, v - self.v0)

    def __call__(self, points: np.ndarray) -> np.ndarray:
        u, v, w = self._local(points)
        slab = np.abs(w - self.offset) - self.depth / 2.0
        return np.maximum(self._profile_sdf(u, v), slab)

    def bounds(self) -> Bounds:
        from app.cad_kernel.geom import profile2d

        p = self.profile
        cx, cy = p.get("cx", 0.0), p.get("cy", 0.0)
        kind = p.get("type", "rect")
        if profile2d.is_loops(p):
            umin, vmin, umax, vmax = profile2d.loops_bounds(p)
        elif kind == "circle":
            r = p.get("radius", 0.02)
            umin, umax, vmin, vmax = cx - r, cx + r, cy - r, cy + r
        elif kind == "polygon":
            verts = np.asarray(p.get("points", [[0, 0]]), dtype=float)
            umin, vmin = verts.min(axis=0)
            umax, vmax = verts.max(axis=0)
        else:
            hw, hh = p.get("width", 0.05) / 2.0, p.get("height", 0.03) / 2.0
            umin, umax, vmin, vmax = cx - hw, cx + hw, cy - hh, cy + hh
        # Shift by the plane origin (profile coords are relative to it).
        umin += self.u0; umax += self.u0; vmin += self.v0; vmax += self.v0
        lo, hi = self.offset - self.depth / 2.0, self.offset + self.depth / 2.0
        if self.normal == "z":
            return Bounds(np.array([umin, vmin, lo]), np.array([umax, vmax, hi]))
        if self.normal == "y":
            return Bounds(np.array([umin, lo, vmin]), np.array([umax, hi, vmax]))
        return Bounds(np.array([lo, umin, vmin]), np.array([hi, umax, vmax]))


# axis name -> the world index it points along, and the two in-plane indices.
_REV_AXES = {"x": (0, 1, 2), "y": (1, 0, 2), "z": (2, 0, 1)}


class RevolveSDF(SDF):
    """A 2D profile (in radius/height) revolved about an axis through a centre."""

    def __init__(self, profile: dict, axis: str, center, angle_deg: float):
        self.profile = profile or {}
        self.ai, self.bi, self.ci = _REV_AXES.get(axis, _REV_AXES["z"])
        self.center = np.array([center.x, center.y, center.z], dtype=float)
        self.angle = float(angle_deg)

    def _profile_extent(self):
        from app.cad_kernel.geom import profile2d

        p = self.profile
        cx, cy = p.get("cx", 0.0), p.get("cy", 0.0)
        kind = p.get("type", "rect")
        if profile2d.is_loops(p):
            _, vmin, umax, vmax = profile2d.loops_bounds(p)
            return umax, vmin, vmax
        if kind == "circle":
            r = p.get("radius", 0.02)
            return cx + r, cy - r, cy + r
        if kind == "polygon":
            verts = np.asarray(p.get("points", [[0, 0]]), dtype=float)
            return float(verts[:, 0].max()), float(verts[:, 1].min()), float(verts[:, 1].max())
        hw, hh = p.get("width", 0.04) / 2.0, p.get("height", 0.03) / 2.0
        return cx + hw, cy - hh, cy + hh

    def __call__(self, points: np.ndarray) -> np.ndarray:
        rel = points - self.center
        a = rel[:, self.ai]              # along the revolve axis (height)
        b = rel[:, self.bi]
        c = rel[:, self.ci]
        r = np.sqrt(b * b + c * c)       # radius from the axis
        d = profile_sdf_2d(self.profile, r, a)
        if self.angle < 359.999:
            # Intersect with an angular wedge [0, angle] about the b-axis.
            ang = np.degrees(np.arctan2(c, b)) % 360.0
            half = self.angle / 2.0
            centered = np.abs(((ang - half + 180.0) % 360.0) - 180.0)
            wedge = centered - half      # <=0 inside the wedge
            d = np.maximum(d, wedge * (r + 1e-6) * np.pi / 180.0)
        return d

    def bounds(self) -> Bounds:
        rmax, hmin, hmax = self._profile_extent()
        rmax = abs(rmax)
        lo = self.center.copy()
        hi = self.center.copy()
        lo[self.ai] += hmin; hi[self.ai] += hmax
        for idx in (self.bi, self.ci):
            lo[idx] -= rmax; hi[idx] += rmax
        return Bounds(lo, hi)


class MirrorSDF(SDF):
    """Union of a child SDF with its reflection across an axis-aligned plane."""

    def __init__(self, child: SDF, plane: str, offset: float):
        self.child = child
        self.normal = _PLANE_NORMAL.get(plane, "x")
        self.axis = {"x": 0, "y": 1, "z": 2}[self.normal]
        self.offset = float(offset)

    def __call__(self, points: np.ndarray) -> np.ndarray:
        reflected = points.copy()
        reflected[:, self.axis] = 2.0 * self.offset - reflected[:, self.axis]
        return np.minimum(self.child(points), self.child(reflected))

    def bounds(self) -> Bounds:
        b = self.child.bounds()
        lo = b.min.copy(); hi = b.max.copy()
        # Reflect the bbox extents across the plane and take the union.
        rlo = 2 * self.offset - hi[self.axis]
        rhi = 2 * self.offset - lo[self.axis]
        lo[self.axis] = min(lo[self.axis], rlo)
        hi[self.axis] = max(hi[self.axis], rhi)
        return Bounds(lo, hi)


class SweepSDF(SDF):
    """A tube of a given radius swept along a polyline path (union of capsules)."""

    def __init__(self, path, radius: float):
        self.pts = np.asarray(path, dtype=float)
        self.radius = float(radius)

    def __call__(self, points: np.ndarray) -> np.ndarray:
        best = np.full(len(points), 1e9)
        for i in range(len(self.pts) - 1):
            a = self.pts[i]; b = self.pts[i + 1]
            ab = b - a
            denom = float(ab @ ab) or 1e-12
            t = np.clip(((points - a) @ ab) / denom, 0.0, 1.0)
            proj = a + t[:, None] * ab
            dist = np.linalg.norm(points - proj, axis=1) - self.radius
            best = np.minimum(best, dist)
        return best

    def bounds(self) -> Bounds:
        lo = self.pts.min(axis=0) - self.radius
        hi = self.pts.max(axis=0) + self.radius
        return Bounds(lo, hi)


class LoftSDF(SDF):
    """Linear blend between two profiles along a plane normal (morphing slab)."""

    def __init__(self, profile_a: dict, profile_b: dict, plane: str, offset_a: float, offset_b: float):
        self.pa = profile_a or {}
        self.pb = profile_b or {}
        self.normal = _PLANE_NORMAL.get(plane, "z")
        self.o0 = float(min(offset_a, offset_b))
        self.o1 = float(max(offset_a, offset_b))
        if offset_a > offset_b:
            self.pa, self.pb = self.pb, self.pa

    def _local(self, points: np.ndarray):
        if self.normal == "z":
            return points[:, 0], points[:, 1], points[:, 2]
        if self.normal == "y":
            return points[:, 0], points[:, 2], points[:, 1]
        return points[:, 1], points[:, 2], points[:, 0]

    def __call__(self, points: np.ndarray) -> np.ndarray:
        u, v, w = self._local(points)
        span = max(self.o1 - self.o0, 1e-9)
        t = np.clip((w - self.o0) / span, 0.0, 1.0)
        blended = (1 - t) * profile_sdf_2d(self.pa, u, v) + t * profile_sdf_2d(self.pb, u, v)
        slab = np.abs(w - (self.o0 + self.o1) / 2.0) - span / 2.0
        return np.maximum(blended, slab)

    def bounds(self) -> Bounds:
        from app.cad_kernel.geom import profile2d

        ext = []
        for p in (self.pa, self.pb):
            cx, cy = p.get("cx", 0.0), p.get("cy", 0.0)
            kind = p.get("type", "rect")
            if profile2d.is_loops(p):
                umin, vmin, umax, vmax = profile2d.loops_bounds(p)
                ext.append((umin, umax, vmin, vmax))
            elif kind == "circle":
                r = p.get("radius", 0.02); ext.append((cx - r, cx + r, cy - r, cy + r))
            elif kind == "polygon":
                verts = np.asarray(p.get("points", [[0, 0]]), dtype=float)
                ext.append((verts[:, 0].min(), verts[:, 0].max(), verts[:, 1].min(), verts[:, 1].max()))
            else:
                hw, hh = p.get("width", 0.04) / 2.0, p.get("height", 0.03) / 2.0
                ext.append((cx - hw, cx + hw, cy - hh, cy + hh))
        umin = min(e[0] for e in ext); umax = max(e[1] for e in ext)
        vmin = min(e[2] for e in ext); vmax = max(e[3] for e in ext)
        if self.normal == "z":
            return Bounds(np.array([umin, vmin, self.o0]), np.array([umax, vmax, self.o1]))
        if self.normal == "y":
            return Bounds(np.array([umin, self.o0, vmin]), np.array([umax, self.o1, vmax]))
        return Bounds(np.array([self.o0, umin, vmin]), np.array([self.o1, umax, vmax]))


def _sdf_box(f):
    return BoxSDF(f.size, f.center)


def _sdf_sphere(f):
    return SphereSDF(f.radius, f.center)


def _sdf_cylinder(f):
    return CylinderSDF(f.radius, f.height, f.center, f.axis)


def _sdf_cone(f):
    return ConeSDF(f.base_radius, f.height, f.center, f.axis)


def _sdf_boolean(f):
    return BooleanSDF(f.operation, [sdf_from_feature(c) for c in f.children])


def _sdf_smooth_boolean(f):
    return SmoothBooleanSDF(
        f.operation,
        f.radius,
        [sdf_from_feature(c) for c in f.children],
        chamfer=type(f).__name__ == "ChamferFeature",
    )


def _sdf_shell(f):
    return ShellSDF(sdf_from_feature(f.children[0]), f.thickness)


def _sdf_rib(f):
    from app.cad_kernel.geom import ribs

    prism = ribs.prism_for(f)
    sweep = np.asarray(prism.sweep, dtype=float)
    section = np.asarray(prism.section, dtype=float)
    corners = np.vstack([section, section + sweep])
    rib = ConvexPrismSDF(ribs.half_spaces(prism), corners)
    return BooleanSDF("union", [sdf_from_feature(f.children[0]), rib])


def _sdf_hole(f):
    from app.cad_kernel.geom import holes

    parts = []
    for tool in holes.tools_for(f):
        center = Point3(*tool.center)
        axis = Vector3(*tool.axis)
        if isinstance(tool, holes.Cyl):
            parts.append(CylinderSDF(tool.radius, tool.height, center, axis))
        else:
            parts.append(ConeSDF(tool.base_radius, tool.height, center, axis))
    tool_sdf = parts[0] if len(parts) == 1 else BooleanSDF("union", parts)
    return BooleanSDF("subtract", [sdf_from_feature(f.children[0]), tool_sdf])


def _sdf_transform(f):
    R = _euler_matrix(f.rotate.x, f.rotate.y, f.rotate.z)
    t = np.array([f.translate.x, f.translate.y, f.translate.z])
    return TransformSDF(sdf_from_feature(f.children[0]), t, R, f.scale)


def _sdf_pattern(f):
    child = sdf_from_feature(f.children[0])
    return BooleanSDF("union", _pattern_transforms(child, f))


def _sdf_extrude(f):
    return ExtrudeSDF(f.profile, f.plane, f.offset, f.depth,
                      plane_origin=getattr(f, "plane_origin", None))


def _sdf_revolve(f):
    return RevolveSDF(f.profile, f.axis, f.center, f.angle)


def _sdf_mirror(f):
    return MirrorSDF(sdf_from_feature(f.children[0]), f.plane, f.offset)


def _sdf_sweep(f):
    return SweepSDF(f.path, f.radius)


def _sdf_loft(f):
    return LoftSDF(f.profile_a, f.profile_b, f.plane, f.offset_a, f.offset_b)


def _sdf_optimize(f):
    cache = f.cache
    if cache and cache.get("density") is not None:
        origin, size = cache["origin"], cache["size"]
        return GridSDF(
            cache["density"],
            [origin["x"], origin["y"], origin["z"]],
            [size["x"], size["y"], size["z"]],
            cache.get("threshold", 0.5),
        )
    # Not solved yet — fall back to the un-optimized input body.
    if f.children:
        return sdf_from_feature(f.children[0])
    raise ValueError("OptimizeFeature has no cache and no input body")


def _sdf_needs_exact_kernel(f):
    raise ValueError(
        f"{type(f).__name__} acts on named faces/edges, which the distance-field "
        f"kernel does not have. It needs the exact B-rep kernel "
        f"(pip install cadquery-ocp)."
    )


def _sdf_sketch(f):
    return f.evaluate()          # raises SketchNotConsumed, naming the sketch


def _sdf_datum_plane(f):
    return f.evaluate()          # raises DatumHasNoBody, naming the plane


def _sdf_import(f):
    if f.cache and f.cache.get("density_packed"):
        return _cached_import_grid_sdf(f.cache)
    raise ValueError("ImportFeature has no voxel cache")


# Feature type name -> builder. Keyed by name rather than class so this table
# needs no imports and cannot drift from the feature registry; adding a feature
# means adding one line here.
def _sdf_derived(f) -> SDF:
    from app.cad_kernel.ops.derived import DerivedNotResolved

    raise DerivedNotResolved(
        f"derived feature {getattr(f, 'id', '?')} reached the evaluator — it "
        f"names geometry in document {getattr(f, 'document_id', '?')!r} and is "
        f"resolved into that document's own features before evaluation")


_SDF_BUILDERS = {
    "BoxFeature": _sdf_box,
    "SphereFeature": _sdf_sphere,
    "CylinderFeature": _sdf_cylinder,
    "ConeFeature": _sdf_cone,
    "BooleanFeature": _sdf_boolean,
    "FilletFeature": _sdf_smooth_boolean,
    "ChamferFeature": _sdf_smooth_boolean,
    "ShellFeature": _sdf_shell,
    "HoleFeature": _sdf_hole,
    "RibFeature": _sdf_rib,
    "TransformFeature": _sdf_transform,
    "PatternFeature": _sdf_pattern,
    "ExtrudeFeature": _sdf_extrude,
    "RevolveFeature": _sdf_revolve,
    "MirrorFeature": _sdf_mirror,
    "SweepFeature": _sdf_sweep,
    "LoftFeature": _sdf_loft,
    "SheetMetalFeature": SheetMetalSDF,
    "OptimizeFeature": _sdf_optimize,
    "ImportFeature": _sdf_import,
    # Topology-addressing features have no field form at all — they act on named
    # faces and edges, which a sampled field does not have. Registered so the
    # registry stays complete and the failure names the cause, rather than
    # arriving as a bare KeyError from deep inside evaluation.
    "EdgeFilletFeature": _sdf_needs_exact_kernel,
    "EdgeChamferFeature": _sdf_needs_exact_kernel,
    # Draft rotates named faces about a neutral plane. A sampled field has no
    # faces, and the nearest field-side approximation — tapering the whole body
    # — is a different solid that `dfm_topology` would then measure and report a
    # draft angle the exact kernel does not agree with.
    "DraftFeature": _sdf_needs_exact_kernel,
    # The distance to a helix has no closed form. The usual field-side trick
    # unwraps the point into helical coordinates and drops the tangential term,
    # which is an approximation that is worst exactly where the pitch matters.
    "HelixFeature": _sdf_needs_exact_kernel,
    # A thread is a helical surface, and the field-side approximation drops the
    # tangential term exactly where the pitch is the interesting part.
    "ThreadFeature": _sdf_needs_exact_kernel,
    # A sheet has no field form at all, and that is not a gap in this table. A
    # signed distance field has a sign everywhere, so it always bounds a volume;
    # a surface with no inside is exactly what it cannot describe.
    "SurfaceFeature": _sdf_needs_exact_kernel,
    # Sewing, offsetting and trimming a surface are all questions about faces
    # and edges, which a sampled field does not have — the same reason the edge
    # treatments are here.
    "KnitFeature": _sdf_needs_exact_kernel,
    "ThickenFeature": _sdf_needs_exact_kernel,
    "TrimFeature": _sdf_needs_exact_kernel,
    "FaceShellFeature": _sdf_needs_exact_kernel,
    "BrepBodyFeature": _sdf_needs_exact_kernel,
    "RuledLoftFeature": _sdf_needs_exact_kernel,
    # A sketch is geometry other features consume; `feature_ops.resolve_sketches`
    # substitutes it and takes it out of the tree before anything evaluates.
    # Registered so reaching here says that, rather than producing a plausible
    # empty solid that would quietly delete the body it was meant to describe.
    "SketchFeature": _sdf_sketch,
    # A datum plane is a reference, not material — pruned by the same stage that
    # removes sketches. Registered for the same reason: reaching here means the
    # pruning was missed, and saying so beats returning an empty solid that
    # would delete whatever the plane sits beside.
    "DatumPlaneFeature": _sdf_datum_plane,
    # Same contract: a coordinate frame, pruned before evaluation, and this
    # entry exists to name it if the pruning is ever missed.
    "MateConnectorFeature": _sdf_datum_plane,
    # A derived reference names geometry in *another* document, which
    # `services/derived_service.resolve` splices in as ordinary features before
    # anything evaluates. Same reason as the three above: reaching here means
    # the resolution was skipped, and a builder that invented a body would hide
    # that behind a part which is quietly not the one that was asked for.
    "DerivedFeature": _sdf_derived,
}


def sdf_from_feature(feature) -> SDF:
    """Convert a feature-tree feature into an SDF expression."""
    builder = _SDF_BUILDERS.get(type(feature).__name__)
    if builder is None:
        raise ValueError(
            f"Feature {type(feature).__name__} cannot be converted to SDF — "
            f"add it to _SDF_BUILDERS in evaluator/sdf.py"
        )
    return builder(feature)


def _pattern_transforms(child: SDF, feature) -> List["SDF"]:
    """The transformed copies of a pattern, placed by `geom/patterns`.

    The arithmetic used to live here *and* in `evaluator/brep._pattern`, whose
    comment about matching this one's choice of dominant axis is the comment you
    write when two copies have already disagreed once.
    """
    from app.cad_kernel.geom import patterns

    return [TransformSDF(child, translate, rotation, 1.0)
            for rotation, translate in patterns.placements(feature)]


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
