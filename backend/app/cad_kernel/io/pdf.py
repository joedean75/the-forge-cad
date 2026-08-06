"""A vector PDF writer, because a shop receives a PDF.

SVG is a sheet to read in a browser and DXF is geometry for the next tool.
Neither is what gets attached to a purchase order, and the drawing had no third
form.

**Written here rather than pulled in.** The roadmap's trap for this was "a PDF
renderer is a dependency, and an export that silently disappears in production is
worse than one that is honestly unavailable" — the OCP / LLM / Stripe pattern,
where the absent case has to be a stated `PRECONDITION`. That is the right rule
for a renderer that must exist. It is the wrong trade *here*, because the thing
being rendered is a drawing sheet: closed polylines, straight leaders, hatching
and monospaced text on one page. PDF's content stream is `m`/`l`/`S` and
`BT … Tj ET`, so the whole writer is this file and there is no absent case to
degrade to. `reportlab` would be 3 MB and a fourth way for a deployment to be
half-configured, to draw lines.

Two details are load-bearing and neither is obvious:

- **PDF counts up the page and measures in points.** SVG counts down and this
  system draws in millimetres, so every coordinate is flipped in Y and scaled by
  72/25.4 — the same flip `io/dxf` makes, for the same reason, and a drawing
  exported upside down is a drawing nobody trusts again.
- **Courier is a base-14 font, so nothing is embedded and its advance is
  exactly 0.6 em.** That is what makes `anchor="middle"` truthful: a centred
  dimension figure is centred by measurement rather than by a guess at the
  metrics of whatever font a viewer substitutes. It is also why the sheet is
  monospaced in the first place — the SVG says `font-family="monospace"`.
"""

from __future__ import annotations

import zlib
from typing import Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]

#: Millimetres to PostScript points.
MM = 72.0 / 25.4

#: Courier's advance width, in ems. Exact — it is a monospaced base-14 font.
_ADVANCE = 0.6

MIME = "application/pdf"


def _num(value: float) -> str:
    """A PDF number: fixed point, no exponent (which PDF does not accept)."""
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _pdf_string(text: str) -> bytes:
    """A literal string, with the three characters that end one escaped."""
    out = str(text).replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    # WinAnsi is a single-byte encoding; anything outside it is dropped rather
    # than mojibaked, because a title block reading "Ø12" as "Ã˜12" looks like a
    # corrupt file rather than a missing glyph.
    return b"(" + out.encode("cp1252", errors="replace") + b")"


class Page:
    """One sheet, drawn in millimetres from the top-left like the SVG it mirrors."""

    def __init__(self, width_mm: float, height_mm: float):
        self.width = float(width_mm)
        self.height = float(height_mm)
        self._ops: List[str] = []

    # -- coordinates --------------------------------------------------------

    def _xy(self, x: float, y: float) -> Tuple[float, float]:
        return x * MM, (self.height - y) * MM

    # -- drawing ------------------------------------------------------------

    def _colour(self, rgb: str, stroke: bool) -> None:
        r, g, b = _rgb(rgb)
        op = "RG" if stroke else "rg"
        self._ops.append(f"{_num(r)} {_num(g)} {_num(b)} {op}")

    def _path(self, points: Sequence[Point], close: bool) -> None:
        first, *rest = points
        x, y = self._xy(*first)
        self._ops.append(f"{_num(x)} {_num(y)} m")
        for px, py in rest:
            x, y = self._xy(px, py)
            self._ops.append(f"{_num(x)} {_num(y)} l")
        if close:
            self._ops.append("h")

    def polyline(self, points: Sequence[Point], *, stroke: Optional[str] = "#000000",
                 width_mm: float = 0.25, dash_mm: Optional[Sequence[float]] = None,
                 fill: Optional[str] = None, close: bool = True) -> None:
        """A polyline or polygon. `fill` and `stroke` are hex, either may be None."""
        if len(points) < 2:
            return
        self._ops.append("q")
        self._ops.append(f"{_num(width_mm * MM)} w")
        if dash_mm:
            pattern = " ".join(_num(d * MM) for d in dash_mm)
            self._ops.append(f"[{pattern}] 0 d")
        if fill:
            self._colour(fill, stroke=False)
        if stroke:
            self._colour(stroke, stroke=True)
        self._path(points, close)
        self._ops.append("B" if (fill and stroke) else "f" if fill else "S")
        self._ops.append("Q")

    def hatched(self, points: Sequence[Point], *, stroke: str = "#1a1f27",
                spacing_mm: float = 2.2, width_mm: float = 0.25,
                outline: Optional[str] = None, outline_width_mm: float = 0.5) -> None:
        """A closed region filled with 45° hatching — a section face.

        The lines are *clipped* to the region rather than a tiling pattern, which
        PDF supports but which needs a pattern resource dictionary per fill. The
        result is identical and the region is the only thing that ever needs one.
        """
        if len(points) < 3:
            return
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)

        self._ops.append("q")
        self._path(points, close=True)
        self._ops.append("W n")                       # clip to the region
        self._ops.append(f"{_num(width_mm * MM)} w")
        self._colour(stroke, stroke=True)
        # 45°: lines of constant (x + y), stepped by spacing * sqrt(2).
        step = spacing_mm * 1.41421356
        span = (hi_x - lo_x) + (hi_y - lo_y)
        offset = lo_x + lo_y
        n = int(span / step) + 2
        for i in range(n):
            c = offset + i * step
            self._path([(c - hi_y, hi_y), (c - lo_y, lo_y)], close=False)
            self._ops.append("S")
        self._ops.append("Q")
        if outline:
            self.polyline(points, stroke=outline, width_mm=outline_width_mm, close=True)

    def text(self, x: float, y: float, value: str, *, size_mm: float = 3.0,
             fill: str = "#000000", anchor: str = "start", rotate_deg: float = 0.0,
             at: Optional[Point] = None) -> None:
        """A run of Courier. `anchor` is `start` or `middle`; `at` is the pivot."""
        value = str(value)
        if not value:
            return
        size = size_mm * MM
        if anchor == "middle":
            x -= _ADVANCE * size_mm * len(value) / 2.0
        px, py = self._xy(x, y)
        self._ops.append("q")
        self._colour(fill, stroke=False)
        self._ops.append("BT")
        self._ops.append(f"/F1 {_num(size)} Tf")
        if rotate_deg:
            import math

            rad = math.radians(rotate_deg)
            cos, sin = math.cos(rad), math.sin(rad)
            # Rotate about the pivot the SVG named, not about the text origin —
            # a rotated dimension figure that pivots on its own baseline lands
            # somewhere else entirely.
            ox, oy = self._xy(*(at or (x, y)))
            dx, dy = px - ox, py - oy
            tx = ox + dx * cos - dy * sin
            ty = oy + dx * sin + dy * cos
            self._ops.append(f"{_num(cos)} {_num(sin)} {_num(-sin)} {_num(cos)} "
                             f"{_num(tx)} {_num(ty)} Tm")
        else:
            self._ops.append(f"{_num(px)} {_num(py)} Td")
        self._ops.append(_pdf_string(value).decode("latin-1") + " Tj")
        self._ops.append("ET")
        self._ops.append("Q")

    def content(self) -> bytes:
        return "\n".join(self._ops).encode("latin-1")


