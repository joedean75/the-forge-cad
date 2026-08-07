"""The operation result envelope — the contract an agent is trained against.

Before this, every response was ``ok: True`` (the field had one construction site
and no failure path could clear it), failures collapsed into ``KeyError``→404 /
``ValueError``→422 / unhandled→bare 500, and nothing reported what an operation
changed. These tests pin the replacement: a computed ok, a typed error code, and
a geometry delta.
"""

import pytest
from fastapi.testclient import TestClient

from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services import op_errors
from main import app

client = TestClient(app)


def _doc(name="envelope"):
    return docs.create_document(name=name, resolution=32, owner_id=None)


# ------------------------------------------------------------- typed errors --

def test_a_missing_argument_is_not_reported_as_not_found():
    """The headline fix: omitting an argument used to answer `404 Not found: 'sketch'`."""
    env = ops.execute("add_sketch", _doc().id, {})
    assert env["ok"] is False
    assert env["error"]["code"] == "MISSING_ARG"
    assert env["error"]["arg"] == "sketch"


def test_a_bad_feature_id_is_distinguishable_from_a_bad_document_id():
    doc = _doc()
    assert ops.execute("get_feature", doc.id, {"feature_id": "nope"})["error"]["code"] == "NO_SUCH_FEATURE"
    assert ops.execute("get_feature", "no-such-doc", {"feature_id": "x"})["error"]["code"] == "NO_SUCH_DOCUMENT"


def test_wrong_document_shape_is_a_precondition_not_a_bad_argument():
    """`flat_pattern` on a solid part is fixed by a prior step, not by other args."""
    doc = _doc()
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    assert ops.execute("flat_pattern", doc.id, {})["error"]["code"] == "PRECONDITION"


def test_an_out_of_catalog_operation_names_what_is_available():
    env = ops.execute("no_such_operation", _doc().id, {})
    assert env["ok"] is False
    assert env["error"]["code"] == "UNKNOWN_OP"
    assert "add_primitive" in env["error"]["message"]


def test_a_bad_enum_value_reports_the_argument_it_came_from():
    env = ops.execute("add_primitive", _doc().id, {"shape": "dodecahedron"})
    assert env["error"]["code"] == "BAD_ARG"
    assert env["error"]["arg"] == "shape"


def test_operation_errors_stay_catchable_as_value_errors():
    """Existing `except ValueError` handlers in the routers must keep working."""
    assert issubclass(op_errors.MissingArgument, ValueError)
    with pytest.raises(ValueError):
        ops.dispatch("flat_pattern", _doc().id, {})


# -------------------------------------------------------------------- delta --

def test_adding_a_feature_reports_the_new_id_without_a_second_call():
    doc = _doc()
    env = ops.execute("add_primitive", doc.id, {"shape": "box"})
    assert env["ok"] is True
    assert env["delta"]["features"]["added"] == [env["result"]["feature_id"]]


def test_a_subtraction_reports_volume_going_down():
    doc = _doc()
    ops.dispatch("add_primitive", doc.id, {"shape": "box",
                                           "params": {"size": {"x": .06, "y": .06, "z": .06}}})
    env = ops.execute("add_primitive", doc.id, {
        "shape": "cylinder", "operation": "subtract",
        "params": {"radius": 0.01, "height": 0.2}})
    assert env["delta"]["volume"]["change"] < 0
    assert env["delta"]["changed_geometry"] is True


def test_deleting_a_feature_reports_it_removed():
    doc = _doc()
    fid = ops.dispatch("add_primitive", doc.id, {"shape": "box"})["feature_id"]
    ops.dispatch("add_primitive", doc.id, {"shape": "sphere"})
    env = ops.execute("delete_feature", doc.id, {"feature_id": fid})
    assert fid in env["delta"]["features"]["removed"]


def test_a_query_reports_no_geometry_change():
    doc = _doc()
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    assert ops.execute("bounding_box", doc.id, {})["delta"]["changed_geometry"] is False


# -------------------------------------------------------------------- state --

def test_state_carries_features_with_their_current_params():
    doc = _doc()
    ops.dispatch("add_primitive", doc.id, {"shape": "box",
                                           "params": {"size": {"x": .08, "y": .04, "z": .01}}})
    box = next(f for f in ops.execute("bounding_box", doc.id, {})["state"]["features"]
               if f["type"] == "BoxFeature")
    # An agent asked to resize needs to see the real param structure, not guess it.
    assert box["params"]["size"] == {"x": .08, "y": .04, "z": .01}


def test_state_omits_the_mesh_and_the_children_array():
    doc = _doc()
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    state = ops.execute("list_features", doc.id, {})["state"]
    assert "mesh_url" not in state
    assert all("children" not in f["params"] for f in state["features"])


# ------------------------------------------- unevaluatable trees are warnings --

def _bind_to_undefined_parameter(doc):
    box = ops.dispatch("add_primitive", doc.id, {"shape": "box"})["feature_id"]
    return ops.execute("set_feature_params", doc.id,
                       {"feature_id": box, "params": {"size": {"x": "=w", "y": 0.02, "z": 0.02}}})


def test_referencing_an_undefined_parameter_is_a_warning_not_a_failure():
    """Binding a feature to a parameter before defining it is a supported workflow.

    The document model tolerates a transient unevaluatable state on purpose, so
    the operation succeeded — the geometry simply is not resolvable yet.
    """
    env = _bind_to_undefined_parameter(_doc())
    assert env["ok"] is True
    assert [w["code"] for w in env["warnings"]] == ["EVAL_FAILED"]
    assert env["state"]["error"]


def test_rollback_is_available_for_harnesses_that_want_an_always_valid_document():
    doc = _doc()
    box = ops.dispatch("add_primitive", doc.id, {"shape": "box"})["feature_id"]
    before = doc.feature_tree
    env = ops.execute("set_feature_params", doc.id,
                      {"feature_id": box,
                       "params": {"size": {"x": "=w", "y": 0.02, "z": 0.02}}},
                      rollback_on_eval_error=True)
    assert env["ok"] is False
    assert env["error"]["code"] == "EVAL_FAILED"
    assert docs.get_document(doc.id).feature_tree == before


# --------------------------------------------------------------- http surface --

def test_http_failures_carry_a_machine_readable_code():
    doc = _doc()
    r = client.post(f"/api/documents/{doc.id}/op",
                    json={"op": "get_feature", "args": {"feature_id": "nope"}})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NO_SUCH_FEATURE"


def test_a_missing_argument_is_a_422_over_http_not_a_404():
    doc = _doc()
    r = client.post(f"/api/documents/{doc.id}/op", json={"op": "add_sketch", "args": {}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "MISSING_ARG"
    assert detail["message"] == "'sketch' is required by add_sketch"
    assert detail["arg"] == "sketch"
    # And what to do about it, so the caller does not have to infer the next
    # move from the prose. See `op_errors.OperationError.remedy`.
    assert "required" in detail["remedy"]


def test_envelope_mode_returns_failures_as_200_for_rollout_harnesses():
    doc = _doc()
    r = client.post(f"/api/documents/{doc.id}/op?envelope=1",
                    json={"op": "get_feature", "args": {"feature_id": "nope"}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["error"]["code"] == "NO_SUCH_FEATURE"


def test_a_successful_http_call_carries_delta_and_state():
    doc = _doc()
    r = client.post(f"/api/documents/{doc.id}/op",
                    json={"op": "add_primitive", "args": {"shape": "box"}})
    body = r.json()
    assert body["ok"] is True
    assert body["delta"]["features"]["added"]
    assert body["state"]["features"]
