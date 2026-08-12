"""Text-to-assembly: one prompt → generated parts placed as an AssemblyDoc.

Both halves are injected here. `planner` decides what parts there are and
`build_part` turns one into a document; by default both go through the LLM.
Everything *between* them — one document per distinct part, instances sharing
it, mm placements landing in metres — is ordinary code, and it is what these
tests are about. It used to need an API key to reach at all, because injecting
the planner still left the parts being built by text-to-CAD.
"""

from fastapi.testclient import TestClient

from main import app
from app.services import text_to_assembly

client = TestClient(app)


def _boxes(*, name, prompt, resolution, owner_id):
    """Stand-in part builder: a box, sized from the plan's own key."""
    from app.services import cad_operations as ops
    from app.services import document_service as docs

    size = {"Base": (0.06, 0.04, 0.01)}.get(name, (0.01, 0.01, 0.05))
    doc = docs.create_document(name=name, resolution=resolution, owner_id=owner_id)
    ops.execute("add_primitive", doc.id, {"shape": "box", "params": {
        "size": {"x": size[0], "y": size[1], "z": size[2]}}})
    return docs.get_document(doc.id)


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
        "a rig", planner=_scripted, build_part=_boxes, resolution=48
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


def test_without_a_model_it_refuses_rather_than_planning_something_unbuildable():
    """There used to be a one-part "fallback" that handed the prompt to the
    rule-based parser. That parser is gone, so the fallback would now plan a
    part nothing can build and fail deep inside the job instead of at the
    request. It raises the same typed condition text-to-CAD does."""
    import pytest

    from app.services.text_to_cad_engine import TextToCadUnavailable

    with pytest.raises(TextToCadUnavailable) as caught:
        text_to_assembly.create_assembly_from_prompt("a two-post rig", resolution=48)
    assert "insert_standard_part" in caught.value.remedy


def test_from_prompt_job_flow_resolves_and_carries_the_reason():
    """The job flow itself — accepted, running, resolved on one poll — plus the
    part that matters when it cannot succeed: a job that fails says *why* and
    what to do, rather than reporting an error nobody can act on."""
    r = client.post("/api/assemblies/from-prompt",
                    json={"prompt": "a two-post rig", "resolution": 48})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] == "running"

    # TestClient runs background tasks before returning — one poll resolves it.
    r = client.get(f"/api/assemblies/from-prompt/{job['job_id']}")
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "error"          # no model is configured here
    assert "language model" in done["error"]
    assert "insert_standard_part" in done["remedy"]


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
