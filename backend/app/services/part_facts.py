"""Facts about a purchased part that its geometry cannot say.

`inspect_interfaces` reads a motor's shaft, bolt circle and can straight off the
body; it cannot read that the motor is the 10-turn, 1400 KV variant, because
nothing about the solid says so. The copilot asked the user for exactly that,
got an answer, and — with nowhere to put it — asked again on the next run. A
recorded fact lives on the part document, so every assembly placing that part
sees it and every instance shares it.

Three rules keep it honest:

* **Every fact says where it came from** (`source`): the user said so, it is on
  a datasheet, it is printed on the part's label, or somebody measured it. A
  fact with no provenance is a guess that has been written down.
* **Values are short and plain.** A string, a number or a boolean, with an
  optional unit. This is a place for "KV 1400", not for a datasheet; a value
  that needs a paragraph is a note, and notes are capped.
* **Geometry is refused.** A shaft diameter or a hole spacing typed in here
  would disagree with the body the first time somebody imported a revised
  model, and `inspect_interfaces` already measures those — so the names that
  describe dimensions are rejected with a pointer to it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

SOURCES = ("user", "datasheet", "label", "measured")
MAX_FACTS = 30
MAX_TEXT = 200
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
#: Names that describe dimensions. Those are measured, not recorded.
_GEOMETRIC = re.compile(
    r"(diameter|_dia$|^dia|radius|length|width|height|depth|thickness|spacing|"
    r"pitch_circle|bolt_circle|hole|shaft_(size|mm)|_mm$|_cm$|_in$)")

#: What `inspect_interfaces` lists as not in the geometry, and the fact names
#: that answer each — so a recorded fact removes the item from that list.
ANSWERS = {
    "KV rating": ("kv", "kv_rating"),
    "winding / turns": ("turns", "winding", "winds"),
    "rated current": ("current", "rated_current", "max_current", "current_a"),
    "manufacturer part number": ("part_number", "manufacturer_part_number", "mpn", "sku"),
}


class FactError(ValueError):
    """A fact that cannot be recorded, and why."""


def _clean_value(name: str, value: Any):
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise FactError(f"{name}: empty value; pass null to remove a fact")
        if len(text) > MAX_TEXT:
            raise FactError(f"{name}: {len(text)} characters (limit {MAX_TEXT}) — "
                            "a fact is a value, not a datasheet")
        return text
    raise FactError(f"{name}: a fact's value is a string, a number or a boolean")


def record(doc, facts: Dict[str, Any], source: str = "user") -> Dict[str, Any]:
    """Merge `facts` into the document's recorded facts. `None` removes one.

    Each entry is either a bare value or `{value, unit?, note?}`. Returns the
    full set as it now stands. Raises `FactError` naming the first bad entry,
    and changes nothing when it does.
    """
    if source not in SOURCES:
        raise FactError(f"source must be one of {', '.join(SOURCES)}")
    if not isinstance(facts, dict) or not facts:
        raise FactError("facts must be a non-empty object of name -> value")

    current = {k: dict(v) for k, v in (getattr(doc, "part_facts", None) or {}).items()}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for raw_name, entry in facts.items():
        name = str(raw_name).strip().lower().replace(" ", "_").replace("-", "_")
        if not _NAME.match(name):
            raise FactError(f"{raw_name!r}: names are lower_snake_case, up to 60 characters")
        if entry is None:
            current.pop(name, None)
            continue
        if _GEOMETRIC.search(name):
            raise FactError(
                f"{name!r} describes a dimension. Dimensions are measured from the "
                "body (inspect_interfaces / component_interfaces), not recorded — a "
                "typed-in size disagrees with the model the first time it is revised")
        if isinstance(entry, dict):
            if "value" not in entry:
                raise FactError(f"{name}: an object entry needs a 'value'")
            row = {"value": _clean_value(name, entry["value"])}
            for key in ("unit", "note"):
                if entry.get(key) not in (None, ""):
                    row[key] = _clean_value(f"{name}.{key}", str(entry[key]))
            unknown = set(entry) - {"value", "unit", "note"}
            if unknown:
                raise FactError(f"{name}: unknown keys {sorted(unknown)}; use value, unit, note")
        else:
            row = {"value": _clean_value(name, entry)}
        row["source"] = source
        row["recorded_at"] = now
        current[name] = row
    if len(current) > MAX_FACTS:
        raise FactError(f"{len(current)} facts on one part (limit {MAX_FACTS})")
    doc.part_facts = current
    return current


def listing(doc) -> Dict[str, Any]:
    return {k: dict(v) for k, v in (getattr(doc, "part_facts", None) or {}).items()}


def compact(doc) -> Optional[Dict[str, Any]]:
    """`{name: "value unit"}` — the form an observation carries, or None."""
    facts = getattr(doc, "part_facts", None) or {}
    if not facts:
        return None
    return {k: (f"{v['value']} {v['unit']}" if v.get("unit") else v["value"])
            for k, v in sorted(facts.items())}


def unanswered(doc, questions) -> list:
    """The items of `questions` no recorded fact answers."""
    names = set((getattr(doc, "part_facts", None) or {}).keys())
    return [q for q in questions if not names & set(ANSWERS.get(q, ()))]
