"""Tests for the prompt-driven geometry parser and endpoint."""

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
        "/api/text-to-cad/generate",
        json={"prompt": "box 100x50x30 mm with a 10 mm hole through center", "resolution": 64},
    )
    assert resp.status_code == 200, resp.text
    cand = resp.json()
    assert cand["name"].startswith("box 100x50x30 mm")
    assert cand["mesh_url"].startswith("data:")
    assert cand["cad"] is not None
    assert cand["cad"]["feature_tree"]["root"]["type"] == "BooleanFeature"


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


def test_cad_reevaluate_endpoint():
    resp = client.post("/api/text-to-cad/generate", json={"prompt": "box 100x50x30 mm", "resolution": 64})
    assert resp.status_code == 200
    cand = resp.json()
    cid = cand["id"]
    original_mass = cand["mass"]

    ft = cand["cad"]["feature_tree"]
    ft["root"]["params"]["size"]["z"] *= 2.0

    resp2 = client.post(f"/api/cad/{cid}/reevaluate", json={"feature_tree": ft, "resolution": 64})
    assert resp2.status_code == 200, resp2.text
    updated = resp2.json()
    assert updated["id"] == cid
    assert updated["mass"] > original_mass * 1.5


def test_chassis_prompt():
    tree = parse_geometry_prompt("a rally style rc chassis on a 185x185x185 3d printer build plate")
    root = tree.root
    assert root.to_dict()["type"] == "BooleanFeature"
    assert root.operation == "subtract"
    base = root.children[0]
    assert base.to_dict()["type"] == "BoxFeature"
    # Plate should fill ~90 % of the build footprint and be ~5 % of the build height.
    assert abs(base.size.x - 0.1665) < 1e-6
    assert abs(base.size.y - 0.1665) < 1e-6
    assert abs(base.size.z - 0.00925) < 1e-6
    # Should have wheel cuts plus mounting holes.
    assert len(root.children) > 1


def test_chassis_prompt_endpoint():
    resp = client.post(
        "/api/text-to-cad/generate",
        json={"prompt": "a rally style rc chassis on a 185x185x185 3d printer build plate", "resolution": 64},
    )
    assert resp.status_code == 200, resp.text
    cand = resp.json()
    assert "chassis" in cand["name"].lower()
    assert cand["mesh_url"].startswith("data:")
    assert cand["cad"] is not None


def test_default_unit_inferred_for_printer_context():
    tree = parse_geometry_prompt("box 100x50x30 3d printer")
    assert tree.root.size.x == 0.1
    assert tree.root.size.y == 0.05
    assert tree.root.size.z == 0.03
