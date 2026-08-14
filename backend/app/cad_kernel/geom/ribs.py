"""A rib's cross-section, described once for both kernels.

A rib is the standard way to stiffen a plastic or cast part, and until now the
only way to model one here was to union a thin box in by hand. That works right
up until somebody changes the wall thickness: the box does not know it is a rib,
so nothing keeps its draft, its tip thickness and its height in the relationship
the moulder needs them in.

**This is a dimensioned rib, not an "up to next" one.** A real CAD rib grows
until it meets material, which needs a face reference — and the SDF kernel has
no faces at all, so a rib built that way would exist on one kernel and not the
other. Height is therefore a parameter. Everything else about it is a real rib:
the tip thickness is what a mould tool is cut to, and the draft opens the base
out so the part releases.

The shape is a **trapezoidal prism**: a trapezoid in the plane perpendicular to
the run, swept along it. That is exactly representable on both sides — a
polygon wire prismed by OCC, and the intersection of six half-spaces as a field
— so the two cannot disagree about the geometry the way two transcriptions of
the same arithmetic eventually do. Both read the corner points from here.
"""

from __future__ import annotations

import math
from typing import List, NamedTuple, Sequence, Tuple

import numpy as np

Vec3 = Tuple[float, float, float]


class RibPrism(NamedTuple):
    """The trapezoid at the start of the run, plus the vector that sweeps it.

    `section` is four points in order — base-left, base-right, tip-right,
    tip-left — lying in the plane at `start`. `sweep` carries them to `end`.
    """
    section: List[Vec3]
    sweep: Vec3


def _unit(v: np.ndarray, fallback: Sequence[float] = (0.0, 0.0, 1.0)) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n < 1e-12:
        return np.asarray(fallback, dtype=float)
    return v / n


def rib_prism(*, start: Sequence[float], end: Sequence[float],
              up: Sequence[float] = (0.0, 0.0, 1.0),
              thickness: float = 0.002, height: float = 0.01,
              draft: float = 0.0) -> RibPrism:
    """The prism a rib occupies.

    `start` and `end` are the centreline **at the base**; the rib grows `height`
    along `up`. `thickness` is at the **tip**, which is how a rib is dimensioned
    and toleranced — the base opens out by `height * tan(draft)` on each side.
    """
    a = np.asarray([float(c) for c in start], dtype=float)
    b = np.asarray([float(c) for c in end], dtype=float)
    run = b - a
    if float(np.linalg.norm(run)) < 1e-9:
        raise ValueError("a rib needs a start and an end that are not the same point")

    u = _unit(np.asarray([float(c) for c in up], dtype=float))
    across = np.cross(u, _unit(run))
    if float(np.linalg.norm(across)) < 1e-9:
        raise ValueError("a rib cannot grow along its own run — `up` is parallel to it")
    across = _unit(across)

    tip = max(float(thickness), 1e-6)
    height = max(float(height), 1e-6)
    # Draft opens the base, never pinches the tip: a mould tool is cut to the
    # tip dimension and the taper is what lets the part come off it.
    base = tip + 2.0 * height * math.tan(math.radians(max(float(draft), 0.0)))

    top = a + u * height
    section = [a - across * (base / 2.0), a + across * (base / 2.0),
               top + across * (tip / 2.0), top - across * (tip / 2.0)]
    return RibPrism([tuple(float(c) for c in p) for p in section],
                    tuple(float(c) for c in run))


def prism_for(feature) -> RibPrism:
    """`rib_prism` read off a `RibFeature`."""
    return rib_prism(
        start=(feature.start.x, feature.start.y, feature.start.z),
        end=(feature.end.x, feature.end.y, feature.end.z),
        up=(feature.up.x, feature.up.y, feature.up.z),
        thickness=feature.thickness, height=feature.height, draft=feature.draft)


def half_spaces(prism: RibPrism) -> List[Tuple[Vec3, Vec3]]:
    """`(point, outward unit normal)` for each of the prism's six planes.

    The rib is their intersection, which is what makes the field form exact on
    the boundary — the only place marching cubes reads. Outside a convex solid
    the maximum of plane distances underestimates the true distance near an
    edge, exactly as `ConeSDF` does next door; the zero set is unaffected.
    """
    section = [np.asarray(p, dtype=float) for p in prism.section]
    sweep = np.asarray(prism.sweep, dtype=float)
    centroid = sum(section) / 4.0 + sweep / 2.0

    planes: List[Tuple[Vec3, Vec3]] = []

    def add(point: np.ndarray, normal: np.ndarray) -> None:
        normal = _unit(normal)
        # Orient outward without assuming which way the section was wound —
        # a caller-supplied `up` can flip the handedness of the cross products.
        if float(np.dot(centroid - point, normal)) > 0:
            normal = -normal
        planes.append((tuple(float(c) for c in point), tuple(float(c) for c in normal)))

    for i in range(4):
        p, q = section[i], section[(i + 1) % 4]
        add(p, np.cross(q - p, sweep))
    cap = np.cross(section[1] - section[0], section[3] - section[0])
    add(section[0], cap)
    add(section[0] + sweep, cap)
    return planes
