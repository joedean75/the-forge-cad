"""Loops of typed 2D curves — the profile representation both kernels read.

A sketch used to be flattened to a ring of points before either kernel saw it.
That made the sketch the one path in the system that *downgrades* geometry on
its way in: an arc became 24 straight segments, so a sketched bore arrived as 24
planar faces, and everything here that counts cylindrical faces — the ``hole``
spec requirement, DFM's smallest bore, `naming`'s face ids, analytic STEP — quietly
stopped seeing it.

A profile is now ``{"type": "loops", "loops": [{"curves": [...]}, ...]}`` where a
curve is one of

    {"kind": "line",   "a": [u, v], "b": [u, v]}
    {"kind": "arc",    "a": [u, v], "b": [u, v], "center": [u, v], "ccw": bool}
    {"kind": "circle", "center": [u, v], "r": float}

**Containment is decided here and only here.** `evaluator/sdf` and
`evaluator/brep` both call into this module for it, because a loop that the
distance field treats as a hole and OpenCascade treats as a second boundary is a
document that means two different things depending on which kernel ran — the
same failure the `LoftFeature` note in CLAUDE.md exists to prevent.

The rule is **even-odd**, matching SVG, DXF and every drafting tool: a point is
inside when an odd number of loops contain it. Nesting therefore works to any
depth without a special case, and a loop's winding direction carries no meaning —
which is what you want, because the direction a user happens to draw in does not
mean anything either.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np


def is_loops(profile: dict) -> bool:
    return bool(profile) and profile.get("type") == "loops"


def loops_of(profile: dict) -> List[dict]:
    return list((profile or {}).get("loops") or [])


# ----------------------------------------------------------------- angles --

def sweep(center, start, end, ccw: bool) -> Tuple[float, float]:
    """``(a0, da)`` for an arc — the same convention `_arc_points` tessellates."""
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    da = a1 - a0
    if ccw:
        while da <= 1e-12:
            da += 2 * math.pi
    else:
        while da >= -1e-12:
            da -= 2 * math.pi
    return a0, da


def _within_sweep(angle: np.ndarray, a0: float, da: float) -> np.ndarray:
    """Whether each angle lies on the arc running ``da`` from ``a0``."""
    delta = (angle - a0) % (2 * math.pi)
    if da >= 0:
        return delta <= da + 1e-12
    return (delta - 2 * math.pi) >= da - 1e-12


def arc_radius(curve: dict) -> float:
    """The arc's radius, averaged over its two endpoints.

    A sketch that solved to a residual rather than to zero has endpoints at
    slightly different radii; averaging is what `_arc_points` already does when
    it tessellates, so the two agree.
    """
    centre, start, end = curve["center"], curve["a"], curve["b"]
    r0 = math.hypot(start[0] - centre[0], start[1] - centre[1])
    r1 = math.hypot(end[0] - centre[0], end[1] - centre[1])
    return (r0 + r1) / 2.0


# --------------------------------------------------------------- distance --

def _line_distance(u, v, curve) -> np.ndarray:
    ax, ay = curve["a"]
    bx, by = curve["b"]
    ex, ey = bx - ax, by - ay
    length2 = ex * ex + ey * ey
    if length2 < 1e-24:
        return np.hypot(u - ax, v - ay)
    t = np.clip(((u - ax) * ex + (v - ay) * ey) / length2, 0.0, 1.0)
    return np.hypot(u - (ax + t * ex), v - (ay + t * ey))


def _arc_distance(u, v, curve) -> np.ndarray:
    cx, cy = curve["center"]
    radius = arc_radius(curve)
    du, dv = u - cx, v - cy
    reach = np.hypot(du, dv)
    a0, da = sweep(curve["center"], curve["a"], curve["b"], bool(curve.get("ccw", True)))
    on_arc = _within_sweep(np.arctan2(dv, du), a0, da)
    # On the arc's angular span the nearest point is radially out; off it, the
    # nearest point is whichever endpoint is closer.
    radial = np.abs(reach - radius)
    to_a = np.hypot(u - curve["a"][0], v - curve["a"][1])
    to_b = np.hypot(u - curve["b"][0], v - curve["b"][1])
    return np.where(on_arc, radial, np.minimum(to_a, to_b))


def _circle_distance(u, v, curve) -> np.ndarray:
    cx, cy = curve["center"]
    return np.abs(np.hypot(u - cx, v - cy) - float(curve["r"]))


_DISTANCE = {"line": _line_distance, "arc": _arc_distance, "circle": _circle_distance}


# --------------------------------------------------------------- crossings --

def _line_crossings(u, v, curve) -> np.ndarray:
    """Half-open crossing test, matching `_poly_sdf_2d`'s convention.

    A ray leaving the point along +u; a vertex exactly on the ray must be
    counted once, not twice, which is what the asymmetric `>=` / `<` does.
    """
    ax, ay = curve["a"]
    bx, by = curve["b"]
    if abs(by - ay) < 1e-24:
        return np.zeros_like(u, dtype=bool)
    t = (v - ay) / (by - ay)
    hit = (t >= 0.0) & (t < 1.0)
    return hit & ((ax + t * (bx - ax)) > u)


def _circular_crossings(u, v, centre, radius, span) -> np.ndarray:
    """How many times a +u ray from each point crosses a circle or arc.

    Both intersections of the horizontal line with the circle are candidates;
    each counts if it is to the right of the point and (for an arc) on the swept
    part. Tangency is excluded on purpose — it is the same measure-zero case the
    half-open vertex rule handles for polygons.
    """
    cx, cy = centre
    dv = v - cy
    inside = radius * radius - dv * dv
    live = inside > 1e-24
    root = np.sqrt(np.where(live, inside, 0.0))

    count = np.zeros_like(u, dtype=int)
    for sign in (-1.0, 1.0):
        ui = cx + sign * root
        hit = live & (ui > u)
        if span is not None:
            a0, da = span
            hit &= _within_sweep(np.arctan2(dv, ui - cx), a0, da)
        count += hit.astype(int)
    return count


def _curve_crossings(u, v, curve) -> np.ndarray:
    kind = curve["kind"]
    if kind == "line":
        return _line_crossings(u, v, curve).astype(int)
    if kind == "circle":
        return _circular_crossings(u, v, curve["center"], float(curve["r"]), None)
    span = sweep(curve["center"], curve["a"], curve["b"], bool(curve.get("ccw", True)))
    return _circular_crossings(u, v, curve["center"], arc_radius(curve), span)


def loop_contains(loop: dict, u, v) -> np.ndarray:
    """Whether each point is inside one loop, by ray crossing parity."""
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    count = np.zeros(u.shape, dtype=int)
    for curve in loop.get("curves") or []:
        count = count + _curve_crossings(u, v, curve)
    return (count % 2) == 1


# ------------------------------------------------------------------- sdf --

def curves_of(profile: dict) -> List[dict]:
    """Every curve of every loop, flattened.

    Even-odd containment does not care which loop a crossing came from: parity
    is additive mod 2, so ``sum(count_i mod 2) mod 2`` and ``sum(count_i) mod 2``
    are the same number. Counting crossings over one flat list is therefore the
    same answer as `loop_contains` per loop, and it is what lets the whole field
    be computed in a single pass over the curves.
    """
    return [curve for loop in loops_of(profile) for curve in (loop.get("curves") or [])]


# The index only pays for itself on a profile big enough to cull and a sample
# set big enough to amortise the binning. Below either, the plain sweep is both
# faster and simpler. A sketched rectangle has four curves; a traced silhouette
# has hundreds.
_INDEX_MIN_CURVES = 32
_INDEX_MIN_POINTS = 2048


def loops_sdf(profile: dict, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Signed distance to a loops profile: negative inside, positive outside.

    Distance is to the nearest curve of any loop, which is exact for lines,
    arcs and circles alike. The sign is even-odd containment. Those two are
    computed from the same curves, so the zero crossing and the inside/outside
    boundary are the same place — a mismatch there is what puts a half-cell step
    in a traced silhouette (see the mask note in `services/drawing`).

    Large profiles go through a uniform grid over the curves (`_binned_sdf`),
    which changes what is *evaluated* and never what is *returned*: a curve is
    skipped for a cell only when it provably cannot be the nearest one there and
    provably cannot contribute a ray crossing. `test_sketch_profiles` compares
    both kernels against closed-form volumes, and the index is additionally
    checked value-for-value against the plain sweep.
    """
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    curves = curves_of(profile)
    if not curves:
        return np.full(u.shape, 1e6)

    flat_u, flat_v = u.reshape(-1), v.reshape(-1)
    if len(curves) < _INDEX_MIN_CURVES or flat_u.size < _INDEX_MIN_POINTS:
        field = _swept_sdf(curves, flat_u, flat_v)
    else:
        field = _binned_sdf(curves, flat_u, flat_v)
    return field.reshape(u.shape)


