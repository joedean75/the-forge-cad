"""Minimal DXF R12 writer — flat patterns, and 2D drawings.

R12 ASCII is the most portable CAD interchange there is — every laser, plasma,
waterjet and router controller reads it, every drafting package imports it, and
it needs no external dependency. Only ``LINE`` entities are emitted, which keeps
the file valid everywhere (R12 predates ``LWPOLYLINE``).

Geometry is separated by layer so the reader can act on the right thing. For a
flat pattern that is the whole point of the format: ``CUT`` carries the outline
the machine should follow and ``BEND`` carries the bend lines, which are marks or
scores, never cuts — sending them on the cut layer would slice the blank in half.

A drawing uses the same mechanism for a different reason. ``VISIBLE``,
``HIDDEN``, ``SECTION`` and ``CUTPLANE`` are what a drafter turns on and off, and
``HIDDEN``/``CUTPLANE`` carry dashed linetypes so the file still reads as an
engineering drawing rather than as a pile of lines. That is the difference
between a drawing you can look at and one you can hand to a machinist: an SVG is
a picture, a DXF is geometry the next tool can measure and edit.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Point = Sequence[float]

CUT_LAYER = "CUT"
BEND_LAYER = "BEND"

VISIBLE_LAYER = "VISIBLE"
HIDDEN_LAYER = "HIDDEN"
SECTION_LAYER = "SECTION"
CUTPLANE_LAYER = "CUTPLANE"

# AutoCAD colour indices, and the linetype each layer draws with.
_LAYERS: Dict[str, Tuple[int, str]] = {
    CUT_LAYER: (7, "CONTINUOUS"),
    BEND_LAYER: (1, "CONTINUOUS"),
    VISIBLE_LAYER: (7, "CONTINUOUS"),
    HIDDEN_LAYER: (8, "DASHED"),
    SECTION_LAYER: (5, "CONTINUOUS"),
    CUTPLANE_LAYER: (1, "DASHDOT"),
}

# R12 linetype definitions: (description, total pattern length, dash/gap runs).
# Positive is pen down, negative is pen up, in drawing units (mm here).
_LINETYPES: Dict[str, Tuple[str, float, Sequence[float]]] = {
    "CONTINUOUS": ("Solid line", 0.0, ()),
    "DASHED": ("Hidden detail", 4.0, (3.0, -1.0)),
    "DASHDOT": ("Cutting plane", 10.0, (6.0, -1.0, 0.0, -1.0, 2.0, -1.0)),
}


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


def _linetype_table() -> str:
    out = _pair(0, "TABLE") + _pair(2, "LTYPE") + _pair(70, len(_LINETYPES))
    for name, (description, pattern_length, runs) in _LINETYPES.items():
        out += (_pair(0, "LTYPE") + _pair(2, name) + _pair(70, 0)
                + _pair(3, description) + _pair(72, 65)
                + _pair(73, len(runs)) + _pair(40, f"{pattern_length:.4f}"))
        for run in runs:
            out += _pair(49, f"{run:.4f}")
    return out + _pair(0, "ENDTAB")


def _tables() -> str:
    # The linetype table has to come first: a LAYER referencing a linetype that
    # has not been defined yet is what makes a reader silently fall back to
    # continuous, which is how hidden detail ends up looking like an outline.
    out = _pair(0, "SECTION") + _pair(2, "TABLES") + _linetype_table()
    out += _pair(0, "TABLE") + _pair(2, "LAYER") + _pair(70, len(_LAYERS))
    for name, (color, linetype) in _LAYERS.items():
        out += (
            _pair(0, "LAYER")
            + _pair(2, name)
            + _pair(70, 0)
            + _pair(62, color)
            + _pair(6, linetype)
        )
    return out + _pair(0, "ENDTAB") + _pair(0, "ENDSEC")


def polyline_to_segments(points: Sequence[Point], close: bool = True) -> List[Tuple[Point, Point]]:
    """Consecutive point pairs, closing the loop unless told not to.

    A cut outline and a traced silhouette are closed loops. A cutting-plane line
    and a leader are not, and closing one draws a stray segment back to its
    start — which on a drawing reads as part of the geometry.
    """
    pts = list(points)
    if len(pts) < 2:
        return []
    if close and tuple(pts[0]) != tuple(pts[-1]):
        pts.append(pts[0])
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def write_layers(layers: Dict[str, Iterable[Sequence[Point]]],
                 open_layers: Optional[Sequence[str]] = None) -> str:
    """An R12 DXF of named layers of polylines, in millimetres.

    ``open_layers`` names the layers whose polylines are open paths rather than
    closed loops. Everything a drawing traces is a loop except the cutting-plane
    lines and their arrows.
    """
    unknown = set(layers) - set(_LAYERS)
    if unknown:
        raise ValueError(f"no layer definition for {sorted(unknown)}")

    left_open = set(open_layers or ())
    body = _tables() + _pair(0, "SECTION") + _pair(2, "ENTITIES")
    for layer, polylines in layers.items():
        close = layer not in left_open
        for polyline in polylines:
            for start, end in polyline_to_segments(polyline, close=close):
                body += _line(start, end, layer)
    return body + _pair(0, "ENDSEC") + _pair(0, "EOF")


def write_dxf(
    outlines: Iterable[Sequence[Point]],
    bend_lines: Iterable[Dict] = (),
) -> str:
    """Return an R12 DXF containing closed outlines and bend lines.

    ``outlines`` are point sequences in millimetres; ``bend_lines`` are dicts
    with ``start``/``end`` points, as produced by
    :meth:`SheetMetalFeature.flat_pattern_outline`.
    """
    return write_layers({
        CUT_LAYER: list(outlines),
        BEND_LAYER: [(b["start"], b["end"]) for b in bend_lines],
    }, open_layers=[BEND_LAYER])
