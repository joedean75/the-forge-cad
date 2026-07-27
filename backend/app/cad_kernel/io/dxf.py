"""Minimal DXF R12 writer for flat patterns.

R12 ASCII is the most portable CAD interchange there is — every laser, plasma,
waterjet and router controller reads it, and it needs no external dependency.
Only ``LINE`` entities are emitted, which keeps the file valid everywhere (R12
predates ``LWPOLYLINE``).

Geometry is separated by layer so a cutter can act on the right thing: ``CUT``
carries the outline it should follow, ``BEND`` carries the bend lines, which are
marks or scores, never cuts. Sending bend lines on the cut layer would slice the
blank in half, so the split is the whole point of the format here.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

Point = Sequence[float]

CUT_LAYER = "CUT"
BEND_LAYER = "BEND"

# AutoCAD colour indices: white outline, red bend lines.
_LAYER_COLORS = {CUT_LAYER: 7, BEND_LAYER: 1}


def _pair(code: int, value) -> str:
    return f"{code}\n{value}\n"


def _line(start: Point, end: Point, layer: str) -> str:
    return (
        _pair(0, "LINE")
        + _pair(8, layer)
        + _pair(10, f"{float(start[0]):.4f}")
        + _pair(20, f"{float(start[1]):.4f}")
        + _pair(30, "0.0")
        + _pair(11, f"{float(end[0]):.4f}")
        + _pair(21, f"{float(end[1]):.4f}")
        + _pair(31, "0.0")
    )


def _tables() -> str:
    out = _pair(0, "SECTION") + _pair(2, "TABLES") + _pair(0, "TABLE") + _pair(2, "LAYER")
    out += _pair(70, len(_LAYER_COLORS))
    for name, color in _LAYER_COLORS.items():
        out += (
            _pair(0, "LAYER")
            + _pair(2, name)
            + _pair(70, 0)
            + _pair(62, color)
            + _pair(6, "CONTINUOUS")
        )
    return out + _pair(0, "ENDTAB") + _pair(0, "ENDSEC")


def polyline_to_segments(points: Sequence[Point]) -> List[Tuple[Point, Point]]:
    """Consecutive point pairs, closing the loop if it isn't already closed."""
    pts = list(points)
    if len(pts) < 2:
        return []
    if tuple(pts[0]) != tuple(pts[-1]):
        pts.append(pts[0])
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def write_dxf(
    outlines: Iterable[Sequence[Point]],
    bend_lines: Iterable[Dict] = (),
) -> str:
    """Return an R12 DXF containing closed outlines and bend lines.

    ``outlines`` are point sequences in millimetres; ``bend_lines`` are dicts
    with ``start``/``end`` points, as produced by
    :meth:`SheetMetalFeature.flat_pattern_outline`.
    """
    body = _tables() + _pair(0, "SECTION") + _pair(2, "ENTITIES")
    for outline in outlines:
        for start, end in polyline_to_segments(outline):
            body += _line(start, end, CUT_LAYER)
    for bend in bend_lines:
        body += _line(bend["start"], bend["end"], BEND_LAYER)
    body += _pair(0, "ENDSEC") + _pair(0, "EOF")
    return body
