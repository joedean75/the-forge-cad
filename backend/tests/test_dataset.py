"""The fine-tuning set is the shape inference sends, or it teaches a wrong API.

The exporter this replaces emitted the prose system prompt and an alternating
tool-call conversation. A real turn is one rebuilt user message — goal, then the
transcript, then the state — and one tool call, and CLAUDE.md records what
happened the last time two generators described one action space. So the pin
here is equality: a sample rebuilt from a recorded run is what that run sent.
"""

import json

import pytest

from app.services import cad_operations, copilot, document_service, llm_client, trajectories
from datasets import build as build_cli
from datasets import corpus


def _fake_provider(monkeypatch, replies):
    """Answer with scripted tool calls and keep every payload."""
    sent = []

    def fake(messages, timeout=0, max_tokens=None, tools=None, model=None):
        sent.append({"messages": messages, "tools": tools})
        return replies[min(len(sent) - 1, len(replies) - 1)]

    monkeypatch.setattr(llm_client, "LLM_API_KEY", "k")
    monkeypatch.setattr(llm_client, "_post_chat", fake)
    monkeypatch.setattr(llm_client, "supports_tools", lambda: True)
    return sent


def _call(name, args, content=""):
    return {"content": content, "tool_calls": [{"id": "c", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}]}


BOX = {"shape": "box", "params": {"size": {"x": 0.04, "y": 0.03, "z": 0.006}}}


def _run(monkeypatch, goal, replies):
    """Run the loop and return the trajectory *it* opened.

    Not a recording wrapped around the call: `copilot.run` starts its own, and
    the steps belong to that one — the outer record would have none.
    """
    doc = document_service.create_document(name="dataset run")
    sent = _fake_provider(monkeypatch, replies)
    copilot.run(doc.id, goal, max_steps=len(replies) + 1, review=False)
    records = trajectories.list_for_document(doc.id)
    assert records, "the run records itself"
    return records[-1], sent


def test_a_sample_is_what_the_run_actually_sent(monkeypatch):
    record, sent = _run(monkeypatch, "a 40 x 30 x 6 plate", [
        _call("add_primitive", BOX, "the plate blank"),
        _call("finish_task", {"outcome": "completed", "summary": "built the plate"}),
    ])
    samples = corpus.samples_from_record(record)
    assert samples, "a completed run with one edit is a sample"

    first = samples[0]
    assert first.messages[:-1] == sent[0]["messages"], "the prompt is rebuilt exactly"
    assert first.tools == sent[0]["tools"], "so are the tool schemas"
    call = first.messages[-1]["tool_calls"][0]["function"]
    assert call["name"] == "add_primitive" and json.loads(call["arguments"]) == BOX
    assert first.messages[-1]["content"] == "the plate blank", "the rationale is the label's text"


def test_the_finishing_turn_is_a_sample_too(monkeypatch):
    record, sent = _run(monkeypatch, "a plate", [
        _call("add_primitive", BOX),
        _call("finish_task", {"outcome": "completed", "summary": "built the plate"}),
    ])
    finish = [s for s in corpus.samples_from_record(record) if s.meta["op"] == "finish_task"]
    assert len(finish) == 1, "a corpus of only edits trains a model that never stops"
    args = json.loads(finish[0].messages[-1]["tool_calls"][0]["function"]["arguments"])
    assert args == {"outcome": "completed", "summary": "built the plate"}
    assert finish[0].messages[:-1] == sent[-1]["messages"], "the turn it was decided on"


def test_a_failed_call_is_shown_but_never_the_label(monkeypatch):
    """Recovering from a failure is most of what an agent does, so the failure
    belongs in the transcript — and nowhere else."""
    record, _ = _run(monkeypatch, "a plate", [
        _call("add_primitive", {"shape": "banana", "params": {}}),
        _call("add_primitive", BOX),
        _call("finish_task", {"outcome": "completed", "summary": "built it"}),
    ])
    samples = corpus.samples_from_record(record)
    labels = [json.loads(s.messages[-1]["tool_calls"][0]["function"]["arguments"])
              for s in samples]
    assert all(l.get("shape", "box") != "banana" for l in labels)
    recovery = [s for s in samples if s.meta["recovers_failure"]]
    assert len(recovery) == 1
    shown = recovery[0].messages[-2]["content"]
    shown = shown if isinstance(shown, str) else json.dumps(shown)
    assert "BAD_ARG" in shown, "the typed code the playbook is indexed on"


