"""Ready-made drawing specs — the sheet somebody would have laid out.

`DrawingSpec` is deliberately open: which views, where the sections cut, what is
dimensioned, what is called out. That is right, and it is also a blank page. A
preset fills it in the way a drafter would for one purpose, from the body's own
measurements and the notes the document already carries.

The rule is the same one `templates` follows: a preset produces the *spec*, and
the caller passes it to `generate_drawing` like any other. It is not a second
drawing pipeline, so nothing here can render something the open path could not,
and a caller who wants the preset minus one section just edits the dict.

Three styles, because they answer three questions:

- **`shop`** — three views, a section through the middle, the overall
  dimensions, and every mechanism note as a callout. What goes to whoever is
  making it.
- **`print`** — the two views a printed part needs (the footprint and the
  height), overall dimensions, no section. A section on a part that is going
  straight to a slicer is a picture nobody reads.
- **`inspection`** — all six views plus a section, the overall dimensions on
  each, and the notes. What somebody checks a part against.

**What is not here, and why.** An *exploded view* needs more than one body, so
it belongs to an assembly, not to a part drawing — offering an "exploded" style
that quietly drew one view would be a label the sheet does not honour. *Detail
bubbles* need a region somebody chose, which is exactly the drafting decision
the open spec exists to keep with the caller. Both are named in `describe()` as
not offered, rather than silently absent: a caller looking for one should find
out here, not from a sheet that came out wrong.

**Dimensions are the body's own extremes.** Endpoints are model millimetres on
each view's own two axes, read off `derived.bbox`, so the printed figures come
from the same place `bounding_box` reads and the drawing cannot disagree with
the part about how big it is.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

MM = 1000.0

#: The world axes each view's horizontal and vertical run along. Mirrors
#: `drawing.VIEWS`, which is the authority — this is asserted against it in
#: `test_drawing_presets` rather than being a second definition to drift.
_VIEW_AXES = {"front": (0, 2), "back": (0, 2), "top": (0, 1),
              "bottom": (0, 1), "right": (1, 2), "left": (1, 2)}

STYLES = ("shop", "print", "inspection")

_VIEWS_BY_STYLE = {
    "shop": ["front", "top", "right"],
    "print": ["front", "top"],
    "inspection": ["front", "back", "top", "bottom", "left", "right"],
}


class PresetError(ValueError):
    """A style that does not exist, or a body with nothing to dimension."""


def _extremes_mm(doc) -> Dict[int, tuple]:
    bbox = getattr(getattr(doc, "derived", None), "bbox", None)
    if bbox is None:
        raise PresetError("this document has no body to draw")
    lo = (bbox.min.x * MM, bbox.min.y * MM, bbox.min.z * MM)
    hi = (bbox.max.x * MM, bbox.max.y * MM, bbox.max.z * MM)
    if min(hi[i] - lo[i] for i in range(3)) <= 0:
        raise PresetError("this document's body has no extent to dimension")
    return {i: (lo[i], hi[i]) for i in range(3)}


def _overall(view: str, extremes: Dict[int, tuple], gap: float) -> List[dict]:
    """The two overall dimensions on one view, placed clear of the outline."""
    h_axis, v_axis = _VIEW_AXES[view]
    h_lo, h_hi = extremes[h_axis]
    v_lo, v_hi = extremes[v_axis]
    return [
        {"view": view, "type": "horizontal",
         "from": [h_lo, v_lo], "to": [h_hi, v_lo], "offset": -gap},
        {"view": view, "type": "vertical",
         "from": [h_lo, v_lo], "to": [h_lo, v_hi], "offset": -gap},
    ]


def _callouts(view: str, notes: Sequence[dict], extremes: Dict[int, tuple],
              gap: float) -> List[dict]:
    """One annotation per mechanism note, stacked so leaders do not overlap.

    Anchored at the body's centre on this view and fanned outwards: a leader
    per note all landing on the same point would draw one box on top of another,
    which is how a sheet ends up with a note nobody can read.
    """
    h_axis, v_axis = _VIEW_AXES[view]
    h_mid = sum(extremes[h_axis]) / 2.0
    v_mid = sum(extremes[v_axis]) / 2.0
    out: List[dict] = []
    for i, note in enumerate(notes):
        out.append({"view": view, "at": [h_mid, v_mid],
                    "mechanism": note["id"],
                    "dx": gap, "dy": -gap - i * 7.0})
    return out


def spec_for(doc, style: str = "shop", *, mechanism: bool = True,
             scale: Any = "auto", sheet: str = "auto") -> Dict[str, Any]:
    """Build a complete `DrawingSpec` for one document in one style."""
    key = str(style or "").strip().lower()
    if key not in STYLES:
        raise PresetError(f"no drawing style {key!r}; one of {', '.join(STYLES)}")

    extremes = _extremes_mm(doc)
    views = list(_VIEWS_BY_STYLE[key])
    gap = max(10.0, 0.12 * max(hi - lo for lo, hi in extremes.values()))

    dimensions: List[dict] = []
    for view in (views if key == "inspection" else views[:1]):
        dimensions.extend(_overall(view, extremes, gap))
    # The top view carries the plan dimensions the front cannot: depth is not
    # visible looking at the front of anything.
    if key != "inspection" and "top" in views:
        dimensions.extend(_overall("top", extremes, gap)[1:])

    annotations: List[dict] = []
    notes = list(getattr(doc, "mechanism", None) or []) if mechanism else []
    if notes:
        annotations = _callouts(views[0], notes, extremes, gap)

    spec: Dict[str, Any] = {
        "views": views,
        "sections": ([] if key == "print" else [{"plane": "xz", "label": "A"}]),
        "dimensions": dimensions,
        "annotations": annotations,
        "projection": "third",
        "sheet": sheet,
        "scale": scale,
        "show_hidden": key != "print",
    }
    return spec


def describe() -> Dict[str, Any]:
    """What each style lays out, and what is deliberately not offered."""
    return {
        "styles": [
            {"key": "shop", "views": _VIEWS_BY_STYLE["shop"], "section": True,
             "summary": "three views, a section, overall dimensions and every "
                        "mechanism note as a callout — what goes to whoever "
                        "is making it"},
            {"key": "print", "views": _VIEWS_BY_STYLE["print"], "section": False,
             "summary": "footprint and height, no section, no hidden detail — "
                        "for a part going straight to a slicer"},
            {"key": "inspection", "views": _VIEWS_BY_STYLE["inspection"],
             "section": True,
             "summary": "all six views and a section, dimensioned on each — "
                        "what a part is checked against"},
        ],
        "not_offered": [
            {"name": "exploded view",
             "reason": "it needs more than one body, so it belongs to an "
                       "assembly drawing rather than a part drawing"},
            {"name": "detail bubble",
             "reason": "the region is the drafting decision the open spec "
                       "exists to keep with the caller; pass a section with "
                       "your own offset instead"},
        ],
        "note": ("a preset produces a DrawingSpec — pass it to generate_drawing, "
                 "or edit it first; it is not a second drawing pipeline"),
    }
