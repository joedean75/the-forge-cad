"""Facts a part's geometry cannot carry are recorded once and seen everywhere.

The copilot asked the user for a motor's KV and turns, got an answer, and asked
again next run because there was nowhere to put it. These pin the place: on
the part document, with a source, visible in every assembly placing the part
and in the interface reading — and not a place to type in dimensions, which are
measured.
"""

import pytest

from app.models.schemas import ComponentTransform, Vector3  # noqa: F401
from app.services import assembly_service, cad_operations, document_service, observation


def _part():
    doc = document_service.create_document(name="A2212 motor")
    env = cad_operations.execute("add_primitive", doc.id, {
        "shape": "cylinder", "params": {"radius": 0.014, "height": 0.03}})
    assert env["ok"], env.get("error")
    return document_service.get_document(doc.id)


def _record(doc_id, facts, **extra):
    return cad_operations.execute("record_part_facts", doc_id,
                                  {"facts": facts, **extra}, include_state=False)


def test_a_recorded_fact_persists_with_its_source():
    doc = _part()
    env = _record(doc.id, {"kv": {"value": 1400, "unit": "rpm/V"}, "turns": 10},
                  source="datasheet")
    assert env["ok"], env.get("error")

    stored = document_service.get_document(doc.id).part_facts
    assert stored["kv"]["value"] == 1400 and stored["kv"]["unit"] == "rpm/V"
    assert stored["turns"]["source"] == "datasheet" and stored["turns"]["recorded_at"]


def test_null_removes_a_fact_and_other_facts_stay():
    doc = _part()
    _record(doc.id, {"kv": 1400, "part_number": "A2212/10T"})
    env = _record(doc.id, {"kv": None})
    assert env["ok"]
    assert set(document_service.get_document(doc.id).part_facts) == {"part_number"}


@pytest.mark.parametrize("facts, fragment", [
    ({"shaft_diameter": 3.17}, "describes a dimension"),
    ({"bolt_circle_mm": 16}, "describes a dimension"),
    ({"KV Rating!": 1400}, "lower_snake_case"),
    ({"kv": ""}, "empty value"),
    ({"kv": [1400]}, "a string, a number or a boolean"),
    ({"kv": {"value": 1400, "colour": "red"}}, "unknown keys"),
])
def test_bad_facts_are_refused_and_nothing_is_written(facts, fragment):
    doc = _part()
    _record(doc.id, {"turns": 10})
    env = _record(doc.id, {"kv": 1400, **facts})
    assert not env["ok"] and env["error"]["code"] == "BAD_ARG"
    assert fragment in env["error"]["message"]
    assert set(document_service.get_document(doc.id).part_facts) == {"turns"}


def test_an_unknown_source_is_refused_by_the_schema():
    env = _record(_part().id, {"kv": 1400}, source="internet")
    assert not env["ok"]


def test_facts_recorded_through_an_assembly_land_on_the_part_and_show_in_its_state():
    part = _part()
    asm = assembly_service.create_assembly("truck")
    asm = assembly_service.add_component(asm.id, part.id)
    asm = assembly_service.add_component(asm.id, part.id)       # two instances
    first = asm.components[0]

    env = cad_operations.execute("record_component_facts", asm.id, {
        "component_id": first.id, "facts": {"kv": 1400, "turns": 10}}, include_state=False)
    assert env["ok"], env.get("error")
    assert document_service.get_document(part.id).part_facts["kv"]["value"] == 1400

    state = observation.summarize_assembly(assembly_service.get_assembly(asm.id))
    assert all(c["facts"] == {"kv": 1400, "turns": 10} for c in state["components"])


def test_an_assembly_cannot_write_facts_onto_someone_else_s_part():
    part = _part()
    asm = assembly_service.create_assembly("truck")
    asm = assembly_service.add_component(asm.id, part.id)
    # The part now belongs to another account — as a part shared into this
    # assembly does. Reading it is allowed; writing to it through here is not.
    owned = document_service.get_document(part.id)
    owned.owner_id = "someone-else"
    document_service.persist_document(owned)
    env = cad_operations.execute("record_component_facts", asm.id, {
        "component_id": asm.components[0].id, "facts": {"kv": 1400}}, include_state=False)
    assert not env["ok"] and env["error"]["code"] == "PRECONDITION"
    assert not document_service.get_document(part.id).part_facts


def test_a_recorded_fact_stops_being_listed_as_unknown():
    from app.services import interfaces

    doc = _part()
    _record(doc.id, {"kv": 1400})
    reading = interfaces.with_facts(
        {"not_in_geometry": ["KV rating", "winding / turns"]},
        document_service.get_document(doc.id))
    assert reading["recorded_facts"] == {"kv": 1400}
    assert reading["not_in_geometry"] == ["winding / turns"]
