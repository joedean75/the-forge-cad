"""The limits a first serious part runs into, and how they fail.

Four ceilings, each of which used to be a flat refusal (or worse, a silent
truncation) with nothing below it. The sprint that produced these set out to
*raise* them; measuring first changed two of the answers, and the measurements
are recorded in the code beside each number so the next person does not have to
re-derive them.

The rule the whole group is held to: **a limit may move, and it may warn before
it, but it may never become a silent truncation.** A plan quietly missing its
last four parts looks like the planner's fault.
"""

import pytest

from app.services import fea, import_service, sketch_solver, text_to_assembly
from app.services import cad_operations as ops
from app.services import document_service as docs

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731


# ================================================================== sketch ==

def _chain(n: int, constraints_per: int = 1) -> dict:
    points = [{"id": f"p{i}", "x": i * 0.01, "y": 0.0} for i in range(n + 1)]
    lines = [{"id": f"l{i}", "a": f"p{i}", "b": f"p{i+1}"} for i in range(n)]
    cons = [{"type": "fixed", "point": "p0", "x": 0.0, "y": 0.0}]
    for i in range(n):
        cons.append({"type": "distance", "a": f"p{i}", "b": f"p{i+1}", "value": 0.01})
        if constraints_per > 1:
            cons.append({"type": "horizontal", "line": f"l{i}"})
        if constraints_per > 2 and i:
            cons.append({"type": "equal_length", "line1": f"l{i-1}", "line2": f"l{i}"})
    return {"points": points, "lines": lines, "constraints": cons}


def test_the_bound_is_on_the_product_not_on_either_count():
    """The measurement that changed the design. Cost tracks entities x
    constraints; refusing a big lightly-constrained sketch on an entity count
    tuned for a heavily-dimensioned one was the actual limitation."""
    # A traced outline: hundreds of points, almost no constraints. This is the
    # shape `image_to_cad.trace_to_tree` produces and the one the old entity cap
    # refused outright, despite it solving in well under a second.
    traced = {"points": [{"id": f"p{i}", "x": i * 0.001, "y": 0.0}
                         for i in range(1500)],
              "constraints": [{"type": "fixed", "point": "p0", "x": 0.0, "y": 0.0}]}
    entities, constraints = sketch_solver.sketch_size(traced)
    assert entities > 600, "this should have been over the old entity cap"
    sketch_solver.check_size(traced)                 # must not raise

    # Half the entities, three constraints each: genuinely expensive.
    small_and_dense = _chain(400, constraints_per=3)
    with pytest.raises(sketch_solver.SketchError, match="units of solver work"):
        sketch_solver.check_size(small_and_dense)


def test_the_refusal_says_what_would_fit():
    """`PRECONDITION`-grade advice: the number that has to change, not just the
    number that was exceeded."""
    with pytest.raises(sketch_solver.SketchError) as exc:
        sketch_solver.check_size(_chain(400, constraints_per=3))
    message = str(exc.value)
    assert "budget" in message
    assert "entities; drop constraints or split it" in message


def test_a_sketch_approaching_the_ceiling_warns_before_it_is_refused():
    """The sketch that gets refused is usually the one after the one that took
    four seconds. Below the refusal there is now a band."""
    assert sketch_solver.size_warning(_chain(20)) is None
    note = sketch_solver.size_warning(_chain(250, constraints_per=3))
    assert note and "% of what the solver will take" in note


def test_the_warning_reaches_the_operation_envelope():
    doc = docs.create_document(name="big sketch", owner_id="dev")
    spec = _chain(250, constraints_per=3)
    result = ops.dispatch("check_sketch", doc.id, {"sketch": spec})
    assert "size_note" in result, result.keys()


def test_a_small_sketch_carries_no_size_note():
    """A note on every sketch is a note nobody reads."""
    doc = docs.create_document(name="small sketch", owner_id="dev")
    result = ops.dispatch("check_sketch", doc.id, {"sketch": _chain(4)})
    assert "size_note" not in result


def test_the_per_axis_caps_still_stop_either_running_away_alone():
    """The product bound is the real one, but a million entities with one
    constraint would still allocate a million-variable system."""
    runaway = {"points": [{"id": f"p{i}", "x": 0.0, "y": 0.0}
                          for i in range(sketch_solver.MAX_ENTITIES + 5)],
               "constraints": [{"type": "fixed", "point": "p0", "x": 0.0, "y": 0.0}]}
    with pytest.raises(sketch_solver.SketchError, match="entities"):
        sketch_solver.check_size(runaway)


# ======================================================== text-to-assembly ==

def test_the_part_budget_moved_and_the_prompt_says_the_new_number():
    """The planner is told the cap, so a prompt quoting the old one would ask
    for six parts and then be capped at sixteen."""
    assert text_to_assembly.MAX_PARTS >= 16
    assert f"at most {text_to_assembly.MAX_PARTS} parts" in text_to_assembly._SYSTEM_PROMPT