#: The CSS colour keywords the drawing stylesheet actually uses. Not a full
#: table on purpose — an unknown name here should be noticed, and the sheet
#: fill silently coming out black is exactly what happened when `white` fell
#: through to the hex parser's zero.
_NAMED = {"white": "ffffff", "black": "000000", "red": "ff0000", "gray": "808080",
          "grey": "808080"}


def _rgb(value: str) -> Tuple[float, float, float]:
    value = _NAMED.get((value or "").strip().lower(), value or "#000000").lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        return (int(value[0:2], 16) / 255.0, int(value[2:4], 16) / 255.0,
                int(value[4:6], 16) / 255.0)
    except ValueError:
        return (0.0, 0.0, 0.0)


def write_pdf(pages: Sequence[Page], *, title: str = "", producer: str = "Forge") -> bytes:
    """Serialize pages into a single PDF document.

    Content streams are Flate-compressed: a traced drawing is tens of thousands
    of `x y l` operators and they compress ~8x, which matters because this is a
    file people email.
    """
    if not pages:
        raise ValueError("a PDF needs at least one page")

    objects: List[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)          # 1-based object number

    catalog = add(b"")               # 1 — patched below, needs the pages id
    pages_obj = add(b"")             # 2 — likewise, needs the kids
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier "
               b"/Encoding /WinAnsiEncoding >>")

    kids: List[int] = []
    for page in pages:
        stream = zlib.compress(page.content())
        contents = add(b"<< /Length " + str(len(stream)).encode() +
                       b" /Filter /FlateDecode >>\nstream\n" + stream + b"\nendstream")
        kid = add(
            b"<< /Type /Page /Parent " + str(pages_obj).encode() + b" 0 R "
            b"/MediaBox [0 0 " + _num(page.width * MM).encode() + b" " +
            _num(page.height * MM).encode() + b"] "
            b"/Resources << /Font << /F1 " + str(font).encode() + b" 0 R >> >> "
            b"/Contents " + str(contents).encode() + b" 0 R >>")
        kids.append(kid)

    info = add(b"<< /Producer " + _pdf_string(producer) +
               b" /Title " + _pdf_string(title or "Drawing") + b" >>")

    objects[catalog - 1] = (b"<< /Type /Catalog /Pages " + str(pages_obj).encode() +
                            b" 0 R >>")
    objects[pages_obj - 1] = (
        b"<< /Type /Pages /Kids [" +
        b" ".join(str(k).encode() + b" 0 R" for k in kids) +
        b"] /Count " + str(len(kids)).encode() + b" >>")

    out = bytearray(b"%PDF-1.4\n")
    # A binary comment, so a transport that sniffs the first bytes treats the
    # file as binary rather than helpfully translating its line endings.
    out += b"%\xe2\xe3\xcf\xd3\n"
    offsets: List[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode() +
            b" /Root " + str(catalog).encode() + b" 0 R"
            b" /Info " + str(info).encode() + b" 0 R >>\n")
    out += b"startxref\n" + str(xref_at).encode() + b"\n%%EOF\n"
    return bytes(out)


