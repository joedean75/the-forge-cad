"""Fine-tuning samples for this API's copilot, in the shape inference sends.

One sample is **one planner turn**: the system prompt and tool schemas the run
was offered, a user turn carrying the goal, the transcript so far and the state
as it stood, and — as the label — the single tool call the run actually made.
That is what `copilot.planner_request` builds for a live turn, and this module
calls the same function, so a model trained here drives the API that exists.
The exporter this replaces emitted the prose system prompt and an alternating
tool-call conversation; a run sends neither, and CLAUDE.md records the same
failure when the action space had three generators.

Where the samples come from:

* **Recorded runs** (`trajectories`, `kind: copilot`) — real work, including the
  half that is worth most: a failed call followed by the call that recovered
  from it. The failure stays in the transcript and is never the label.
* **Decompiled documents** — `decompiler.decompile_document` recovers an
  operation sequence from a finished body, replays it and compares mesh hashes,
  so a sequence is only kept when it rebuilds the part exactly. The catalog's
  own parts come with a goal written for them ("ISO 4762 M4×16 …"), the
  templates come with parameter variations, and a user's library is whatever
  they built.
* **A finishing turn**, because a corpus of only edits trains a model that never
  stops. Real runs record the decision; a synthetic sequence gets one whose
  summary states the measured result, marked `synthetic_finish`.

What is refused rather than fudged: a run whose prompt carried reference images
(the bytes are never kept, so the sample would be missing what the model saw),
a run that ended in a provider error, and any step that failed as a *label*.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: Runs that ended in one of these are the ones whose actions are worth copying.
#: `planner_error` is mostly provider billing failures, and a truncated run's
#: last action is not evidence of anything.
GOOD_OUTCOMES = ("done", "answered", "completed")
#: Roughly, for the card: four characters to a token is close enough to size an
#: epoch, and nothing here depends on the number.
CHARS_PER_TOKEN = 4


@dataclass
class Sample:
    """One training line plus the provenance that stays out of it."""
    messages: List[dict]
    tools: Optional[List[dict]]
    meta: Dict[str, Any] = field(default_factory=dict)

    def line(self) -> dict:
        """The training line. Trainers reject unknown top-level keys, so the
        provenance travels in a parallel meta file rather than in here."""
        body: Dict[str, Any] = {"messages": self.messages}
        if self.tools:
            body["tools"] = self.tools
            body["parallel_tool_calls"] = False
        return body

    def fingerprint(self) -> str:
        """Identity for de-duplication: what was asked and what was answered.

        The system prompt and the tools are the same in thousands of samples, so
        hashing the whole line would dedup nothing.
        """
        user = self.messages[-2]["content"] if len(self.messages) > 1 else ""
        if isinstance(user, list):
            user = "".join(p.get("text", "") for p in user if isinstance(p, dict))
        return hashlib.sha256(
            (str(user) + json.dumps(self.messages[-1], sort_keys=True)).encode()
        ).hexdigest()

    def chars(self) -> int:
        return len(json.dumps(self.line()))


def _tool_call(name: str, args: dict, rationale: str = "",
               profile: str = "full") -> dict:
    """The label: the assistant turn, in the form this profile's turn answers in.

    `full` is a provider tool call. `finetuned` sends no tool schemas, so the
    answer is the JSON object `action_space._DONE_PROTOCOL` describes and
    `llm_client._extract_json` parses — the same prose path `FORGE_LLM_TOOLS=0`
    already uses. Training on the tool-call form and serving the prose one (or
    the reverse) is the drift the single generator exists to prevent.
    """
    if profile == "finetuned":
        body = ({"done": True, "summary": args.get("summary") or "",
                 "result_kind": args.get("outcome") or "completed"}
                if name == "finish_task" else
                {"op": name, "args": args, "rationale": rationale or ""})
        return {"role": "assistant", "content": json.dumps(body)}
    return {"role": "assistant", "content": rationale or "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def _entry_from_step(step: dict) -> dict:
    """A recorded step as the planner reads it back on the next turn.

    Built through `copilot.transcript_entry`, the loop's own shape, and
    re-compacted here: results recorded before the transcript compaction landed
    are full size, and a sample must show what a run would show today.
    """
    from app.services import copilot

    env: Dict[str, Any] = {"ok": step.get("ok"), "result": step.get("result") or {}}
    if step.get("error"):
        env["error"] = step["error"]
    if step.get("warnings"):
        env["warnings"] = step["warnings"]
    entry = copilot.transcript_entry(step.get("op") or "", step.get("args") or {}, env,
                                     step.get("rationale") or "")
    if step.get("warnings"):
        entry["warnings"] = step["warnings"]
    return entry


def _turn_state(base: Optional[dict], context: Optional[dict]) -> dict:
    """The observation as the planner saw it: what `execute` recorded, plus the
    per-turn block the loop adds (selection, budget, recent outcomes).

    A record written before `planner_context` was recorded has no such block;
    the sample is still usable and says so through `state_exact: false`.
    """
    state = dict(base or {})
    if context:
        state["request_context"] = context.get("request_context")
        if context.get("parent_assembly_brief"):
            state["parent_assembly_brief"] = context["parent_assembly_brief"]
    return state


def _subject_of(record: dict) -> str:
    """Which half of the catalog the run drove.

    Recorded from `context_version: 3` on; before that it has to be inferred,
    and the id is the evidence — an assembly run's subject id names an assembly.
    """
    if record.get("subject") in ("document", "assembly"):
        return record["subject"]
    from app.services import assembly_service

    doc_id = record.get("doc_id")
    try:
        return "assembly" if doc_id and assembly_service.get_assembly(doc_id) else "document"
    except Exception:  # noqa: BLE001 — a missing subject is not a failure
        return "document"


def _tools_for(subject: str, goal: str, summary: dict, ops: Sequence[str],
               objective: str = ""):
    """The tool set a turn was offered, reconstructed.

    `initial_tools` is what the run started with; every operation the run went on
    to call is added in first-use order, which is both what discovery did and
    what makes the label reachable — a sample whose answer is not in its own
    tool list teaches a model to invent one.
    """
    from app.services import copilot_context

    # The same query the loop ranks on: the goal *and* the work plan's
    # objective, which starts as the goal. A different query is a different tool
    # set, and then the rebuilt prompt is not the one that was sent.
    enabled = set(copilot_context.initial_tools(subject, f"{goal} {objective or goal}", summary))
    discovery: List[str] = []
    for op in ops:
        if op and op not in enabled:
            enabled.add(op)
            discovery.append(op)
    return enabled, discovery


#: Operations the loop runs for itself on the way to "done". They belong in a
#: transcript — the planner reads their results — and never in a label.
GATE_OPS = ("check_geometry", "check_interference", "verify_against_spec",
            "render_view", "render_assembly")


def samples_from_record(record: dict, *, include_unfinished: bool = False,
                        source: str = "recorded",
                        synthetic_finish: bool = False,
                        profile: str = "full") -> List[Sample]:
    """Every turn of one recorded run that is worth learning from.

    A sample is built per *planner decision*, not per executed step: the done
    gate executes its own checks, and a decision that the schema refused never
    became a step at all.
    """
    from app.services import copilot

    steps = [s for s in (record.get("steps") or []) if s.get("op")]
    if not steps:
        return []
    if not include_unfinished and (record.get("outcome") or "") not in GOOD_OUTCOMES:
        return []
    if int(record.get("image_count") or 0) > 0:
        return []          # the prompt had pictures in it; the bytes are gone

    subject = _subject_of(record)
    goal = record.get("goal") or ""
    history = record.get("history") or ""
    events = record.get("events") or []
    contexts = {e.get("turn"): e for e in events if e.get("event") == "planner_context"}
    decisions = [e for e in events if e.get("event") == "planner_decision" and e.get("op")]
    finish = next((e for e in reversed(events) if e.get("event") == "finish_task"), None)

    enabled, discovery = _tools_for(subject, goal, steps[0].get("obs_before") or {},
                                    [s.get("op") for s in steps],
                                    objective=record.get("objective") or "")
    entries = [_entry_from_step(s) for s in steps]

    def state_at(after_steps: int, turn: Optional[int]) -> dict:
        """The observation as the planner saw it on the turn after `after_steps`."""
        if after_steps < len(steps):
            base = steps[after_steps].get("obs_before")
        else:
            base = steps[-1].get("obs_after") or steps[-1].get("obs_before")
        return _turn_state(base, contexts.get(turn))

    def sample(op: str, args: dict, rationale: str, after_steps: int,
               turn: Optional[int], extra: Optional[dict] = None) -> Optional[Sample]:
        state = state_at(after_steps, turn)
        if not state:
            return None
        transcript = entries[:after_steps]
        messages, tools = copilot.planner_request(
            goal, list(transcript), state, subject=subject, history=history,
            enabled_tools=enabled, discovery_order=discovery, profile=profile)
        return Sample(
            messages=[*messages, _tool_call(op, args, rationale, profile)],
            tools=tools,
            meta={"source": source, "record_id": record.get("id"),
                  "doc_id": record.get("doc_id"), "subject": subject, "op": op,
                  "turn": turn if turn is not None else after_steps,
                  "model": record.get("model"), "outcome": record.get("outcome"),
                  "recovers_failure": bool(transcript and not transcript[-1].get("ok")),
                  "license": "internal", "verified": source != "recorded",
                  "state_exact": turn in contexts, "profile": profile,
                  **(extra or {})})

    out: List[Sample] = []
    if decisions:
        for event in decisions:
            after = int(event.get("after_steps") or 0)
            # Only a decision whose call then succeeded: the failure stays in
            # the transcript of later turns, where recovering from it is the
            # lesson, and is never the thing to copy.
            executed = steps[after] if after < len(steps) else None
            if not executed or executed.get("op") != event["op"] or not executed.get("ok"):
                continue
            built = sample(event["op"], executed.get("args") or {},
                           event.get("rationale") or executed.get("rationale") or "",
                           after, event.get("turn"))
            if built:
                out.append(built)
    else:
        # A record from before decisions were recorded. The gate's own calls
        # cannot be told from the planner's, so they are excluded by name.
        for index, step in enumerate(steps):
            if not step.get("ok") or step.get("op") in GATE_OPS:
                continue
            built = sample(step["op"], step.get("args") or {}, step.get("rationale") or "",
                           index, None, {"state_exact": False})
            if built:
                out.append(built)

    if finish or (synthetic_finish and out):
        after = int((finish or {}).get("after_steps") or len(steps))
        state = state_at(after, (finish or {}).get("turn"))
        args = ({"outcome": finish.get("outcome") or "completed",
                 "summary": finish.get("summary") or record.get("summary") or ""}
                if finish else
                {"outcome": "completed", "summary": _finish_summary(goal, state)})
        built = sample("finish_task", args, "", after, (finish or {}).get("turn"),
                       {} if finish else {"synthetic_finish": True})
        if built:
            out.append(built)
    return out


def _finish_summary(goal: str, summary: dict) -> str:
    """A finishing summary for a synthetic sequence, stating what was measured.

    Derived rather than invented: a sentence about a body the replay built, so
    the label is true even though no model wrote it.
    """
    size = summary.get("bbox_size_mm") or {}
    dims = " x ".join(f"{float(size[a]):g}" for a in ("x", "y", "z") if a in size)
    volume = summary.get("volume_mm3")
    measured = ", ".join(x for x in (f"{dims} mm" if dims else "",
                                     f"{float(volume):.0f} mm³" if volume else "") if x)
    return f"Built {goal}." + (f" Measured {measured}." if measured else "")


def samples_from_document(doc_id: str, goal: str, *, source: str,
                          license_name: str = "internal",
                          resolution: int = 48,
                          profile: str = "full") -> List[Sample]:
    """Turn samples for a document, via a decompiled and *verified* sequence.

    `decompile_document` replays what it recovered into a scratch document and
    compares mesh hashes, so a sequence that rebuilds a different part is
    dropped rather than taught. The replay is what produces the trajectory these
    samples are read from — the operations genuinely ran.
    """
    from app.services import decompiler, trajectories

    report = decompiler.decompile_document(doc_id, goal=goal, resolution=resolution,
                                           record=True)
    if not report.get("ok") or not report.get("trajectory_id"):
        return []
    record = trajectories.get(report["trajectory_id"])
    if record is None:
        return []
    record = {**record, "outcome": "done", "subject": "document", "history": ""}
    samples = samples_from_record(record, source=source, synthetic_finish=True,
                                  profile=profile)
    for sample in samples:
        sample.meta.update({"source": source, "license": license_name, "verified": True})
    return samples


# ------------------------------------------------------------ recovery pairs --

#: How a call is made wrong. Both kinds are refused by `op_schema.validate`
#: *before* a handler runs, which is what makes them safe to inject: the
#: document is untouched, so the correct call that follows behaves exactly as it
#: would have alone. A mutation that could half-apply would corrupt the very
#: sequence the sample is teaching.
def _without_a_required_argument(op: str, args: dict) -> Optional[dict]:
    """Drop a required argument — a `MISSING_ARG`."""
    from app.services import cad_operations

    required = [name for name, spec in (cad_operations.OPERATIONS[op].params or {}).items()
                if isinstance(spec, dict) and spec.get("required") and name in args]
    if not required:
        return None
    return {k: v for k, v in args.items() if k != required[0]}


def _with_an_invented_argument(op: str, args: dict) -> Optional[dict]:
    """Add a plausible name the schema does not declare — a `BAD_ARG`.

    The failure the logs show most: `add_primitive` with `width`/`depth` instead
    of `params.size`, which used to be dropped on the floor and answered with a
    default cube until `additionalProperties: false` was enforced.
    """
    invented = {"add_primitive": "width", "add_extrude": "height",
                "add_hole": "diameter", "set_feature_params": "value"}
    return {**args, invented.get(op, "size_mm"): 10.0}


MUTATIONS = (_with_an_invented_argument, _without_a_required_argument)


def recovery_samples_from_document(doc_id: str, goal: str, *, source: str,
                                  every: int = 1, resolution: int = 48) -> List[Sample]:
    """Turn samples where the label is the call that *recovered* from a failure.

    The corpus's thinnest spot: one recovery sample in 2,004, while recovering
    from a typed failure is most of what an agent does — and the fine-tune
    duly stopped at the first refusal in five of six eval tasks. So a verified
    sequence is replayed with a deliberately wrong call injected before every
    `every`-th step. The wrong call is **really executed**, so the transcript
    carries the real code and message, and it is never the label.
    """
    from app.services import cad_operations, decompiler, document_service, trajectories

    report = decompiler.decompile_document(doc_id, goal=goal, resolution=resolution,
                                           record=False)
    if not report.get("ok") or not report.get("steps"):
        _drop(f"{source}:not_decompilable")
        return []

    fresh = document_service.create_document(name=f"recovery {doc_id[:8]}", owner_id="dataset")
    created: List[Optional[str]] = []
    groups: List[Optional[str]] = []
    injected = 0
    with trajectories.recording(fresh.id, goal=goal, kind="recovery") as tid:
        for index, step in enumerate(report["steps"]):
            args = decompiler.resolve_refs(step["args"], created, groups)
            if index % every == 0:
                for mutate in MUTATIONS:
                    wrong = mutate(step["op"], args)
                    if wrong is None or wrong == args:
                        continue
                    env = cad_operations.execute(step["op"], fresh.id, wrong)
                    if env["ok"]:
                        # It was accepted, so it changed the document and the
                        # sequence no longer describes this body. Stop rather
                        # than teach a corrupted transcript.
                        _drop(f"{source}:mutation_accepted")
                        return []
                    injected += 1
                    break
            env = cad_operations.execute(step["op"], fresh.id, args)
            if not env["ok"]:
                _drop(f"{source}:recovery_replay_failed")
                return []
            result = env.get("result") or {}
            created.append(result.get("feature_id"))
            groups.append(result.get("group_id"))
    if not injected:
        return []
    record = trajectories.get(tid)
    if record is None:
        return []
    record = {**record, "outcome": "done", "subject": "document"}
    samples = samples_from_record(record, source=source, synthetic_finish=True,
                                  profile=_PROFILE.get("value", "full"))
    for sample in samples:
        sample.meta.update({"source": source, "verified": True, "injected_failures": injected})
    return samples


# ----------------------------------------------------------------- coverage --

MM = 0.001


def _c_run(doc: str, op: str, **args) -> dict:
    from app.services import cad_operations

    env = cad_operations.execute(op, doc, args, include_state=False)
    if not env["ok"]:
        raise _CoverageFailed(f"{op}: {(env.get('error') or {}).get('message')}")
    return env.get("result") or {}


class _CoverageFailed(RuntimeError):
    pass


def _box(doc, x, y, z, **kw):
    return _c_run(doc, "add_primitive", shape="box",
                  params={"size": {"x": x * MM, "y": y * MM, "z": z * MM},
                          "center": {"x": 0.0, "y": 0.0, "z": 0.0}}, **kw)


#: Sequences for the operations no other source produces a label for.
#:
#: Measured on the 18-task goal suite: four tasks failed *only* their feature
#: requirement with the right bounding box built — 0 × EdgeFilletFeature,
#: 0 × EdgeChamferFeature, 0 × FaceShellFeature, 0 × RibFeature — and all three
#: material tasks failed with the material unset. Those five operations have zero
#: labels between them in 6,173, while five others are 97% of the corpus. A model
#: does not choose an operation it has never seen chosen, however plainly the
#: prompt lists its name, and none of this is missing capability: every one works
#: and is tested.
#:
#: The other sources cannot supply them. `decompiler` *does* emit `fillet_edges`,
#: `chamfer_edges`, `shell_faces` and `add_rib` — no library document or template
#: happens to contain those features, which is the whole reason. And
#: `set_material` is document metadata rather than a tree node, so decompiling a
#: body can never produce it at all.
#:
#: Three things each sequence does deliberately:
#:
#: * **`set_spec` first.** `BRIEF_RULE` tells the planner to record the
#:   measurable requirements before its first edit, and the corpus carried two
#:   `set_spec` labels — the rule was taught by prose alone.
#: * **`list_faces` before `shell_faces`.** The id comes from the read, so this
#:   is a turn whose argument is a value the previous turn returned, which is
#:   most of what driving this catalog is and almost none of what the corpus
#:   shows.
#: * **Dimensions differ per row**, so what is learned is the operation and not
#:   one memorised literal.
#: The rows are **generated**, because a handful of labels does not move a model.
#: The first version wrote twelve sequences by hand and produced 3 `fillet_edges`
#: labels against 2,533 `add_primitive` — present in the corpus and invisible in
#: it. Enumerating shapes against dimensions against the operation's *own*
#: parameter gives ~40-60 labels each with genuine variety, which is the
#: difference between showing an operation and showing one memorised call.
#:
#: Deterministic: no sampling, so two builds of this corpus are the same corpus.

#: (x, y, z) in mm. Spread so the numbers in the labels are not one plate.
_COV_BLOCKS = ((40, 40, 20), (90, 50, 10), (30, 30, 30), (120, 25, 12),
               (70, 45, 8), (60, 60, 25))
#: (radius, height) in mm.
_COV_ROUNDS = ((25, 8), (15, 40), (30, 12))
#: Grades, with the phrase a request would use for each.
_COV_GRADES = (("steel-1018", "1018 steel"), ("al-6061", "6061 aluminium"),
               ("ti-6al4v", "Ti-6Al-4V titanium"), ("al-7075", "7075 aluminium"),
               ("ss-316l", "316L stainless"), ("steel-4140", "4140 chromoly"),
               ("al-5052", "5052 aluminium"), ("steel-a36", "A36 structural steel"))


def _cyl(doc, r, h, **kw):
    return _c_run(doc, "add_primitive", shape="cylinder",
                  params={"radius": r * MM, "height": h * MM,
                          "center": {"x": 0.0, "y": 0.0, "z": 0.0}}, **kw)


def _bbox_req(axis, value, tolerance=1.0):
    return {"kind": "bounding_box", "axis": axis, "value": value, "tolerance": tolerance}


def _block_reqs(x, y, z):
    return [_bbox_req("x", x), _bbox_req("y", y), _bbox_req("z", z)]


def _round_reqs(r, h):
    return [_bbox_req("x", 2 * r), _bbox_req("y", 2 * r), _bbox_req("z", h)]


def _cov_edge_rows(op: str, key: str, values, word: str, feature: str):
    """`fillet_edges` and `chamfer_edges` over every shape and every size."""
    rows = []
    for index, (x, y, z) in enumerate(_COV_BLOCKS):
        for value in values:
            if value * 2 >= min(x, y, z):
                continue                     # would consume the part
            rows.append({
                "id": f"{op}-{x}x{y}x{z}-{value:g}",
                "goal": f"A {x:g} x {y:g} x {z:g} mm block with every edge "
                        f"{word} by {value:g} mm.",
                "requirements": _block_reqs(x, y, z) + [{"kind": "feature", "value": feature}],
                "build": (lambda x=x, y=y, z=z, value=value:
                          lambda doc: (_box(doc, x, y, z),
                                       _c_run(doc, op, edges="all", **{key: value * MM})))(),
            })
    for r, h in _COV_ROUNDS:
        for value in values:
            if value * 2 >= min(2 * r, h):
                continue
            rows.append({
                "id": f"{op}-d{2*r:g}x{h:g}-{value:g}",
                "goal": f"A {2*r:g} mm diameter, {h:g} mm long cylinder with both "
                        f"ends {word} by {value:g} mm.",
                "requirements": _round_reqs(r, h) + [{"kind": "feature", "value": feature}],
                "build": (lambda r=r, h=h, value=value:
                          lambda doc: (_cyl(doc, r, h),
                                       _c_run(doc, op, edges="all", **{key: value * MM})))(),
            })
    return rows


def _shell(doc, x, y, z, wall):
    """The id comes from the read, which is the turn shape the corpus lacks."""
    _box(doc, x, y, z)
    faces = _c_run(doc, "list_faces")["faces"]
    top = max((f for f in faces if f.get("kind") == "plane"),
              key=lambda f: f.get("position_mm", {}).get("z", -1e9))
    _c_run(doc, "shell_faces", faces=[top["id"]], thickness=wall * MM)


def _cov_shell_rows():
    rows = []
    for x, y, z in _COV_BLOCKS:
        for wall in (1.5, 2, 3):
            if wall * 3 >= min(x, y, z):
                continue
            rows.append({
                "id": f"shell-{x}x{y}x{z}-{wall:g}",
                "goal": f"A {x:g} x {y:g} x {z:g} mm box hollowed out to a "
                        f"{wall:g} mm wall, open at the top.",
                "requirements": _block_reqs(x, y, z)
                                + [{"kind": "feature", "value": "FaceShellFeature"}],
                "build": (lambda x=x, y=y, z=z, wall=wall:
                          lambda doc: _shell(doc, x, y, z, wall))(),
            })
    return rows


def _rib(doc, x, y, z, thickness, height):
    _box(doc, x, y, z)
    _c_run(doc, "add_rib", start={"x": -(x / 2 - 5) * MM, "y": 0.0, "z": z / 2 * MM},
           end={"x": (x / 2 - 5) * MM, "y": 0.0, "z": z / 2 * MM},
           thickness=thickness * MM, height=height * MM)


#: A rib belongs on a plate, and the block table is mostly not plates — filtering
#: it left 9 labels where the other families have 20-30.
_COV_PLATES = ((80, 40, 5), (60, 60, 6), (120, 25, 8), (90, 50, 10), (100, 40, 6),
               (70, 45, 8), (50, 50, 4))


def _cov_rib_rows():
    rows = []
    for x, y, z in _COV_PLATES:
        for thickness, height in ((3, 10), (4, 14), (2.5, 8)):
            rows.append({
                "id": f"rib-{x}x{y}x{z}-{thickness:g}x{height:g}",
                "goal": f"An {x:g} x {y:g} x {z:g} mm plate with a rib along its "
                        f"centreline, {thickness:g} mm thick at the tip and "
                        f"{height:g} mm tall.",
                # The rib's centreline sits on the top face at +z/2 and grows
                # `height` from there, so the extent is `z + height` — not
                # `z/2 + height`, which is the top *coordinate*. Every rib row
                # was dropped until this was right, which is what the per-row
                # spec check is for: the same arithmetic slip put two of the
                # eval tasks out by a bend allowance and a radius name.
                "requirements": _block_reqs(x, y, z + height)
                                + [{"kind": "feature", "value": "RibFeature"}],
                "build": (lambda x=x, y=y, z=z, t=thickness, h=height:
                          lambda doc: _rib(doc, x, y, z, t, h))(),
            })
    return rows


def _cov_material_rows():
    """`set_material` cannot come from any other source: it is not a tree node,
    so decompiling a body never produces it however many bodies are decompiled."""
    rows = []
    for index, (grade, phrase) in enumerate(_COV_GRADES):
        x, y, z = _COV_BLOCKS[index % len(_COV_BLOCKS)]
        rows.append({
            "id": f"material-{grade}-block",
            "goal": f"A {x:g} x {y:g} x {z:g} mm plate in {phrase}.",
            "requirements": _block_reqs(x, y, z) + [{"kind": "material", "value": grade}],
            "build": (lambda x=x, y=y, z=z, grade=grade:
                      lambda doc: (_box(doc, x, y, z),
                                   _c_run(doc, "set_material", material_id=grade)))(),
        })
        r, h = _COV_ROUNDS[index % len(_COV_ROUNDS)]
        rows.append({
            "id": f"material-{grade}-round",
            "goal": f"A {2*r:g} mm diameter, {h:g} mm long shaft in {phrase}.",
            "requirements": _round_reqs(r, h) + [{"kind": "material", "value": grade}],
            "build": (lambda r=r, h=h, grade=grade:
                      lambda doc: (_cyl(doc, r, h),
                                   _c_run(doc, "set_material", material_id=grade)))(),
        })
    return rows


def _cov_combined_rows():
    """Two of the missing operations in one part, which is how a request arrives."""
    rows = []
    for (x, y, z), (grade, phrase), radius in zip(
            _COV_BLOCKS, _COV_GRADES, (2, 3, 1.5, 2, 2.5, 3)):
        rows.append({
            "id": f"combined-{grade}-{x}x{y}x{z}",
            "goal": f"A {x:g} x {y:g} x {z:g} mm bracket in {phrase}, with every "
                    f"edge rounded by {radius:g} mm.",
            "requirements": _block_reqs(x, y, z)
                            + [{"kind": "feature", "value": "EdgeFilletFeature"},
                               {"kind": "material", "value": grade}],
            "build": (lambda x=x, y=y, z=z, grade=grade, radius=radius:
                      lambda doc: (_box(doc, x, y, z),
                                   _c_run(doc, "fillet_edges", edges="all",
                                          radius=radius * MM),
                                   _c_run(doc, "set_material", material_id=grade)))(),
        })
    return rows


def coverage_rows():
    return (_cov_edge_rows("fillet_edges", "radius", (1.5, 2, 3), "rounded",
                           "EdgeFilletFeature")
            + _cov_edge_rows("chamfer_edges", "distance", (1, 1.5, 2), "chamfered",
                             "EdgeChamferFeature")
            + _cov_shell_rows() + _cov_rib_rows()
            + _cov_material_rows() + _cov_combined_rows())


def coverage_samples(*, owner_id: str = "dataset", profile: str = "full") -> List[Sample]:
    """Turn samples for the operations nothing else in the corpus demonstrates.

    Authored rather than recovered, so the verification is different in kind and
    not weaker: the other sources replay a recovered sequence and compare mesh
    hashes against the source body, while here the sequence *is* the ground truth
    and what has to be checked is that it builds what its goal says. So each row
    carries the measurable requirements, they are recorded with `set_spec` as the
    first turn, and a row whose body does not score 1.0 is dropped rather than
    taught — the same rule, against a spec instead of a hash.
    """
    from app.services import cad_operations, document_service as docs, trajectories

    out: List[Sample] = []
    for row in coverage_rows():
        doc = docs.create_document(name=f"coverage {row['id']}", owner_id=owner_id)
        try:
            with trajectories.recording(doc.id, goal=row["goal"], kind="coverage") as tid:
                _c_run(doc.id, "set_spec", requirements=list(row["requirements"]))
                row["build"](doc.id)
        except _CoverageFailed as exc:
            _drop(f"coverage:{row['id']}:{exc}"[:80])
            continue
        # Outside the recording: the done gate scores the spec itself, and a turn
        # spent calling `verify_against_spec` before finishing is one the playbook
        # tells the planner not to take.
        report = cad_operations.execute("verify_against_spec", doc.id, {},
                                        include_state=False).get("result") or {}
        if float(report.get("score") or 0.0) < 1.0:
            unmet = [c.get("detail") for c in report.get("checks", [])
                     if c.get("status") != "pass"]
            _drop(f"coverage:{row['id']}:unmet:{'; '.join(str(u) for u in unmet)[:60]}")
            continue
        record = trajectories.get(tid)
        if record is None:
            _drop(f"coverage:{row['id']}:not_recorded")
            continue
        record = {**record, "outcome": "done", "subject": "document", "history": ""}
        samples = samples_from_record(record, source="coverage", synthetic_finish=True,
                                      profile=profile)
        for sample in samples:
            sample.meta.update({"source": "coverage", "license": "internal",
                                "verified": True, "coverage_id": row["id"]})
        if not samples:
            _drop(f"coverage:{row['id']}:no_samples")
        out.extend(samples)
    return out


# ------------------------------------------------------------------ sources --

def recorded_samples(*, include_unfinished: bool = False,
                     profile: str = "full") -> List[Sample]:
    from app.services import trajectories

    out: List[Sample] = []
    for record in trajectories.list_all():
        if (record.get("kind") or "") != "copilot":
            continue
        out.extend(samples_from_record(record, include_unfinished=include_unfinished,
                                       profile=profile))
    return out


#: Why a source produced no samples, counted like `decompiler.corpus_report`'s
#: `by_cause`: a build that silently drops two thirds of a catalog is one whose
#: coverage nobody can see.
DROPS: Dict[str, int] = {}
#: The prompt profile the current build is using, for paths that are called
#: without it being threaded through every argument list.
_PROFILE: Dict[str, str] = {"value": "full"}


def _drop(cause: str) -> None:
    DROPS[cause] = DROPS.get(cause, 0) + 1


def catalog_samples(limit: Optional[int] = None, *, owner_id: str = "dataset",
                    profile: str = "full") -> List[Sample]:
    """Every generated purchased part, as (designation -> operations).

    The catalog is the one source here with a *written* goal for each sample:
    "ISO 4762 M4×16" is what somebody would ask for, and the row already says
    it. Configured parameters are left at their defaults — a variation is a
    different document and would be a near-duplicate sample.
    """
    from app.services import parts_catalog, standard_parts

    out: List[Sample] = []
    for entry in parts_catalog.catalog()[: limit or None]:
        try:
            doc_id = standard_parts.document_for(entry, owner_id)
        except Exception:  # noqa: BLE001 — one unbuildable row is not a failure
            _drop("catalog:not_built")
            continue
        goal = f"{entry.designation} — {entry.category}, built to {entry.standard}"
        samples = samples_from_document(doc_id, goal, source="catalog", profile=profile)
        if not samples:
            _drop("catalog:not_decompilable")
        out.extend(samples)
    return out


def _variation_goal(name: str, parameters: Sequence[dict]) -> str:
    """A goal that states the dimensions it was built to, from the parameters.

    True by construction rather than written: the values are the ones the
    sequence uses, so a model sees a request in millimetres beside the literals
    that satisfy it — which is the one thing a CAD agent has to get right and
    the thing a goal like "an L-bracket" never teaches.
    """
    said = ", ".join(f"{p['name'].replace('_', ' ')} {float(p['value']):g} "
                     f"{p.get('unit') or 'mm'}" for p in parameters)
    return f"{name} with {said}" if said else name


def template_samples(*, owner_id: str = "dataset", variations: int = 0,
                     factors: Sequence[float] = (0.6, 1.5, 2.2),
                     profile: str = "full") -> List[Sample]:
    """The starter parts, each as its own sequence.

    A template *is* an operation sequence, so this is the one source whose
    ground truth needs no recovery at all — but it still goes through the
    decompiler, because what a model should learn is the sequence that rebuilds
    the body the template produced, not the sequence somebody typed.
    """
    from app.services import document_service as docs
    from app.services import cad_operations, templates

    out: List[Sample] = []
    for row in templates.describe():
        key = row["key"]
        doc = docs.create_document(name=f"template {key}", owner_id=owner_id)
        env = cad_operations.execute("apply_template", doc.id, {"template": key},
                                     include_state=False)
        if not env["ok"]:
            continue
        goal = f"{row.get('name') or key}: {row.get('description') or key}"
        samples = samples_from_document(doc.id, goal, source="template", profile=profile)
        if not samples:
            _drop("template:not_decompilable")
        out.extend(samples)

        # **Variations are where the volume is, and they are still verified.**
        # Rebinding the parameters gives a genuinely different sequence — the
        # same shape at other dimensions — and the goal states the numbers it
        # was built to, so the literals in the labels are the answer to a
        # request somebody could have written.
        for factor in list(factors)[:variations]:
            env = cad_operations.execute("get_parameters", doc.id, {}, include_state=False)
            parameters = (env.get("result") or {}).get("parameters") or []
            if not parameters:
                break
            scaled = [{"name": p["name"], "expr": f"{round(float(p['value']) * factor, 3):g}"}
                      for p in parameters]
            copy = docs.create_document(name=f"template {key} x{factor:g}", owner_id=owner_id)
            if not cad_operations.execute("apply_template", copy.id, {"template": key},
                                         include_state=False)["ok"]:
                _drop("variation:not_applied")
                continue
            if not cad_operations.execute("set_parameters", copy.id, {"parameters": scaled},
                                          include_state=False)["ok"]:
                _drop("variation:not_rebound")
                continue
            values = (cad_operations.execute("get_parameters", copy.id, {},
                                             include_state=False).get("result") or {})
            varied = samples_from_document(
                copy.id, _variation_goal(row.get("name") or key,
                                         values.get("parameters") or scaled),
                source="template_variation", profile=profile)
            if not varied:
                _drop("variation:not_decompilable")
            out.extend(varied)
    return out


def recovery_from_templates(limit: Optional[int] = None, *,
                            owner_id: str = "dataset",
                            catalog_parts: int = 60) -> List[Sample]:
    """Recovery pairs over the starter templates and their variations.

    The templates are used because their sequences are known to replay exactly,
    so an injected refusal is the only thing that differs from the plain sample —
    which is what makes the pair teach recovery rather than noise.
    """
    from app.services import cad_operations, document_service as docs
    from app.services import templates

    out: List[Sample] = []
    for row in templates.describe()[: limit or None]:
        doc = docs.create_document(name=f"recovery {row['key']}", owner_id=owner_id)
        if not cad_operations.execute("apply_template", doc.id, {"template": row["key"]},
                                      include_state=False)["ok"]:
            _drop("recovery:template_not_applied")
            continue
        goal = f"{row.get('name') or row['key']}: {row.get('description') or row['key']}"
        out.extend(recovery_samples_from_document(doc.id, goal, source="recovery"))

    # The catalog too, and not for volume: its sequences are short, so a refusal
    # lands on the *first* call, which is exactly where the fine-tune stopped in
    # five of six eval tasks.
    from app.services import parts_catalog, standard_parts

    for entry in parts_catalog.catalog()[:catalog_parts]:
        try:
            doc_id = standard_parts.document_for(entry, owner_id)
        except Exception:  # noqa: BLE001
            continue
        goal = f"{entry.designation} — {entry.category}, built to {entry.standard}"
        out.extend(recovery_samples_from_document(doc_id, goal, source="recovery"))
    return out


def library_samples(owner_id: Optional[str] = None, limit: Optional[int] = None,
                    profile: str = "full") -> List[Sample]:
    """Documents somebody actually built, where the decompiler can verify them.

    The goal is the document's own prompt when it has one, and its name
    otherwise — a name is a weak goal, which is why `by_cause` in the card
    matters more here than anywhere else.
    """
    from app.services import document_service as docs

    out: List[Sample] = []
    for summary in docs.list_documents(owner_id)[: limit or None]:
        goal = getattr(summary, "source_prompt", None) or getattr(summary, "name", "") or ""
        if not goal:
            _drop("library:no_goal")
            continue
        samples = samples_from_document(summary.id, goal, source="library", profile=profile)
        if not samples:
            _drop("library:not_decompilable")
        out.extend(samples)
    return out


# -------------------------------------------------------------------- build --

def split_of(key: str, val_fraction: float = 0.1) -> str:
    """Which split a *source* belongs to — never a sample.

    Every turn of one run, and every turn of one document's sequence, share a
    prefix: put one turn in validation and the rest in training and the
    validation number measures memorisation of that very run.
    """
    if val_fraction <= 0:
        return "train"
    bucket = int(hashlib.sha256(str(key).encode()).hexdigest()[:8], 16) % 1000
    return "val" if bucket < val_fraction * 1000 else "train"


def assemble(samples: Iterable[Sample], *, val_fraction: float = 0.1) -> Dict[str, Any]:
    """Deduplicate, split by source, and report what came out."""
    seen: set = set()
    splits: Dict[str, List[Sample]] = {"train": [], "val": []}
    dropped = 0
    for sample in samples:
        finger = sample.fingerprint()
        if finger in seen:
            dropped += 1
            continue
        seen.add(finger)
        key = sample.meta.get("record_id") or sample.meta.get("doc_id") or finger
        split = split_of(str(key), val_fraction)
        sample.meta["split"] = split
        splits[split].append(sample)
    return {"splits": splits, "duplicates_dropped": dropped}


def stats(splits: Dict[str, List[Sample]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, rows in splits.items():
        by_source: Dict[str, int] = {}
        by_op: Dict[str, int] = {}
        chars = 0
        for sample in rows:
            by_source[sample.meta.get("source", "?")] = by_source.get(sample.meta.get("source", "?"), 0) + 1
            by_op[sample.meta.get("op", "?")] = by_op.get(sample.meta.get("op", "?"), 0) + 1
            chars += sample.chars()
        out[name] = {
            "samples": len(rows),
            "recovery_samples": sum(1 for s in rows if s.meta.get("recovers_failure")),
            # Samples whose prompt is *exactly* what a turn sent, rather than the
            # recorded observation without the loop's per-turn block. Replayed
            # sequences and runs recorded before `planner_context` have none, and
            # a corpus that hid that would be claiming a fidelity it lacks.
            "state_exact": sum(1 for s in rows if s.meta.get("state_exact")),
            "verified": sum(1 for s in rows if s.meta.get("verified")),
            "finish_samples": sum(1 for s in rows if s.meta.get("op") == "finish_task"),
            "approx_tokens": chars // CHARS_PER_TOKEN,
            "mean_tokens": (chars // CHARS_PER_TOKEN // len(rows)) if rows else 0,
            "by_source": dict(sorted(by_source.items(), key=lambda kv: -kv[1])),
            "by_op": dict(sorted(by_op.items(), key=lambda kv: -kv[1])[:20]),
        }
    return out
