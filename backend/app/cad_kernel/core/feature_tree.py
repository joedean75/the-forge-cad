"""Parametric feature tree for the Forge CAD kernel.

A feature tree is a declarative recipe that produces a `Solid`.  Features are
serializable so designs can be stored, transferred, and regenerated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from app.cad_kernel.core.topology import Solid
from app.cad_kernel.core.types import Vector3


@dataclass
class Feature:
    """Base class for all parametric features."""

    id: Optional[str] = None
    name: str = "Feature"
    params: Dict[str, Any] = field(default_factory=dict)

    def evaluate(self) -> Solid:
        raise NotImplementedError

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.__class__.__name__,
            "id": self.id,
            "name": self.name,
            "params": self._serialize_params(),
        }

    def _serialize_params(self) -> Dict[str, Any]:
        return self.params

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Feature":
        from app.cad_kernel.ops.primitives import (
            BoxFeature,
            SphereFeature,
            CylinderFeature,
            ConeFeature,
        )
        from app.cad_kernel.ops.booleans import BooleanFeature
        from app.cad_kernel.ops.modifiers import (
            FilletFeature,
            ChamferFeature,
            ShellFeature,
            HoleFeature,
            PatternFeature,
            TransformFeature,
        )
        from app.cad_kernel.ops.generative import OptimizeFeature
        from app.cad_kernel.ops.imported import ImportFeature
        from app.cad_kernel.ops.sketch import ExtrudeFeature
        from app.cad_kernel.ops.advanced import (
            RevolveFeature,
            MirrorFeature,
            SweepFeature,
            LoftFeature,
        )

        registry = {
            "BoxFeature": BoxFeature,
            "SphereFeature": SphereFeature,
            "CylinderFeature": CylinderFeature,
            "ConeFeature": ConeFeature,
            "BooleanFeature": BooleanFeature,
            "FilletFeature": FilletFeature,
            "ChamferFeature": ChamferFeature,
            "ShellFeature": ShellFeature,
            "HoleFeature": HoleFeature,
            "PatternFeature": PatternFeature,
            "TransformFeature": TransformFeature,
            "OptimizeFeature": OptimizeFeature,
            "ImportFeature": ImportFeature,
            "ExtrudeFeature": ExtrudeFeature,
            "RevolveFeature": RevolveFeature,
            "MirrorFeature": MirrorFeature,
            "SweepFeature": SweepFeature,
            "LoftFeature": LoftFeature,
        }
        cls = registry.get(data["type"])
        if cls is None:
            raise ValueError(f"Unknown feature type: {data['type']}")
        return cls._from_dict(data)


@dataclass
class FeatureTree:
    """A rooted tree of features."""

    root: Feature
    version: str = "brep-v1"

    def evaluate(self) -> Solid:
        return self.root.evaluate()

    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "root": self.root.to_dict()}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "FeatureTree":
        return FeatureTree(root=Feature.from_dict(data["root"]), version=data.get("version", "brep-v1"))