def test_a_run_that_stopped_short_is_left_out_unless_asked_for(monkeypatch):
    record, _ = _run(monkeypatch, "a plate", [_call("add_primitive", BOX)] * 3)
    assert record["outcome"] not in corpus.GOOD_OUTCOMES
    assert corpus.samples_from_record(record) == []
    assert corpus.samples_from_record(record, include_unfinished=True)


def test_a_run_whose_prompt_had_images_is_left_out():
    record = {"id": "r", "goal": "match this photo", "outcome": "done", "image_count": 2,
              "steps": [{"i": 0, "op": "add_primitive", "args": BOX, "ok": True,
                         "obs_before": {"name": "x"}, "result": {}}]}
    assert corpus.samples_from_record(record) == [], "the bytes are gone; the sample would lie"


def test_every_turn_of_one_run_lands_in_one_split():
    assert corpus.split_of("record-a", 0.5) == corpus.split_of("record-a", 0.5)
    keys = [f"record-{i}" for i in range(200)]
    held = [k for k in keys if corpus.split_of(k, 0.1) == "val"]
    assert 5 <= len(held) <= 30, "roughly a tenth of sources, not of samples"
    assert corpus.split_of("anything", 0.0) == "train"


def test_identical_turns_are_deduplicated():
    def sample():
        return corpus.Sample(messages=[{"role": "user", "content": "same"},
                                       _call("bounding_box", {})],
                             tools=[], meta={"record_id": "r", "source": "recorded"})
    built = corpus.assemble([sample(), sample()], val_fraction=0)
    assert built["duplicates_dropped"] == 1
    assert len(built["splits"]["train"]) == 1


def test_a_template_becomes_verified_samples_and_a_card():
    """End to end on one template: decompiled, replayed, mesh hashes compared —
    so a sequence that rebuilds a different part never reaches the set."""
    from app.services import templates

    doc = document_service.create_document(name="plate template", owner_id="dataset")
    key = templates.describe()[0]["key"]
    env = cad_operations.execute("apply_template", doc.id, {"template": key},
                                 include_state=False)
    assert env["ok"], env.get("error")
    samples = corpus.samples_from_document(doc.id, "a starter plate", source="template")
    assert samples, "the starter templates are decompilable by construction"
    assert all(s.meta["verified"] and s.meta["source"] == "template" for s in samples)
    assert samples[-1].meta["op"] == "finish_task" and samples[-1].meta["synthetic_finish"]
    summary = json.loads(samples[-1].messages[-1]["tool_calls"][0]["function"]["arguments"])
    assert "mm" in summary["summary"], "the composed summary states what was measured"

    report = {"figures": corpus.stats(corpus.assemble(samples, val_fraction=0)["splits"]),
              "duplicates_dropped": 0, "sources": ["template"], "val_fraction": 0.0,
              "database": "test"}
    text = build_cli.card(report)
    assert "# Forge copilot fine-tuning set" in text and "template" in text


def test_a_training_line_carries_no_provenance():
    """Trainers reject unknown top-level keys, and provenance is not input."""
    sample = corpus.Sample(messages=[{"role": "user", "content": "x"}], tools=[{"a": 1}],
                           meta={"source": "recorded", "license": "internal"})
    assert set(sample.line()) == {"messages", "tools", "parallel_tool_calls"}
    assert sample.line()["parallel_tool_calls"] is False


@pytest.mark.parametrize("subject", ["document", "assembly"])
def test_the_subject_is_read_from_the_record_or_inferred_from_the_id(subject):
    from app.services import assembly_service

    if subject == "assembly":
        asm = assembly_service.create_assembly("inferred")
        assert corpus._subject_of({"doc_id": asm.id}) == "assembly"
    else:
        doc = document_service.create_document(name="inferred")
        assert corpus._subject_of({"doc_id": doc.id}) == "document"
    assert corpus._subject_of({"doc_id": "x", "subject": subject}) == subject


# --------------------------------------------------- external datasets, converted

PLATE_PROGRAM = """
import cadquery as cq
p_len = 80.0
p_wid = 60.0
p_thk = 10.0
p_hole_dia = 12.0
body = cq.Workplane('XY').box(p_len, p_wid, p_thk).faces('>Z').workplane().hole(p_hole_dia)
result = body
"""