def _swept_sdf(curves: List[dict], u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Every curve against every point."""
    distance = np.full(u.shape, np.inf)
    crossings = np.zeros(u.shape, dtype=np.int64)
    for curve in curves:
        distance = np.minimum(distance, _DISTANCE[curve["kind"]](u, v, curve))
        crossings += _curve_crossings(u, v, curve)
    return np.where((crossings % 2) == 1, -distance, distance)


def _concat_ranges(starts: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """``concatenate([arange(s, s + c) for s, c in zip(starts, counts)])``.

    Written as a cumsum rather than a Python loop over the runs: the runs are
    per *cell*, and there are as many of them as there are cells a curve is a
    candidate for. Counts are all positive here — empty cells are filtered
    before the call, and a zero-length run would make two runs share an offset.
    """
    total = int(counts.sum())
    if total == 0:
        return np.empty(0, dtype=np.int64)
    steps = np.ones(total, dtype=np.int64)
    offsets = np.zeros(len(counts), dtype=np.int64)
    np.cumsum(counts[:-1], out=offsets[1:])
    steps[offsets] = starts
    steps[offsets[1:]] -= starts[:-1] + counts[:-1] - 1
    return np.cumsum(steps)


def _binned_sdf(curves: List[dict], u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """The same field, evaluating each curve only where it can matter.

    Sample points are binned into a uniform grid. For each cell two exact
    exclusions are made, and both are *provable* rather than heuristic:

    * **Distance.** ``D`` is the true distance from the cell centre to the
      nearest curve, so for any point in the cell the nearest curve is no
      further than ``D + rad``, where ``rad`` is the cell's half-diagonal. A
      curve whose bounding box is more than ``D + 2·rad`` from the centre is
      further than that from every point in the cell, so it cannot be nearest.
    * **Crossings.** A ray runs from the point along +u, so a curve can only
      cross it when the point's v is inside the curve's v-range and the point's
      u is left of the curve's right edge. That is a rectangle test against the
      cell.

    Measured on 300 curves over a 128×128 grid, the distance test leaves 57 of
    300 curves per cell for a circle and 29 for a lobed outline; the crossing
    test leaves 11. The circle is the worst case on purpose — points deep inside
    a ring are genuinely near-equidistant from all of it, and no index can
    conjure locality that the geometry does not have.
    """
    count = len(curves)
    boxes = np.array([curve_bounds(curve) for curve in curves], dtype=float)

    side = int(np.clip(round(math.sqrt(count)), 4, 24))
    u0, u1 = float(u.min()), float(u.max())
    v0, v1 = float(v.min()), float(v.max())
    du = (u1 - u0) / side or 1.0
    dv = (v1 - v0) / side or 1.0

    column = np.clip(((u - u0) / du).astype(np.int64), 0, side - 1)
    row = np.clip(((v - v0) / dv).astype(np.int64), 0, side - 1)
    cell = column * side + row
    order = np.argsort(cell, kind="stable")
    ordered = cell[order]
    labels = np.arange(side * side)
    starts = np.searchsorted(ordered, labels, side="left")
    ends = np.searchsorted(ordered, labels, side="right")
    occupied = ends > starts

    grid_u = u0 + (np.arange(side) + 0.5) * du
    grid_v = v0 + (np.arange(side) + 0.5) * dv
    centre_u = np.repeat(grid_u, side)
    centre_v = np.tile(grid_v, side)
    rad = 0.5 * math.hypot(du, dv)

    # The true nearest-curve distance at each cell centre. One sweep over the
    # cells (a few hundred points), which is what makes the bound tight enough
    # to be worth having — a witness-point upper bound culls far less.
    nearest = np.full(side * side, np.inf)
    for curve in curves:
        nearest = np.minimum(nearest, _DISTANCE[curve["kind"]](centre_u, centre_v, curve))

    cu, cv = centre_u[:, None], centre_v[:, None]
    away = np.maximum(np.maximum(boxes[None, :, 0] - cu, cu - boxes[None, :, 2]),
                      np.maximum(boxes[None, :, 1] - cv, cv - boxes[None, :, 3]))
    near_enough = away <= (nearest + 2.0 * rad)[:, None]

    cell_u0 = np.repeat(u0 + np.arange(side) * du, side)[:, None]
    cell_v0 = np.tile(v0 + np.arange(side) * dv, side)[:, None]
    can_cross = ((boxes[None, :, 3] >= cell_v0) & (boxes[None, :, 1] <= cell_v0 + dv)
                 & (boxes[None, :, 2] > cell_u0))

    distance = np.full(u.shape, np.inf)
    crossings = np.zeros(u.shape, dtype=np.int64)
    for index, curve in enumerate(curves):
        measure = _DISTANCE[curve["kind"]]
        cells = np.flatnonzero(near_enough[:, index] & occupied)
        if cells.size:
            # Each point sits in exactly one cell, so the gathered indices are
            # unique and these are plain assignments rather than scatter-adds.
            picked = order[_concat_ranges(starts[cells], ends[cells] - starts[cells])]
            here = measure(u[picked], v[picked], curve)
            distance[picked] = np.minimum(distance[picked], here)
        cells = np.flatnonzero(can_cross[:, index] & occupied)
        if cells.size:
            picked = order[_concat_ranges(starts[cells], ends[cells] - starts[cells])]
            crossings[picked] += _curve_crossings(u[picked], v[picked], curve)

    return np.where((crossings % 2) == 1, -distance, distance)


def curve_bounds(curve: dict) -> Tuple[float, float, float, float]:
    """``(umin, vmin, umax, vmax)`` for one curve."""
    if curve["kind"] == "circle":
        cx, cy = curve["center"]
        radius = float(curve["r"])
        return cx - radius, cy - radius, cx + radius, cy + radius
    points = [curve["a"], curve["b"]]
    if curve["kind"] == "arc":
        # An arc bulges past its endpoints wherever it crosses an axis
        # direction, so those extremes have to be added explicitly — a box
        # around the chord is not a box around the arc.
        cx, cy = curve["center"]
        radius = arc_radius(curve)
        a0, da = sweep(curve["center"], curve["a"], curve["b"],
                       bool(curve.get("ccw", True)))
        for quarter in range(4):
            angle = quarter * math.pi / 2.0
            if bool(_within_sweep(np.array([angle]), a0, da)[0]):
                points.append([cx + radius * math.cos(angle),
                               cy + radius * math.sin(angle)])
    us = [p[0] for p in points]
    vs = [p[1] for p in points]
    return min(us), min(vs), max(us), max(vs)


def loops_bounds(profile: dict) -> Tuple[float, float, float, float]:
    """``(umin, vmin, umax, vmax)`` of every curve in the profile."""
    boxes = [curve_bounds(curve) for curve in curves_of(profile)]
    if not boxes:
        return 0.0, 0.0, 0.0, 0.0
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


# ------------------------------------------------------------- containment --

def _sample_point(loop: dict) -> Tuple[float, float]:
    """A point on the loop, for testing which other loops enclose it."""
    curve = (loop.get("curves") or [{}])[0]
    if curve.get("kind") == "circle":
        return float(curve["center"][0]) + float(curve["r"]), float(curve["center"][1])
    return float(curve["a"][0]), float(curve["a"][1])


def nesting(profile: dict) -> List[int]:
    """How many other loops enclose each loop — 0 outer, 1 hole, 2 island, …

    The B-rep side needs this explicitly (OCC wants a face's outer wire and its
    holes named separately) while the SDF gets the same answer implicitly from
    even-odd parity. Deriving both from this one function is what keeps them
    describing the same shape.
    """
    loops = loops_of(profile)
    depths = []
    for i, loop in enumerate(loops):
        pu, pv = _sample_point(loop)
        u = np.array([pu]); v = np.array([pv])
        depth = 0
        for j, other in enumerate(loops):
            if i == j:
                continue
            if bool(loop_contains(other, u, v)[0]):
                depth += 1
        depths.append(depth)
    return depths


def faces_of(profile: dict) -> List[Dict[str, Any]]:
    """Group loops into ``{"outer": loop, "holes": [loop, …]}`` by nesting depth.

    Even depth is material and odd depth is a void, so an island inside a hole
    becomes its own face rather than a special case.
    """
    loops = loops_of(profile)
    depths = nesting(profile)
    out: List[Dict[str, Any]] = []
    for i, loop in enumerate(loops):
        if depths[i] % 2 != 0:
            continue
        holes = []
        for j, other in enumerate(loops):
            if i == j or depths[j] != depths[i] + 1:
                continue
            # A hole belongs to the outer loop that immediately encloses it.
            pu, pv = _sample_point(other)
            if bool(loop_contains(loop, np.array([pu]), np.array([pv]))[0]):
                holes.append(other)
        out.append({"outer": loop, "holes": holes})
    return out


def tessellate(profile: dict, segments: int = 48) -> List[List[List[float]]]:
    """Each loop as a ring of points — for callers that can only take polygons."""
    rings: List[List[List[float]]] = []
    for loop in loops_of(profile):
        ring: List[List[float]] = []
        for curve in loop.get("curves") or []:
            if curve["kind"] == "line":
                ring.append([float(curve["a"][0]), float(curve["a"][1])])
            elif curve["kind"] == "circle":
                cx, cy = curve["center"]
                radius = float(curve["r"])
                ring.extend([[cx + radius * math.cos(2 * math.pi * i / segments),
                              cy + radius * math.sin(2 * math.pi * i / segments)]
                             for i in range(segments)])
            else:
                cx, cy = curve["center"]
                radius = arc_radius(curve)
                a0, da = sweep(curve["center"], curve["a"], curve["b"],
                                bool(curve.get("ccw", True)))
                steps = max(2, int(segments * abs(da) / (2 * math.pi)) + 1)
                ring.extend([[cx + radius * math.cos(a0 + da * i / steps),
                              cy + radius * math.sin(a0 + da * i / steps)]
                             for i in range(steps)])
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def area(profile: dict) -> float:
    """Signed-area sum over tessellated loops, holes subtracted. For sanity checks."""
    total = 0.0
    depths = nesting(profile)
    # Per-loop rather than against `tessellate(profile)`: that drops degenerate
    # rings, which would slide every later loop onto the wrong nesting depth and
    # silently flip a hole into material.
    for depth, loop in zip(depths, loops_of(profile)):
        ring = (tessellate({"type": "loops", "loops": [loop]}) or [[]])[0]
        if len(ring) < 3:
            continue
        loop_area = 0.0
        for i, (ux, uy) in enumerate(ring):
            vx, vy = ring[(i + 1) % len(ring)]
            loop_area += ux * vy - vx * uy
        total += abs(loop_area / 2.0) * (1 if depth % 2 == 0 else -1)
    return total


def curve_count(profile: dict) -> int:
    return sum(len(loop.get("curves") or []) for loop in loops_of(profile))


def kinds(profile: dict) -> Sequence[str]:
    return [c["kind"] for loop in loops_of(profile) for c in (loop.get("curves") or [])]
