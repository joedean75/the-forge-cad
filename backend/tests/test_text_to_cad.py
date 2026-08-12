"""Text-to-CAD needs a model, and says so.

The rule-based parser is gone. It read dimensions out of a prompt and emitted a
primitive, so a prompt describing a real part came back as a box with roughly
the right bounding volume and none of the described features — indistinguishable
from success by looking at it, and recorded with `source_prompt` so the training
corpus filled with (prompt -> unrelated box) pairs.

What has to hold now: the refusal is *typed* and carries somewhere to go, and it
is the only thing that changed — a blank document, a template and an import all
still work with no credentials anywhere.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from app.services import llm_client
from app.services.text_to_cad_engine import TextToCadUnavailable, generate_part_tree

client = TestClient(app)

pytestmark = pytest.mark.skipif(
    llm_client.is_configured(), reason="a key is configured; this covers the unconfigured path")


def test_generation_without_a_model_raises_rather_than_approximating():
    with pytest.raises(TextToCadUnavailable) as caught:
        generate_part_tree("a 100x50x30 mm mounting plate with four M4 holes")
    assert "language model" in str(caught.value)
    assert "template" in caught.value.remedy


def test_the_http_surface_reports_it_as_a_precondition_with_a_remedy():
    r = client.post("/api/documents", json={"prompt": "a 100x50x30 mm plate"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "PRECONDITION"
    assert detail["remedy"]


def test_no_document_is_created_by_a_refused_prompt():
    """The failure mode to avoid is a half-made document: a blank part named
    after a prompt it never honoured, which looks like the feature working."""
    before = len(client.get("/api/documents").json())
    client.post("/api/documents", json={"prompt": "a gearbox housing"})
    assert len(client.get("/api/documents").json()) == before


def test_an_ai_edit_without_a_model_fails_the_edit_and_keeps_the_part():
    from app.services import document_service as docs
    from app.services.text_to_cad_engine import edit_part_tree

    doc = client.post("/api/documents", json={"name": "keepme"}).json()
    client.post(f"/api/documents/{doc['id']}/op",
                json={"op": "add_primitive", "args": {"shape": "box"}})
    before = docs.get_document(doc["id"]).feature_tree

    # An edit returns None rather than raising: the part is unchanged, which is
    # a normal outcome. Generation has nothing to fall back *to*, so it raises.
    assert edit_part_tree(before, "make it twice as tall") is None
    assert docs.get_document(doc["id"]).feature_tree == before


def test_the_credential_free_path_to_a_part_still_works():
    """Removing the parser is only defensible because this exists."""
    doc = client.post("/api/documents", json={"name": "template route"}).json()
    listed = client.post(f"/api/documents/{doc['id']}/op?envelope=1",
                         json={"op": "list_templates", "args": {}}).json()
    assert listed["ok"] and listed["result"]["templates"]

    built = client.post(f"/api/documents/{doc['id']}/op?envelope=1",
                        json={"op": "apply_template", "args": {"template": "plate"}}).json()
    assert built["ok"], built
    box = client.post(f"/api/documents/{doc['id']}/op?envelope=1",
                      json={"op": "bounding_box", "args": {}}).json()
    assert box["result"]["size_mm"]["x"] == pytest.approx(100.0, abs=1.0)
