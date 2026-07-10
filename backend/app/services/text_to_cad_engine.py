"""Text-to-CAD engine: LLM-first feature-tree generation with a rule fallback.

Strategy:
  1. If an LLM is configured, ask it for a full kernel feature tree and *validate
     it by evaluating* (a malformed tree from the model must never reach the doc).
  2. On no LLM, an invalid tree, or geometry that fails to mesh, fall back to the
     deterministic, credential-free rule parser.

This is what makes text-to-CAD produce real multi-feature parts instead of bare
primitives, while still degrading gracefully with no API key (per CLAUDE.md).
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Literal, Optional, Tuple

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.kernel import Kernel
from app.models.schemas import Dimension
from app.services import llm_client as llm
from app.services.text_to_cad_geometry import parse_geometry_prompt

_LOGGER = logging.getLogger(__name__)

Source = Literal["llm", "llm-repaired", "rule"]


def _extract_dimensions(tree_dict: dict) -> List[Dimension]:
    """Pull any LLM-emitted dimension callouts from the tree response."""
    out: List[Dimension] = []
    for d in (tree_dict.get("dimensions") or [])[:12]:
        try:
            a, b = d["a"], d["b"]
            out.append(
                Dimension(
                    id=str(uuid.uuid4()),
                    a={"x": float(a["x"]), "y": float(a["y"]), "z": float(a["z"])},
                    b={"x": float(b["x"]), "y": float(b["y"]), "z": float(b["z"])},
                    label=d.get("label"),
                    source="llm",
                )
            )
        except Exception:
            continue
    return out

# Resolution used only to validate that an LLM tree meshes into real geometry.
_VALIDATE_RES = 48


def _try_tree(tree_dict: Optional[dict]) -> Tuple[Optional[FeatureTree], Optional[str]]:
    """Validate a candidate tree dict; return (tree, None) or (None, error)."""
    if not tree_dict:
        return None, "model returned no tree"
    try:
        tree = FeatureTree.from_dict(tree_dict)
    except Exception as exc:
        return None, f"invalid feature tree: {exc}"
    try:
        solid = Kernel.evaluate_tree(tree, resolution=_VALIDATE_RES)
    except Exception as exc:
        return None, f"geometry failed to evaluate: {exc}"
    if sum(len(f.mesh.faces) for f in solid.shell.faces) == 0:
        return None, "geometry meshed empty (features do not overlap / no material)"
    return tree, None


def generate_part_tree(prompt: str) -> Tuple[FeatureTree, List[Dimension], Source]:
    """Return (feature_tree, dimensions, source).

    LLM-first with one self-repair pass (the failed tree + error are fed back for
    a correction), then the deterministic rule parser as a final fallback. The LLM
    may annotate the part with dimension callouts, which ride along here.
    """
    if llm.is_configured():
        tree_dict = llm.generate_feature_tree(prompt)
        tree, error = _try_tree(tree_dict)
        if tree is not None:
            return tree, _extract_dimensions(tree_dict), "llm"

        # One repair attempt with the concrete error.
        if tree_dict is not None and error:
            _LOGGER.info("LLM tree failed (%s); attempting repair", error)
            repaired = llm.repair_feature_tree(prompt, tree_dict, error)
            tree, error = _try_tree(repaired)
            if tree is not None:
                return tree, _extract_dimensions(repaired), "llm-repaired"
            _LOGGER.info("LLM repair failed (%s); falling back to rule parser", error)

    return parse_geometry_prompt(prompt), [], "rule"


def edit_part_tree(
    current_tree: dict, instruction: str
) -> Optional[Tuple[FeatureTree, List[Dimension], Source]]:
    """Apply an LLM edit to an existing tree. Returns None if it can't produce
    valid geometry (there is no rule-based fallback for edits)."""
    if not llm.is_configured():
        return None

    tree_dict = llm.edit_feature_tree(instruction, current_tree)
    tree, error = _try_tree(tree_dict)
    if tree is not None:
        return tree, _extract_dimensions(tree_dict), "llm"

    if tree_dict is not None and error:
        _LOGGER.info("LLM edit failed (%s); attempting repair", error)
        repaired = llm.repair_feature_tree(instruction, tree_dict, error)
        tree, error = _try_tree(repaired)
        if tree is not None:
            return tree, _extract_dimensions(repaired), "llm-repaired"

    return None
