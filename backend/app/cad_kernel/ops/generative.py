"""Generative optimization as a feature (Phase 3).

An OptimizeFeature wraps an input body and carries the cached result of a SIMP
solve (a density grid + framing). It evaluates to that cached body via a GridSDF,
so a generatively-optimized result composes with further parametric features. If
the cache is empty (not yet solved) it falls back to the input body, keeping the
document renderable at all times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.cad_kernel.core.feature_tree import Feature
from app.cad_kernel.core.topology import Solid


@dataclass
class OptimizeFeature(Feature):
    cache: Optional[Dict[str, Any]] = None  # {density, origin, size, threshold}
    setup: Optional[Dict[str, Any]] = None  # OptimizationSetup summary (provenance)
    inputs_hash: Optional[str] = None
    children: List[Feature] = field(default_factory=list)  # [input_body]

    def evaluate(self) -> Solid:
        from app.cad_kernel.evaluator.sdf import evaluate_sdf, sdf_from_feature

        return evaluate_sdf(sdf_from_feature(self))

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "cache": self.cache,
            "setup": self.setup,
            "inputs_hash": self.inputs_hash,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "OptimizeFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Optimize"),
            cache=p.get("cache"),
            setup=p.get("setup"),
            inputs_hash=p.get("inputs_hash"),
            children=[Feature.from_dict(c) for c in p.get("children", [])],
        )
