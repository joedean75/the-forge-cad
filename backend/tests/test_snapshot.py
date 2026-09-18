"""Rendering the body so an agent can look at it.

Three questions the copilot's own tests cannot answer, because they stub the
provider rather than the geometry: does asking for a picture *change* the
document, is a picture as private as the thing it is a picture of, and does a
camera on the Z axis produce an image at all.
"""

import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import http_part
from main import app
from app.services import snapshot, thumbnail

client = TestClient(app)


def _signup(email: str) -> dict:
    r = client.post("/api/auth/signup", json={"email": email, "password": "supersecret1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_rendering_does_not_move_the_documents_mesh_hash():
    """A review re-tessellates at its own resolution, and must not publish it.

    The mesh hash is the document's identity for an immutable, cached-forever
    URL, and the review loop decides whether to render again by comparing it. If
    asking for a picture rewrote it, every render would invite another render.
    """
    doc_id = http_part(client, (0.06, 0.04, 0.008), resolution=64)
    before = client.get(f"/api/documents/{doc_id}").json()["derived"]["mesh_hash"]
    r = client.post(f"/api/documents/{doc_id}/op",
                    json={"op": "render_view", "args": {"cameras": ["iso", "top"]}})
    assert r.status_code == 200, r.text
    assert len(r.json()["result"]["views"]) == 2
    after = client.get(f"/api/documents/{doc_id}").json()["derived"]["mesh_hash"]
    assert after == before


def test_a_rendered_assembly_view_is_as_private_as_the_assembly():
    """The artifact route is new, and it is an access boundary before it is a file.

    404 rather than 403, like every other owner-scoped route here: existence is
    not leaked either.
    """
    alice, bob = _signup("alice-views@example.com"), _signup("bob-views@example.com")
    part = http_part(client, (0.05, 0.04, 0.006), resolution=32, headers=alice)
    asm_id = client.post("/api/assemblies", json={"name": "rig"}, headers=alice).json()["id"]
    placed = client.post(f"/api/assemblies/{asm_id}/op",
                         json={"op": "insert_part", "args": {"part_id": part}}, headers=alice)
    assert placed.status_code == 200, placed.text
    rendered = client.post(f"/api/assemblies/{asm_id}/op",
                           json={"op": "render_assembly", "args": {"cameras": ["iso"]}},
                           headers=alice)
    assert rendered.status_code == 200, rendered.text
    url = rendered.json()["result"]["views"][0]["url"]
    assert url.startswith(f"/api/assemblies/{asm_id}/artifact/")

    mine = client.get(url, headers=alice)
    assert mine.status_code == 200
    assert mine.headers["content-type"] == "image/png"
    assert "immutable" in mine.headers["cache-control"]
    assert mine.headers["cache-control"].startswith("private")
    assert client.get(url, headers=bob).status_code == 404
    assert client.get(f"/api/assemblies/{asm_id}/artifact/0123456789abcdef",
                      headers=alice).status_code == 404


@pytest.mark.parametrize("camera", sorted(thumbnail.CAMERAS))
def test_every_camera_renders_a_picture_rather_than_a_blank(camera):
    """A camera on the Z axis is the one that silently produces nothing.

    Looking straight down, `cross(world_up, forward)` is the zero vector and
    every pixel comes out NaN — an empty frame that raises no error, which is
    the one failure a review render must not have.
    """
    doc_id = http_part(client, (0.06, 0.04, 0.008), resolution=48)
    views = snapshot.packet("document", doc_id, cameras=[camera])
    assert len(views) == 1 and views[0]["camera"] == camera
    assert all(np.isfinite(views[0]["up"])), "a degenerate basis leaves NaN in the frame"

    pixels = np.asarray(Image.open(io.BytesIO(views[0]["png"])).convert("L"))
    # The part is drawn: more than one tone, and it occupies a real share of the
    # frame rather than a stray pixel.
    background = np.bincount(pixels.ravel()).argmax()
    drawn = (pixels != background).mean()
    assert len(np.unique(pixels)) > 1, "the frame is a single flat colour"
    assert drawn > 0.05, f"only {drawn:.1%} of the frame has the part in it"
