"""Bounded request context and planner-only tools; no CAD mutations here."""
from __future__ import annotations

import re
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=160)]
Note = Annotated[str, Field(max_length=1000)]


class WorkPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(default="", max_length=4000)
    requirements: list[Note] = Field(default_factory=list, max_length=30)
    completed: list[Note] = Field(default_factory=list, max_length=30)
    remaining: list[Note] = Field(default_factory=list, max_length=30)
    decisions: list[Note] = Field(default_factory=list, max_length=20)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component_id: Optional[Identifier] = None
    feature_id: Optional[Identifier] = None
    face_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    edge_ids: list[Identifier] = Field(default_factory=list, max_length=64)


class Viewport(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    image_index: int = Field(ge=0, le=5)
    camera_direction: tuple[float, float, float]
    camera_up: tuple[float, float, float]
    projection: Literal["perspective", "orthographic"]


class RequestContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: Selection = Field(default_factory=Selection)
    task_state: Optional[WorkPlan] = None
    viewport: Optional[Viewport] = None


def resolve_selection(subject: str, obj, selection: Selection, summary: dict) -> dict:
    """Resolve only within the authorised subject; never load arbitrary IDs."""
    from app.services import cad_operations

    out = {"requested": selection.model_dump(exclude_none=True),
           "captured_at": "start of this request; re-query topology after edits",
           "unresolved": []}
    if subject == "assembly":
        if selection.component_id:
            out["component"] = next((c for c in summary["components"]
                                     if c["id"] == selection.component_id), None)
            if out["component"] is None:
                out["unresolved"].append(selection.component_id)
        out["unresolved"] += ([selection.feature_id] if selection.feature_id else []) + selection.face_ids + selection.edge_ids
    else:
        if selection.component_id:
            out["unresolved"].append(selection.component_id)
        if selection.feature_id:
            out["feature"] = next((f for f in summary["features"]
                                   if f["id"] == selection.feature_id), None)
            if out["feature"] is None:
                out["unresolved"].append(selection.feature_id)
        # Topology in the UI is listed against the whole evaluated body.
        for kind, ids in (("faces", selection.face_ids), ("edges", selection.edge_ids)):
            if not ids:
                continue
            env = cad_operations.execute(f"list_{kind}", obj.id, {}, include_state=False)
            rows = (env.get("result") or {}).get(kind) or []
            out[kind] = [row for row in rows if row.get("id") in ids]
            found = {row["id"] for row in out[kind]}
            out["unresolved"] += [i for i in ids if i not in found]
    return out


def initial_plan(goal: str, previous: Optional[WorkPlan], summary: dict) -> dict:
    if previous:
        return previous.model_dump()
    requirements = (summary.get("spec") or {}).get("requirements") or []
    return WorkPlan(objective=goal[:4000], remaining=[goal[:1000]], requirements=[
        str(row)[:1000] for row in requirements[:30]
    ]).model_dump()


_CORE = {
    "add_primitive", "add_extrude", "add_modifier", "set_feature_params",
    "get_feature", "list_features", "bounding_box", "check_geometry",
    "get_spec", "set_spec", "verify_against_spec", "set_parameter", "list_parameters",
}


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z][a-z0-9]+", text.lower())
            if len(w) > 2 and w not in {"the", "and", "for", "with", "this", "that", "part"}}


def rank_operations(subject: str, query: str) -> list[str]:
    from app.services import cad_operations
    terms = _words(query)
    scores = []
    for op in cad_operations.OPERATIONS.values():
        if op.subject != subject:
            continue
        score = 4 * len(terms & _words(op.name)) + len(terms & _words(op.description))
        if score:
            scores.append((score, op.name))
    return [name for _, name in sorted(scores, key=lambda s: (-s[0], s[1]))]


def initial_tools(subject: str, goal: str, summary: dict) -> set[str]:
    from app.services import cad_operations
    valid = {op.name for op in cad_operations.OPERATIONS.values() if op.subject == subject}
    if subject == "assembly":
        return valid  # Already a compact catalog; no reason to hide mating tools.
    names = (_CORE | set(rank_operations(subject, goal)[:8])) & valid
    if (summary.get("selection") or {}).get("faces"):
        names |= {"list_faces", "shell_faces"}
    if (summary.get("selection") or {}).get("edges"):
        names |= {"list_edges", "fillet_edges", "chamfer_edges"}
    return names & valid


def tool_directory(subject: str, enabled: set[str]) -> list[dict]:
    from app.services import cad_operations
    return [{"name": op.name, "description": op.description[:120]}
            for op in cad_operations.OPERATIONS.values()
            if op.subject == subject and op.name not in enabled]


CONTROL_RULES = """
Planning context:
- For a multi-step build, use update_work_plan to record a compact checklist of
  requirements, completed work, remaining work and explicit assumptions/decisions.
  Reconcile a saved plan with the latest request and current geometry. Do not
  repeat completed work just because it appears in the original objective.
- Update the plan at meaningful milestones or when requirements change, not
  after every query. Plan entries are notes, not proof; verify geometry/specs.
- Read the remaining planner-turn and wall-clock budget. Reserve about two
  turns for verification and handoff. If work remains, leave an accurate plan.
- Only a relevant subset of CAD schemas may be loaded. The operation directory
  lists additional operations for this subject. Use discover_operations with
  exact names or a search query to load their full schemas before calling them.
- update_work_plan and discover_operations do not mutate geometry, but do take
  a planner turn. Never invent a CAD operation or an ID to bypass discovery.
"""


def control_tools() -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "update_work_plan",
            "description": "Record or replace the current concise work checklist. Preserve original requirements and decisions when continuing; completed means observed work, not intended work.",
            "parameters": WorkPlan.model_json_schema(),
        }},
        {"type": "function", "function": {
            "name": "discover_operations",
            "description": "Load additional CAD tool schemas for this subject using exact names from the directory or a short task query. Does not execute them.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {
                "names": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "query": {"type": "string", "maxLength": 500},
            }},
        }},
    ]


def discover(subject: str, args: dict) -> list[str]:
    from app.services import cad_operations
    names = args.get("names") or []
    query = args.get("query") or ""
    if not isinstance(names, list) or len(names) > 8 or not all(isinstance(n, str) for n in names):
        raise ValueError("names must be up to eight operation names")
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("query must be a string of at most 500 characters")
    valid = {op.name for op in cad_operations.OPERATIONS.values() if op.subject == subject}
    if any(name not in valid for name in names):
        raise ValueError("Only names from this subject's operation directory can be loaded")
    found = list(dict.fromkeys(names + rank_operations(subject, query)[:8]))[:8]
    if not found:
        raise ValueError("No matching operations; use exact names from the directory")
    return found
