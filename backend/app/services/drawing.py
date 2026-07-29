"""2D engineering drawings (roadmap feature 01).

The authoritative manufacturing document. The part's SDF is sampled into an
occupancy grid once; each standard orthographic view is that grid **projected**
along a view axis (through-holes and concavities come out naturally), contoured
to outlines. A section view is a single mid-plane slice with hatching. Views are
laid out in third-angle projection with overall dimensions and a title block,
emitted as a self-contained SVG (printable / convertible; DXF & PDF are
follow-ons over the same polyline data).
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional, Tuple

import numpy as np
from skimage import measure

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator.sdf import sdf_from_feature
from app.services import document_service, feature_ops, materials

MM = 1000.0
_GRID = 120          # samples along the longest axis
_MARGIN = 18.0       # drawing margin, mm (paper units)
_GAP = 24.0          # gap between views, mm
Polyline = List[Tuple[float, float]]


def _field(doc) -> Tuple[np.ndarray, np.ndarray, float]:
    """Sample the body's signed distance field; return (values, lo_mm, cell_mm).

    Values rather than a boolean occupancy mask. A mask forces the outline onto
    cell boundaries, so a dimension is only correct to within a cell — and when a
    sample point lands exactly on the surface, ``sdf <= 0`` counts it as inside
    and the silhouette grows by half a cell. (A 35 mm plate-and-boss came out
    35.33 mm that way.) Contouring the field itself interpolates the crossing and
    removes both errors.

    The sampled box is padded by a couple of cells so the body never sits flush
    against the grid edge: an interpolated contour needs an outside sample to
    cross towards, which is also why the evaluator expands its own bounds.
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
    lo = lo - 2.0 * cell
    hi = hi + 2.0 * cell
    dims = np.maximum(((hi - lo) / cell).astype(int) + 1, 2)
    axes = [lo[i] + np.arange(dims[i]) * cell for i in range(3)]
    gx, gy, gz = np.meshgrid(*axes, indexing="ij")
    pts = np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])
    values = sdf(pts).reshape(dims)
    return values, lo * MM, cell * MM


def _contours(field: np.ndarray, o0: float, o1: float, cell: float) -> List[Polyline]:
    """Marching-squares outlines of a 2D signed field, in paper mm."""
    if field.min() > 0.0:
        return []
    out: List[Polyline] = []
    for c in measure.find_contours(field, 0.0):
        # Simplify in index units. Straight edges collapse losslessly however
        # tight this is; on a curve it bounds the error, so keep it well under a
        # cell now that the contour itself is accurate.
        c = measure.approximate_polygon(c, tolerance=0.15)
        if len(c) < 3:
            continue
        out.append([(o0 + i * cell, o1 + j * cell) for i, j in c])
    return out


# View = (label, axis projected away, horizontal grid-axis, vertical grid-axis,
#         world-mm origins for those axes).
def _view_outline(field, lo, cell, drop: int, ha: int, va: int) -> List[Polyline]:
    # The silhouette of a projection is the closest approach along the view ray:
    # negative wherever any point on that ray is inside the body. It is the
    # signed-field counterpart of OR-ing an occupancy mask, and it interpolates.
    projected = field.min(axis=drop)
    if ha > va:  # keep (horizontal, vertical) ordering of the projected field
        projected = projected.T
    return _contours(projected, lo[ha], lo[va], cell)


def _bounds(polys: List[Polyline]) -> Tuple[float, float, float, float]:
    xs = [p[0] for pl in polys for p in pl]
    ys = [p[1] for pl in polys for p in pl]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _model_size_mm(doc) -> Tuple[float, float, float]:
    """The body's overall size from the evaluated bbox — the authoritative figure."""
    b = doc.derived.bbox
    if b is None:
        return (0.0, 0.0, 0.0)
    return (round((b.max.x - b.min.x) * MM, 3),
            round((b.max.y - b.min.y) * MM, 3),
            round((b.max.z - b.min.z) * MM, 3))


