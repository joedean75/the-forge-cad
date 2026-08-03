"""The surface an agent is actually shown, and whether it can be got wrong.

The catalog was built to be machine-checkable and most of it was. What these
tests pin is the part that was not: a schema that was *declared* and never
enforced below the top level, lengths with no unit anywhere, and defaults that
invented geometry and reported success.

Every assertion here corresponds to a call that once returned ``ok: true`` and a
body nobody asked for. That is the failure mode this whole surface exists to
avoid, and it is invisible from inside a run — the model is told it succeeded.
"""

import pytest

from app.services import action_space, cad_operations as ops, document_service as docs
from app.services import op_errors, op_schema, units


def _doc(name="surface"):
    return docs.create_document(name=name).id


def _params(op_name):
    return next(o for o in ops.catalog() if o["name"] == op_name)["params"]


# ------------------------------------------------------- schema completeness --

def test_every_parameter_says_what_it_is():
    """A model handed a bare parameter name has to guess, and it guesses wrong
    on exactly the arguments that matter."""
    missing = []

    def walk(op, params, prefix=""):
        for name, descriptor in (params or {}).items():
            if not (descriptor or {}).get("description") and name not in units.COMPONENTS:
                missing.append(f"{op}.{prefix}{name}")
            nested = (descriptor or {}).get("properties")
            if isinstance(nested, dict):
                walk(op, nested, f"{prefix}{name}.")

    for entry in ops.catalog():
        walk(entry["name"], entry["params"])
    assert not missing, f"parameters with no description: {missing}"


def test_every_parameter_has_a_unit_or_is_declared_not_to():
    """Not "most lengths say metres" — *every* parameter is classified, so a new
    one has to be ruled on rather than defaulting to silence."""
    unclassified = [f"{op.name}.{path}"
                    for op in ops.OPERATIONS.values()
                    for path in units.unclassified(op.params)]
    assert not unclassified, f"no unit and not declared unitless: {unclassified}"


def test_a_length_states_its_unit_in_the_description_it_ships_with():
    size = _params("add_primitive")["params"]["properties"]["size"]
    assert "metres" in size["description"]
    assert "metres" in size["properties"]["x"]["description"]


def test_a_vector_component_takes_its_meaning_from_the_vector():
    """`rotate.x` is degrees and `center.x` is metres, and neither can be known
    from the name `x`."""
    modifier = _params("add_modifier")["params"]["properties"]
    assert modifier["rotate"]["properties"]["x"]["unit"] == "deg"
    assert modifier["center"]["properties"]["x"]["unit"] == "m"
    # `axis` is a direction: dimensionless, and its components inherit that.
    assert "unit" not in modifier["axis"]["properties"]["x"]


def test_the_emitted_schema_is_schema_shaped_all_the_way_down():
    """`required` is our spelling — a boolean on the property — and JSON Schema
    wants an array on the object above. Emitting the boolean produced something
    no validator would accept, in the one payload built to be handed to one."""
    for tool in action_space.tools():
        def check(node):
            assert not isinstance(node.get("required"), bool), tool["function"]["name"]
            for child in (node.get("properties") or {}).values():
                check(child)
        check(tool["function"]["parameters"])


def test_a_length_cannot_be_zero_or_negative_in_the_schema():
    radius = _params("add_modifier")["params"]["properties"]["radius"]
    assert op_schema.json_schema({"radius": radius})["properties"]["radius"]["exclusiveMinimum"] == 0


# ---------------------------------------------------- refusals, not inventions --

def test_an_invented_parameter_name_is_refused():
    """This is the one that mattered most. `additionalProperties: false` was
    declared and never enforced below the top level, so three invented keys were
    dropped on the floor and a default 50 mm cube came back as a success."""
    env = ops.execute("add_primitive", _doc(), {
        "shape": "box", "params": {"width": 0.1, "depth": 0.06, "thickness": 0.008}})
    assert not env["ok"]
    assert env["error"]["code"] == "BAD_ARG"
    assert "width" in env["error"]["message"]
    # And it says what the field *is* called, so the next call can be right.
    assert "size" in env["error"]["message"]


def test_a_vector_passed_as_a_list_is_refused_at_the_boundary():
    env = ops.execute("add_primitive", _doc(), {
        "shape": "box", "params": {"size": [0.1, 0.06, 0.008]}})
    assert not env["ok"] and env["error"]["code"] == "BAD_ARG"


