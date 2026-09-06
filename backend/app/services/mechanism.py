"""How the part moves, recorded once and read everywhere.

A drivetrain drawing is mostly not dimensions. It is *which way things turn*:
this shaft runs on this axis, that spur takes 54 teeth at module 0.8 and turns
the other way from the pinion, this pocket takes a 5x11x4 bearing, that hole
takes an M2.5 cap screw. None of it is derivable from the solid — a cylinder
does not know it is a shaft, and nothing in the geometry says which way a gear
was meant to run.

The tempting shortcut is to type it onto the sheet, as free text in a
`DrawingSpec` annotation. It works exactly once. Mirror the part for the other
side of the car and every arrow on the sheet is now wrong, silently, because the
text never knew what it was describing. Regenerate the drawing at a different
scale and the notes are wherever the old sheet put them.

So a mechanism note lives **on the document**, and the drawing reads it. One
statement, one place, and a `mechanism` annotation in a `DrawingSpec` names the
note rather than repeating its words — the same rule `gdt_feature` and
`tolerance_label` already follow, and for the same reason: a callout that
restates a fact is a callout that will one day contradict it.

Four kinds, because they answer four questions a shop or an assembler asks:

- **`shaft`** — what turns, about which axis. The axis is the useful half: a
  drawing view can then say whether it is looking down the shaft or across it.
- **`gear`** — teeth, module and direction. Two meshed gears turn opposite ways,
  which is the single most common thing to get wrong when reading a drawing of a
  gear train, and the one a rotation arrow settles.
- **`bearing`** — the designation that goes in a pocket. It is in the catalog
  (`bearing-5x11x4`), so the note names it and the BOM and the drawing agree.
- **`hardware`** — the screw a hole takes. Same reasoning: `clearance_hole`
  already sized the hole from the screw, and this records *which* screw so the
  callout cannot drift from the bore.

**Direction is only meaningful with an axis**, because "clockwise" is a
statement about a viewing direction, and it is refused without one rather than
being interpreted against an arbitrary default.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

KINDS = ("shaft", "gear", "bearing", "hardware")
DIRECTIONS = ("cw", "ccw")


class MechanismError(ValueError):
    """A note that would say something the geometry cannot support."""


def _unit(axis: Optional[dict]) -> Optional[Dict[str, float]]:
    if not axis:
        return None
    vector = [float(axis.get(a, 0.0)) for a in "xyz"]
    length = sum(v * v for v in vector) ** 0.5
    if length < 1e-9:
        raise MechanismError("the axis is a zero vector; give a direction to turn about")
    return {a: round(v / length, 9) for a, v in zip("xyz", vector)}


def note(kind: str, label: str, *, axis: Optional[dict] = None,
         direction: Optional[str] = None, teeth: Optional[int] = None,
         module_mm: Optional[float] = None, designation: str = "",
         text: str = "") -> Dict[str, Any]:
    """Build one validated mechanism note."""
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise MechanismError(f"unknown kind {kind!r}; one of {', '.join(KINDS)}")
    if not str(label or "").strip():
        raise MechanismError("a note needs a label — it is what the drawing prints")

    unit = _unit(axis)
    if direction is not None:
        direction = str(direction).strip().lower()
        if direction not in DIRECTIONS:
            raise MechanismError(
                f"direction must be one of {', '.join(DIRECTIONS)}")
        if kind not in ("shaft", "gear"):
            raise MechanismError(f"a {kind} note does not turn, so it has no direction")
        if unit is None:
            # "Clockwise" is a claim about where you are standing. Without the
            # axis it is not wrong, it is unreadable — and a default axis would
            # make it confidently wrong instead.
            raise MechanismError(
                "a direction needs an axis: 'clockwise' is a statement about "
                "the direction you are looking from")
    if kind == "gear" and teeth is not None and int(teeth) < 4:
        raise MechanismError("a gear with fewer than four teeth is not a gear")

    return {
        "id": f"m{uuid.uuid4().hex[:8]}",
        "kind": kind,
        "label": str(label).strip(),
        "axis": unit,
        "direction": direction,
        "teeth": int(teeth) if teeth else None,
        "module_mm": float(module_mm) if module_mm else None,
        "designation": str(designation).strip() or None,
        "note": str(text).strip(),
    }


def describe(entry: Dict[str, Any]) -> str:
    """The line a drawing prints for this note.

    Assembled from the fields rather than stored, so a note whose teeth change
    prints the new count without anybody editing a string.
    """
    kind = entry.get("kind")
    label = entry.get("label", "")
    bits: List[str] = [label]

    if kind == "gear":
        teeth, module = entry.get("teeth"), entry.get("module_mm")
        if teeth and module:
            bits.append(f"{teeth}T m{module:g}")
        elif teeth:
            bits.append(f"{teeth}T")
    # A designation prints for every kind that carries one. A shaft's is its
    # stock size, which is exactly the thing an assembler needs and exactly the
    # thing that got dropped while this only looked at bearings and hardware.
    if entry.get("designation"):
        bits.append(str(entry["designation"]))

    if entry.get("direction"):
        bits.append("CW" if entry["direction"] == "cw" else "CCW")
    if entry.get("note"):
        bits.append(str(entry["note"]))
    return " · ".join(b for b in bits if b)


def by_id(doc, note_id: str) -> Optional[Dict[str, Any]]:
    for entry in getattr(doc, "mechanism", None) or []:
        if entry.get("id") == note_id or entry.get("label") == note_id:
            return entry
    return None


def ratio(driver: Dict[str, Any], driven: Dict[str, Any]) -> Dict[str, Any]:
    """The gear ratio between two toothed notes, and which way the output runs.

    Two externally meshed gears turn **opposite** ways. That is the fact a
    rotation arrow on a drawing exists to convey, and the one most often drawn
    wrong — so it is computed here rather than left to whoever places the arrow.
    """
    for entry in (driver, driven):
        if entry.get("kind") != "gear" or not entry.get("teeth"):
            raise MechanismError("a ratio needs two gear notes with tooth counts")
    reduction = float(driven["teeth"]) / float(driver["teeth"])
    turning = driver.get("direction")
    opposite = None
    if turning:
        opposite = "ccw" if turning == "cw" else "cw"
    return {
        "driver": driver["label"], "driven": driven["label"],
        "ratio": round(reduction, 4),
        "driver_direction": turning,
        "driven_direction": opposite,
        "note": ("externally meshed gears turn opposite ways"
                 if opposite else
                 "give the driver a direction to learn which way the output runs"),
    }
