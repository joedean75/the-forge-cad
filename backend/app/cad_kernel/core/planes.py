"""Sketch-plane ↔ world-axis mappings, in one place.

Several services (SDF evaluator, datum resolution, profile cutting) map a sketch
plane to its world axes. Keeping the mapping here avoids the drift that comes
from re-declaring ``{"xy": ...}`` dicts in each module.

Convention: for a plane, ``(u, v, w)`` are the world-axis indices of the two
in-plane axes and the normal. ``xy → (0, 1, 2)`` (u=x, v=y, normal=z).
"""

from __future__ import annotations

from typing import Tuple

# plane -> (u, v, normal) world-axis indices.
PLANE_UVW = {"xy": (0, 1, 2), "xz": (0, 2, 1), "yz": (1, 2, 0)}

# plane -> normal axis name.
PLANE_NORMAL = {"xy": "z", "xz": "y", "yz": "x"}


def plane_uvw(plane: str) -> Tuple[int, int, int]:
    return PLANE_UVW.get(plane, (0, 1, 2))


def plane_normal(plane: str) -> str:
    return PLANE_NORMAL.get(plane, "z")