@pytest.mark.parametrize("op,args", [
    ("add_extrude", {"depth": 0.008}),
    ("add_revolve", {}),
    ("add_sweep", {"radius": 0.004}),
    ("add_loft", {}),
    ("add_ruled_loft", {}),
], ids=lambda v: v if isinstance(v, str) else "")
def test_an_operation_with_no_geometry_to_work_from_refuses(op, args):
    """A default is the answer a caller would have chosen. A profile nobody
    mentioned is not a default — it is an invention, reported as a success."""
    env = ops.execute(op, _doc(), args)
    assert not env["ok"], f"{op} built something from nothing"
    assert env["error"]["code"] == "MISSING_ARG"


def test_a_dimensional_default_is_still_allowed():
    """The line is geometry, not dimensions. A box with a default size is a box;
    an extrude with a default *profile* is a different part."""
    env = ops.execute("add_primitive", _doc(), {"shape": "box"})
    assert env["ok"]


# ------------------------------------------------------------------- units --

def test_millimetres_passed_as_metres_are_warned_about_by_name():
    env = ops.execute("add_primitive", _doc(), {
        "shape": "box", "params": {"size": {"x": 100, "y": 60, "z": 8}}})
    assert env["ok"], "a 100 m box is legal geometry — this is a warning, not a refusal"
    codes = [w["code"] for w in env["warnings"]]
    assert "SUSPECT_UNITS" in codes
    message = next(w["message"] for w in env["warnings"] if w["code"] == "SUSPECT_UNITS")
    assert "millimetres" in message or "mm" in message


def test_a_part_sized_part_is_not_warned_about():
    """The band has to be generous or it cries wolf on real work: a 2 m sheet
    and a 0.15 mm wall are both things this system makes."""
    env = ops.execute("add_primitive", _doc(), {
        "shape": "box", "params": {"size": {"x": 2.0, "y": 0.6, "z": 0.0002}}})
    assert env["ok"] and not env["warnings"]


def test_an_angle_is_never_mistaken_for_a_length():
    env = ops.execute("add_modifier", _doc(), {
        "modifier": "transform", "params": {"rotate": {"x": 0, "y": 0, "z": 90}}})
    assert not [w for w in env["warnings"] if w["code"] == "SUSPECT_UNITS"]


# ---------------------------------------------------------------- remedies --

def test_every_error_code_says_what_to_do_next():
    classes = [op_errors.UnknownOperation, op_errors.MissingArgument,
               op_errors.BadArgument, op_errors.NoSuchDocument,
               op_errors.NoSuchFeature, op_errors.PreconditionFailed,
               op_errors.EvaluationFailed, op_errors.InternalError]
    remedies = {c.code: c.remedy for c in classes}
    assert len(remedies) == 8
    assert all(len(r) > 20 for r in remedies.values())
    # The distinction that matters: a precondition is not fixed by arguments.
    assert "earlier" in op_errors.PreconditionFailed.remedy


def test_a_failure_carries_its_remedy_to_the_caller():
    env = ops.execute("flat_pattern", _doc(), {})
    assert env["error"]["code"] == "PRECONDITION"
    assert env["error"]["remedy"]


# ------------------------------------------------------- one action space --

def test_the_prompt_and_the_tools_describe_the_same_operations():
    """They used to be built in three places and agreed by coincidence. The
    corpus is training data: if it describes a different API from the one used
    at inference, it teaches a model to drive something that does not exist."""
    from app.services import trajectories

    named = {t["function"]["name"] for t in action_space.tools()}
    assert named == set(ops.OPERATIONS)

    prose = action_space.catalog_text()
    for name in named:
        assert f"- {name} (" in prose

    # The corpus export uses the same generator, not a second one.
    assert trajectories._catalog_text() == action_space.system_prompt(with_tools=False)


def test_the_prose_catalog_carries_types_units_and_requiredness():
    """It used to be a list of identifiers. Everything a model needs to get an
    argument right was left to be guessed."""
    prose = action_space.catalog_text()
    assert "shape: enum(box|sphere|cylinder|cone) required" in prose
    assert "depth: number m" in prose


def test_the_tool_prompt_does_not_repeat_the_catalog():
    """The schemas are already in the payload; restating them in prose is the
    drift `action_space` exists to stop."""
    assert "add_primitive" not in action_space.system_prompt(with_tools=True)
    assert "add_primitive" in action_space.system_prompt(with_tools=False)