def _write(tmp_path, rows):
    path = tmp_path / "converted.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def test_a_cadquery_program_converts_to_this_catalog_in_metres():
    from datasets import external

    row = external.convert_cadquery(PLATE_PROGRAM, name="plate")
    assert row["license"] == "Apache-2.0" and row["source"] == "zero-to-cad"
    assert [op["op"] for op in row["ops"]] == ["add_primitive", "add_hole"]
    size = row["ops"][0]["args"]["params"]["size"]
    assert (size["x"], size["y"], size["z"]) == (0.08, 0.06, 0.01), "mm in, metres out"
    assert row["ops"][1]["args"]["radius"] == pytest.approx(0.006)
    assert "len 80 mm" in row["goal"], "the goal states the dimensions it was built to"


def test_a_program_the_converter_cannot_read_is_dropped_by_cause():
    """Named for the *converter*, because the first version of this said "this
    catalog cannot express" and used a `revolve` to prove it — while `add_revolve`
    existed the whole time. The chain reader could not read the program; the
    kernel could always have built it, and the interpreter now does. The
    distinction is the whole reason the interpreter was written.

    So the case here is one that genuinely has no equivalent — and it is no longer
    a *selected-face* shell, because `shell_faces` can now be told a face by
    position and the interpreter converts that. What remains is the **closed**
    hollow: `add_modifier("shell")` is `abs(d) - t/2`, so its outer surface sits
    half a wall outside the body, and `MakeThickSolid` with no removed faces
    returns the inner solid rather than the wall — measured, 44,064 mm³ against a
    27,936 mm³ wall on a 60 x 40 x 30 box at 3 mm.
    """
    from datasets import external

    external.DROPS.clear()
    assert external.convert_cadquery("""
import cadquery as cq
t = 2.0
body = cq.Workplane('XY').box(20, 20, 20).shell(t)
""") is None
    assert external.DROPS, "a silent drop is a yield nobody can see"


def test_a_shell_through_a_named_face_is_expressible_after_all():
    """The case the test above used to use. `shell_faces` always hollowed *and*
    opened; what it lacked was a way to be told which face without an evaluated
    body to measure ids on."""
    from datasets import external

    row = external.convert_cadquery("""
import cadquery as cq
t = 2.0
body = cq.Workplane('XY').box(20, 20, 20).faces('>Z').shell(t)
""")
    assert row is not None
    assert row["ops"][-1]["op"] == "shell_faces"
    assert row["ops"][-1]["args"]["faces"] == "top"


def test_the_interpreter_reads_what_the_chain_reader_could_not():
    """The same `revolve` program the previous test used to call unexpressible."""
    from datasets import external

    row = external.convert_cadquery("""
import cadquery as cq
r = 10.0
body = cq.Workplane('XZ').circle(r).revolve()
""")
    assert row is not None and row["converted_by"] == "interpreter"
    assert [o["op"] for o in row["ops"]] == ["add_revolve"]


def test_a_converted_sequence_is_kept_only_if_it_builds_the_source_s_body(tmp_path):
    """The check that catches a converter bug: a valid solid can be the wrong
    solid, so where the source states its geometry, the replay has to match."""
    from datasets import external

    row = external.convert_cadquery(PLATE_PROGRAM, name="plate")
    good = {**row, "expect": {"size_mm": {"x": 80, "y": 60, "z": 10}}}
    samples = external.samples_from_file(_write(tmp_path, [good]))
    assert samples and all(s.meta["license"] == "Apache-2.0" for s in samples)
    assert samples[0].meta["verified_by"] == "matches_source"

    external.DROPS.clear()
    wrong = {**row, "expect": {"size_mm": {"x": 120, "y": 60, "z": 10}}}
    assert external.samples_from_file(_write(tmp_path, [wrong])) == []
    assert external.DROPS.get("verify:bbox_mismatch") == 1


# ------------------------------------------------------ recovery pairs, injected

def test_an_injected_call_is_refused_before_it_can_change_anything():
    """Both mutations have to fail *at validation*: one that half-applied would
    corrupt the sequence the sample is meant to teach."""
    from app.services import cad_operations

    doc = document_service.create_document(name="mutation check")
    args = {"shape": "box", "params": {"size": {"x": 0.04, "y": 0.03, "z": 0.006}}}
    for mutate in corpus.MUTATIONS:
        wrong = mutate("add_primitive", args)
        if wrong is None:
            continue
        env = cad_operations.execute("add_primitive", doc.id, wrong, include_state=False)
        assert not env["ok"], f"{mutate.__name__} was accepted: {wrong}"
        assert env["error"]["code"] in ("BAD_ARG", "MISSING_ARG")
    features = cad_operations.execute("list_features", doc.id, {}, include_state=False)
    assert not [f for f in features["result"]["features"] if f["type"] == "BoxFeature"]


