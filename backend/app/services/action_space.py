"""One description of the action space, for the prompt, the tools and the corpus.

There were three. The copilot built its own catalog string; `trajectories`
built a second one for the training export; and `op_schema.tool_definitions`
built the real JSON Schema that neither of them used. The first two agreed only
by coincidence, and both handed the model **parameter names and nothing else** —
no types, no units, no required list, no nesting — while the third, the one
thing a constrained decoder could actually be pointed at, was served on an
endpoint and otherwise ignored.

That split has a specific cost beyond duplication. The corpus is training data;
if it describes a different action space from the one used at inference, a model
trained on it is being taught to drive an API that does not exist. So this
module is the only place either is generated.

`tools()` is the strong form and the default when the provider supports it.
`catalog_text()` is the fallback, and it now carries the type, the unit and the
required-ness — the same facts, in prose, for a provider that cannot take a
schema.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

#: The rules that are not derivable from the schema. Everything that *is*
#: derivable — names, types, units, requiredness — is generated below rather
#: than written here, because a rule written twice is a rule that will disagree
#: with itself.
_RULES = """You are Forge Copilot, an engineering agent that builds and edits a CAD part by calling operations one at a time.

Rules:
- ALL lengths are in METRES (100 mm = 0.1). The document summary reports sizes in MILLIMETRES, so a part the summary calls 100 mm is 0.1 in an argument. Centre parts near the origin.
- Use ONLY the operations listed, with their exact parameter names. A name that is not listed will be rejected.
- Most operations act on a part document. The ones marked [assembly] act on an assembly instead — they take an assembly id, and a part id will not work.
- Take the minimum number of purposeful steps.
- To change an existing feature, read its CURRENT params in the document summary and pass the same structure to set_feature_params. Do not invent parameter names.
- Query operations (bounding_box, list_features, verify_against_spec) do not change the part — use them to check rather than to guess.
- If a call returned an error, read the code: MISSING_ARG and BAD_ARG mean fix the arguments, PRECONDITION means a different earlier step is needed. Do not repeat a call that failed the same way.
- If a call returned a warning, act on it. A SUSPECT_UNITS warning means the number was almost certainly metres-for-millimetres.
- This is an internal design tool. Prefer direct progress with the existing CAD operations; do not add commercial-release or publishing steps.
- Inspect the current document before edits. After edits, check the relevant dimensions and declared design requirements before reporting completion. A successful tool call alone does not establish that the design meets the goal.
- When the part matches the goal, stop. Do not keep adding features."""

_ASSEMBLY_RULES = """
- You are working on an ASSEMBLY, not a part. Every operation here takes the
  assembly's id, and none of them changes a part's own geometry: to move
  material you open that part as a document, which is a different run. Here you
  place components, mate them, and measure what you get.
- Read list_components and list_mates before mating. A mate names component ids,
  and inventing one is the commonest way this fails.
- A mate is the only thing that *rotates* a part into place. A transform set by
  hand on a mated component is an output the next solve overwrites.
- Before reporting success, run check_interference. Parts passing through each
  other look correct in every other reading.

Building a whole assembly from a description is the normal job here, and it is
a loop rather than a plan you write once:
- Work **one part at a time**: get the part, mate it to what is already there,
  then move to the next. Building everything first and mating at the end means
  placing parts whose position nothing has established yet.
- For a part nobody has modelled, `build_part` designs it and places it — it
  runs the part copilot on a new document. For anything purchased (screws, nuts,
  bearings, motors, standoffs, plate, tube, LiPo) use `insert_standard_part`
  instead: modelling a screw is slower and less accurate than the catalog.
- A part arrives at the origin, which is where everything else built this way
  already is. Until a mate holds it, it is not assembled — it is overlapping.
- **You are not done while any component is unmated or any two parts
  interfere.** Both are checked when you say you are done, and you are told
  exactly which; fix those and say done again.
- If two parts overlap, **change the mate that positions them** — its
  `base_face`/`moved_face`, `axis` or `offset_mm` — or use `place_component` on
  a part no mate holds. Do not delete the mate: that leaves the part unheld
  *and* still overlapping, which fails both checks instead of one. A part built
  by `build_part` is centred on the origin, so stacking two of them means
  mating a `max` face to a `min` face along the stacking axis."""


_DONE_PROTOCOL = """
Each turn respond with EXACTLY ONE JSON object, nothing else:
  {"op": "<operation_name>", "args": { ... }, "rationale": "<one short sentence>"}
or, when the goal is fully satisfied:
  {"done": true, "summary": "<what you accomplished>"}
"""


def tools(subject: Optional[str] = None) -> List[Dict[str, Any]]:
    """The catalog as OpenAI-style function definitions — the strong form.

    `subject` narrows it to the operations that take one kind of id.
    """
    from app.services import op_schema

    return op_schema.tool_definitions(subject)


def _one_param(name: str, descriptor: Dict[str, Any]) -> str:
    descriptor = descriptor or {}
    kind = descriptor.get("type") or (
        "enum(" + "|".join(map(str, descriptor["enum"])) + ")" if descriptor.get("enum") else "any")
    if isinstance(kind, list):
        kind = "|".join(kind)
    bits = [f"{name}: {kind}"]
    if descriptor.get("required"):
        bits.append("required")
    unit = descriptor.get("unit")
    if unit:
        bits.append(unit)
    return " ".join(bits)


def catalog_text(kinds: Optional[Sequence[str]] = None,
                 subject: Optional[str] = None) -> str:
    """The catalog as prose, carrying the facts the schema carries.

    It used to be ``name (kind): description · params: a, b, c`` — a list of
    identifiers, from which the model had to guess types, nesting, units and
    which arguments it could not leave out. Everything here is generated from
    the same descriptors `tools()` emits, so the two cannot describe different
    APIs.
    """
    from app.services import cad_operations

    lines: List[str] = []
    for op in cad_operations.catalog():
        if kinds and op["kind"] not in kinds:
            continue
        if subject is not None and op.get("subject", "document") != subject:
            continue
        params = op["params"] or {}
        rendered = "; ".join(_one_param(n, d) for n, d in params.items()) or "—"
        # `label`, not `subject`: this used to reuse the parameter's name, so the
        # filter above compared every operation after the first against "".
        label = " [assembly]" if op.get("subject") == "assembly" else ""
        lines.append(f"- {op['name']} ({op['kind']}){label}: {op['description']}"
                     f"\n    args: {rendered}")
    return "\n".join(lines)


def system_prompt(*, with_tools: bool, subject: Optional[str] = None) -> str:
    """The planner's system message.

    ``with_tools`` says whether the provider is being handed `tools()`. When it
    is, the catalog is not repeated in the prompt — the schemas are already
    there, and restating them in prose is the drift this module exists to stop.
    """
    rules = _RULES + (_ASSEMBLY_RULES if subject == "assembly" else "")
    if with_tools:
        return rules + (
            "\n\nCall exactly one operation per turn using the provided tools. "
            "When the goal is fully satisfied, reply with plain text summarising "
            "what you built instead of calling a tool.")
    return (rules + _DONE_PROTOCOL + "\n\nAvailable operations:\n"
            + catalog_text(subject=subject))
