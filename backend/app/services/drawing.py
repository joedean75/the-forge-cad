"""2D engineering drawings: deterministic projection, model-chosen presentation.

The split this module exists to enforce: **the kernel owns the geometry, the
caller owns the engineering judgement.** Projection, section-cutting and
hidden-line extraction are pure functions of the body — same input, same output,
every time. Which views to draw, where to cut, what to dimension, what to call
out and at what scale are decisions, and decisions belong to whoever is driving.

That was previously inverted. ``generate_drawing`` took one boolean; the views
were hardcoded front/top/right, the section plane was hardcoded to the middle of
Y, and the only dimensions were overall width and height. The kernel made every
choice and left the caller a switch. An agent could not produce a drawing that
said anything specific about a part.

Now a :class:`DrawingSpec` names the views, the section planes, the dimensions
and the annotations, and this module renders exactly that.

Two accuracy notes, because a drawing is a manufacturing document:

* Outlines contour the **signed distance field**, not a boolean occupancy mask.
  A mask pins the outline to cell boundaries, and where a sample lands exactly on
  the surface ``sdf <= 0`` counts it inside and the silhouette gains half a cell
  (a 35 mm plate-and-boss came out 35.33 mm). Contouring the field interpolates
  the crossing instead.
* Printed dimension figures come from the evaluated body, not from measuring the
  traced silhouette, so a drawing and ``bounding_box`` cannot disagree about how
  big the part is.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field as _dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from skimage import measure

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator.sdf import sdf_from_feature
from app.services import document_service, feature_ops, materials

MM = 1000.0
_GRID = 120          # samples along the longest axis
_MARGIN = 18.0       # drawing margin, mm (paper units)
_GAP = 24.0          # gap between views, mm
_TITLE_H = 26.0

Polyline = List[Tuple[float, float]]

# ISO A-series, landscape, in mm.
SHEETS: Dict[str, Tuple[float, float]] = {
    "a4": (297.0, 210.0), "a3": (420.0, 297.0), "a2": (594.0, 420.0),
    "a1": (841.0, 594.0), "a0": (1189.0, 841.0),
}

# Preferred drafting scales, largest first. A drawing states a real scale rather
# than an arbitrary fit factor, so a printed view can be measured.
_SCALE_SERIES = [10.0, 5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01]


@dataclass(frozen=True)
class ViewDef:
    """One orthographic direction, as axis indices into the sampled grid."""

    label: str
    drop: int    # world axis projected away
    ha: int      # world axis drawn horizontally
    va: int      # world axis drawn vertically
    hflip: bool = False
    vflip: bool = False


# Grid axes: 0=X, 1=Y, 2=Z. FRONT/TOP/RIGHT are unflipped; the opposing three
# are the same projection mirrored, which is what looking from the other side is.
VIEWS: Dict[str, ViewDef] = {
    "front": ViewDef("FRONT", drop=1, ha=0, va=2),
    "back": ViewDef("BACK", drop=1, ha=0, va=2, hflip=True),
    "top": ViewDef("TOP", drop=2, ha=0, va=1),
    "bottom": ViewDef("BOTTOM", drop=2, ha=0, va=1, vflip=True),
    "right": ViewDef("RIGHT", drop=0, ha=1, va=2),
    "left": ViewDef("LEFT", drop=0, ha=1, va=2, hflip=True),
}

# Where each view sits relative to FRONT, in (column, row) with row increasing
# downwards. Third angle puts a view on the side it is seen from; first angle
# projects it through to the far side, which is why they mirror.
_LAYOUT = {
    "third": {"front": (0, 0), "top": (0, -1), "bottom": (0, 1),
              "right": (1, 0), "left": (-1, 0), "back": (2, 0)},
    "first": {"front": (0, 0), "top": (0, 1), "bottom": (0, -1),
              "right": (-1, 0), "left": (1, 0), "back": (-2, 0)},
}

# Section plane -> (world axis normal to it, horizontal axis, vertical axis).
_SECTION_PLANES = {"xy": (2, 0, 1), "xz": (1, 0, 2), "yz": (0, 1, 2)}


@dataclass
class DrawingSpec:
    """What to draw. Everything here is a choice, not geometry."""

    views: List[str] = _dc_field(default_factory=lambda: ["front", "top", "right"])
    sections: List[dict] = _dc_field(default_factory=list)   # {plane, offset_mm?, label?}
    dimensions: List[dict] = _dc_field(default_factory=list)  # see _render_dimension
    annotations: List[dict] = _dc_field(default_factory=list)  # {view, at, text|gdt_feature, ...}
    projection: str = "third"        # third | first
    sheet: str = "auto"              # auto | a4 | a3 | a2 | a1 | a0
    scale: Any = "auto"              # "auto" or a number (1.0 = full size)
    show_hidden: bool = True

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "DrawingSpec":
        raw = dict(raw or {})
        spec = cls()
        for key in ("views", "sections", "dimensions", "annotations"):
            if raw.get(key) is not None:
                setattr(spec, key, list(raw[key]))
        for key in ("projection", "sheet", "scale", "show_hidden"):
            if raw.get(key) is not None:
                setattr(spec, key, raw[key])
        unknown = [v for v in spec.views if str(v).lower() not in VIEWS]
        if unknown:
            raise ValueError(f"unknown view(s) {unknown}; choose from {sorted(VIEWS)}")
        if spec.projection not in _LAYOUT:
            raise ValueError("projection must be 'first' or 'third'")
        if isinstance(spec.sheet, str) and spec.sheet.lower() not in ("auto", *SHEETS):
            raise ValueError(f"sheet must be auto or one of {sorted(SHEETS)}")
        for section in spec.sections:
            if section.get("plane", "xz") not in _SECTION_PLANES:
                raise ValueError(f"section plane must be one of {sorted(_SECTION_PLANES)}")
        return spec


# --------------------------------------------------------------- sampling --

def _field(doc) -> Tuple[np.ndarray, np.ndarray, float]:
    """Sample the body's signed distance field; return (values, lo_mm, cell_mm).

    The sampled box is padded by a couple of cells so the body never sits flush
    against the grid edge: an interpolated contour needs an outside sample to
    cross towards.
    """
    active = feature_ops.resolve_datums(feature_ops.resolve_suppressed(doc.feature_tree))
    if doc.parameters:
        from app.services import parameters
        scope, _ = parameters.resolve_values(doc.parameters)
        active = parameters.resolve_tree(active, scope)
    sdf = sdf_from_feature(FeatureTree.from_dict(active).root)
    b = sdf.bounds()
    lo = np.asarray(b.min, dtype=float)
    hi = np.asarray(b.max, dtype=float)
    cell = float(np.maximum(hi - lo, 1e-6).max()) / _GRID
    lo, hi = lo - 2.0 * cell, hi + 2.0 * cell
    dims = np.maximum(((hi - lo) / cell).astype(int) + 1, 2)
    axes = [lo[i] + np.arange(dims[i]) * cell for i in range(3)]
    gx, gy, gz = np.meshgrid(*axes, indexing="ij")
    values = sdf(np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])).reshape(dims)
    return values, lo * MM, cell * MM


def _contours(field2d: np.ndarray, o0: float, o1: float, cell: float) -> List[Polyline]:
    """Marching-squares outlines of a 2D signed field, in model mm."""
    # A contour at level 0 needs values genuinely either side of it. Testing
    # `min > 0` is not enough: a sample landing exactly on the surface gives
    # -0.0, which is not greater than 0, and marching squares then traces a
    # degenerate outline through a flat zero region.
    if field2d.size == 0 or not (field2d.min() < 0.0 < field2d.max()):
        return []
    out: List[Polyline] = []
    for c in measure.find_contours(field2d, 0.0):
        # Simplify in index units. Straight edges collapse losslessly however
        # tight this is; on a curve it bounds the error, so keep it well under a
        # cell now that the contour itself is accurate.
        c = measure.approximate_polygon(c, tolerance=0.15)
        if len(c) < 3:
            continue
        out.append([(o0 + i * cell, o1 + j * cell) for i, j in c])
    return out


def _silhouette(field: np.ndarray, drop: int) -> np.ndarray:
    """The projected outline field: closest approach along the view ray.

    Negative wherever any point on the ray is inside the body — the signed-field
    counterpart of OR-ing an occupancy mask, and unlike a mask it interpolates.
    """
    return field.min(axis=drop)


def _occluded(field: np.ndarray, drop: int, min_gap: float) -> np.ndarray:
    """A signed field whose negative region is structure hidden behind the front face.

    For every ray, look only at the span between where it enters and leaves the
    body, and take the *largest* field value there. Solid all the way through and
    that maximum stays negative. Pass through a void — a cross-drilled hole, an
    internal cavity — and it goes positive in the middle, so the zero crossing of
    the negated quantity traces exactly the outline of what is hidden.

    This is how hidden detail is recovered from an SDF with no B-rep to consult,
    and it is contoured at level 0 like every other outline here, so hidden edges
    are as accurate as visible ones rather than being quantised to cells.

    ``min_gap`` is how deep a void has to be before it counts as one. Without it a
    ray grazing the surface reads its own boundary sample as a cavity and the view
    fills with spurious dashes; below about half a cell the grid cannot resolve a
    void anyway, so there is nothing real to draw.
    """
    inside = field <= 0.0
    present = inside.any(axis=drop)

    index = np.arange(field.shape[drop]).reshape(
        [-1 if ax == drop else 1 for ax in range(3)])
    first = np.expand_dims(np.argmax(inside, axis=drop), drop)
    last = np.expand_dims(
        field.shape[drop] - 1 - np.argmax(np.flip(inside, axis=drop), axis=drop), drop)

    interior = np.where((index >= first) & (index <= last), field, -np.inf)
    gap = interior.max(axis=drop)
    # Off the body there is nothing behind anything; +1 keeps it firmly outside
    # so no contour is drawn along the visible silhouette itself.
    return np.where(present, -(gap - min_gap), 1.0)


def _oriented(polys: List[Polyline], view: ViewDef) -> List[Polyline]:
    """Mirror traced geometry for views seen from the opposite side."""
    if not (view.hflip or view.vflip):
        return polys
    hs, vs = (-1.0 if view.hflip else 1.0), (-1.0 if view.vflip else 1.0)
    return [[(h * hs, v * vs) for h, v in pl] for pl in polys]


def _trace_view(field, lo, cell, view: ViewDef, show_hidden: bool) -> Dict[str, Any]:
    visible = _contours(_transpose(_silhouette(field, view.drop), view),
                        lo[view.ha], lo[view.va], cell)
    hidden: List[Polyline] = []
    if show_hidden:
        # `field` is in world metres; `cell` is the same spacing in mm.
        occluded = _occluded(field, view.drop, min_gap=0.5 * cell / MM)
        hidden = _contours(_transpose(occluded, view), lo[view.ha], lo[view.va], cell)
    return {"visible": _oriented(visible, view), "hidden": _oriented(hidden, view)}


def _transpose(projected: np.ndarray, view: ViewDef) -> np.ndarray:
    """Put the projected plane in (horizontal, vertical) order."""
    return projected.T if view.ha > view.va else projected


def _section(field, lo, cell, plane: str, offset_mm: Optional[float]) -> Dict[str, Any]:
    """Cut the body on a plane and trace the cut face."""
    normal, ha, va = _SECTION_PLANES[plane]
    extent = field.shape[normal]
    if offset_mm is None:
        index = extent // 2
    else:
        index = int(round((float(offset_mm) - lo[normal]) / cell))
    index = max(0, min(extent - 1, index))
    sliced = np.take(field, index, axis=normal)
    if ha > va:
        sliced = sliced.T
    return {
        "visible": _contours(sliced, lo[ha], lo[va], cell),
        "hidden": [],
        "cut_at_mm": round(float(lo[normal] + index * cell), 3),
    }


# ----------------------------------------------------------------- layout --

def _bounds(polys: Sequence[Polyline]) -> Tuple[float, float, float, float]:
    xs = [p[0] for pl in polys for p in pl]
    ys = [p[1] for pl in polys for p in pl]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)


def _measure_item(item: Dict[str, Any]) -> Dict[str, Any]:
    b = _bounds(list(item["visible"]) + list(item["hidden"]))
    item["bounds"] = b
    item["w"], item["h"] = b[2] - b[0], b[3] - b[1]
    return item


def _choose_scale(content_w: float, content_h: float,
                  avail_w: float, avail_h: float) -> float:
    """Largest preferred drafting scale that still fits the sheet."""
    if content_w <= 0 or content_h <= 0:
        return 1.0
    for candidate in _SCALE_SERIES:
        if content_w * candidate <= avail_w and content_h * candidate <= avail_h:
            return candidate
    return _SCALE_SERIES[-1]


def _scale_text(scale: float) -> str:
    if abs(scale - 1.0) < 1e-9:
        return "1:1"
    if scale > 1.0:
        return f"{scale:g}:1"
    inverse = 1.0 / scale
    return f"1:{inverse:g}" if abs(inverse - round(inverse)) > 1e-9 else f"1:{round(inverse)}"


def _place_items(items: Dict[str, Dict[str, Any]], spec: DrawingSpec) -> None:
    """Assign each view/section a paper origin, in unscaled mm."""
    grid = _LAYOUT[spec.projection]
    cols = sorted({grid[name][0] for name in items if name in grid})
    rows = sorted({grid[name][1] for name in items if name in grid})

    col_w = {c: max((items[n]["w"] for n in items if n in grid and grid[n][0] == c),
                    default=0.0) for c in cols}
    row_h = {r: max((items[n]["h"] for n in items if n in grid and grid[n][1] == r),
                    default=0.0) for r in rows}

    col_x, x = {}, 0.0
    for c in cols:
        col_x[c] = x
        x += col_w[c] + _GAP
    row_y, y = {}, 0.0
    for r in rows:
        row_y[r] = y
        y += row_h[r] + _GAP

    for name, item in items.items():
        if name in grid:
            c, r = grid[name]
            item["x"], item["y"] = col_x[c], row_y[r]

    # Sections go on their own row beneath the projected views: they are not part
    # of the projection relationship, so placing them in the grid would imply one.
    section_y = y
    section_x = 0.0
    for name, item in items.items():
        if name not in grid:
            item["x"], item["y"] = section_x, section_y
            section_x += item["w"] + _GAP


# ----------------------------------------------------------------- render --

def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_STYLE = (
    '<style>'
    '.outline{fill:#f4f6f9;stroke:#1a1f27;stroke-width:0.5}'
    '.hidden{fill:none;stroke:#5a6472;stroke-width:0.3;stroke-dasharray:2 1.4}'
    '.sec{fill:url(#hatch);stroke:#1a1f27;stroke-width:0.5}'
    '.dim{stroke:#c2492a;stroke-width:0.25;fill:none}'
    '.dimt{fill:#c2492a;font-size:3px}.lbl{fill:#5a6472;font-size:3.2px}'
    '.note{fill:#1a1f27;font-size:2.8px}'
    '.frame{fill:#fff;stroke:#1a1f27;stroke-width:0.3}'
    '.lead{stroke:#1a1f27;stroke-width:0.25;fill:none}'
    '.tb{fill:none;stroke:#1a1f27;stroke-width:0.4}.tbt{fill:#1a1f27;font-size:3.2px}'
    '.tbk{fill:#5a6472;font-size:2.4px}'
    '</style>'
    # A real cross-hatch: the section used to claim hatching in prose and render
    # a flat fill.
    '<defs><pattern id="hatch" width="2.2" height="2.2" patternUnits="userSpaceOnUse" '
    'patternTransform="rotate(45)">'
    '<line x1="0" y1="0" x2="0" y2="2.2" stroke="#1a1f27" stroke-width="0.25"/>'
    '</pattern></defs>'
)


class _Sheet:
    """Maps model mm in a view to paper mm, and accumulates SVG."""

    def __init__(self, items: Dict[str, Dict[str, Any]], scale: float,
                 origin_x: float, origin_y: float):
        self.items = items
        self.scale = scale
        self.ox = origin_x
        self.oy = origin_y
        self.svg: List[str] = []

    def to_paper(self, view_name: str, h_mm: float, v_mm: float) -> Tuple[float, float]:
        """A point in a view's own model coordinates → paper coordinates."""
        item = self.items[view_name]
        b = item["bounds"]
        return (self.ox + item["x"] * self.scale + (h_mm - b[0]) * self.scale,
                self.oy + item["y"] * self.scale + (b[3] - v_mm) * self.scale)

    def path(self, view_name: str, polys: Sequence[Polyline], cls: str) -> None:
        for pl in polys:
            pts = [self.to_paper(view_name, h, v) for h, v in pl]
            d = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in pts) + " Z"
            self.svg.append(f'<path class="{cls}" d="{d}"/>')


