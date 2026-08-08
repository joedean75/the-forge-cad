"""Starter parts.

A template exists so the first edit is changing a number. Everything here is a
way that promise breaks:

* **The literal and the expression have to agree.** Each feature is built with a
  number and then rebound to an expression over the parameters. If the two
  disagree the body silently jumps the first time somebody touches a parameter,
  which is the worst moment for it to happen.
* **A parameter has to actually drive.** Doubling `length` has to double the
  part, and move the holes with it.
* **It has to be an ordinary tree.** No imported body, nothing the catalog could
  not have built — a template that reaches past the operation surface is content
  a model cannot reproduce.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from app.services import templates

client = TestClient(app)


def _new(name="Template test"):
    resp = client.post("/api/documents", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _op(doc_id, op, **args):
    resp = client.post(f"/api/documents/{doc_id}/op?envelope=1",
                       json={"op": op, "args": args})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _bbox_mm(doc_id):
    env = _op(doc_id, "bounding_box")
    assert env["ok"], env
    size = env["result"]["size_mm"]
    return (size["x"], size["y"], size["z"])


def _built(key):
    doc_id = _new(key)
    env = _op(doc_id, "apply_template", template=key)
    assert env["ok"], env
    return doc_id, env["result"]


ALL_KEYS = sorted(templates.TEMPLATES)


# ------------------------------------------------------------ every template --

@pytest.mark.parametrize("key", ALL_KEYS)
def test_every_template_builds_a_body(key):
    doc_id, result = _built(key)
    assert result["steps"] == len(templates.TEMPLATES[key].steps)
    x, y, z = _bbox_mm(doc_id)
    assert min(x, y, z) > 0.5, f"{key} evaluated to nothing: {(x, y, z)}"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_every_template_names_its_parameters(key):
    doc_id, _ = _built(key)
    env = _op(doc_id, "get_parameters")
    assert env["ok"], env
    named = {p["name"] for p in env["result"]["parameters"]}
    assert named == {p["name"] for p in templates.TEMPLATES[key].parameters}


@pytest.mark.parametrize("key", ALL_KEYS)
def test_the_literal_and_the_expression_agree(key):
    """Rebinding to expressions must not move the body.

    Each feature is built with a number and then bound to an expression over the
    parameters at their declared values. The two are written by hand in
    `templates.py`, so this is the check that they say the same thing: measure
    the part, re-set the parameters to exactly what they already are (which
    forces a re-resolve through the expressions), and measure again.
    """
    doc_id, _ = _built(key)
    before = _bbox_mm(doc_id)
    env = _op(doc_id, "set_parameters",
              parameters=templates.TEMPLATES[key].parameters)
    assert env["ok"], env
    after = _bbox_mm(doc_id)
    for a, b in zip(before, after):
        assert a == pytest.approx(b, abs=1e-6), f"{key}: {before} -> {after}"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_every_template_is_an_editable_tree(key):
    """No opaque bodies: an ImportFeature or a BrepBodyFeature has no parameters
    and nothing the decompiler can recover, so it would look like content while
    dragging the corpus health of every feature type beside it to zero."""
    doc_id, _ = _built(key)
    env = _op(doc_id, "list_features")
    assert env["ok"], env
    kinds = {f["type"] for f in env["result"]["features"]}
    assert not kinds & {"ImportFeature", "BrepBodyFeature", "OptimizeFeature"}


# ------------------------------------------------------- the parameters drive --

def test_lengthening_the_plate_lengthens_the_plate():
    doc_id, _ = _built("plate")
    x0, y0, z0 = _bbox_mm(doc_id)
    assert (round(x0), round(y0), round(z0)) == (100, 60, 8)

    params = [dict(p) for p in templates.TEMPLATES["plate"].parameters]
    for p in params:
        if p["name"] == "length":
            p["expr"] = "140"
    env = _op(doc_id, "set_parameters", parameters=params)
    assert env["ok"], env
    x1, y1, z1 = _bbox_mm(doc_id)
    assert round(x1) == 140 and round(y1) == 60 and round(z1) == 8


def test_the_plate_holes_move_with_the_plate():
    """A hole bound to a literal stays put while the plate around it grows, and
    ends up outside the material — the failure a template is supposed to prevent."""
    doc_id, _ = _built("plate")
    params = [dict(p) for p in templates.TEMPLATES["plate"].parameters]
    for p in params:
        if p["name"] == "length":
            p["expr"] = "180"
    assert _op(doc_id, "set_parameters", parameters=params)["ok"]

    # The *stored* tree keeps the expression — that is the point of a parameter —
    # so the positions are read off the resolved tree the evaluator sees.
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services import document_service

    resolved = document_service.resolved_tree(document_service.get_document(doc_id))
    holes = [n for n in walk_nodes(resolved) if "clearance" in (n.get("name") or "").lower()]
    assert len(holes) == 4
    xs = sorted({round(n["params"]["center"]["x"] * 1000, 3) for n in holes})
    # inset 10 mm from each end of a 180 mm plate
    assert xs == [-80.0, 80.0]


def test_the_enclosure_is_hollow():
    doc_id, _ = _built("enclosure")
    env = _op(doc_id, "mass_properties")
    assert env["ok"], env
    volume_mm3 = env["result"]["volume_cm3"] * 1000.0
    assert volume_mm3 < 0.5 * 120 * 80 * 40, "the cavity was not cut"
    assert volume_mm3 > 0.05 * 120 * 80 * 40, "there is nothing left"


def test_the_shaft_is_stepped():
    doc_id, _ = _built("shaft")
    x, y, z = _bbox_mm(doc_id)
    assert round(z) == 145                       # 120 shaft + 25 journal
    assert round(x) == 20 and round(y) == 20     # the journal is the smaller end


# ------------------------------------------------------------------ refusals --

def test_a_template_refuses_a_document_that_already_has_a_body():
    doc_id, _ = _built("plate")
    env = _op(doc_id, "apply_template", template="bracket")
    assert not env["ok"]
    assert env["error"]["code"] == "PRECONDITION"
    assert "starting point" in env["error"]["message"]


def test_an_unknown_template_is_named_and_the_real_ones_listed():
    doc_id = _new()
    env = _op(doc_id, "apply_template", template="spaceship")
    assert not env["ok"]
    assert env["error"]["code"] == "BAD_ARG"
    assert env["error"]["arg"] == "template"
    assert "plate" in env["error"]["message"]


def test_the_listing_is_reachable_and_says_what_you_can_change():
    doc_id = _new()
    env = _op(doc_id, "list_templates")
    assert env["ok"], env
    rows = {t["key"]: t for t in env["result"]["templates"]}
    assert set(rows) == set(ALL_KEYS)
    plate = rows["plate"]
    assert {p["name"] for p in plate["parameters"]} >= {"length", "width", "thickness"}
    assert all(p["unit"] == "mm" for p in plate["parameters"])
    assert plate["summary"]


# ------------------------------------------------ what a client reads as geometry --

def test_the_document_publishes_a_numeric_tree_beside_the_expressions():
    """`feature_tree` keeps `"=length"`; something has to carry the number.

    A client that reads the tree as geometry — snap points, selection outlines,
    a face offset for a sketch plane — cannot do arithmetic on an expression,
    and re-implementing the evaluator on the far side would be a second copy of
    it drifting from the one that produced the body on screen. The whole
    viewport crashed on the first template for exactly this reason.
    """
    doc_id, _ = _built("plate")
    doc = client.get(f"/api/documents/{doc_id}").json()

    from app.cad_kernel.core.feature_tree import walk_nodes

    stored = [n for n in walk_nodes(doc["feature_tree"])
              if n.get("type") == "BoxFeature"][0]
    assert stored["params"]["size"]["x"] == "=length"

    numeric = doc["derived"]["numeric_tree"]
    assert numeric is not None
    plate = [n for n in walk_nodes(numeric) if n.get("type") == "BoxFeature"][0]
    assert plate["params"]["size"]["x"] == pytest.approx(0.100)
    assert plate["params"]["center"]["z"] == pytest.approx(0.004)
    # Same nodes and same ids, so a selection made against one resolves in the other.
    assert plate["id"] == stored["id"]
    assert len(list(walk_nodes(numeric))) == len(list(walk_nodes(doc["feature_tree"])))


def test_a_document_without_parameters_carries_no_numeric_tree():
    """It would be a byte-for-byte copy of the tree in every response."""
    doc_id = _new()
    client.post(f"/api/documents/{doc_id}/op",
                json={"op": "add_primitive", "args": {"shape": "box"}})
    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["derived"]["numeric_tree"] is None
