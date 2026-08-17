"""2.5D CAM: roughing (area clearing) + finishing (profiling) → G-code.

Shapely-free and SDF-native. On each cutting plane we sample the body's signed
distance field. Two operations fall straight out of it:

* Roughing — the flat endmill center can sit anywhere the field is >= tool
  radius without gouging the part, so we raster-clear the stock (minus the part)
  with a zig-zag at the given stepover.
* Finishing — the isocontour at level = tool radius is exactly the tool-center
  path tangent to the part wall, i.e. a clean profile pass.

Both are stacked from the top of stock down in stepdown increments and
post-processed to grbl/Klipper-compatible G-code.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import List, Tuple

import numpy as np

from app.cad_kernel.evaluator.sdf import sdf_from_feature

M = 1000.0  # metres -> millimetres for G-code


@dataclass
class ToolParams:
    diameter_mm: float = 3.0
    stepdown_mm: float = 1.0
    stepover_mm: float = 1.2
    stock_margin_mm: float = 4.0
    feed_mm_min: float = 600.0
    plunge_mm_min: float = 200.0
    spindle_rpm: int = 12000
    safe_z_mm: float = 5.0


# A roughing cut: a horizontal segment (constant y) at depth z, in world metres.
RoughSeg = Tuple[float, float, float]  # (x0, x1, y)


def _sample_plane(sdf, lo, hi, z: float, nx: int, ny: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    xs = np.linspace(lo[0], hi[0], nx)
    ys = np.linspace(lo[1], hi[1], ny)
    gx, gy = np.meshgrid(xs, ys, indexing="xy")  # field indexed [iy, ix]
    pts = np.stack([gx.ravel(), gy.ravel(), np.full(gx.size, z)], axis=1)
    field = sdf(pts).reshape(ny, nx)
    return field, xs, ys


def _runs(mask_row: np.ndarray) -> List[Tuple[int, int]]:
    """Contiguous [start, end] column runs where a boolean row is True."""
    idx = np.where(mask_row)[0]
    if idx.size == 0:
        return []
    splits = np.where(np.diff(idx) > 1)[0]
    starts = np.concatenate([[idx[0]], idx[splits + 1]])
    ends = np.concatenate([idx[splits], [idx[-1]]])
    return list(zip(starts.tolist(), ends.tolist()))


@dataclass
class Passes:
    """What the planner decided, before any of it becomes G-code.

    Split out so `cam/verify.py` can check the *same* passes the post-processor
    writes. Re-deriving them there would be a second planner, and a verifier
    that checks a different toolpath from the one the shop receives is worse
    than no verifier — it would report a clean cut for a program that gouges.
    """

    rough: List[Tuple[float, List["RoughSeg"]]]
    finish: List[Tuple[float, List[np.ndarray]]]
    stock_lo: np.ndarray          # metres, tool-centre stock box
    stock_hi: np.ndarray
    safe_z: float
    tool_radius_m: float


def plan_passes(root_feature, tool: ToolParams) -> Passes:
    """Roughing segments and finishing contours, level by level."""
    from skimage.measure import find_contours

    sdf = sdf_from_feature(root_feature)
    b = sdf.bounds()
    lo, hi = np.asarray(b.min, dtype=float), np.asarray(b.max, dtype=float)
    span = np.maximum(hi - lo, 1e-4)

    tool_d_m = tool.diameter_mm / M
    tool_r_m = tool_d_m / 2.0
    stepdown_m = tool.stepdown_mm / M
    stepover_m = max(tool.stepover_mm / M, 1e-4)
    # The tool-centre clearable ring is (margin - tool_diameter) wide, so a margin
    # smaller than the tool leaves nothing to rough. Guarantee a usable ring.
    margin_m = max(tool.stock_margin_mm / M, tool_d_m + stepover_m)

    # Stock = part bbox padded laterally; tool centre must stay a radius inside.
    lo_s = lo.copy()
    hi_s = hi.copy()
    lo_s[:2] -= margin_m
    hi_s[:2] += margin_m
    span_xy = hi_s[:2] - lo_s[:2]

    aspect = span_xy / span_xy.max()
    base = 200
    nx = max(40, int(base * aspect[0]))
    ny = max(40, int(base * aspect[1]))

    z_top, z_bottom = hi[2], lo[2]
    levels = list(np.arange(z_top - stepdown_m, z_bottom - stepdown_m, -stepdown_m))
    levels = [max(z_bottom, z) for z in levels] or [z_bottom]
    safe_z = z_top + tool.safe_z_mm / M

    rough_by_level: List[Tuple[float, List[RoughSeg]]] = []
    finish_by_level: List[Tuple[float, List[np.ndarray]]] = []

    row_step = max(1, int(round(stepover_m / (span_xy[1] / (ny - 1)))))
    # Keep the tool centre inside the stock rectangle.
    x_ok_lo, x_ok_hi = lo_s[0] + tool_r_m, hi_s[0] - tool_r_m
    y_ok_lo, y_ok_hi = lo_s[1] + tool_r_m, hi_s[1] - tool_r_m

    for z in levels:
        field, xs, ys = _sample_plane(sdf, lo_s, hi_s, z, nx, ny)

        # ── Roughing: raster-clear where the tool centre clears the part ──
        clearable = field >= tool_r_m
        col_ok = (xs >= x_ok_lo) & (xs <= x_ok_hi)
        segs: List[RoughSeg] = []
        flip = False
        for iy in range(0, ny, row_step):
            y = ys[iy]
            if not (y_ok_lo <= y <= y_ok_hi):
                continue
            row = clearable[iy] & col_ok
            runs = _runs(row)
            if flip:
                runs = runs[::-1]
            for (c0, c1) in runs:
                x0, x1 = xs[c0], xs[c1]
                if abs(x1 - x0) < 0.5 * stepover_m:  # skip negligible slivers
                    continue
                segs.append((x1, x0, y) if flip else (x0, x1, y))
            flip = not flip
        if segs:
            rough_by_level.append((z, segs))

        # ── Finishing: profile contour at the tool-radius isocontour ──
        if field.min() <= tool_r_m <= field.max():
            polylines: List[np.ndarray] = []
            for c in find_contours(field, level=tool_r_m):
                wx = np.interp(np.clip(c[:, 1], 0, nx - 1), np.arange(nx), xs)
                wy = np.interp(np.clip(c[:, 0], 0, ny - 1), np.arange(ny), ys)
                polylines.append(np.stack([wx, wy], axis=1))
            if polylines:
                finish_by_level.append((z, polylines))

    return Passes(rough_by_level, finish_by_level, lo_s, hi_s, safe_z, tool_r_m)


def generate_toolpaths(root_feature, tool: ToolParams) -> dict:
    passes = plan_passes(root_feature, tool)
    rough_by_level, finish_by_level = passes.rough, passes.finish
    safe_z = passes.safe_z

    gcode, cut_len_m, n_plunges = _emit_gcode(rough_by_level, finish_by_level, safe_z, tool)

    feed_mps = tool.feed_mm_min / M / 60.0
    cut_minutes = (cut_len_m / max(feed_mps, 1e-9)) / 60.0
    plunge_minutes = n_plunges * (tool.stepdown_mm / max(tool.plunge_mm_min, 1e-9))
    est_minutes = round(cut_minutes + plunge_minutes, 2)

    gcode_b64 = base64.b64encode(gcode.encode("utf-8")).decode("ascii")
    n_rough = sum(len(s) for _, s in rough_by_level)
    n_finish = sum(len(p) for _, p in finish_by_level)
    return {
        "gcode_url": f"data:text/plain;base64,{gcode_b64}",
        "operations": [
            f"Roughing · {len(rough_by_level)} levels · {n_rough} passes · {tool.stepover_mm:g}mm stepover",
            f"Finishing · {len(finish_by_level)} levels · {n_finish} contours",
            f"Ø{tool.diameter_mm:g}mm flat endmill · {tool.stepdown_mm:g}mm stepdown",
        ],
        "estimated_minutes": est_minutes,
        "stock_size": {
            "x": float(passes.stock_hi[0] - passes.stock_lo[0]),
            "y": float(passes.stock_hi[1] - passes.stock_lo[1]),
            "z": float(passes.stock_hi[2] - passes.stock_lo[2]),
        },
    }


def _emit_gcode(rough_by_level, finish_by_level, safe_z, tool: ToolParams) -> Tuple[str, float, int]:
    L: List[str] = []
    sz = safe_z * M
    fr, pl = tool.feed_mm_min, tool.plunge_mm_min

    L += [
        "; Forge 2.5D CAM — roughing + finishing",
        f"; tool Ø{tool.diameter_mm:g}mm flat endmill, stepdown {tool.stepdown_mm:g}mm, stepover {tool.stepover_mm:g}mm",
        "G21 ; mm",
        "G90 ; absolute",
        "G17 ; XY plane",
        f"M3 S{int(tool.spindle_rpm)}",
        f"G0 Z{sz:.3f}",
    ]

    cut_len = 0.0
    plunges = 0

    L.append("; --- roughing ---")
    for z, segs in rough_by_level:
        for (x0, x1, y) in segs:
            L.append(f"G0 X{x0 * M:.3f} Y{y * M:.3f}")
            L.append(f"G1 Z{z * M:.3f} F{pl:g}")
            L.append(f"G1 X{x1 * M:.3f} Y{y * M:.3f} F{fr:g}")
            L.append(f"G0 Z{sz:.3f}")
            cut_len += abs(x1 - x0)
            plunges += 1

    L.append("; --- finishing ---")
    for z, polylines in finish_by_level:
        for poly in polylines:
            if len(poly) < 2:
                continue
            x0, y0 = poly[0]
            L.append(f"G0 X{x0 * M:.3f} Y{y0 * M:.3f}")
            L.append(f"G1 Z{z * M:.3f} F{pl:g}")
            for (x, y) in poly[1:]:
                L.append(f"G1 X{x * M:.3f} Y{y * M:.3f} F{fr:g}")
            L.append(f"G1 X{x0 * M:.3f} Y{y0 * M:.3f} F{fr:g}")
            L.append(f"G0 Z{sz:.3f}")
            cut_len += float(np.linalg.norm(np.diff(np.vstack([poly, poly[0]]), axis=0), axis=1).sum())
            plunges += 1

    L += [f"G0 Z{sz:.3f}", "M5 ; spindle off", "M30 ; end"]
    return "\n".join(L) + "\n", cut_len, plunges
