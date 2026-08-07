"""Datum planes — what a new document starts with instead of a block.

A blank document used to be a 100 mm box flagged `placeholder`, so that the
first real feature could quietly replace it. It worked, and it was the wrong
first impression twice over: the viewport opened on a part nobody asked for, and
the flag was a rule you had to know about to predict what `add_primitive` would
do. Worse, it is not what any CAD package does — every one of them opens on
three reference planes and an empty space.

So a blank document is now three `DatumPlaneFeature`s and no body. They are
ordinary features: they appear in the tree, `delete_feature` removes them, and
`add_plane` puts one back at any orientation and offset. Nothing about them is
special-cased into "the starting state".

**A plane contributes no geometry**, which is exactly the contract
`SketchFeature` already has, so it is implemented the same way:
`feature_ops.resolve_sketches` prunes it out of the evaluation tree before
anything downstream — booleans, the mesher, the bounding box, DFM — has to have
an opinion about a node that is not a solid. The evaluator entry below exists to
*say* that if the pruning is ever missed, rather than returning a plausible
empty solid that would silently delete the body it sits next to.

`size` is the drawn extent, not a boundary. The plane is mathematically
infinite; the square is a display decision, and a caller that wants a bigger one
for a bigger part should be able to say so rather than having it inferred from a
bounding box that does not exist yet on a blank document.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from app.cad_kernel.core.feature_tree import Feature, register_feature
from app.cad_kernel.core.topology import Solid

#: The three base orientations, and the axis each one's normal runs along.
PLANES = ("xy", "xz", "yz")
NORMAL_AXIS = {"xy": "z", "xz": "y", "yz": "x"}

#: What a new document opens with. Named for both the view and the plane,
#: because "Front" alone is a convention that differs between packages and
#: "XZ" alone is not what anyone says out loud.
DEFAULT_PLANES: Tuple[Tuple[str, str], ...] = (
    ("Top (XY)", "xy"),
    ("Front (XZ)", "xz"),
    ("Right (YZ)", "yz"),
)

#: Drawn extent of a new plane, in metres. Roughly the size of the parts this
#: system is usually pointed at, so the first sketch has something to sit on.
DEFAULT_SIZE = 0.12


class DatumHasNoBody(ValueError):
    """A DatumPlaneFeature reached an evaluator, so it was not pruned."""


@register_feature
@dataclass
class DatumPlaneFeature(Feature):
    """A reference plane. Contributes no geometry; sketches and views sit on it."""

    plane: str = "xy"        # xy | xz | yz
    offset: float = 0.0      # along the plane normal, metres
    size: float = DEFAULT_SIZE   # drawn extent, metres — a display decision

    def evaluate(self) -> Solid:
        raise DatumHasNoBody(
            f"datum plane {self.id} has no body of its own — it is a reference to "
            "sketch on or measure from, not material")

    def _serialize_params(self) -> Dict[str, Any]:
        return {"plane": self.plane, "offset": self.offset, "size": self.size}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "DatumPlaneFeature":
        p = data.get("params") or {}
        return cls(
            id=data.get("id"),
            name=data.get("name", "Plane"),
            plane=p.get("plane", "xy"),
            offset=p.get("offset", 0.0),
            size=p.get("size", DEFAULT_SIZE),
        )


def default_planes() -> list:
    """The serialized nodes a blank document starts with.

    Ids are assigned by the caller's allocator, not here: `feature_ops.
    IdAllocator` is what makes a replayed operation sequence reproduce a
    document byte for byte, and a node that mints its own id defeats it.
    """
    return [
        {"type": "DatumPlaneFeature", "name": name,
         "params": {"plane": plane, "offset": 0.0, "size": DEFAULT_SIZE}}
        for name, plane in DEFAULT_PLANES
    ]