def test_recovery_samples_label_the_call_that_recovered():
    from app.services import cad_operations, templates

    key = templates.describe()[0]["key"]
    doc = document_service.create_document(name="recovery source", owner_id="dataset")
    assert cad_operations.execute("apply_template", doc.id, {"template": key},
                                 include_state=False)["ok"]
    samples = corpus.recovery_samples_from_document(doc.id, "a starter plate",
                                                    source="recovery")
    assert samples, "the starter templates replay exactly, so this must produce pairs"
    recovery = [s for s in samples if s.meta.get("recovers_failure")]
    assert recovery, "the point of the source"
    shown = recovery[0].messages[-2]["content"]
    shown = shown if isinstance(shown, str) else json.dumps(shown)
    assert "BAD_ARG" in shown or "MISSING_ARG" in shown, "the real typed code, executed"
    label = json.loads(recovery[0].messages[-1]["tool_calls"][0]["function"]["arguments"]) \
        if recovery[0].messages[-1].get("tool_calls") else \
        json.loads(recovery[0].messages[-1]["content"])
    assert label, "the label is the correct call, never the refused one"


def test_the_finetuned_profile_states_one_output_contract_and_no_discovery():
    """The profile that trains and serves a local model must not describe the
    surface it withholds.

    It used to take `system_prompt(with_tools=True)`, whose closing sentence
    asks for a tool call and a plain-text finish, *and* `_DONE_PROTOCOL`, which
    asks for one JSON object — while handing over no tools at all. It also
    carried `CONTROL_RULES`, which tells a planner to load a schema with
    `discover_operations` before calling an operation; in this profile every
    name is already listed and no schema is ever sent, so that is an
    instruction it cannot follow, once per turn, in ~370 tokens. Nothing caught
    either, because both were correct prose for the other profile.
    """
    from app.services import copilot, copilot_context

    messages, tools = copilot.planner_request(
        "a 40 x 30 x 6 mm plate", [], {"kind": "document"}, profile="finetuned")
    system = messages[0]["content"]

    assert tools is None
    assert "EXACTLY ONE JSON object" in system
    assert "using the provided tools" not in system
    assert "reply with plain text" not in system
    assert copilot_context.CONTROL_RULES not in system
    assert "update_work_plan" not in system
    # What stays: the failure playbook and the brief rule are about operations
    # this profile does have, and the brief rule is the only source of the
    # behaviour — the corpus carries two `set_spec` samples in 2,428. The
    # playbook's own UNKNOWN_OP line still names `discover_operations`, which is
    # why this asserts the absence of the *block* rather than of the word: one
    # stale pointer inside a block that is otherwise about the operations this
    # profile has is worth keeping the block for, where 434 tokens of rules
    # about two tools it does not ship is not.
    assert "PRECONDITION" in system and "set_spec" in system


def test_the_full_profile_keeps_the_planning_rules():
    """The trim is scoped to one profile: production runs on `full`."""
    from app.services import copilot_context

    full = copilot_context.control_rules("document")
    assert "discover_operations" in full and "update_work_plan" in full
    assert copilot_context.CONTROL_RULES not in copilot_context.control_rules(
        "document", planning=False)


def test_the_coverage_source_labels_the_operations_nothing_else_does():
    """The five the 18-task suite showed missing, and why no other source has them.

    Four tasks failed *only* their feature requirement with the right bounding box
    built, and all three material tasks failed with the material unset — because
    `fillet_edges`, `chamfer_edges`, `shell_faces`, `add_rib` and `set_material`
    had zero labels between them in 6,173. `decompiler` emits the first four
    happily; no library document or template contains those features. And
    `set_material` is document metadata rather than a tree node, so decompiling
    any number of bodies can never produce it.
    """
    from datasets import corpus

    corpus.DROPS.clear()
    samples = corpus.coverage_samples(profile="finetuned")
    counts: dict = {}
    for sample in samples:
        op = sample.meta.get("op")
        counts[op] = counts.get(op, 0) + 1

    assert not corpus.DROPS, f"a coverage sequence did not build what it claims: {corpus.DROPS}"
    for op in ("fillet_edges", "chamfer_edges", "shell_faces", "add_rib", "set_material"):
        assert counts.get(op, 0) >= 15, (
            f"{op} has {counts.get(op, 0)} labels; a handful in six thousand is "
            f"present in the corpus and invisible in it — the first version of "
            f"this source wrote twelve sequences by hand and produced three")
    # The read-then-act turn: `shell_faces` takes a face id that only the
    # previous turn's result can supply, which is most of what driving this
    # catalog is and almost none of what the corpus showed.
    assert counts.get("list_faces", 0) >= 15
    # `BRIEF_RULE` was taught by prose alone: two `set_spec` labels in 6,173.
    assert counts.get("set_spec", 0) >= 100


