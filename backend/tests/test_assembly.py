"""Tests for prompt-driven assembly generation."""

from fastapi.testclient import TestClient

from app.services.assembly_generator import parse_assembly_prompt, _LAYOUTS
from main import app


client = TestClient(app)


def test_parse_truss_spec():
    spec = parse_assembly_prompt("truss span 2 m 3 bays")
    assert spec.kind == "truss"
    assert spec.span == 2.0
    assert spec.bays == 3


def test_parse_bridge_spec():
    spec = parse_assembly_prompt("bridge span 3 m width 0.6 m")
    assert spec.kind == "bridge"
    assert spec.span == 3.0
    assert spec.width == 0.6


def test_parse_space_frame_spec():
    spec = parse_assembly_prompt("space frame 2x1 m height 0.3 m")
    assert spec.kind == "space_frame"


def test_parse_size_expression_sets_span_and_width():
    spec = parse_assembly_prompt("platform 1.2x0.6 m height 1.5 m 2 shelves")
    assert spec.kind == "platform"
    assert spec.span == 1.2
    assert spec.width == 0.6
    assert spec.height == 1.5


def test_parse_gantry_spec():
    spec = parse_assembly_prompt("gantry span 2 m height 1.2 m width 0.5 m")
    assert spec.kind == "gantry"
    assert spec.span == 2.0
    assert spec.height == 1.2
    assert spec.width == 0.5


def test_parse_crane_spec():
    spec = parse_assembly_prompt("crane height 2 m jib 1.5 m")
    assert spec.kind == "crane"
    assert spec.height == 2.0
    assert spec.span == 1.5


def test_parse_scissor_spec():
    spec = parse_assembly_prompt("scissor lift span 1.2 m height 0.6 m")
    assert spec.kind == "scissor"
    assert spec.span == 1.2
    assert spec.height == 0.6


def test_parse_arch_spec():
    spec = parse_assembly_prompt("arch span 2 m height 0.8 m")
    assert spec.kind == "arch"
    assert spec.span == 2.0
    assert spec.height == 0.8


def test_parse_cantilever_spec():
    spec = parse_assembly_prompt("cantilever span 1 m height 0.6 m")
    assert spec.kind == "cantilever"
    assert spec.span == 1.0
    assert spec.height == 0.6


def test_ambiguous_tall_prompt_becomes_tower():
    spec = parse_assembly_prompt("something 0.4x0.4 m height 2 m")
    assert spec.kind == "tower"


def test_generic_prompt_does_not_default_to_truss():
    spec = parse_assembly_prompt("make me a structure")
    assert spec.kind != "truss"


def test_truss_layout():
    spec = parse_assembly_prompt("truss span 2 m 2 bays")
    layout = _LAYOUTS[spec.kind](spec)
    assert len(layout.nodes) == 5
    assert len(layout.struts) > 0


def test_tower_layout():
    spec = parse_assembly_prompt("tower height 2 m 3 levels")
    layout = _LAYOUTS[spec.kind](spec)
    assert len(layout.nodes) == 16
    assert len(layout.struts) > 0


def test_gantry_layout():
    spec = parse_assembly_prompt("gantry span 2 m height 1 m width 0.5 m")
    layout = _LAYOUTS[spec.kind](spec)
    assert len(layout.nodes) > 4
    assert len(layout.struts) > 0


def test_crane_layout():
    spec = parse_assembly_prompt("crane height 1.5 m jib 1 m 2 bays")
    layout = _LAYOUTS[spec.kind](spec)
    assert len(layout.nodes) > 2
    assert len(layout.struts) > 0


def test_scissor_layout():
    spec = parse_assembly_prompt("scissor lift span 1 m height 0.5 m")
    layout = _LAYOUTS[spec.kind](spec)
    assert len(layout.nodes) > 2
    assert len(layout.struts) > 0


def test_platform_layout():
    spec = parse_assembly_prompt("platform 1x0.6 m height 1 m")
    layout = _LAYOUTS[spec.kind](spec)
    assert len(layout.nodes) > 4
    assert len(layout.struts) > 0


def test_arch_layout():
    spec = parse_assembly_prompt("arch span 2 m height 0.8 m")
    layout = _LAYOUTS[spec.kind](spec)
    assert len(layout.nodes) > 4
    assert len(layout.struts) > 0


def test_cantilever_layout():
    spec = parse_assembly_prompt("cantilever span 1 m height 0.6 m")
    layout = _LAYOUTS[spec.kind](spec)
    assert len(layout.nodes) > 2
    assert len(layout.struts) > 0


def test_assembly_generate_endpoint_includes_kind():
    resp = client.post("/api/assembly/generate", json={"prompt": "truss span 1.5 m 2 bays", "resolution": 64})
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["id"]
    assert result["kind"] == "truss"
    assert len(result["nodes"]) > 0
    assert len(result["struts"]) > 0
    assert all(n.get("candidate_id") for n in result["nodes"])
    assert all(n.get("mesh_url", "").startswith("data:") for n in result["nodes"])


def test_assembly_step_export():
    resp = client.post("/api/assembly/generate", json={"prompt": "truss span 1 m 2 bays", "resolution": 64})
    assert resp.status_code == 200
    assembly_id = resp.json()["id"]
    export = client.get(f"/api/export/assembly/{assembly_id}/step")
    assert export.status_code == 200, export.text
    assert "ISO-10303-21" in export.text
    assert "MANIFOLD_SOLID_BREP" in export.text