def page_from_svg(svg: str, width_mm: float, height_mm: float,
                  styles: Optional[Dict[str, dict]] = None) -> Page:
    """Render the SVG subset `services/drawing` emits onto a page.

    Deliberately a *subset* parser, not an SVG renderer: the input is a document
    this system generated, so the vocabulary is closed — `rect`, `path` with
    `M`/`L`/`Z` only, `line`, `polygon`, `circle`, `text` and `g`. Anything else
    is ignored rather than approximated, because a drawing that silently loses a
    leader is worse than one that never had it.

    Going through the SVG rather than re-deriving the geometry is the point: the
    sheet, the scale, the placement and the title block are decided once, so the
    PDF cannot be a *different* drawing from the one on screen — which is the
    same reason `_dxf_layers` lays out from the same `_Sheet`.
    """
    import re
    import xml.etree.ElementTree as ET

    page = Page(width_mm, height_mm)
    styles = styles or {}
    root = ET.fromstring(svg)

    def style_of(element) -> dict:
        base = dict(styles.get(element.get("class") or "", {}))
        inline = element.get("style") or ""
        for declaration in inline.split(";"):
            if ":" in declaration:
                key, value = declaration.split(":", 1)
                base[key.strip()] = value.strip().removesuffix("px")
        for key in ("fill", "stroke", "stroke-width", "font-size"):
            if element.get(key) is not None:
                base[key] = element.get(key)
        return base

    def paint(style: dict) -> dict:
        fill = style.get("fill")
        stroke = style.get("stroke")
        dash = style.get("stroke-dasharray")
        return {
            # `url(#hatch)` is the section fill and is handled by the caller of
            # `polyline`; here it simply is not a colour.
            "fill": None if not fill or fill in ("none",) or fill.startswith("url(") else fill,
            "stroke": None if not stroke or stroke == "none" else stroke,
            "width_mm": float(style.get("stroke-width") or 0.25),
            "dash_mm": [float(v) for v in re.split(r"[ ,]+", dash.strip())] if dash else None,
        }

    def points_of(d: str) -> List[Point]:
        out: List[Point] = []
        for pair in re.findall(r"(-?[\d.]+)\s*,\s*(-?[\d.]+)", d):
            out.append((float(pair[0]), float(pair[1])))
        return out

    def walk(element) -> None:
        tag = element.tag.rsplit("}", 1)[-1]
        style = style_of(element)
        if tag == "rect":
            x, y = float(element.get("x", 0)), float(element.get("y", 0))
            w, h = float(element.get("width", 0)), float(element.get("height", 0))
            page.polyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], **paint(style))
        elif tag == "path":
            pts = points_of(element.get("d", ""))
            if (style.get("fill") or "").startswith("url("):
                page.hatched(pts, outline=paint(style)["stroke"],
                             outline_width_mm=paint(style)["width_mm"])
            else:
                page.polyline(pts, **paint(style))
        elif tag == "line":
            page.polyline([(float(element.get("x1", 0)), float(element.get("y1", 0))),
                           (float(element.get("x2", 0)), float(element.get("y2", 0)))],
                          close=False, **paint(style))
        elif tag == "polygon":
            page.polyline(points_of(element.get("points", "")), **paint(style))
        elif tag == "circle":
            cx, cy = float(element.get("cx", 0)), float(element.get("cy", 0))
            r = float(element.get("r", 0))
            import math

            circle = [(cx + r * math.cos(t * math.pi / 8), cy + r * math.sin(t * math.pi / 8))
                      for t in range(16)]
            page.polyline(circle, **paint(style))
        elif tag == "text":
            rotate = element.get("transform") or ""
            angle, pivot = 0.0, None
            match = re.match(r"rotate\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\)", rotate)
            if match:
                angle = float(match.group(1))
                pivot = (float(match.group(2)), float(match.group(3)))
            page.text(float(element.get("x", 0)), float(element.get("y", 0)),
                      "".join(element.itertext()),
                      size_mm=float(style.get("font-size") or 3.0),
                      fill=style.get("fill") or "#000000",
                      anchor=element.get("text-anchor") or "start",
                      # SVG rotates clockwise; PDF's matrix rotates the other
                      # way, so the sign flips or every vertical figure reads
                      # upside down.
                      rotate_deg=-angle, at=pivot)
        for child in element:
            walk(child)

    for child in root:
        if child.tag.rsplit("}", 1)[-1] in ("style", "defs"):
            continue
        walk(child)
    return page