def test_the_library_source_skips_the_builders_own_documents():
    """`library` means documents somebody built, not the ones this builder made.

    `catalog`, `template`, `recovery` and `coverage` each create scratch documents
    and leave them in the datastore, so a later build's library pass decompiled
    them a second time — with the document's *name* as the goal, which is how
    "template plate x1.5" became a goal a model was taught to build. Measured on
    the build database: 714 of 1,234 documents were the builder's own, and library
    went 1,020 -> 4,789 samples between two builds of the same corpus purely by
    accumulation. Deduplication could not see it, because the goals differ.
    """
    from app.services import cad_operations, document_service
    from datasets import corpus

    MM = 0.001
    made = {}
    for owner, name in ((corpus.BUILDER_OWNER, "template scratch"), ("someone", "Bracket")):
        doc = document_service.create_document(name=name, owner_id=owner)
        cad_operations.execute("add_primitive", doc.id, {
            "shape": "box", "params": {"size": {"x": 40 * MM, "y": 30 * MM, "z": 6 * MM},
                                       "center": {"x": 0.0, "y": 0.0, "z": 0.0}}})
        made[owner] = doc.id

    # The library source's goal *is* the document name (or its prompt), which is
    # the whole problem with reading back the builder's own scratch parts.
    goals = " ".join(str(m.get("content") or "")
                     for sample in corpus.library_samples()
                     for m in sample.messages if m.get("role") == "user")
    assert "Bracket" in goals, "a document somebody built was skipped"
    assert "template scratch" not in goals, (
        "the builder's own scratch document was decompiled as if it were a "
        "library part, with its scratch name as the goal")


#: One exported SketchGraphs sketch in the contract `docs/CAD-DATASET-SOURCING.md`
#: documents: a constrained rectangle with a bored circle, which is most of what
#: that dataset contains.
SG_SQUARE = {
    "id": "sg-1",
    "entities": [
        {"kind": "line", "id": "l0", "start": {"x": 0, "y": 0}, "end": {"x": 50, "y": 0}},
        {"kind": "line", "id": "l1", "start": {"x": 50, "y": 0}, "end": {"x": 50, "y": 30}},
        {"kind": "line", "id": "l2", "start": {"x": 50, "y": 30}, "end": {"x": 0, "y": 30}},
        {"kind": "line", "id": "l3", "start": {"x": 0, "y": 30}, "end": {"x": 0, "y": 0}},
        {"kind": "circle", "id": "c0", "center": {"x": 25, "y": 15}, "radius": 6},
    ],
    "constraints": [
        {"type": "Coincident", "refs": ["l0.end", "l1.start"]},
        {"type": "Coincident", "refs": ["l1.end", "l2.start"]},
        {"type": "Coincident", "refs": ["l2.end", "l3.start"]},
        {"type": "Coincident", "refs": ["l3.end", "l0.start"]},
        {"type": "Horizontal", "refs": ["l0"]},
        {"type": "Vertical", "refs": ["l1"]},
        {"type": "Length", "refs": ["l0"], "value": 50},
        {"type": "Fix", "refs": ["l0.start"]},
        {"type": "Diameter", "refs": ["c0"], "value": 12},
        {"type": "Parallel", "refs": ["l0", "l2"]},
        {"type": "Perpendicular", "refs": ["l0", "l1"]},
    ],
}


