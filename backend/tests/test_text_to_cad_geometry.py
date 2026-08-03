"""Tests for the prompt-driven geometry parser and endpoint."""

import pytest
from fastapi.testclient import TestClient

from app.services.text_to_cad_geometry import parse_geometry_prompt
from app.cad_kernel.kernel import Kernel
from app.cad_kernel.geom.meshing import mesh_volume
from main import app


client = TestClient(app)


def _volume(prompt: str, resolution: int = 64) -> float:
    tree = parse_geometry_prompt(prompt)
    solid = Kernel.evaluate_tree(tree, resolution=resolution)
    mesh = Kernel.tessellate(solid)
    return abs(mesh_volume(mesh.vertices, mesh.faces))


def test_box_prompt():
    tree = parse_geometry_prompt("box 100x50x30 mm")
    assert tree.root.to_dict()["type"] == "BoxFeature"
    assert tree.root.size.x == 0.1
    assert tree.root.size.y == 0.05
    assert tree.root.size.z == 0.03


def test_box_with_bosses():
    tree = parse_geometry_prompt("box 100x50x30 mm with two mounting bosses on top")
    root = tree.root
    assert root.to_dict()["type"] == "BooleanFeature"
    assert root.operation == "union"
    # Second child should be a group of two bosses.
    group = root.children[1]
    assert group.to_dict()["type"] == "BooleanFeature"
    assert len(group.children) == 2


def test_box_with_hole():
    vol_solid = _volume("box 100x50x30 mm")
    vol_hole = _volume("box 100x50x30 mm with a 10 mm hole through center")
    # Removing material should reduce volume.
    assert vol_hole < vol_solid


def test_text_to_cad_generate_endpoint():
    resp = client.post(
        "/api/documents",
        json={"prompt": "box 100x50x30 mm with a 10 mm hole through center", "resolution": 64},
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["provenance"] == "text_to_cad"
    assert "/mesh/" in doc["derived"]["mesh_url"]
    assert doc["feature_tree"]["root"]["type"] == "BooleanFeature"


def test_cube_prompt():
    tree = parse_geometry_prompt("cube 50 mm")
    root = tree.root
    assert root.to_dict()["type"] == "BoxFeature"
    assert root.size.x == 0.05
    assert root.size.y == 0.05
    assert root.size.z == 0.05


def test_pipe_prompt():
    tree = parse_geometry_prompt("pipe radius 20 mm wall 3 mm height 100 mm")
    root = tree.root
    assert root.to_dict()["type"] == "BooleanFeature"
    assert root.operation == "subtract"
    outer = root.children[0]
    inner = root.children[1]
    assert outer.radius == 0.02
    assert inner.radius == 0.017


def test_document_reevaluates_after_feature_tree_edit():
    """Editing the tree re-evaluates the solid: doubling height grows the mass."""
    resp = client.post("/api/documents", json={"prompt": "box 100x50x30 mm", "resolution": 64})
    assert resp.status_code == 200
    doc = resp.json()
    doc_id = doc["id"]
    original_mass = doc["derived"]["mass"]

    ft = doc["feature_tree"]
    ft["root"]["params"]["size"]["z"] *= 2.0

    resp2 = client.put(f"/api/documents/{doc_id}", json={"feature_tree": ft, "resolution": 64})
    assert resp2.status_code == 200, resp2.text
    updated = resp2.json()
    assert updated["id"] == doc_id
    assert updated["derived"]["mass"] > original_mass * 1.5


def test_dimensions_drive_the_geometry_not_the_subject_words():
    """The parser reads numbers, not nouns.

    A canned "RC chassis" shape used to short-circuit this prompt and return a
    fixed plate with wheel cutouts, ignoring the dimensions asked for. Whatever
    the subject is called, the size in the prompt is what must come back.
    """
    tree = parse_geometry_prompt("a rally style rc chassis 185x120x8 mm")
    root = tree.root
    assert root.to_dict()["type"] == "BoxFeature"
    assert root.size.x == pytest.approx(0.185)
    assert root.size.y == pytest.approx(0.120)
    assert root.size.z == pytest.approx(0.008)


@pytest.mark.parametrize("prompt", [
    "a circle plate 50x50x5 mm",
    "an arch bracket 60x40x10 mm",
    "a torch mount 30x20x5 mm",
    "a force sensor plate 80x60x4 mm",
])
def test_words_containing_rc_are_not_hijacked(prompt):
    """`"rc" in prompt` once matched circle, arch, torch, force… and every one
    of those returned an RC chassis instead of the part described."""
    tree = parse_geometry_prompt(prompt)
    assert tree.root.to_dict()["type"] == "BoxFeature"


def test_subject_prompt_still_builds_a_document():
    resp = client.post(
        "/api/documents",
        json={"prompt": "a rally style rc chassis on a 185x185x185 3d printer build plate", "resolution": 64},
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert "/mesh/" in doc["derived"]["mesh_url"]
    assert doc["feature_tree"]["root"] is not None


def test_default_unit_inferred_for_printer_context():
    tree = parse_geometry_prompt("box 100x50x30 3d printer")
    assert tree.root.size.x == 0.1
    assert tree.root.size.y == 0.05
    assert tree.root.size.z == 0.03
