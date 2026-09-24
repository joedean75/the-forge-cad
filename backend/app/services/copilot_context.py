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
    "get_spec", "set_spec", "verify_against_spec", "set_parameters", "get_parameters",
    # Nearly every request names a material, and every live run so far spent
    # two turns on it being unloaded: refused once, then discovered.
    "set_material",
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
- Finish with finish_task: completed only for a verified build, answered for a
  report, needs_input for a clarification, paused for unfinished work. Plain
  text is an answer and does not certify that a design is complete.
- For a multi-step build, use update_work_plan to record a compact checklist of
  requirements, completed work, remaining work and explicit assumptions/decisions.
  Reconcile a saved plan with the latest request and current geometry. Do not
  repeat completed work just because it appears in the original objective.
- Update the plan at meaningful milestones or when requirements change, not
  after every query. Plan entries are notes, not proof; verify geometry/specs.
- When the design is complete, update remaining to [] and record what was
  verified. Answering a status question does not complete unfinished design
  work: keep that work in the plan even if the latest question is answered.
- Read the remaining planner-turn and wall-clock budget. If work remains,
  leave an accurate plan.
- Finishing runs the checks for you: the spec is scored, the solid is
  validated and the result is rendered for you to look at. Do not spend turns
  calling verify_against_spec or check_geometry just before finishing — call
  them only to diagnose something. Keep turns for the repairs they may ask for.
- Only a relevant subset of CAD schemas may be loaded. The operation directory
  lists additional operations for this subject. Use discover_operations with
  exact names or a search query to load their full schemas before calling them.
- update_work_plan and discover_operations do not mutate geometry, but do take
  a planner turn. Never invent a CAD operation or an ID to bypass discovery.
"""


#: What to do about a failure, keyed to the code it carries.
#:
#: Every failure already arrives with a typed code, a message and a remedy, and
#: the standing rules already cover three of the codes. What neither can tell a
#: planner is the part that only becomes visible across *several* turns: which
#: axis of the call to change, and that the loop is counting. A planner that
#: knows the third identical retry ends the run tries something else on the
#: second — and `PRECONDITION` is the case where changing the arguments looks
#: like progress and is the wrong axis entirely, so the repeat guard never even
#: trips.
PLAYBOOK = """
Reading a failure: the code says which axis to change, not just what went wrong.
- MISSING_ARG / BAD_ARG — the arguments. Fix the named one and call again.
- PRECONDITION — the order. A different *earlier* step is needed; the same call
  with different arguments fails the same way, and because the arguments
  changed, nothing will stop you repeating it.
- NO_SUCH_FEATURE — the id. Call list_features and take one from it; ids shift
  when a feature is deleted.
- EVAL_FAILED — the edit applied and the body will not evaluate. Undo it
  (delete_feature, or set_feature_params back to what worked) before building
  anything on top of it; a second edit over a broken body compounds the first.
- UNKNOWN_OP — the name. Load the real one with discover_operations rather than
  spelling one from memory.
- NO_SUCH_DOCUMENT for an id you know exists — the door. A part operation was
  sent to an assembly or the reverse, and the message names the other route.
- INTERNAL — not your arguments. Build it a different way; the same call fails
  the same way.

Three budgets end the run, so change something before you reach them: three
identical failed calls stop it, three identical reads with no edit between them
stop it, and another insert_standard_part after two catalog failures stops it —
search_assembly_parts first.
"""


#: Write down what the part is supposed to be, before building it.
#:
#: `verify_against_spec` is the reward function and `_unmet_requirements` is the
#: gate that reads it — and both are inert when nobody ever wrote a spec, which
#: was the common case: the run finished, the score was never computed, and
#: "done" meant only that the body was a valid solid. A brief recorded from the
#: request turns that gate back on.
#:
#: **Only what the request says.** A spec is scored, so an invented dimension is
#: not a harmless guess — it becomes a target the run then passes, which is a
#: false reading of the one number meant to say whether the part is right.
#: Document subject only: an assembly is judged by its mates and interference,
#: and `set_spec` does not take an assembly id.
BRIEF_RULE = """
- Write the brief before you build. On a request to build or change geometry,
  record the measurable requirements with set_spec before the first edit:
  overall size (bounding_box, in mm), holes (hole: diameter and count), and
  mass, volume, material, watertight or bodies where the request gives them.
- Record only what the request states or clearly implies. A spec is what the
  finished part is scored against, so a number you invent becomes a target this
  run will then report as met — a guess is worse than an absent requirement.
- If the state above already shows a spec, reconcile with it rather than
  rewriting it. A request with nothing measurable in it — a fillet, a rename, a
  question — needs no spec, and inventing one to have one is the failure above.
