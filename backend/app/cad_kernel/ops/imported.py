"""Imported-mesh geometry as a feature (roadmap feature 02: universal import).

An ImportFeature carries a voxelized occupancy grid of an uploaded mesh
(STL/OBJ/PLY/faceted STEP) in the same ``{density, origin, size, threshold}``
cache shape OptimizeFeature uses, and evaluates through the same GridSDF bridge.
That makes an imported body a first-class tree node: it can be unioned/cut,
filleted, shelled, optimized — anything a native body can.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.cad_kernel.core.feature_tree import Feature
from app.cad_kernel.core.topology import Solid


@dataclass
class ImportFeature(Feature):
    cache: Optional[Dict[str, Any]] = None  # {density, origin, size, threshold}
    source: Optional[Dict[str, Any]] = None  # {filename, format, triangles, units}
    _unused: Any = field(default=None, repr=False)

    def evaluate(self) -> Solid:
        from app.cad_kernel.evaluator.sdf import evaluate_sdf, sdf_from_feature

        return evaluate_sdf(sdf_from_feature(self))

    def _serialize_params(self) -> Dict[str, Any]:
        return {"cache": self.cache, "source": self.source}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "ImportFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Import"),
            cache=p.get("cache"),
            source=p.get("source"),
        )
