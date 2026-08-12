"""Text-to-CAD: an LLM writes the feature tree, and it is validated by evaluating.

A model returns a whole kernel feature tree, which is built and meshed before it
is allowed anywhere near a document — a malformed tree is a repair attempt, not
a saved part. One repair pass is made with the concrete error fed back.

**There is no rule-based fallback, and that is deliberate.** There used to be a
487-line parser that read dimensions out of a prompt and emitted a primitive.
It was honest about being rule-based and still wrong for the job: it could
answer "a 100x50x30 mm plate" and nothing else, so a prompt describing an actual
part came back as a box with roughly the right bounding volume and none of the
described features. A generator that silently substitutes a different part is
worse than one that says it cannot help — the user cannot tell the difference by
looking, and the document keeps `source_prompt`, so the corpus filled with
(prompt -> unrelated box) pairs that are actively wrong as training data.

With no LLM configured this raises `TextToCadUnavailable`. The credential-free
path to a part is now a real one: an empty document opens on the template
gallery, and every template is an operation sequence that builds a parametric
part. That is a better answer than a parsed box and it needs no API key.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Literal, Optional, Tuple

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.kernel import Kernel
from app.models.schemas import Dimension
from app.services import llm_client as llm

_LOGGER = logging.getLogger(__name__)

Source = Literal["llm", "llm-repaired"]


class TextToCadUnavailable(RuntimeError):
    """No LLM is configured, or the model could not produce valid geometry.

    Carries `remedy` so the HTTP layer can pass on what to do instead rather
    than turning it into a bare 500.
    """

    def __init__(self, message: str, remedy: str):
        super().__init__(message)
        self.remedy = remedy


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

    One self-repair pass: the failed tree and the concrete error are fed back for
    a correction. The model may annotate the part with dimension callouts, which
    ride along here.

    Raises `TextToCadUnavailable` rather than substituting an approximation —
    see the module docstring.
    """
    if not llm.is_configured():
        raise TextToCadUnavailable(
            "text-to-CAD needs a language model and none is configured",
            remedy="set LLM_API_KEY in backend/.env, or start from a template — "
                   "list_templates / apply_template build a parametric part with "
                   "no model involved")

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

    raise TextToCadUnavailable(
        f"the model did not produce buildable geometry ({error})",
        remedy="rephrase with explicit dimensions, or start from a template and "
               "edit it")


def edit_part_tree(
    current_tree: dict, instruction: str
) -> Optional[Tuple[FeatureTree, List[Dimension], Source]]:
    """Apply an LLM edit to an existing tree, or None if it cannot.

    Returns None rather than raising: an edit that fails leaves the part exactly
    as it was, which is a normal outcome the caller reports as a failed edit.
    Generation has nothing to fall back *to*, which is why it raises instead.
    """
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
