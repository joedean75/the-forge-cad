"""Driving parameters, and what happens when a binding runs ahead of them."""

import json

from app.services import cad_operations, document_service


def test_a_binding_to_an_undefined_parameter_names_it():
    """Binding before defining is a supported intermediate state, so this is a
    warning rather than a refusal — but the expression string used to reach the
    kernel, which came back as `unsupported operand type(s) for /: 'str' and
    'float'`: a leaked Python error naming nothing. A fine-tuned model did
    exactly this in an eval run (it bound size to "=length" after its
    set_parameters call had been refused), and the message told nobody why."""
    doc = document_service.create_document(name="bound too early")
    built = cad_operations.execute("add_primitive", doc.id, {
        "shape": "box", "params": {"size": {"x": 0.05, "y": 0.05, "z": 0.01}}})
    assert built["ok"], built.get("error")
    feature = built["result"]["feature_id"]

    env = cad_operations.execute("set_feature_params", doc.id, {
        "feature_id": feature,
        "params": {"size": {"x": "=length", "y": 0.05, "z": 0.01}}})
    reported = json.dumps(env.get("warnings") or []) + json.dumps(env.get("state") or {})
    assert "length" in reported, reported
    assert "unsupported operand" not in reported, "the Python error must not leak"


def test_defining_the_parameter_afterwards_resolves_the_binding():
    """The other half of the supported workflow: the warning clears once the
    parameter exists, and the body measures what the expression says."""
    doc = document_service.create_document(name="bound then defined")
    built = cad_operations.execute("add_primitive", doc.id, {
        "shape": "box", "params": {"size": {"x": 0.05, "y": 0.05, "z": 0.01}}})
    feature = built["result"]["feature_id"]
    cad_operations.execute("set_feature_params", doc.id, {
        # A length parameter is declared in mm and resolves to metres, so the
        # binding is the bare name — the module docstring's own example.
        "feature_id": feature, "params": {"size": {"x": "=length", "y": 0.05, "z": 0.01}}})

    env = cad_operations.execute("set_parameters", doc.id, {
        "parameters": [{"name": "length", "expr": "80"}]})
    assert env["ok"], env.get("error")
    size = cad_operations.execute("bounding_box", doc.id, {},
                                  include_state=False)["result"]["size_mm"]
    assert abs(size["x"] - 80.0) < 0.5, size