def test_a_sketchgraphs_sketch_converts_to_this_solver_s_constraints():
    """15M constrained sketches against `create_sketch`, which has no other source.

    Three mappings are the substance rather than renaming: `Length` on a line is
    a `distance` between its own endpoints, `Diameter` is a `radius` halved, and
    `Fix` must carry *both* coordinates — `fixed` emits one row per axis given,
    and a missing axis used to default to the point's live value, a residual
    identically zero that pins nothing while still counting as an equation.
    """
    from datasets import external

    row = external.convert_sketchgraphs(SG_SQUARE)
    assert row is not None
    spec = row["ops"][0]["args"]["sketch"]
    assert row["ops"][0]["op"] == "create_sketch"
    assert row["verify"] == "sketch", "a sketch has no body for a geometry check"
    assert row["license"] == "Onshape ToU 1.g.ii"

    kinds = {c["type"] for c in spec["constraints"]}
    assert {"coincident", "horizontal", "vertical", "distance", "radius",
            "fixed", "parallel", "perpendicular"} <= kinds

    assert {"type": "radius", "circle": "c0", "value": 6.0} in spec["constraints"], \
        "a diameter is a radius under another name"
    assert {"type": "distance", "a": "l0.start", "b": "l0.end",
            "value": 50.0} in spec["constraints"]
    assert {"type": "fixed", "point": "l0.start", "x": 0.0,
            "y": 0.0} in spec["constraints"]
    # Endpoints are named points and coincident ones are *not* merged: joining
    # them is what the Coincident constraints are for, and merging here would
    # discard the relation the dataset exists to carry.
    assert len(spec["points"]) == 9 and len(spec["lines"]) == 4


@pytest.mark.parametrize("record, cause", [
    ({"entities": [{"kind": "spline", "id": "s0"}], "constraints": []},
     "sketchgraphs:entity:spline"),
    ({"entities": [{"kind": "line", "id": "l0", "start": {"x": 0, "y": 0},
                    "end": {"x": 5, "y": 0}}],
      "constraints": [{"type": "Mirror", "refs": ["l0", "l0"]}]},
     "sketchgraphs:constraint:Mirror"),
])
def test_a_sketch_this_solver_cannot_express_is_dropped_by_cause(record, cause):
    """Whole, not partially. A sketch missing one relation solves to a different
    shape, and unlike a solid there is no volume to catch it afterwards."""
    from datasets import external

    external.DROPS.clear()
    assert external.convert_sketchgraphs(record) is None
    assert cause in external.DROPS


def test_a_converted_sketch_is_verified_by_the_solver_not_by_geometry(tmp_path):
    from datasets import external

    path = tmp_path / "sg.jsonl"
    path.write_text(json.dumps(external.convert_sketchgraphs(SG_SQUARE)) + "\n")
    external.DROPS.clear()
    samples = external.samples_from_file(str(path))
    assert not external.DROPS, external.DROPS
    assert [s.meta.get("op") for s in samples] == ["create_sketch", "finish_task"]


def test_the_yield_report_separates_what_blocks_from_what_would_unblock(tmp_path):
    """A drop histogram names the *first* refusal, which is not the same question.

    `convert_cadquery` stops at the first call it cannot express, so causes rank
    by which call is met earliest. Measured on Zero-To-CAD's 10,000 test programs,
    that put `center` at 28.9% — while the best call to actually *add* wins 30
    programs, because `union` is unsupported in 58.6% of them and most need
    several. `unblocks` counts only programs whose whole unsupported set is one
    call: those are the ones supporting it would win. Same distinction as
    `decompiler.corpus_report`'s `blocking`.
    """
    from datasets import yield_report

    rows = [
        # Converts: box + hole are both expressible.
        {"uuid": "a", "program": "import cadquery as cq\n"
                                 "r = cq.Workplane('XY').box(40, 30, 6).faces('>Z')"
                                 ".hole(5)\n"},
        # One call away — `shell` alone stands between this and a conversion.
        {"uuid": "b", "program": "import cadquery as cq\n"
                                 "r = cq.Workplane('XY').box(40, 30, 6).shell(2)\n"},
        # Two missing calls, so adding either one wins nothing here.
        {"uuid": "c", "program": "import cadquery as cq\n"
                                 "r = cq.Workplane('XY').box(9, 9, 9).shell(1).revolve(90)\n"},
    ]
    path = tmp_path / "rows.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))

    report = yield_report.cadquery_yield(rows)
    assert report["rows"] == 3 and report["converted"] == 1
    assert report["unblocks"] == {"shell": 1}, report["unblocks"]
    assert report["blocked_by"]["shell"] == 2, "both rows mention it"
    assert report["blocked_by"]["revolve"] == 1
    assert report["ops_emitted"].get("add_hole") == 1
    # The CLI is the way this is used, so it has to run.
    assert yield_report.main([str(path), "--json"]) == 0
