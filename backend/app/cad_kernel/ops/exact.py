"""Features that need addressable topology, and so exist only on the B-rep kernel.

These are the operations a signed distance field cannot express. `FilletFeature`
next door is a smooth boolean *between bodies*; it rounds where two shapes meet
and has no way to name one edge of one shape. `ShellFeature` is `abs(d) - t/2`,
which hollows everything uniformly and cannot leave a face open. Both are useful,
neither is what an engineer means by "fillet that edge" or "shell this out
through the top face" — and those are the idioms parametric CAD runs on.

Each of these holds *references* to the entities it acts on rather than indices,
because a rebuild reorders topology (see `core/naming`). The references are
re-resolved against the freshly built child body on every evaluation, so an
upstream dimension change carries the fillet with it instead of moving it to
some unrelated edge.

There is no SDF fallback. A document using one of these needs the exact kernel,
and says so plainly rather than quietly evaluating to something else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.cad_kernel.core.feature_tree import Feature, register_feature
from app.cad_kernel.core.topology import Solid


class RequiresExactKernel(Exception):
    """This feature cannot be evaluated without OpenCascade."""


def _children_from(data: Dict[str, Any]) -> List[Feature]:
    return [Feature.from_dict(c) for c in data["params"].get("children", [])]


def _evaluate_exact(feature: Feature) -> Solid:
    from app.cad_kernel.evaluator import brep
    from app.cad_kernel.kernel import Kernel

    shape = brep.brep_from_feature(feature)
    if shape is None:
        raise RequiresExactKernel(
            f"{type(feature).__name__} needs the exact B-rep kernel "
            f"(pip install cadquery-ocp) and a body that can be built exactly")
    vertices, faces = brep.tessellate(shape)
    return Kernel.from_mesh(vertices, faces)


@register_feature
@dataclass
class EdgeFilletFeature(Feature):
    """A real fillet: named edges of the child body rounded to a radius.

    `radius_end` makes it variable. OCC's `BRepFilletAPI_MakeFillet.Add` takes
    two radii and interpolates linearly along the edge's own parametrisation,
    which is what every CAD system calls a variable-radius fillet. Left at
    zero — the default, and what every stored feature deserialises to — the
    constant-radius `Add` is called instead, byte for byte as before. That
    matters more than it looks: a law-driven fillet is a *different surface*
    from a constant one even when the two radii are equal, so routing existing
    documents through it would change bodies nobody edited.
    """

    radius: float = 0.002
    #: End radius for a variable fillet. 0 means constant — see above.
    radius_end: float = 0.0
    # Descriptors from `core.naming`, not indices — see the module docstring.
    edges: List[Dict[str, Any]] = field(default_factory=list)
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_exact(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"radius": self.radius, "radius_end": self.radius_end,
                "edges": self.edges,
                "children": [c.to_dict() for c in self.children]}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "EdgeFilletFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Fillet"),
                   radius=p.get("radius", 0.002),
                   radius_end=float(p.get("radius_end", 0.0) or 0.0),
                   edges=p.get("edges", []) or [],
                   children=_children_from(data))


@register_feature
@dataclass
class DraftFeature(Feature):
    """Taper named faces away from a pull direction so the part releases.

    Every moulded and cast part needs this, and `services/dfm_topology` has been
    *measuring* it — reporting faces that will not come out of a tool — with no
    feature able to add it. This is the treatment for the disease that report
    diagnoses.

    Exact-only, and not because of effort. Draft is a rotation of a face about
    its intersection with a neutral plane, which is a statement about *faces*;
    a sampled field has none, and the nearest field-side approximation (tapering
    the whole body) is a different solid. `dfm_topology` would then measure the
    approximation and report a draft the exact kernel would not agree with.

    `neutral_offset` moves the pivot plane along the pull direction from the
    body's own base. Zero — the default — pivots about the parting line at the
    bottom of the body, which is where a two-plate tool splits.
    """

    angle: float = 2.0                # degrees, the mould-shop default
    pull: Dict[str, float] = field(
        default_factory=lambda: {"x": 0.0, "y": 0.0, "z": 1.0})
    neutral_offset: float = 0.0
    faces: List[Dict[str, Any]] = field(default_factory=list)
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_exact(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"angle": self.angle, "pull": self.pull,
                "neutral_offset": self.neutral_offset, "faces": self.faces,
                "children": [c.to_dict() for c in self.children]}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "DraftFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Draft"),
                   angle=float(p.get("angle", 2.0)),
                   pull=p.get("pull") or {"x": 0.0, "y": 0.0, "z": 1.0},
                   neutral_offset=float(p.get("neutral_offset", 0.0) or 0.0),
                   faces=p.get("faces", []) or [],
                   children=_children_from(data))


@register_feature
@dataclass
class EdgeChamferFeature(Feature):
    """A real chamfer: named edges cut back by a distance."""

    distance: float = 0.002
    edges: List[Dict[str, Any]] = field(default_factory=list)
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_exact(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"distance": self.distance, "edges": self.edges,
                "children": [c.to_dict() for c in self.children]}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "EdgeChamferFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Chamfer"),
                   distance=p.get("distance", 0.002), edges=p.get("edges", []) or [],
                   children=_children_from(data))


@register_feature
@dataclass
class FaceShellFeature(Feature):
    """A real shell: hollowed to a wall thickness with named faces removed.

    The open faces are the point. Without them this is `ShellFeature`, which
    closes the body back up and gives you a hollow lump with no way in.
    """

    thickness: float = 0.002
    faces: List[Dict[str, Any]] = field(default_factory=list)
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_exact(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"thickness": self.thickness, "faces": self.faces,
                "children": [c.to_dict() for c in self.children]}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "FaceShellFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Shell"),
                   thickness=p.get("thickness", 0.002), faces=p.get("faces", []) or [],
                   children=_children_from(data))


@register_feature
@dataclass
class RuledLoftFeature(Feature):
    """A real loft: a ruled surface between two profiles.

    `LoftFeature` next door blends the two profiles' *distance values*, which is
    not a ruled surface — the waist of a circle-to-square transition bulges the
    way an average of two fields does, not the way a straight line drawn between
    the two rims does. Both are useful shapes and they are genuinely different,
    which is why this is a second feature rather than a better implementation of
    the first. Swapping the old one for this behind the same name would change
    what an existing document means depending on whether OCC is installed.

    `ruled=False` asks `ThruSections` for a smooth C1 surface through the
    sections instead of straight rulings — the same operation every parametric
    CAD system offers as "loft" versus "ruled loft".
    """

    profile_a: Dict[str, Any] = field(
        default_factory=lambda: {"type": "rect", "width": 0.04, "height": 0.04})
    profile_b: Dict[str, Any] = field(
        default_factory=lambda: {"type": "circle", "radius": 0.015})
    plane: str = "xy"
    offset_a: float = 0.0
    offset_b: float = 0.05
    ruled: bool = True

    def evaluate(self) -> Solid:
        return _evaluate_exact(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"profile_a": self.profile_a, "profile_b": self.profile_b,
                "plane": self.plane, "offset_a": self.offset_a,
                "offset_b": self.offset_b, "ruled": self.ruled}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "RuledLoftFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Ruled loft"),
                   profile_a=p.get("profile_a") or {"type": "rect", "width": 0.04, "height": 0.04},
                   profile_b=p.get("profile_b") or {"type": "circle", "radius": 0.015},
                   plane=p.get("plane", "xy"),
                   offset_a=float(p.get("offset_a", 0.0)),
                   offset_b=float(p.get("offset_b", 0.05)),
                   ruled=bool(p.get("ruled", True)))


@register_feature
@dataclass
class HelixFeature(Feature):
    """A round wire wound as a helix — a coil spring, an auger flight, a coil.

    Nothing here could make one before, which is why springs were absent from
    the catalog entirely. `SweepFeature` next door is a union of *capsules*
    along a polyline, so a helix approximated that way is faceted at whatever
    segment count somebody picked, and the facets are visible at any count a
    document can afford.

    Exact-only. The distance to a helix has no closed form — the usual
    field-side trick unwraps the point into helical coordinates and ignores the
    tangential term, which is an approximation that is worst exactly where the
    pitch is interesting. A body that is one shape with OCP installed and
    another without is the thing this kernel split exists to prevent.

    `pitch` is the rise per turn and `turns` may be fractional. A left-handed
    coil winds the other way, which is a real distinction for a spring that has
    to nest with something.
    """

    helix_radius: float = 0.01     # of the coil, to the wire's centreline
    wire_radius: float = 0.001     # of the round section being wound
    pitch: float = 0.004           # rise per turn
    turns: float = 5.0
    center: Dict[str, float] = field(
        default_factory=lambda: {"x": 0.0, "y": 0.0, "z": 0.0})
    axis: Dict[str, float] = field(
        default_factory=lambda: {"x": 0.0, "y": 0.0, "z": 1.0})
    right_handed: bool = True

    def evaluate(self) -> Solid:
        return _evaluate_exact(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"helix_radius": self.helix_radius, "wire_radius": self.wire_radius,
                "pitch": self.pitch, "turns": self.turns, "center": self.center,
                "axis": self.axis, "right_handed": self.right_handed}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "HelixFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Helix"),
                   helix_radius=float(p.get("helix_radius", 0.01)),
                   wire_radius=float(p.get("wire_radius", 0.001)),
                   pitch=float(p.get("pitch", 0.004)),
                   turns=float(p.get("turns", 5.0)),
                   center=p.get("center") or {"x": 0.0, "y": 0.0, "z": 0.0},
                   axis=p.get("axis") or {"x": 0.0, "y": 0.0, "z": 1.0},
                   right_handed=bool(p.get("right_handed", True)))


@register_feature
@dataclass
class ThreadFeature(Feature):
    """An ISO 68-1 metric thread, as the threaded cylinder it makes.

    A *threaded cylinder*, deliberately, rather than a groove or a ridge: union
    it and you have a stud, subtract it and you have a tapped hole, because an
    internal thread is the negative of an external one and a tap is a screw.
    One body answers both, and neither needs a flag saying which it is.

    Exact-only, like the helix it is built on. A distance field has no closed
    form for a helical surface and the field-side approximation drops the
    tangential term exactly where the pitch is the interesting part.

    **The profile is the ISO basic triangle, truncated, and given in the axial
    plane.** Over one pitch the form is a crest flat of `P/8` at the major
    diameter, two flanks at 30 degrees to the radius, and a root flat of `P/4`
    at `d1 = D - 1.0825 P`. So the material standing above the minor cylinder is
    a trapezoid `0.75 P` wide at the root narrowing to `P/8` at the crest over a
    radial depth of `5H/8`, and `(0.75P - 0.125P)/2 / (5H/8) = tan 30` — which
    is how you know the numbers are one profile rather than four.

    `length` is the threaded length, not the turn count: a thread is specified
    by how far along the shank it runs. `pitch` is the coarse pitch for the
    designation unless something else is asked for.
    """

    diameter: float = 0.008        # D, the nominal major diameter
    pitch: float = 0.00125         # rise per turn
    length: float = 0.02           # how far the thread runs
    center: Dict[str, float] = field(
        default_factory=lambda: {"x": 0.0, "y": 0.0, "z": 0.0})
    axis: Dict[str, float] = field(
        default_factory=lambda: {"x": 0.0, "y": 0.0, "z": 1.0})
    right_handed: bool = True

    def evaluate(self) -> Solid:
        return _evaluate_exact(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"diameter": self.diameter, "pitch": self.pitch,
                "length": self.length, "center": self.center,
                "axis": self.axis, "right_handed": self.right_handed}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "ThreadFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Thread"),
                   diameter=float(p.get("diameter", 0.008)),
                   pitch=float(p.get("pitch", 0.00125)),
                   length=float(p.get("length", 0.02)),
                   center=p.get("center") or {"x": 0.0, "y": 0.0, "z": 0.0},
                   axis=p.get("axis") or {"x": 0.0, "y": 0.0, "z": 1.0},
                   right_handed=bool(p.get("right_handed", True)))


@register_feature
@dataclass
class BrepBodyFeature(Feature):
    """An imported B-rep solid, carried as serialized OpenCascade geometry.

    The alternative — and what `ImportFeature` does — is to voxelise the incoming
    body into a density grid. That works for a mesh, where there is nothing
    better to recover, but throwing away a STEP file's analytic surfaces loses
    the only thing that made it worth importing: exact dimensions, faces you can
    select, and the ability to hand it back out as something another CAD system
    can edit.

    The payload is OCC's own BRep format, deflated and base64'd. It is lossless
    and cheap to re-read, which matters because the body is deserialized on every
    evaluation rather than kept in memory.
    """

    payload: str = ""                       # zlib+base64 BRep
    source: Dict[str, Any] = field(default_factory=dict)
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_exact(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"payload": self.payload, "source": self.source,
                "children": [c.to_dict() for c in self.children]}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "BrepBodyFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Imported body"),
                   payload=p.get("payload", ""), source=p.get("source", {}) or {})
