"""FDM slicer: document body → printable G-code (Klipper/Marlin flavor).

The part's signed distance field is sliced directly: each layer's occupancy is
sampled from the 3D SDF and rebuilt into a true *in-plane* 2D signed distance
(EDT) — the 3D field can't be used for offsets near horizontal faces, where it
measures distance to the floor/ceiling instead of the outline. Perimeter insets
are then just iso-contours of that layer field at negative levels; the
offsetting that dominates mesh-based slicers falls out of the representation.

Print quality features expected of a production workflow:
  * solid top/bottom skins (layer-lookahead: any inner region not covered
    ``skin_layers`` deep above *and* below is skinned dense),
  * rectilinear sparse infill (alternating 45°/135°),
  * skirt, retraction, greedy travel ordering, part-cooling fan control,
  * Douglas-Peucker simplification (0.05 mm) for real-slicer G-code density.

Output targets standard RepRap flavor (relative extrusion, M83) and uploads
unmodified to Moonraker/Klipper via the existing machine service.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

import numpy as np
from pydantic import BaseModel, Field
from scipy.ndimage import distance_transform_edt, map_coordinates
from skimage import measure

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator.sdf import sdf_from_feature
from app.services import document_service, feature_ops

MM = 1000.0  # part space is metres; the printer speaks mm

_MAX_LAYERS = 1500
_MAX_GRID = 900  # per-axis sample cap per layer
_PLA_DENSITY_G_CM3 = 1.24


class SliceSettings(BaseModel):
    """User-tunable print settings (mm / mm-per-second / °C)."""

    layer_height: float = Field(0.2, ge=0.05, le=0.6)
    nozzle_diameter: float = Field(0.4, ge=0.15, le=1.2)
    wall_count: int = Field(2, ge=1, le=6)
    infill: float = Field(0.15, ge=0.0, le=1.0)  # 0..1 density
    skin_layers: int = Field(3, ge=0, le=8)  # solid top/bottom shells (0 = off)
    speed_print: float = Field(45.0, ge=5, le=300)
    speed_first_layer: float = Field(20.0, ge=5, le=100)
    speed_travel: float = Field(150.0, ge=10, le=500)
    temp_nozzle: int = Field(210, ge=150, le=320)
    temp_bed: int = Field(60, ge=0, le=120)
    fan_percent: int = Field(100, ge=0, le=100)  # part cooling from layer 3 on
    filament_diameter: float = Field(1.75, gt=0)
    retract_mm: float = Field(1.0, ge=0, le=8)
    retract_speed: float = Field(35.0, ge=5, le=80)
    skirt_loops: int = Field(2, ge=0, le=5)
    bed_center_x: float = Field(110.0)
    bed_center_y: float = Field(110.0)


Polyline = List[Tuple[float, float]]


def _order_polylines(polys: List[Polyline], start: Tuple[float, float]) -> List[Polyline]:
    """Greedy nearest-start ordering to keep travel moves short."""
    remaining = list(polys)
    out: List[Polyline] = []
    pos = start
    while remaining:
        best = min(range(len(remaining)),
                   key=lambda i: (remaining[i][0][0] - pos[0]) ** 2 + (remaining[i][0][1] - pos[1]) ** 2)
        p = remaining.pop(best)
        out.append(p)
        pos = p[-1]
    return out


class _Field2D:
    """In-plane signed distance (mm) to a region boundary, from an occupancy mask."""

    def __init__(self, mask: np.ndarray, x0: float, y0: float, cell: float):
        if mask.any():
            d_in = distance_transform_edt(mask)
            d_out = distance_transform_edt(~mask)
            self.field = (d_out - d_in) * cell
        else:
            self.field = np.full(mask.shape, 1e6)
        self.x0, self.y0, self.cell = x0, y0, cell

    def contours(self, level: float) -> List[Polyline]:
        """Iso-contours of the field (negative level = inset inside the region)."""
        if self.field.min() > level:
            return []
        out = []
        tol = 0.05 / self.cell  # simplify to 0.05 mm — real-slicer G-code density
        for c in measure.find_contours(self.field, level):
            if len(c) < 3:
                continue
            c = measure.approximate_polygon(c, tolerance=tol)
            if len(c) < 3:
                continue
            pts = [(self.x0 + i * self.cell, self.y0 + j * self.cell) for i, j in c]
            out.append(pts)
        return out

    def mask(self, level: float) -> np.ndarray:
        return self.field <= level

    def clip_line(self, a: Tuple[float, float], b: Tuple[float, float], level: float) -> List[Polyline]:
        """Segments of a→b lying where field <= level (used for infill/skin)."""
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(int(length / (self.cell * 0.75)), 2)
        t = np.linspace(0.0, 1.0, n)
        xs = a[0] + (b[0] - a[0]) * t
        ys = a[1] + (b[1] - a[1]) * t
        coords = np.vstack([(xs - self.x0) / self.cell, (ys - self.y0) / self.cell])
        vals = map_coordinates(self.field, coords, order=1, mode="constant", cval=1e6)
        inside = vals <= level
        segs: List[Polyline] = []
        start: Optional[int] = None
        for i, ok in enumerate(inside):
            if ok and start is None:
                start = i
            elif not ok and start is not None:
                if i - start >= 2:
                    segs.append([(xs[start], ys[start]), (xs[i - 1], ys[i - 1])])
                start = None
        if start is not None and n - start >= 2:
            segs.append([(xs[start], ys[start]), (xs[-1], ys[-1])])
        return segs


def _fill_lines(field: _Field2D, bbox: Tuple[float, float, float, float],
                spacing: float, angle_deg: float, level: float) -> List[Polyline]:
    """Parallel rectilinear lines at an angle, clipped to where field <= level."""
    x_min, y_min, x_max, y_max = bbox
    cx, cy = (x_min + x_max) / 2, (y_min + y_max) / 2
    half = math.hypot(x_max - x_min, y_max - y_min) / 2 + spacing
    ang = math.radians(angle_deg)
    dx, dy = math.cos(ang), math.sin(ang)      # line direction
    px, py = -dy, dx                            # perpendicular
    out: List[Polyline] = []
    k = -half
    flip = False
    while k <= half:
        ax, ay = cx + px * k - dx * half, cy + py * k - dy * half
        bx, by = cx + px * k + dx * half, cy + py * k + dy * half
        segs = field.clip_line((ax, ay), (bx, by), level)
        if flip:
            segs = [list(reversed(s)) for s in reversed(segs)]
        out.extend(segs)
        flip = not flip
        k += spacing
    return out


def slice_document(doc_id: str, settings: Optional[SliceSettings] = None) -> Dict:
    """Slice a document's body into G-code + stats (+ a sampled layer preview)."""
    s = settings or SliceSettings()
    doc = document_service.get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)

    active = feature_ops.resolve_datums(feature_ops.resolve_suppressed(doc.feature_tree))
    root = FeatureTree.from_dict(active).root
    sdf = sdf_from_feature(root)
    b = sdf.bounds()
    lo = np.asarray(b.min, dtype=float) * MM
    hi = np.asarray(b.max, dtype=float) * MM
    size = hi - lo
    if size[2] <= 0:
        raise ValueError("The part has no height to slice.")

    n_layers = int(math.ceil(size[2] / s.layer_height))
    if n_layers > _MAX_LAYERS:
        raise ValueError(
            f"{n_layers} layers at {s.layer_height} mm exceeds the {_MAX_LAYERS}-layer cap — "
            "raise the layer height."
        )

    # XY sample grid: half-nozzle cells, padded for the skirt, capped for cost.
    pad = 4.0 + s.skirt_loops * s.nozzle_diameter * 2
    cell = max(s.nozzle_diameter / 2.0, float(max(size[0], size[1]) + 2 * pad) / _MAX_GRID)
    x0, y0 = float(lo[0] - pad), float(lo[1] - pad)
    nx = int((size[0] + 2 * pad) / cell) + 2
    ny = int((size[1] + 2 * pad) / cell) + 2

    # Per-layer occupancy, memoized in a sliding window for the skin lookahead.
    occ_cache: "OrderedDict[int, np.ndarray]" = OrderedDict()

    def occupancy(i: int) -> np.ndarray:
        if i < 0 or i >= n_layers:
            return np.zeros((nx, ny), dtype=bool)
        hit = occ_cache.get(i)
        if hit is not None:
            occ_cache.move_to_end(i)
            return hit
        z_mid = (float(lo[2]) + s.layer_height * (i + 0.5)) / MM
        xs = x0 + np.arange(nx) * cell
        ys = y0 + np.arange(ny) * cell
        gx, gy = np.meshgrid(xs, ys, indexing="ij")
        pts = np.column_stack([gx.ravel() / MM, gy.ravel() / MM, np.full(gx.size, z_mid)])
        occ = (sdf(pts) <= 0.0).reshape(nx, ny)
        occ_cache[i] = occ
        while len(occ_cache) > 2 * s.skin_layers + 3:
            occ_cache.popitem(last=False)
        return occ

    # Place the part at the bed center, bottom on the plate.
    off_x = s.bed_center_x - float(lo[0] + size[0] / 2)
    off_y = s.bed_center_y - float(lo[1] + size[1] / 2)

    extr_w = s.nozzle_diameter * 1.1
    e_per_mm = (s.layer_height * extr_w) / (math.pi * (s.filament_diameter / 2) ** 2)
    wall_levels = [-(s.nozzle_diameter / 2 + i * extr_w) for i in range(s.wall_count)]
    inner_level = -(s.nozzle_diameter / 2 + s.wall_count * extr_w)
    sparse_spacing = extr_w / s.infill if s.infill > 0 else None
    part_bbox = (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))

    g: List[str] = [
        "; Sliced by The Forge (SDF slicer)",
        f"; part: {doc.name}",
        f"; layer_height={s.layer_height} walls={s.wall_count} infill={int(s.infill * 100)}% skins={s.skin_layers}",
        "M83 ; relative extrusion",
        "G21 ; mm",
        "G90 ; absolute moves",
        f"M140 S{s.temp_bed}",
        f"M104 S{s.temp_nozzle}",
        f"M190 S{s.temp_bed}",
        f"M109 S{s.temp_nozzle}",
        "G28 ; home",
        "M107 ; fan off for first layers",
        "G92 E0",
    ]

    time_s = 90.0  # rough overhead for homing after heat
    filament_mm = 0.0
    pos = (s.bed_center_x, s.bed_center_y)
    retracted = False
    preview: List[Dict] = []
    preview_every = max(1, n_layers // 48)

    def travel_to(p: Tuple[float, float], feed: float) -> None:
        nonlocal pos, time_s, retracted
        d = math.hypot(p[0] - pos[0], p[1] - pos[1])
        if d < 1e-6:
            return
        if s.retract_mm > 0 and d > 1.5 and not retracted:
            g.append(f"G1 E-{s.retract_mm:.3f} F{int(s.retract_speed * 60)}")
            retracted = True
        g.append(f"G0 X{p[0] + off_x:.3f} Y{p[1] + off_y:.3f} F{int(feed * 60)}")
        time_s += d / feed
        pos = p

    def extrude_polyline(poly: Polyline, feed: float, closed: bool) -> None:
        nonlocal pos, time_s, filament_mm, retracted
        travel_to(poly[0], s.speed_travel)
        if retracted:
            g.append(f"G1 E{s.retract_mm:.3f} F{int(s.retract_speed * 60)}")
            retracted = False
        path = poly + [poly[0]] if closed else poly
        for i in range(1, len(path)):
            a, b2 = path[i - 1], path[i]
            d = math.hypot(b2[0] - a[0], b2[1] - a[1])
            if d < 1e-4:
                continue
            e = d * e_per_mm
            g.append(f"G1 X{b2[0] + off_x:.3f} Y{b2[1] + off_y:.3f} E{e:.5f} F{int(feed * 60)}")
            time_s += d / feed
            filament_mm += e
            pos = b2

    for layer in range(n_layers):
        z_print = s.layer_height * (layer + 1)
        part = _Field2D(occupancy(layer), x0, y0, cell)
        feed = s.speed_first_layer if layer == 0 else s.speed_print

        g.append(f"; layer {layer + 1}/{n_layers}")
        if layer == 2 and s.fan_percent > 0:
            g.append(f"M106 S{int(s.fan_percent * 255 / 100)} ; part cooling")
        g.append(f"G1 Z{z_print:.3f} F600")
        time_s += 0.05

        walls: List[Polyline] = []
        for lvl in wall_levels:
            walls.extend(part.contours(lvl))

        # Inner region split into solid skin (near a top/bottom surface within
        # skin_layers) and sparse infill (fully interior).
        inner_mask = part.mask(inner_level)
        skin_lines: List[Polyline] = []
        sparse_lines: List[Polyline] = []
        if inner_mask.any():
            if s.skin_layers > 0:
                covered_above = np.logical_and.reduce(
                    [occupancy(layer + j) for j in range(1, s.skin_layers + 1)])
                covered_below = np.logical_and.reduce(
                    [occupancy(layer - j) for j in range(1, s.skin_layers + 1)])
                skin_mask = inner_mask & ~(covered_above & covered_below)
            else:
                skin_mask = np.zeros_like(inner_mask)
            sparse_mask = inner_mask & ~skin_mask

            if skin_mask.any():
                sf = _Field2D(skin_mask, x0, y0, cell)
                skin_lines = _fill_lines(sf, part_bbox, extr_w,
                                         45.0 if layer % 2 == 0 else 135.0, -extr_w * 0.25)
            if sparse_spacing is not None and sparse_mask.any():
                pf = _Field2D(sparse_mask, x0, y0, cell)
                sparse_lines = _fill_lines(pf, part_bbox, sparse_spacing,
                                           45.0 if layer % 2 == 0 else 135.0, -extr_w * 0.25)

        skirt: List[Polyline] = []
        if layer == 0:
            for i in range(s.skirt_loops):
                skirt.extend(part.contours(3.0 + i * extr_w))

        for poly in skirt:
            extrude_polyline(poly, feed, closed=True)
        for poly in _order_polylines(walls, pos) if walls else []:
            extrude_polyline(poly, feed, closed=True)
        for poly in _order_polylines(skin_lines, pos) if skin_lines else []:
            extrude_polyline(poly, feed, closed=False)
        for poly in _order_polylines(sparse_lines, pos) if sparse_lines else []:
            extrude_polyline(poly, feed, closed=False)

        if layer % preview_every == 0 or layer == n_layers - 1:
            def rp(polys: List[Polyline]) -> List[List[List[float]]]:
                return [[[round(x, 2), round(y, 2)] for x, y in p] for p in polys]
            preview.append({"layer": layer + 1, "z": round(z_print, 3),
                            "walls": rp(walls), "skin": rp(skin_lines), "infill": rp(sparse_lines)})

    g += [
        "M107 ; fan off",
        "M104 S0", "M140 S0",
        f"G1 E-{max(s.retract_mm, 2.0):.1f} F{int(s.retract_speed * 60)}",
        "G91", "G1 Z10 F600", "G90",
        "G28 X Y ; present the part",
        "M84 ; motors off",
    ]

    gcode = "\n".join(g) + "\n"
    fil_r_cm = s.filament_diameter / 20.0
    grams = (filament_mm / 10.0) * math.pi * fil_r_cm ** 2 * _PLA_DENSITY_G_CM3
    stats = {
        "layers": n_layers,
        "estimated_minutes": round(time_s / 60.0, 1),
        "filament_mm": round(filament_mm, 0),
        "filament_g": round(grams, 1),
        "size_mm": [round(float(v), 2) for v in size],
        "gcode_bytes": len(gcode),
        "settings": s.model_dump(),
    }
    return {"gcode": gcode, "stats": stats, "preview": preview}
