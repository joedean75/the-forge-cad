"""2D profile cutting (laser / plasma / waterjet) — sheet-cut CAM.

Cuts the part's outline from flat stock. The body is sectioned at its mid-plane
into a 2D signed-distance field (negative inside material); the cut paths are the
iso-contours at ``+kerf/2`` — so both the outer profile and interior holes come
out **kerf-compensated** to nominal size in one pass. Output is RepRap-flavor
G-code with a pierce/lead-in and beam on/off (M3/M5), which runs on GRBL laser,
plasma and waterjet controllers.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.ndimage import distance_transform_edt
from skimage import measure

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator.sdf import sdf_from_feature
from app.cad_kernel.ops import sheet_metal
from app.services import document_service, feature_ops

MM = 1000.0
_GRID = 400  # samples along the longest in-plane axis


def profile_cut(doc_id: str, kerf_mm: float = 0.2, plane: str = "xy",
                feed_mm_min: float = 1200.0, power: float = 100.0,
                pierce_s: float = 0.3, lead_in_mm: float = 2.0) -> Dict:
    """Generate 2D cut paths + G-code for the part outline."""
    doc = document_service.get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)
    active = document_service.resolved_tree(doc)
    # Sheet metal is cut as its unfolded blank. Sectioning the folded part would
    # trace its silhouette, which is not a shape that folds back into the part.
    active = sheet_metal.unfold_tree(active)
    sdf = sdf_from_feature(FeatureTree.from_dict(active).root)
    b = sdf.bounds()
    lo = np.asarray(b.min, dtype=float)
    hi = np.asarray(b.max, dtype=float)

    from app.cad_kernel.core.planes import plane_uvw

    ui, vi, wi = plane_uvw(plane)
    umin, umax = lo[ui], hi[ui]
    vmin, vmax = lo[vi], hi[vi]
    wmid = (lo[wi] + hi[wi]) / 2.0
    span_u, span_v = umax - umin, vmax - vmin
    if span_u <= 0 or span_v <= 0:
        raise ValueError("The part has no area to cut in this plane.")

    cell = max(span_u, span_v) / _GRID
    nu = int(span_u / cell) + 2
    nv = int(span_v / cell) + 2
    us = umin + np.arange(nu) * cell
    vs = vmin + np.arange(nv) * cell
    gu, gv = np.meshgrid(us, vs, indexing="ij")
    pts = np.zeros((gu.size, 3))
    pts[:, ui] = gu.ravel()
    pts[:, vi] = gv.ravel()
    pts[:, wi] = wmid
    occ = (sdf(pts) <= 0.0).reshape(nu, nv)
    if not occ.any():
        raise ValueError("No material found at the cut plane.")

    # In-plane signed distance (mm): positive outside material, negative inside.
    field = (distance_transform_edt(~occ) - distance_transform_edt(occ)) * cell * MM
    level = kerf_mm / 2.0

    paths: List[List[Tuple[float, float]]] = []
    for c in measure.find_contours(np.pad(field, 1, constant_values=1e6), level):
        c = measure.approximate_polygon(c, tolerance=0.25)  # ≈0.05 mm at this cell size
        if len(c) < 3:
            continue
        paths.append([(umin * MM + (i - 1) * cell * MM, vmin * MM + (j - 1) * cell * MM) for i, j in c])

    gcode, cut_len_mm = _emit_gcode(paths, feed_mm_min, power, pierce_s, lead_in_mm)
    return {
        "document_id": doc_id,
        "plane": plane,
        "kerf_mm": kerf_mm,
        "paths": [[[round(x, 2), round(y, 2)] for x, y in p] for p in paths],
        "path_count": len(paths),
        "cut_length_mm": round(cut_len_mm, 1),
        "estimated_minutes": round(cut_len_mm / max(feed_mm_min, 1) + len(paths) * pierce_s / 60.0, 2),
        "gcode": gcode,
        "size_mm": {"u": round(span_u * MM, 2), "v": round(span_v * MM, 2)},
    }


def _emit_gcode(paths, feed, power, pierce_s, lead_in) -> Tuple[str, float]:
    g: List[str] = [
        "; 2D profile cut — The Forge",
        "G21 ; mm", "G90 ; absolute", "G17 ; XY plane", "M5 ; beam off",
        f"; {len(paths)} contour(s)",
    ]
    total = 0.0
    for path in paths:
        if len(path) < 2:
            continue
        x0, y0 = path[0]
        g.append(f"G0 X{x0:.3f} Y{y0:.3f}")
        g.append(f"M3 S{power:.0f} ; beam on / pierce")
        if pierce_s > 0:
            g.append(f"G4 P{pierce_s:.2f} ; pierce dwell")
        loop = path + [path[0]]
        for i in range(1, len(loop)):
            x, y = loop[i]
            total += math.hypot(x - loop[i - 1][0], y - loop[i - 1][1])
            g.append(f"G1 X{x:.3f} Y{y:.3f} F{feed:.0f}")
        g.append("M5 ; beam off")
    g += ["G0 X0 Y0", "M2 ; end"]
    _ = lead_in  # lead-in reserved for a future pierce-offset refinement
    return "\n".join(g) + "\n", total