def _render_dimension(sheet: _Sheet, dim: dict, items: Dict[str, Dict[str, Any]]) -> str:
    """One dimension. Endpoints are model mm on the view's own two axes.

    The figure printed is the true model distance between the endpoints, not the
    length of the drawn line, so a scaled drawing still states the part's size.
    """
    view_name = str(dim.get("view", "front")).lower()
    if view_name not in items:
        raise ValueError(f"dimension references view {view_name!r}, which is not drawn")

    a = dim.get("from")
    b = dim.get("to")
    if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))
            and len(a) == 2 and len(b) == 2):
        raise ValueError("dimension needs 'from' and 'to' as [h_mm, v_mm]")

    kind = str(dim.get("type", "horizontal")).lower()
    offset = float(dim.get("offset", 8.0))
    value = dim.get("label")
    if value is None:
        value = abs(b[0] - a[0]) if kind == "horizontal" else abs(b[1] - a[1])
        if kind == "aligned":
            value = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
    text = value if isinstance(value, str) else f"{float(value):.1f}"

    pa = sheet.to_paper(view_name, a[0], a[1])
    pb = sheet.to_paper(view_name, b[0], b[1])

    if kind == "vertical":
        x = min(pa[0], pb[0]) - offset
        y0, y1 = min(pa[1], pb[1]), max(pa[1], pb[1])
        mid = (y0 + y1) / 2
        return (f'<g><line class="dim" x1="{x:.2f}" y1="{y0:.2f}" x2="{x:.2f}" y2="{y1:.2f}"/>'
                f'<line class="dim" x1="{x:.2f}" y1="{y0:.2f}" x2="{pa[0]:.2f}" y2="{y0:.2f}"/>'
                f'<line class="dim" x1="{x:.2f}" y1="{y1:.2f}" x2="{pb[0]:.2f}" y2="{y1:.2f}"/>'
                f'<text class="dimt" x="{x-1:.2f}" y="{mid:.2f}" text-anchor="middle" '
                f'transform="rotate(-90 {x-1:.2f} {mid:.2f})">{_esc(text)}</text></g>')

    if kind == "aligned":
        mx, my = (pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2
        return (f'<g><line class="dim" x1="{pa[0]:.2f}" y1="{pa[1]:.2f}" '
                f'x2="{pb[0]:.2f}" y2="{pb[1]:.2f}"/>'
                f'<text class="dimt" x="{mx:.2f}" y="{my-1:.2f}" '
                f'text-anchor="middle">{_esc(text)}</text></g>')

    y = max(pa[1], pb[1]) + offset
    x0, x1 = min(pa[0], pb[0]), max(pa[0], pb[0])
    return (f'<g><line class="dim" x1="{x0:.2f}" y1="{y:.2f}" x2="{x1:.2f}" y2="{y:.2f}"/>'
            f'<line class="dim" x1="{x0:.2f}" y1="{y:.2f}" x2="{x0:.2f}" y2="{pa[1]:.2f}"/>'
            f'<line class="dim" x1="{x1:.2f}" y1="{y:.2f}" x2="{x1:.2f}" y2="{pb[1]:.2f}"/>'
            f'<text class="dimt" x="{(x0+x1)/2:.2f}" y="{y-1:.2f}" '
            f'text-anchor="middle">{_esc(text)}</text></g>')


def _annotation_text(doc, note: dict) -> str:
    """Resolve an annotation to the string it prints.

    A note may carry literal ``text``, or name a GD&T frame already on the
    document via ``gdt_feature`` — so a callout is anchored to geometry without
    being retyped, and cannot drift from the frame it came from.
    """
    from app.services import tolerances

    if note.get("text"):
        return str(note["text"])
    feature = note.get("gdt_feature")
    if feature:
        for frame in getattr(doc, "gdt", []) or []:
            if frame.feature == feature:
                return tolerances.format_fcf(frame.symbol, frame.tolerance_mm, frame.datums)
        raise ValueError(f"no GD&T frame for feature {feature!r}")
    tol_label = note.get("tolerance_label")
    if tol_label:
        for callout in getattr(doc, "tolerances", []) or []:
            if callout.label == tol_label:
                return _callout_text(callout)
        raise ValueError(f"no tolerance callout labelled {tol_label!r}")
    raise ValueError("annotation needs 'text', 'gdt_feature' or 'tolerance_label'")


def _render_annotation(sheet: _Sheet, doc, note: dict,
                       items: Dict[str, Dict[str, Any]]) -> str:
    """A leader line from a point on the geometry to a framed callout."""
    view_name = str(note.get("view", "front")).lower()
    if view_name not in items:
        raise ValueError(f"annotation references view {view_name!r}, which is not drawn")
    at = note.get("at")
    if not (isinstance(at, (list, tuple)) and len(at) == 2):
        raise ValueError("annotation needs 'at' as [h_mm, v_mm] on the view")

    text = _annotation_text(doc, note)
    anchor = sheet.to_paper(view_name, at[0], at[1])
    dx, dy = float(note.get("dx", 12.0)), float(note.get("dy", -10.0))
    tx, ty = anchor[0] + dx, anchor[1] + dy

    width = max(10.0, 1.7 * len(text) + 3.0)
    box_x = tx if dx >= 0 else tx - width
    parts = [
        f'<g><line class="lead" x1="{anchor[0]:.2f}" y1="{anchor[1]:.2f}" '
        f'x2="{tx:.2f}" y2="{ty:.2f}"/>',
        f'<circle cx="{anchor[0]:.2f}" cy="{anchor[1]:.2f}" r="0.5" fill="#1a1f27"/>',
    ]
    if note.get("frame", True):
        parts.append(f'<rect class="frame" x="{box_x:.2f}" y="{ty-3.6:.2f}" '
                     f'width="{width:.2f}" height="5.0"/>')
    parts.append(f'<text class="note" x="{box_x+1.5:.2f}" y="{ty:.2f}">{_esc(text)}</text></g>')
    return "".join(parts)


def _callout_text(callout) -> str:
    from app.services import tolerances

    if callout.kind == "fit" and callout.spec:
        try:
            if "/" in callout.spec:
                fit = tolerances.fit(callout.nominal_mm, callout.spec)
                return (f"{callout.label}: ⌀{callout.nominal_mm:g} {callout.spec} "
                        f"({fit['type']} {fit['min_clearance']:+g}/{fit['max_clearance']:+g})")
            limits = tolerances.limits_mm(callout.nominal_mm, callout.spec)
            return (f"{callout.label}: {callout.nominal_mm:g} {callout.spec} "
                    f"[{limits['min']:g}/{limits['max']:g}]")
        except ValueError:
            return f"{callout.label}: {callout.nominal_mm:g} {callout.spec}"
    return f"{callout.label}: {callout.nominal_mm:g} +{callout.plus:g}/-{callout.minus:g}"


def _tolerance_block(doc, x: float, y: float, anchored: set) -> str:
    """General note plus every callout not already anchored to the geometry."""
    from app.services import tolerances

    lines: List[str] = []
    if doc.general_tolerance:
        lines.append(f"GENERAL TOL: ISO 2768-{doc.general_tolerance}")
    for callout in getattr(doc, "tolerances", []) or []:
        if callout.label not in anchored:
            lines.append(_callout_text(callout))
    for frame in getattr(doc, "gdt", []) or []:
        if frame.feature not in anchored:
            lines.append(f"{frame.feature}: "
                         f"{tolerances.format_fcf(frame.symbol, frame.tolerance_mm, frame.datums)}")
    if not lines:
        return ""
    rows = "".join(
        f'<text class="tbk" x="{x:.1f}" y="{y + 4 + i*4.2:.1f}" font-size="2.6">{_esc(ln)}</text>'
        for i, ln in enumerate(lines)
    )
    return f"<g>{rows}</g>"


def _model_size_mm(doc) -> Tuple[float, float, float]:
    """The body's overall size from the evaluated bbox — the authoritative figure."""
    b = doc.derived.bbox
    if b is None:
        return (0.0, 0.0, 0.0)
    return (round((b.max.x - b.min.x) * MM, 3),
            round((b.max.y - b.min.y) * MM, 3),
            round((b.max.z - b.min.z) * MM, 3))


def _auto_dimensions(items: Dict[str, Dict[str, Any]], spec: DrawingSpec,
                     model_mm: Tuple[float, float, float]) -> List[dict]:
    """Overall width and height on the first drawn view.

    Only used when the caller specifies none. Explicit dimensions are the point
    of the spec; this keeps a bare `generate_drawing` call useful.
    """
    first = next((v for v in spec.views if v in items), None)
    if first is None:
        return []
    view = VIEWS[first]
    b = items[first]["bounds"]
    return [
        {"view": first, "type": "horizontal", "from": [b[0], b[1]], "to": [b[2], b[1]],
         "label": f"{model_mm[view.ha]:.1f}", "offset": 9.0},
        {"view": first, "type": "vertical", "from": [b[0], b[1]], "to": [b[0], b[3]],
         "label": f"{model_mm[view.va]:.1f}", "offset": 6.0},
    ]


def generate_drawing(doc_id: str, section: bool = True,
                     spec: Optional[dict] = None) -> Dict:
    """Render a multi-view engineering drawing. Returns svg + metadata.

    ``spec`` selects views, sections, dimensions, annotations, projection angle,
    sheet and scale. Without one, the historical default is drawn: third-angle
    front/top/right with a mid-Y section and overall dimensions.
    """
    doc = document_service.get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)
    document_service.ensure_derived(doc)

    drawing_spec = DrawingSpec.from_dict(spec)
    if spec is None and not section:
        drawing_spec.sections = []
    elif not drawing_spec.sections and section:
        drawing_spec.sections = [{"plane": "xz", "label": "A-A"}]

    field, lo, cell = _field(doc)
    model_mm = _model_size_mm(doc)

    items: Dict[str, Dict[str, Any]] = {}
    for name in drawing_spec.views:
        key = str(name).lower()
        items[key] = _measure_item(
            _trace_view(field, lo, cell, VIEWS[key], drawing_spec.show_hidden))

    section_meta: List[dict] = []
    for i, section_spec in enumerate(drawing_spec.sections):
        plane = section_spec.get("plane", "xz")
        label = section_spec.get("label") or f"{chr(ord('A') + i)}-{chr(ord('A') + i)}"
        key = f"section:{label}"
        cut = _section(field, lo, cell, plane, section_spec.get("offset_mm"))
        items[key] = _measure_item(cut)
        items[key]["label"] = f"SECTION {label}"
        section_meta.append({"label": label, "plane": plane,
                             "cut_at_mm": cut["cut_at_mm"],
                             "contours": len(cut["visible"])})

    _place_items(items, drawing_spec)

    content_w = float(max((it["x"] + it["w"] for it in items.values()), default=0.0))
    content_h = float(max((it["y"] + it["h"] for it in items.values()), default=0.0))

    sheet_name = str(drawing_spec.sheet).lower()
    if sheet_name in SHEETS:
        paper_w, paper_h = SHEETS[sheet_name]
        avail_w = paper_w - 2 * _MARGIN
        avail_h = paper_h - 2 * _MARGIN - _TITLE_H
        scale = (_choose_scale(content_w, content_h, avail_w, avail_h)
                 if drawing_spec.scale == "auto" else float(drawing_spec.scale))
    else:
        scale = 1.0 if drawing_spec.scale == "auto" else float(drawing_spec.scale)
        paper_w = float(content_w * scale + 2 * _MARGIN)
        paper_h = float(content_h * scale + 2 * _MARGIN + _TITLE_H)

    sheet = _Sheet(items, scale, _MARGIN, _MARGIN)
    sheet.svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {paper_w:.1f} {paper_h:.1f}" '
        f'font-family="monospace">')
    sheet.svg.append(_STYLE)
    sheet.svg.append(f'<rect x="1" y="1" width="{paper_w-2:.1f}" height="{paper_h-2:.1f}" '
                     f'fill="white" stroke="#1a1f27" stroke-width="0.6"/>')

    for key, item in items.items():
        is_section = key.startswith("section:")
        sheet.path(key, item["visible"], "sec" if is_section else "outline")
        if item["hidden"]:
            sheet.path(key, item["hidden"], "hidden")
        label = item.get("label") or VIEWS[key].label
        lx, ly = sheet.to_paper(key, item["bounds"][0], item["bounds"][1])
        sheet.svg.append(f'<text class="lbl" x="{lx:.1f}" y="{ly + 5:.1f}">{label}</text>')

    dimensions = drawing_spec.dimensions or _auto_dimensions(items, drawing_spec, model_mm)
    for dim in dimensions:
        sheet.svg.append(_render_dimension(sheet, dim, items))

    anchored: set = set()
    for note in drawing_spec.annotations:
        sheet.svg.append(_render_annotation(sheet, doc, note, items))
        if note.get("gdt_feature"):
            anchored.add(note["gdt_feature"])
        if note.get("tolerance_label"):
            anchored.add(note["tolerance_label"])

    mat = materials.get_material(doc.material_id)
    tb_w, tb_h = 78.0, _TITLE_H - 4
    tbx, tby = paper_w - tb_w - _MARGIN / 2, paper_h - tb_h - _MARGIN / 2
    fields = [
        ("PART", doc.name[:28]),
        ("MATERIAL", mat.name),
        ("MASS", f"{(doc.derived.mass or 0)*1000:.1f} g"),
        ("SCALE", _scale_text(scale)),
        ("UNITS", "mm"),
        ("PROJ", "3rd angle" if drawing_spec.projection == "third" else "1st angle"),
        ("DATE", _dt.date.today().isoformat()),
        ("REV", (doc.version_history[-1].label if doc.version_history else "—")[:10]),
    ]
    sheet.svg.append(f'<rect class="tb" x="{tbx:.1f}" y="{tby:.1f}" '
                     f'width="{tb_w:.1f}" height="{tb_h:.1f}"/>')
    col = tb_w / 4
    for i, (k, v) in enumerate(fields):
        cx = tbx + (i % 4) * col + 1.5
        cy = tby + (i // 4) * (tb_h / 2)
        sheet.svg.append(f'<text class="tbk" x="{cx:.1f}" y="{cy+4:.1f}">{_esc(k)}</text>')
        sheet.svg.append(f'<text class="tbt" x="{cx:.1f}" y="{cy+8.5:.1f}" '
                         f'font-size="2.8">{_esc(v)}</text>')

    sheet.svg.append(_tolerance_block(doc, _MARGIN / 2 + 1, tby, anchored))
    sheet.svg.append("</svg>")

    view_counts = {k: len(v["visible"]) for k, v in items.items() if not k.startswith("section:")}
    hidden_counts = {k: len(v["hidden"]) for k, v in items.items() if not k.startswith("section:")}
    first_view = next((v for v in drawing_spec.views if v in items), None)
    traced = items[first_view]["bounds"] if first_view else (0, 0, 0, 0)

    return {
        "svg": "\n".join(s for s in sheet.svg if s),
        # Kept flat for the existing UI, which reads front/top/right/section.
        "views": {**{k: view_counts.get(k, 0) for k in ("front", "top", "right")},
                  **view_counts,
                  "section": sum(len(items[k]["visible"]) for k in items if k.startswith("section:"))},
        # Which views were actually asked for — `views` above pads the three
        # legacy keys with zeros, so it cannot be used to tell "not drawn" from
        # "drawn but empty".
        "drawn_views": [v for v in drawing_spec.views if v in items],
        "hidden": hidden_counts,
        "sections": section_meta,
        # From the evaluated body, not measured off the traced views, so these
        # agree with bounding_box/mass_properties rather than drifting.
        "bbox_mm": {"width": model_mm[0], "depth": model_mm[1], "height": model_mm[2]},
        # What the first view actually traced; a divergence from bbox_mm means
        # the silhouette is wrong.
        "traced_mm": {"width": round(float(traced[2] - traced[0]), 3),
                      "depth": round(float(model_mm[1]), 3),
                      "height": round(float(traced[3] - traced[1]), 3)},
        "scale": float(scale),
        "scale_text": _scale_text(scale),
        "projection": drawing_spec.projection,
        "sheet_mm": {"width": round(float(paper_w), 1), "height": round(float(paper_h), 1)},
        "dimensions": len(dimensions),
        "annotations": len(drawing_spec.annotations),
        "material": mat.name,
    }


# --------------------------------------------------------------- verifier --

def check_drawing(doc_id: str, spec: Optional[dict] = None) -> dict:
    """Is this drawing a usable manufacturing document?

    The reward signal for the drawing half of the problem. A model can produce a
    drawing that renders perfectly and still says nothing about the part — no
    dimensions, a section that misses the feature it was meant to reveal, a
    callout pointing into empty space. These are the checks that catch that.
    """
    from app.models.schemas import DfmCheck

    checks: List[DfmCheck] = []
    try:
        out = generate_drawing(doc_id, spec=spec)
    except (ValueError, KeyError) as exc:
        checks.append(DfmCheck(id="renders", label="Drawing renders",
                               status="fail", detail=str(exc)))
        return _drawing_report(checks)

    checks.append(DfmCheck(id="renders", label="Drawing renders", status="pass",
                           detail=f"{out['sheet_mm']['width']:.0f}×"
                                  f"{out['sheet_mm']['height']:.0f} mm, {out['scale_text']}"))

    drawn = out["drawn_views"]
    empty = [name for name in drawn if out["views"].get(name, 0) == 0]
    checks.append(DfmCheck(
        id="views", label="Views carry geometry",
        status="pass" if not empty else "fail",
        detail=f"{len(drawn)} view(s) traced" if not empty
               else f"empty: {', '.join(sorted(empty))}",
        value=float(len(drawn))))

    n_dims = out["dimensions"]
    checks.append(DfmCheck(
        id="dimensioned", label="Part is dimensioned",
        status="pass" if n_dims >= 2 else ("warn" if n_dims else "fail"),
        detail=f"{n_dims} dimension(s)" + ("" if n_dims else " — the drawing states no sizes"),
        value=float(n_dims)))

    model, traced = out["bbox_mm"], out["traced_mm"]
    drift = max(abs(traced["width"] - model["width"]), abs(traced["height"] - model["height"]))
    checks.append(DfmCheck(
        id="fidelity", label="Views match the body",
        status="pass" if drift < 0.1 else "fail",
        detail=f"{drift:.3f} mm worst-axis difference from the evaluated bbox",
        value=round(drift, 4)))

    sections = out["sections"]
    if sections:
        empty_cuts = [s["label"] for s in sections if s["contours"] == 0]
        checks.append(DfmCheck(
            id="sections", label="Sections cut material",
            status="pass" if not empty_cuts else "fail",
            detail="all sections cut the body" if not empty_cuts
                   else f"cuts outside the body: {', '.join(empty_cuts)}",
            value=float(len(sections))))

    return _drawing_report(checks, out)


def _drawing_report(checks, out: Optional[dict] = None) -> dict:
    failed = [c.id for c in checks if c.status == "fail"]
    return {
        "valid": not failed,
        "checks": [c.model_dump() for c in checks],
        "failed": failed,
        "warnings": [c.id for c in checks if c.status == "warn"],
        **({"scale_text": out["scale_text"], "views": out["views"],
            "dimensions": out["dimensions"]} if out else {}),
    }
