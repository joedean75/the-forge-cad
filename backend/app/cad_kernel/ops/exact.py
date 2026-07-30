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
    """A real fillet: named edges of the child body rounded to a radius."""

    radius: float = 0.002
    # Descriptors from `core.naming`, not indices — see the module docstring.
    edges: List[Dict[str, Any]] = field(default_factory=list)
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_exact(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"radius": self.radius, "edges": self.edges,
                "children": [c.to_dict() for c in self.children]}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "EdgeFilletFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Fillet"),
                   radius=p.get("radius", 0.002), edges=p.get("edges", []) or [],
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
