"""The operation catalog as real JSON Schema, and the gate that enforces it.

The catalog described arguments in what its own docstring called a
"JSON-schema-ish params block": no `required`, no object wrapper, no validation.
A model could not be constrained to it, required-ness was enforced by accident
(a handler indexing `args["sketch"]` and raising), and declared defaults were
decorative because each handler re-applied its own.
"""

import pytest

from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services import op_schema
from app.services.op_errors import BadArgument, MissingArgument


def _doc():
    return docs.create_document(name="schema", resolution=32, owner_id=None)


# ----------------------------------------------------------- schema shape --

def test_every_operation_emits_a_valid_object_schema():
    for entry in ops.catalog():
        schema = entry["schema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        # Closed: a misspelled argument must be rejected, not silently ignored.
        assert schema["additionalProperties"] is False


def test_required_arguments_reach_the_schema():
    schema = next(e["schema"] for e in ops.catalog() if e["name"] == "set_feature_params")
    assert set(schema["required"]) == {"feature_id", "params"}


def test_an_operation_with_no_arguments_declares_none_required():
    schema = next(e["schema"] for e in ops.catalog() if e["name"] == "list_features")
    assert "required" not in schema
    assert schema["properties"] == {}


def test_enum_arguments_get_a_type_so_a_decoder_can_narrow_early():
    prop = op_schema.json_schema({"shape": {"enum": ["box", "sphere"]}})["properties"]["shape"]
    assert prop["type"] == "string"
    assert prop["enum"] == ["box", "sphere"]


def test_the_internal_required_flag_is_not_leaked_into_the_schema():
    """`required` belongs on the object, not on the property."""
    prop = op_schema.json_schema({"x": {"type": "string", "required": True}})["properties"]["x"]
    assert prop == {"type": "string"}


def test_the_opaque_params_blob_now_names_its_keys():
    """The likeliest place for a model to invent parameter names."""
    schema = next(e["schema"] for e in ops.catalog() if e["name"] == "add_primitive")
    primitive = schema["properties"]["params"]
    assert set(primitive["properties"]) == {"size", "radius", "base_radius", "height",
                                            "center", "axis"}

    modifier = next(e["schema"] for e in ops.catalog()
                    if e["name"] == "add_modifier")["properties"]["params"]
    assert {"thickness", "spacing", "count", "translate"} <= set(modifier["properties"])


def test_the_catalog_still_carries_the_descriptor_block_everything_reads():
    for entry in ops.catalog():
        assert set(entry) >= {"name", "kind", "description", "params", "schema"}


def test_tool_definitions_are_function_calling_shaped():
    tools = op_schema.tool_definitions()
    by_name = {t["function"]["name"]: t for t in tools}
    assert len(tools) == len(ops.OPERATIONS)
    add = by_name["add_primitive"]
    assert add["type"] == "function"
    assert add["function"]["parameters"]["required"] == ["shape"]
    assert add["function"]["description"]


# ------------------------------------------------------------- validation --

def test_a_missing_required_argument_is_refused_before_the_handler_runs():
    with pytest.raises(MissingArgument) as exc:
        op_schema.validate({"feature_id": {"type": "string", "required": True}}, {},
                           op="delete_feature")
    assert exc.value.arg == "feature_id"
    assert "delete_feature" in exc.value.message


def test_a_value_outside_an_enum_is_refused():
    with pytest.raises(BadArgument) as exc:
        op_schema.validate({"plane": {"enum": ["xy", "xz", "yz"]}}, {"plane": "ab"})
    assert exc.value.arg == "plane"


def test_a_wrongly_typed_argument_is_refused():
    with pytest.raises(BadArgument) as exc:
        op_schema.validate({"depth": {"type": "number"}}, {"depth": "deep"})
    assert "must be number" in exc.value.message


def test_a_boolean_is_not_accepted_as_a_number():
    """`True` is an int in Python; a depth of True is a bug, not a dimension."""
    with pytest.raises(BadArgument):
        op_schema.validate({"depth": {"type": "number"}}, {"depth": True})


def test_an_unknown_argument_is_named_alongside_what_is_accepted():
    with pytest.raises(BadArgument) as exc:
        op_schema.validate({"feature_id": {"type": "string"}}, {"featureId": "f1"},
                           op="get_feature")
    assert "featureId" in exc.value.message
    assert "feature_id" in exc.value.message


def test_an_omitted_optional_argument_is_fine():
    # `validate` signals by raising, so the assertion is that it returns.
    assert op_schema.validate({"label": {"type": "string"}}, {}) is None


def test_an_explicit_null_does_not_trip_type_checking():
    """A caller that spells an unset optional as `null` means the same thing as
    omitting it — type-checking `None` against `number` would refuse it."""
    assert op_schema.validate({"radius": {"type": "number"}}, {"radius": None}) is None


# --------------------------------------------------- enforced end to end --

def test_a_misspelled_argument_is_caught_by_the_api():
    env = ops.execute("add_primitive", _doc().id, {"shapes": "box"})
    assert env["ok"] is False
    assert env["error"]["code"] in ("BAD_ARG", "MISSING_ARG")


def test_a_bad_enum_is_caught_before_any_geometry_changes():
    doc = _doc()
    before = docs.get_document(doc.id).feature_tree
    env = ops.execute("add_primitive", doc.id, {"shape": "box", "operation": "smoosh"})
    assert env["ok"] is False
    assert env["error"]["arg"] == "operation"
    assert docs.get_document(doc.id).feature_tree == before


def test_validation_does_not_reject_the_calls_the_catalog_advertises():
    """Guards against a schema that is stricter than the handlers really are."""
    doc = _doc()
    for op, args in [
        ("add_primitive", {"shape": "cylinder", "params": {"radius": .01, "height": .04,
                                                           "center": {"x": 0, "y": 0, "z": 0},
                                                           "axis": {"x": 0, "y": 0, "z": 1}}}),
        ("add_modifier", {"modifier": "hole", "params": {"radius": .003, "depth": .05,
                                                         "center": {"x": 0, "y": 0, "z": 0}}}),
        ("add_extrude", {"profile": {"type": "rect", "width": .04, "height": .02},
                         "plane": "xy", "depth": .01}),
        ("set_lifecycle_state", {"state": "in_review"}),
        ("compute_fit", {"nominal_mm": 20.0, "fit": "H7/g6"}),
    ]:
        env = ops.execute(op, doc.id, args)
        assert env["ok"], (op, env.get("error"))


def test_the_tools_endpoint_serves_the_action_space_for_function_calling():
    from fastapi.testclient import TestClient
    from main import app

    tools = TestClient(app).get("/api/operations/tools").json()["tools"]
    assert len(tools) == len(ops.OPERATIONS)
    assert all(t["type"] == "function" for t in tools)
    assert all("parameters" in t["function"] for t in tools)


def test_the_catalog_endpoint_carries_the_schema_too():
    from fastapi.testclient import TestClient
    from main import app

    catalog = TestClient(app).get("/api/operations").json()["operations"]
    assert all("schema" in entry for entry in catalog)