def test_a_plan_over_the_cap_is_reported_not_silently_sliced(monkeypatch):
    """The failure this whole group exists to prevent. The old code sliced the
    list and said nothing, so an assembly arrived missing its last parts and
    looked like the planner's fault."""
    over = text_to_assembly.MAX_PARTS + 3
    plan = {
        "name": "too much",
        "parts": [{"key": f"k{i}", "name": f"part {i}",
                   "prompt": f"a 10 mm cube number {i}"} for i in range(over)],
        "components": [{"part": "k0"}],
    }
    notes = []

    def build_part(name=None, prompt=None, resolution=None, owner_id=None):
        return docs.create_document(name=name or "p", owner_id=owner_id)

    text_to_assembly.create_assembly_from_prompt(
        "too much", planner=lambda _p: plan, build_part=build_part,
        on_progress=notes.append, owner_id="dev")

    assert any("left out" in str(n) for n in notes), notes
    assert any(f"capped at {text_to_assembly.MAX_PARTS}" in str(n) for n in notes), notes


# ==================================================================== FEA ==

def test_the_element_budget_is_chosen_from_the_body_not_from_a_constant():
    """A flat 8k is a statement about the *volume*, and stress lives across the
    thin direction. A 1.5 mm sheet got one element through its thickness, where
    the answer is not approximate but meaningless."""
    from app.cad_kernel.evaluator.sdf import Bounds
    import numpy as np

    cube = Bounds(np.array([0.0, 0.0, 0.0]), np.array([.05, .05, .05]))
    sheet = Bounds(np.array([0.0, 0.0, 0.0]), np.array([.2, .2, .0015]))
    assert fea.default_elements(cube) == fea._DEFAULT_ELEMENTS      # already ample
    assert fea.default_elements(sheet) > fea._DEFAULT_ELEMENTS


def test_the_budget_never_drops_below_what_it_used_to_be():
    """Nothing that used to resolve may now resolve less."""
    from app.cad_kernel.evaluator.sdf import Bounds
    import numpy as np

    for span in ((.05, .05, .05), (.2, .01, .01), (.001, .001, .001)):
        bounds = Bounds(np.zeros(3), np.asarray(span))
        assert fea.default_elements(bounds) >= fea._DEFAULT_ELEMENTS


def test_the_budget_is_capped_where_a_solve_stops_being_waitable():
    from app.cad_kernel.evaluator.sdf import Bounds
    import numpy as np

    foil = Bounds(np.zeros(3), np.array([.5, .5, .00002]))
    assert fea.default_elements(foil) == fea._MAX_ELEMENTS


def test_an_explicit_budget_is_honoured_and_reported_as_the_callers():
    """`budget_chosen` distinguishes "the solver picked this" from "you asked
    for this", which changes what an under-resolved result means."""
    from app.cad_kernel.core.feature_tree import Feature

    class _P:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = x, y, z

    class _Load:
        def __init__(self, point, force):
            self.point, self.force = point, force

    beam = Feature.from_dict({"type": "BoxFeature", "params": {
        "size": V(.2, .02, .02), "center": V(0, 0, 0)}})
    result = fea.solve(beam, youngs_pa=200e9, yield_mpa=250.0, density=7800.0,
                       loads=[_Load(_P(.1, 0, 0), _P(0, 0, -200.0))],
                       constraints=[], target_elements=3000)
    assert result["mesh"]["budget"] == 3000
    assert result["mesh"]["budget_chosen"] is False


# ================================================================= import ==

def test_a_step_file_may_be_larger_than_a_mesh():
    """The payload is worth more: OCC keeps the analytic surfaces, so the faces
    stay addressable. A mesh is voxelised, and past a million triangles the
    extra data does not survive the grid."""
    assert import_service.max_bytes_for("step") > import_service.max_bytes_for("stl")
    assert import_service.max_bytes_for("stl") == import_service.MAX_FILE_BYTES
    assert import_service.max_bytes_for(".STEP") == import_service.MAX_EXACT_FILE_BYTES


def test_an_oversized_mesh_is_told_that_step_has_more_room():
    """A refusal that names the alternative, not just the number."""
    payload = b"x" * (import_service.MAX_FILE_BYTES + 1)
    with pytest.raises(import_service.ImportError_) as exc:
        import_service.import_document("huge.stl", payload, owner_id="dev")
    assert "STEP and IGES may be up to" in str(exc.value)


def test_the_transfer_cap_clears_the_largest_file_it_must_carry():
    """The body limit runs before the body is assembled and knows nothing about
    formats, so it has to admit the biggest acceptable file plus its multipart
    framing — otherwise a legal STEP is refused by the wrong layer, with a
    message about bytes rather than about the file."""
    from app import body_limit

    assert body_limit.UPLOAD_MAX_BYTES > import_service.MAX_EXACT_FILE_BYTES
    assert body_limit.limit_for("/api/documents/import") == body_limit.UPLOAD_MAX_BYTES
    assert body_limit.limit_for("/api/documents/x/op") == body_limit.DEFAULT_MAX_BYTES
