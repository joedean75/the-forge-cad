"""Does this toolpath cut the right part?

The answer to "can we have CAM simulation" — and the reason it is *this* rather
than a physics engine. Material removal is a geometry problem: subtract the
swept tool volume from the stock and compare the remainder with the target
body. There are no forces in it, no contacts and no dynamics, which is why
NVIDIA's Newton was investigated and declined (`docs/CAM-SIMULATION-SOURCING.md`).

Everything needed is already here. `cam/toolpath.py` is SDF-native — it finds
where a flat endmill's centre can sit by sampling the body's signed distance
field — so the same field checks the path it produced. No new dependency.

Two numbers come out, and they are the two a machinist asks for:

* **Gouge** — material inside the finished part that the cut removed. Any
  non-zero figure is a scrapped part, so this is a failing check rather than a
  metric.
* **Leftover** — stock left standing that is not part of the finished part.
  *Some is expected*: a 2.5-axis path cannot reach an undercut, and the scallops
  between raster passes are real. A lot means the stepover or stepdown is wrong.

**The verifier reads the planner's own passes** (`toolpath.plan_passes`) rather
than re-deriving them. A verifier that checks a different toolpath from the one
the shop receives is worse than none: it would report a clean cut for a program
that gouges.

**A flat endmill cutting top-down reduces to one 2D array.** A pass at height
`z` removes everything above `z` within a tool radius of its centreline, so a
point is cut exactly when it lies at or above the *lowest* level that cleared
its (x, y). That "floor" array is the whole swept volume, computed once, and it
is what keeps this a fraction of a second instead of a sweep per segment.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from app.cad_kernel.evaluator.sdf import sdf_from_feature
from app.services.cam.toolpath import Passes, ToolParams, plan_passes

MM = 1000.0

#: Samples across the longest side of the stock. The check is a volume ratio,
#: so it converges quickly; 128 puts the discretisation error well under the
#: tolerances below on every shape this was tried on.
DEFAULT_SAMPLES = 128

#: Leftover under this fraction of the stock is the scalloping and unreachable
#: material any 2.5-axis path leaves, not a mistake. Above it, the stepover or
#: stepdown is wrong.
LEFTOVER_WARN = 0.02

#: Gouge has no acceptable band — it is a scrapped part — but a single sample
#: cell of numerical noise is not a gouge. One cell in a hundred thousand.
GOUGE_TOLERANCE = 1e-5


def _floor_map(passes: Passes, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """The lowest z the tool reached over each (x, y), or +inf if never cut.

    Levels descend, so each covered cell simply keeps overwriting with the
    lower value and the final array is the minimum by construction.
    """
    grid_x, grid_y = np.meshgrid(xs, ys, indexing="xy")
    floor = np.full(grid_x.shape, np.inf)
    radius_sq = passes.tool_radius_m ** 2

    for z, segments in passes.rough:
        covered = np.zeros(grid_x.shape, dtype=bool)
        for (x0, x1, y) in segments:
            lo, hi = (x0, x1) if x0 <= x1 else (x1, x0)
            # Distance to an axis-aligned segment, without a square root.
            dx = np.maximum(0.0, np.maximum(lo - grid_x, grid_x - hi))
            dy = grid_y - y
            covered |= (dx * dx + dy * dy) <= radius_sq
        floor = np.where(covered, np.minimum(floor, z), floor)

    for z, polylines in passes.finish:
        covered = np.zeros(grid_x.shape, dtype=bool)
        for poly in polylines:
            points = np.asarray(poly, dtype=float)
            if len(points) < 2:
                continue
            for (ax, ay), (bx, by) in zip(points[:-1], points[1:]):
                ex, ey = bx - ax, by - ay
                length_sq = ex * ex + ey * ey
                if length_sq < 1e-18:
                    continue
                t = np.clip(((grid_x - ax) * ex + (grid_y - ay) * ey) / length_sq, 0.0, 1.0)
                dx = grid_x - (ax + t * ex)
                dy = grid_y - (ay + t * ey)
                covered |= (dx * dx + dy * dy) <= radius_sq
        floor = np.where(covered, np.minimum(floor, z), floor)

    return floor


def verify_toolpaths(root_feature, tool: Optional[ToolParams] = None, *,
                     samples: int = DEFAULT_SAMPLES) -> Dict[str, Any]:
    """Cut the stock with the planned path and compare it against the part."""
    tool = tool or ToolParams()
    passes = plan_passes(root_feature, tool)
    field = sdf_from_feature(root_feature)

    lo, hi = np.asarray(passes.stock_lo, float), np.asarray(passes.stock_hi, float)
    span = np.maximum(hi - lo, 1e-6)
    counts = np.maximum(8, np.round(samples * span / span.max()).astype(int))
    nx, ny, nz = (int(c) for c in counts)

    xs = np.linspace(lo[0], hi[0], nx)
    ys = np.linspace(lo[1], hi[1], ny)
    zs = np.linspace(lo[2], hi[2], nz)
    cell = float(np.prod(span / np.maximum(counts - 1, 1)))

    floor = _floor_map(passes, xs, ys)                        # (ny, nx)
    # (nz, ny, nx): removed where the sample sits at or above the cut floor.
    removed = zs[:, None, None] >= floor[None, :, :]

    grid_x, grid_y = np.meshgrid(xs, ys, indexing="xy")
    shape = (nz, ny, nx)
    points = np.stack([
        np.broadcast_to(grid_x[None, :, :], shape).ravel(),
        np.broadcast_to(grid_y[None, :, :], shape).ravel(),
        np.broadcast_to(zs[:, None, None], shape).ravel(),
    ], axis=1)
    part = field(points).reshape(shape) <= 0.0

    gouge_cells = int(np.count_nonzero(part & removed))
    leftover_cells = int(np.count_nonzero(~part & ~removed))
    part_cells = max(int(np.count_nonzero(part)), 1)
    stock_cells = max(nz * ny * nx, 1)

    gouge_mm3 = gouge_cells * cell * MM ** 3
    leftover_mm3 = leftover_cells * cell * MM ** 3
    gouge_fraction = gouge_cells / part_cells
    leftover_fraction = leftover_cells / stock_cells

    checks: List[Dict[str, Any]] = []
    if gouge_fraction > GOUGE_TOLERANCE:
        checks.append({
            "name": "Gouge", "status": "fail",
            "detail": (f"the path removes {gouge_mm3:.1f} mm³ of the finished part "
                       f"({gouge_fraction:.1%} of it). Any gouge is a scrapped "
                       f"part — reduce the tool diameter or the stepdown."),
        })
    else:
        checks.append({"name": "Gouge", "status": "pass",
                       "detail": "the path stays clear of the finished part"})

    if leftover_fraction > LEFTOVER_WARN:
        checks.append({
            "name": "Leftover stock", "status": "warn",
            "detail": (f"{leftover_mm3:.1f} mm³ of stock is left standing "
                       f"({leftover_fraction:.1%} of the block). Some is "
                       f"unavoidable — a 2.5-axis path cannot reach an undercut "
                       f"— but this much usually means the stepover or stepdown "
                       f"is too coarse."),
        })
    else:
        checks.append({"name": "Leftover stock", "status": "pass",
                       "detail": "only scalloping and unreachable material remains"})

    return {
        "gouge_mm3": round(gouge_mm3, 3),
        "gouge_fraction": round(gouge_fraction, 6),
        "leftover_mm3": round(leftover_mm3, 3),
        "leftover_fraction": round(leftover_fraction, 6),
        "checks": checks,
        # Stated because it bounds every figure above: this is a sampled
        # comparison, and a feature thinner than a cell is invisible to it.
        "sample_mm": round(float(np.max(span / np.maximum(counts - 1, 1))) * MM, 4),
        "grid": [nx, ny, nz],
        "levels": len(passes.rough),
    }
