"""Mate connectors — an addressable coordinate frame you can put on a part.

Assembly mates here were four keywords that read a component's *bounding-box
extremes*: `fasten`, `coincident`, `concentric`, `distance`. That covers a
bolted stack and nothing else, and the reason is structural rather than a
missing count. Every mate was a relationship between two parts' silhouettes, so
there was nothing to place, nothing to pattern, and nowhere to say "this hole,
not that one". Adding eight more mate types to that scheme would have been
adding rows to the wrong table.

A mate connector is the table. It is a frame — origin, primary axis, in-plane
reference — that lives *in the part*, is named, and survives the part being
edited. A mate is then a relationship between two frames, and every mate type
worth having is the same operation with a different set of degrees of freedom
left free.

**Bodyless**, exactly as `SketchFeature` and `DatumPlaneFeature` are: it appears
in the tree, `delete_feature` removes it, and `feature_ops.resolve_sketches`
prunes it out before anything evaluates. The evaluator entry exists to *say* so
if the pruning is ever missed, rather than returning an empty solid that would
delete the body beside it.

**The frame is orthonormalised on read, not trusted as given.** A caller
supplying an `x_axis` that is not perpendicular to `z_axis` means the primary
direction — `z_axis` is the mate axis, the thing a revolute turns about — so `x`
is projected off it. Accepting a skewed pair would make the third axis
non-orthogonal and every rotation derived from it a shear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

import numpy as np

from app.cad_kernel.core.feature_tree import Feature, register_feature
from app.cad_kernel.core.topology import Solid
from app.cad_kernel.core.types import Point3, Vector3


class ConnectorHasNoBody(ValueError):
    """A MateConnectorFeature reached an evaluator, so it was not pruned."""


def _vd(v) -> Dict[str, float]:
    return {"x": v.x, "y": v.y, "z": v.z}


def _vec(d: Dict[str, Any], fallback) -> Vector3:
    if not d:
        return fallback
    return Vector3(float(d.get("x", 0.0)), float(d.get("y", 0.0)), float(d.get("z", 0.0)))


def _unit(v: np.ndarray, fallback) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return np.asarray(fallback, dtype=float) if n < 1e-12 else v / n


@register_feature
@dataclass
class MateConnectorFeature(Feature):
    """A named coordinate frame on a part, for assemblies to mate against."""

    origin: Point3 = field(default_factory=Point3)
    #: The mate axis — what a revolute turns about and a slider travels along.
    z_axis: Vector3 = field(default_factory=lambda: Vector3(0.0, 0.0, 1.0))
    #: In-plane reference, fixing rotation about `z_axis`.
    x_axis: Vector3 = field(default_factory=lambda: Vector3(1.0, 0.0, 0.0))

    def evaluate(self) -> Solid:
        raise ConnectorHasNoBody(
            "a mate connector is a coordinate frame and contributes no geometry; "
            "it should have been pruned by feature_ops.resolve_sketches")

    def frame(self) -> Tuple[np.ndarray, np.ndarray]:
        """`(origin, rotation)` in part space, orthonormal by construction."""
        z = _unit(np.array([self.z_axis.x, self.z_axis.y, self.z_axis.z]), (0, 0, 1))
        raw = np.array([self.x_axis.x, self.x_axis.y, self.x_axis.z], dtype=float)
        x = raw - z * float(np.dot(raw, z))
        if float(np.linalg.norm(x)) < 1e-9:
            # The caller's reference is parallel to the mate axis and says
            # nothing. Any perpendicular will do, so pick one deterministically
            # — a document must rebuild to the same frame every time.
            seed = np.array([1.0, 0.0, 0.0]) if abs(z[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
            x = seed - z * float(np.dot(seed, z))
        x = _unit(x, (1, 0, 0))
        y = np.cross(z, x)
        return (np.array([self.origin.x, self.origin.y, self.origin.z]),
                np.column_stack([x, y, z]))

    def _serialize_params(self) -> Dict[str, Any]:
        return {"origin": _vd(self.origin), "z_axis": _vd(self.z_axis),
                "x_axis": _vd(self.x_axis)}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "MateConnectorFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Mate connector"),
                   origin=Point3(**{k: float(v) for k, v in
                                    (p.get("origin") or {"x": 0, "y": 0, "z": 0}).items()}),
                   z_axis=_vec(p.get("z_axis"), Vector3(0.0, 0.0, 1.0)),
                   x_axis=_vec(p.get("x_axis"), Vector3(1.0, 0.0, 0.0)))
