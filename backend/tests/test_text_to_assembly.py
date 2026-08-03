"""Text-to-assembly: one prompt → generated parts placed as an AssemblyDoc."""

from fastapi.testclient import TestClient

from main import app
from app.services import text_to_assembly

client = TestClient(app)


def _scripted(prompt: str) -> dict:
    """Deterministic stand-in for the LLM planner: a base and two posts."""
    return {
        "name": "Two-post rig",
        "parts": [
            {"key": "base", "name": "Base", "prompt": "box 0.06x0.04x0.01 m"},
            {"key": "post", "name": "Post", "prompt": "box 0.01x0.01x0.05 m"},
        ],
        "components": [
            {"part": "base"},
            {"part": "post", "translate_mm": {"x": -20, "y": 0, "z": 30}},
            {"part": "post", "translate_mm": {"x": 20, "y": 0, "z": 30}},
        ],
    }


def test_scripted_plan_builds_parts_and_placements():
    asm = text_to_assembly.create_assembly_from_prompt(
        "a rig", planner=_scripted, resolution=48
    )
    assert asm.name == "Two-post rig"
    assert len(asm.components) == 3
    # One part document per distinct part; instances share it.
    part_ids = {c.part_id for c in asm.components}
    assert len(part_ids) == 2
    posts = [c for c in asm.components if c.name == "Post"]
    assert len(posts) == 2
    # Placements arrive in mm and land in metres.
    assert abs(posts[0].transform.translate.x - (-0.02)) < 1e-9
    assert abs(posts[1].transform.translate.x - 0.02) < 1e-9
    # Components resolve real geometry from their parts.
    assert all(c.mesh_url and "/mesh/" in c.mesh_url for c in asm.components)


def test_credential_free_fallback_is_single_part():
    # conftest disables the LLM: the fallback wraps the rule-parsed part.
    asm = text_to_assembly.create_assembly_from_prompt("box 0.05x0.05x0.05 m", resolution=48)
    assert len(asm.components) == 1
    assert asm.components[0].mesh_url


def test_from_prompt_job_flow_over_http():
    r = client.post("/api/assemblies/from-prompt", json={"prompt": "box 0.05x0.05x0.05 m", "resolution": 48})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] == "running"
    # TestClient runs background tasks before returning — one poll resolves it.
    r = client.get(f"/api/assemblies/from-prompt/{job['job_id']}")
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "done" and done["assembly_id"]
    a = client.get(f"/api/assemblies/{done['assembly_id']}")
    assert a.status_code == 200
    assert len(a.json()["components"]) == 1


def test_unknown_job_is_404():
    assert client.get("/api/assemblies/from-prompt/nope").status_code == 404


def test_bad_plan_is_an_error_job():
    def broken(prompt: str) -> dict:
        return {"name": "x", "parts": [], "components": []}

    try:
        text_to_assembly.create_assembly_from_prompt("x", planner=broken)
        raise AssertionError("expected ValueError for an empty plan")
    except ValueError:
        pass
