"""Material catalog: integrity, grouping, and the SI -> MPa/GPa solver adapter."""

import pytest
from fastapi.testclient import TestClient

from conftest import http_part
from main import app
from app.services import materials as M


client = TestClient(app)


def test_every_material_has_a_declared_category_and_tier():
    known = {c["id"] for c in M.CATEGORIES}
    for m in M.list_materials():
        assert m.category in known, f"{m.id} has category '{m.category}' not in CATEGORIES"
        assert m.tier in M.TIERS, f"{m.id} has tier '{m.tier}'"


def test_catalog_covers_both_audiences():
    tiers = {m.tier for m in M.list_materials()}
    assert tiers == set(M.TIERS), "catalog must serve makers and professionals"


def test_ids_are_unique_and_properties_are_physical():
    mats = M.list_materials()
    assert len({m.id for m in mats}) == len(mats)
    for m in mats:
        assert m.density > 0
        assert m.youngs_modulus > 0
        assert m.yield_strength > 0
        assert 0 < m.poisson_ratio < 0.5, f"{m.id} has a non-physical Poisson ratio"
        assert m.cost_per_kg > 0


def test_categories_are_ordered_nonempty_and_counted():
    cats = M.list_categories()
    assert cats, "expected at least one populated category"
    order = [c["id"] for c in M.CATEGORIES]
    assert [c["id"] for c in cats] == [c for c in order if c in {x["id"] for x in cats}]
    for c in cats:
        assert c["count"] == sum(1 for m in M.list_materials() if m.category == c["id"])
        assert c["count"] > 0


def test_ids_predating_the_category_split_still_resolve():
    """Material ids are persisted on documents, so these must never be renamed."""
    for legacy_id in ("al-6061", "al-7075", "steel-1018", "steel-304", "ti-6al4v",
                      "alsi10mg", "brass-360", "pla", "abs", "petg", "nylon-pa12", "resin-std"):
        assert M.get_material(legacy_id).id == legacy_id


def test_unknown_material_falls_back_to_default():
    assert M.get_material("no-such-material").id == M.DEFAULT_MATERIAL_ID
    assert M.get_material(None).id == M.DEFAULT_MATERIAL_ID


@pytest.mark.parametrize(
    "material_id,yield_mpa,modulus_gpa",
    [("al-6061", 276.0, 68.9), ("al-7075", 503.0, 71.7), ("ti-6al4v", 880.0, 113.8)],
)
def test_spec_adapter_converts_si_to_the_solver_units(material_id, yield_mpa, modulus_gpa):
    """The solver schema is MPa/GPa while the catalog is SI — conversion must hold.

    These are the values the retired legacy table carried, so a mismatch here
    would silently move every safety factor.
    """
    spec = M.spec_material_for(material_id)
    assert spec.yield_strength == pytest.approx(yield_mpa)
    assert spec.youngs_modulus == pytest.approx(modulus_gpa)
    assert spec.density == M.get_material(material_id).density


def test_materials_endpoint_returns_materials_and_categories():
    resp = client.get("/api/documents/meta/materials")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["materials"]) == len(M.list_materials())
    assert [c["id"] for c in body["categories"]] == [c["id"] for c in M.list_categories()]
    sample = body["materials"][0]
    for field in ("id", "name", "category", "tier", "density", "process", "notes"):
        assert field in sample


def test_a_document_can_be_bound_to_a_newly_added_grade():
    doc_id = http_part(client, (0.04, 0.04, 0.04), resolution=32)
    resp = client.post(f"/api/documents/{doc_id}/material", json={"material_id": "steel-4140"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["material_id"] == "steel-4140"
