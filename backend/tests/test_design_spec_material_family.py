"""A material requirement names a grade or a family — a request says 'aluminium'."""

from app.services import cad_operations as ops
from app.services import document_service as docs

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731


def _plate(material="al-6061"):
    doc = docs.create_document(name="plate", owner_id="dev")
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": V(.05, .05, .01), "center": V(0, 0, 0)}})
    ops.dispatch("set_material", doc.id, {"material_id": material})
    return doc.id


def _score(doc_id, value):
    ops.dispatch("set_spec", doc_id, {"requirements": [{"kind": "material", "value": value}]})
    report = ops.dispatch("verify_against_spec", doc_id, {})
    return report["checks"][0]


def test_a_family_is_met_by_any_grade_of_it():
    """What a live run wrote, faithfully, from 'an aluminium plate'."""
    row = _score(_plate("al-6061"), "aluminum")
    assert row["status"] == "pass"


def test_the_british_spelling_names_the_same_family():
    assert _score(_plate("al-7075"), "aluminium")["status"] == "pass"


def test_a_family_is_not_met_by_another_family():
    doc = _plate("al-6061")
    assert _score(doc, "steel")["status"] == "fail"


def test_an_exact_grade_still_means_exactly_that_grade():
    doc = _plate("al-6061")
    assert _score(doc, "al-6061")["status"] == "pass"
    assert _score(doc, "al-7075")["status"] == "fail"


def test_a_value_that_is_neither_says_so():
    row = _score(_plate(), "unobtanium")
    assert row["status"] == "fail"
    assert "neither a material id nor a category" in row["detail"]