"""


def control_rules(subject: str = "document", *, planning: bool = True) -> str:
    """The standing rules, plus the ones that only make sense for this subject.

    Assembled here rather than concatenated at the call site because the planner
    prompt is built twice — once for tool-calling providers and once, from
    scratch, for prose ones — and a rule added to only one of them is a rule
    production sends to half its runs.

    ``planning=False`` drops `CONTROL_RULES`, which is about `update_work_plan`
    and `discover_operations`. The `finetuned` profile ships neither — it sends
    every operation name and no schemas — so there telling a planner to load a
    schema before calling it is an instruction it cannot follow, and 434 tokens
    of it on every turn. The playbook and the brief rule stay: those are about
    the operations that profile does have.
    """
    return ((CONTROL_RULES if planning else "") + PLAYBOOK
            + (BRIEF_RULE if subject == "document" else ""))


def control_tools() -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "finish_task", "description": "Report the outcome; only verified geometry is completed.",
            "parameters": {"type": "object", "properties": {
                "outcome": {"type": "string", "enum": ["completed", "answered", "needs_input", "paused"]},
                "summary": {"type": "string"}}, "required": ["outcome", "summary"], "additionalProperties": False}}},
        {"type": "function", "function": {
            "name": "update_work_plan",
            "description": "Update the concise work checklist; omitted fields retain their previous values. Preserve original requirements and decisions when continuing; completed means observed work, not intended work.",
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


def part_brief(asm, args: dict, parent: dict) -> tuple[dict, list[str]]:
    """Only parts in the authorised assembly can supply interface context."""
    from app.services import document_service, observation
    from app.services.op_errors import BadArgument

    ids = args.get("component_ids")
    if ids is None:
        selected = ((parent.get("selection") or {}).get("requested") or {}).get("component_id")
        ids = [selected] if selected else [c.id for c in asm.components[:8]]
    if not isinstance(ids, list) or len(ids) > 8 or not all(isinstance(i, str) for i in ids):
        raise BadArgument("component_ids must name up to eight components", arg="component_ids")
    members = {c.id: c for c in asm.components}
    if any(i not in members for i in ids):
        raise BadArgument("An interface component is not in this assembly", arg="component_ids")
    requirements = args.get("requirements") or []
    if not isinstance(requirements, list) or len(requirements) > 30 or not all(isinstance(r, str) and len(r) <= 1000 for r in requirements):
        raise BadArgument("requirements must be up to 30 short strings", arg="requirements")
    images = parent.get("images") or []
    indices = args.get("image_indices", list(range(len(images))))
    if not isinstance(indices, list) or len(indices) > 6 or not all(type(i) is int and 0 <= i < len(images) for i in indices):
        raise BadArgument("image_indices must refer to attached parent images", arg="image_indices")

    summary = parent.get("summary") or observation.summarize_assembly(asm)
    viewport = (summary.get("request_context") or {}).get("viewport")
    if viewport and viewport["image_index"] in indices:
        viewport = {**viewport, "image_index": indices.index(viewport["image_index"])}
    else:
        viewport = None
    interfaces = []
    for component in summary["components"]:
        if component["id"] not in ids:
            continue
        part = document_service.get_document(component["part_id"])
        item = dict(component)
        if part:
            state = observation.summarize(part)
            item["part_design"] = {key: state[key] for key in (
                "material_id", "spec", "manufacturing", "parameters", "general_tolerance",
            ) if key in state}
            item["part_design"]["features"] = state["features"][:40]
            item["part_design"]["omitted_features"] = max(0, len(state["features"]) - 40)
        interfaces.append(item)
    history = parent.get("history") or ""
    if len(history) > 12000:
        history = history[:6000] + "\n[Middle history omitted; use the work plan.]\n" + history[-6000:]
    return {
        "assembly_id": asm.id, "assembly_name": asm.name,
        "assembly_goal": (parent.get("goal") or "")[:12000],
        "conversation_context": history,
        "assembly_work_plan": parent.get("task_state"),
        "part_requirements": requirements,
        "interfaces": interfaces,
        "omitted_components": len(asm.components) - len(interfaces),
        "mates": [m.model_dump() for m in asm.mates if m.base in ids or m.moved in ids],
        "reference_image_indices": indices,
        "viewport": viewport,
        "instructions": "Build only the requested part. Interface dimensions, materials, hole patterns and clearances constrain its fit; do not copy neighbouring parts wholesale. Local coordinates are part-space, world coordinates are assembly-space. Build near the part origin; the parent will place and mate it. Attached images are references from the parent assembly, not necessarily pictures of this part. Preserve explicit user requirements; label assumptions.",
    }, [images[i] for i in indices]