def generate_drawing(doc_id: str, section: bool = True) -> Dict:
    """Build a dimensioned multi-view SVG drawing. Returns svg + metadata."""
    doc = document_service.get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)
    document_service.ensure_derived(doc)
    field, lo, cell = _field(doc)
    model_mm = _model_size_mm(doc)

    # Third-angle standard views. Grid axes: 0=X, 1=Y, 2=Z.
    front = _view_outline(field, lo, cell, drop=1, ha=0, va=2)  # look -Y → X right, Z up
    top = _view_outline(field, lo, cell, drop=2, ha=0, va=1)    # look -Z → X right, Y up
    right = _view_outline(field, lo, cell, drop=0, ha=1, va=2)  # look -X → Y right, Z up

    section_polys: List[Polyline] = []
    if section:
        mid = field.shape[1] // 2
        section_polys = _contours(field[:, mid, :], lo[0], lo[2], cell)

    fb = _bounds(front); tb = _bounds(top); rb = _bounds(right)
    fw, fh = fb[2] - fb[0], fb[3] - fb[1]
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    rw, rh = rb[2] - rb[0], rb[3] - rb[1]
    sb = _bounds(section_polys)
    sw, sh = (sb[2] - sb[0], sb[3] - sb[1]) if section_polys else (0.0, 0.0)

    # Layout: front bottom-left, top above it, right to its right (third-angle),
    # section top-right of the right view.
    x_front = _MARGIN
    y_front = _MARGIN + max(th, sh) + _GAP
    x_top = _MARGIN
    y_top = _MARGIN
    x_right = _MARGIN + max(fw, tw) + _GAP
    y_right = y_front
    x_sec = x_right + rw + _GAP if section_polys else x_right
    y_sec = _MARGIN

    title_h = 26.0
    content_w = max(x_right + rw, x_front + fw, x_sec + sw) + _MARGIN
    content_h = y_front + fh + _MARGIN + title_h

    def place(polys, ox, oy, bx0, by1):
        # Map view-local mm (y-up) to SVG paper (y-down) at offset ox,oy.
        return [[(ox + (px - bx0), oy + (by1 - py)) for px, py in pl] for pl in polys]

    def path(polys, cls):
        segs = []
        for pl in polys:
            d = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in pl) + " Z"
            segs.append(f'<path class="{cls}" d="{d}"/>')
        return "\n".join(segs)

    svg: List[str] = []
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {content_w:.1f} {content_h:.1f}" '
        f'font-family="monospace">'
    )
    svg.append(
        '<style>.outline{fill:#f4f6f9;stroke:#1a1f27;stroke-width:0.5}'
        '.sec{fill:#cdd6e2;stroke:#1a1f27;stroke-width:0.5}'
        '.dim{stroke:#c2492a;stroke-width:0.25;fill:none}'
        '.dimt{fill:#c2492a;font-size:3px}.lbl{fill:#5a6472;font-size:3.2px}'
        '.tb{fill:none;stroke:#1a1f27;stroke-width:0.4}.tbt{fill:#1a1f27;font-size:3.2px}'
        '.tbk{fill:#5a6472;font-size:2.4px}</style>'
    )
    svg.append(f'<rect x="1" y="1" width="{content_w-2:.1f}" height="{content_h-2:.1f}" '
               f'fill="white" stroke="#1a1f27" stroke-width="0.6"/>')

    # Views + labels.
    for polys, ox, oy, bx0, by1, w, h, label in (
        (front, x_front, y_front, fb[0], fb[3], fw, fh, "FRONT"),
        (top, x_top, y_top, tb[0], tb[3], tw, th, "TOP"),
        (right, x_right, y_right, rb[0], rb[3], rw, rh, "RIGHT"),
    ):
        if not polys:
            continue
        svg.append(path(place(polys, ox, oy, bx0, by1), "outline"))
        svg.append(f'<text class="lbl" x="{ox:.1f}" y="{oy + h + 5:.1f}">{label}</text>')
        # Overall width/height dimensions on the front view. The dimension *line*
        # spans the drawn view, but the number printed against it comes from the
        # evaluated body — on a manufacturing drawing the figure has to be the
        # part's real dimension, not one measured off a traced silhouette.
        if label == "FRONT":
            svg.append(_dim_h(ox, oy + h + 9, w, label=model_mm[0]))
            svg.append(_dim_v(ox - 6, oy, h, label=model_mm[2]))

    if section_polys:
        svg.append(path(place(section_polys, x_sec, y_sec, sb[0], sb[3]), "sec"))
        svg.append(f'<text class="lbl" x="{x_sec:.1f}" y="{y_sec + sh + 5:.1f}">SECTION A-A</text>')

    # Title block, bottom-right.
    mat = materials.get_material(doc.material_id)
    tb_w, tb_h = 78.0, title_h - 4
    tbx = content_w - tb_w - _MARGIN / 2
    tby = content_h - tb_h - _MARGIN / 2
    fields = [
        ("PART", doc.name[:28]),
        ("MATERIAL", mat.name),
        ("MASS", f"{(doc.derived.mass or 0)*1000:.1f} g"),
        ("DATE", _dt.date.today().isoformat()),
        ("UNITS", "mm"),
        ("REV", (doc.version_history[-1].label if doc.version_history else "—")[:10]),
    ]
    svg.append(f'<rect class="tb" x="{tbx:.1f}" y="{tby:.1f}" width="{tb_w:.1f}" height="{tb_h:.1f}"/>')
    col = tb_w / 3
    for i, (k, v) in enumerate(fields):
        cx = tbx + (i % 3) * col + 1.5
        cy = tby + (i // 3) * (tb_h / 2)
        svg.append(f'<text class="tbk" x="{cx:.1f}" y="{cy+4:.1f}">{_esc(k)}</text>')
        svg.append(f'<text class="tbt" x="{cx:.1f}" y="{cy+8.5:.1f}">{_esc(str(v))}</text>')

    # Tolerance / GD&T block (feature 02): general note + callouts + FCFs.
    svg.append(_tolerance_block(doc, _MARGIN / 2 + 1, tby))
    svg.append("</svg>")

    return {
        "svg": "\n".join(svg),
        "views": {"front": len(front), "top": len(top), "right": len(right), "section": len(section_polys)},
        # From the evaluated body, not measured off the traced views, so these
        # agree with bounding_box/mass_properties rather than drifting by the
        # contouring error.
        "bbox_mm": {"width": model_mm[0], "depth": model_mm[1], "height": model_mm[2]},
        # What the views actually traced. Equal to bbox_mm to within the
        # contour tolerance; a divergence means the silhouette is off.
        "traced_mm": {"width": round(float(fw), 3), "depth": round(float(th), 3),
                      "height": round(float(fh), 3)},
        "material": mat.name,
    }


def _tolerance_block(doc, x: float, y: float) -> str:
    """Render the general-tolerance note, fit/± callouts and GD&T frames."""
    from app.services import tolerances

    lines: List[str] = []
    if doc.general_tolerance:
        lines.append(f"GENERAL TOL: ISO 2768-{doc.general_tolerance}")
    for c in getattr(doc, "tolerances", []) or []:
        if c.kind == "fit" and c.spec:
            try:
                if "/" in c.spec:
                    f = tolerances.fit(c.nominal_mm, c.spec)
                    lines.append(f"{c.label}: ⌀{c.nominal_mm:g} {c.spec} ({f['type']} "
                                 f"{f['min_clearance']:+g}/{f['max_clearance']:+g})")
                else:
                    lim = tolerances.limits_mm(c.nominal_mm, c.spec)
                    lines.append(f"{c.label}: {c.nominal_mm:g} {c.spec} "
                                 f"[{lim['min']:g}/{lim['max']:g}]")
            except ValueError:
                lines.append(f"{c.label}: {c.nominal_mm:g} {c.spec}")
        else:
            lines.append(f"{c.label}: {c.nominal_mm:g} +{c.plus:g}/-{c.minus:g}")
    for f in getattr(doc, "gdt", []) or []:
        lines.append(f"{f.feature}: {tolerances.format_fcf(f.symbol, f.tolerance_mm, f.datums)}")

    if not lines:
        return ""
    rows = "".join(
        f'<text class="tbk" x="{x:.1f}" y="{y + 4 + i*4.2:.1f}" font-size="2.6">{_esc(ln)}</text>'
        for i, ln in enumerate(lines[:8])
    )
    return f'<g>{rows}</g>'


def _dim_h(x: float, y: float, length: float, label: Optional[float] = None) -> str:
    text = f"{label if label is not None else length:.1f}"
    return (f'<g><line class="dim" x1="{x:.1f}" y1="{y:.1f}" x2="{x+length:.1f}" y2="{y:.1f}"/>'
            f'<text class="dimt" x="{x+length/2:.1f}" y="{y-1:.1f}" text-anchor="middle">{text}</text></g>')


def _dim_v(x: float, y: float, length: float, label: Optional[float] = None) -> str:
    text = f"{label if label is not None else length:.1f}"
    return (f'<g><line class="dim" x1="{x:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{y+length:.1f}"/>'
            f'<text class="dimt" x="{x-1:.1f}" y="{y+length/2:.1f}" text-anchor="middle" '
            f'transform="rotate(-90 {x-1:.1f} {y+length/2:.1f})">{text}</text></g>')
