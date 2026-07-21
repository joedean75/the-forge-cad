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


def _occupancy(doc) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample the body into a boolean occupancy grid; return (occ, lo_mm, cell_mm)."""
    active = feature_ops.resolve_datums(feature_ops.resolve_suppressed(doc.feature_tree))
    if doc.parameters:
        from app.services import parameters
        scope, _ = parameters.resolve_values(doc.parameters)
        active = parameters.resolve_tree(active, scope)
    sdf = sdf_from_feature(FeatureTree.from_dict(active).root)
    b = sdf.bounds()
    lo = np.asarray(b.min, dtype=float)
    hi = np.asarray(b.max, dtype=float)
    size = np.maximum(hi - lo, 1e-6)
    cell = float(size.max()) / _GRID
    dims = np.maximum((size / cell).astype(int) + 1, 2)
    xs = lo[0] + (np.arange(dims[0]) + 0.5) * cell
    ys = lo[1] + (np.arange(dims[1]) + 0.5) * cell
    zs = lo[2] + (np.arange(dims[2]) + 0.5) * cell
    gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")
    pts = np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])
    occ = (sdf(pts) <= 0.0).reshape(dims)
    return occ, lo * MM, cell * MM


def _contours(mask: np.ndarray, o0: float, o1: float, cell: float) -> List[Polyline]:
    """Marching-squares outlines of a 2D boolean mask, in paper mm."""
    if not mask.any():
        return []
    padded = np.pad(mask.astype(float), 1)  # close outlines at the border
    out: List[Polyline] = []
    for c in measure.find_contours(padded, 0.5):
        c = measure.approximate_polygon(c, tolerance=0.6)
        if len(c) < 3:
            continue
        out.append([(o0 + (i - 1) * cell, o1 + (j - 1) * cell) for i, j in c])
    return out


# View = (label, axis projected away, horizontal grid-axis, vertical grid-axis,
#         world-mm origins for those axes).
def _view_outline(occ, lo, cell, drop: int, ha: int, va: int) -> List[Polyline]:
    mask = occ.any(axis=drop)
    if ha > va:  # keep (horizontal, vertical) ordering of the projected mask
        mask = mask.T
    return _contours(mask, lo[ha], lo[va], cell)


def _bounds(polys: List[Polyline]) -> Tuple[float, float, float, float]:
    xs = [p[0] for pl in polys for p in pl]
    ys = [p[1] for pl in polys for p in pl]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generate_drawing(doc_id: str, section: bool = True) -> Dict:
    """Build a dimensioned multi-view SVG drawing. Returns svg + metadata."""
    doc = document_service.get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)
    document_service.ensure_derived(doc)
    occ, lo, cell = _occupancy(doc)

    # Third-angle standard views. Grid axes: 0=X, 1=Y, 2=Z.
    front = _view_outline(occ, lo, cell, drop=1, ha=0, va=2)   # look -Y → X right, Z up
    top = _view_outline(occ, lo, cell, drop=2, ha=0, va=1)     # look -Z → X right, Y up
    right = _view_outline(occ, lo, cell, drop=0, ha=1, va=2)   # look -X → Y right, Z up

    section_polys: List[Polyline] = []
    if section:
        mid = occ.shape[1] // 2
        smask = occ[:, mid, :]
        section_polys = _contours(smask, lo[0], lo[2], cell)

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
        # Overall width/height dimensions on the front view.
        if label == "FRONT":
            svg.append(_dim_h(ox, oy + h + 9, w))
            svg.append(_dim_v(ox - 6, oy, h))

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
        "bbox_mm": {"width": round(float(fw), 2), "depth": round(float(th), 2), "height": round(float(fh), 2)},
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


def _dim_h(x: float, y: float, length: float) -> str:
    return (f'<g><line class="dim" x1="{x:.1f}" y1="{y:.1f}" x2="{x+length:.1f}" y2="{y:.1f}"/>'
            f'<text class="dimt" x="{x+length/2:.1f}" y="{y-1:.1f}" text-anchor="middle">{length:.1f}</text></g>')


def _dim_v(x: float, y: float, length: float) -> str:
    return (f'<g><line class="dim" x1="{x:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{y+length:.1f}"/>'
            f'<text class="dimt" x="{x-1:.1f}" y="{y+length/2:.1f}" text-anchor="middle" '
            f'transform="rotate(-90 {x-1:.1f} {y+length/2:.1f})">{length:.1f}</text></g>')
