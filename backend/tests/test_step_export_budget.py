"""STEP export must stream and must refuse to build an unbounded document.

A faceted STEP carries ~20 entity lines (~1.3 kB) per triangle, so a part meshed
at high resolution runs to hundreds of megabytes. Building that as one string
was enough to OOM a small instance, so the size is budgeted up front.

The budget only governs the *faceted* writer. A body with an exact B-rep form
exports as analytic surfaces — a handful of entities however curved it is — and
has no triangles to count, so these tests deliberately use a shelled body, which
`abs(d) - t/2` puts outside what OCC can represent.
"""

from fastapi.testclient import TestClient

from main import app
from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.io.step import stream_step, triangle_count, write_step
from app.cad_kernel.kernel import Kernel
from app.cad_kernel.ops.primitives import BoxFeature
from app.models.schemas import Vector3
from app.services import document_analysis


client = TestClient(app)


def _faceted_doc(resolution=32):
    """A document with no exact form, so the faceted writer is the one under test."""
    doc = client.post("/api/documents", json={"resolution": resolution}).json()
    client.post(f"/api/documents/{doc['id']}/op",
                json={"op": "add_primitive", "args": {"shape": "box"}})
    client.post(f"/api/documents/{doc['id']}/op",
                json={"op": "add_modifier",
                      "args": {"modifier": "shell", "params": {"thickness": 0.004}}})
    return doc


def _solid(resolution=32):
    tree = FeatureTree(BoxFeature(size=Vector3(x=0.1, y=0.05, z=0.03)))
    return Kernel.evaluate_tree(tree, resolution=resolution)


def test_streamed_output_matches_the_buffered_writer():
    solid = _solid()
    buffered = write_step(solid, "x").splitlines()
    streamed = "".join(stream_step(solid, "x")).splitlines()

    assert len(buffered) == len(streamed)
    # FILE_NAME embeds a generation timestamp, so it legitimately differs.
    differing = [i for i, (a, b) in enumerate(zip(buffered, streamed)) if a != b]
    assert all(buffered[i].startswith("FILE_NAME(") for i in differing)


def test_stream_is_chunked_rather_than_one_blob():
    chunks = list(stream_step(_solid(), "x"))
    assert len(chunks) > 2, "expected a header, batched entities, and a footer"


def test_export_is_refused_above_the_triangle_budget(monkeypatch):
    monkeypatch.setenv("FORGE_STEP_MAX_TRIANGLES", "10")
    doc = _faceted_doc()

    resp = client.get(f"/api/documents/{doc['id']}/step")
    assert resp.status_code == 413
    detail = resp.json()["detail"]
    assert "triangles" in detail and "resolution" in detail


def test_export_succeeds_within_budget(monkeypatch):
    monkeypatch.setenv("FORGE_STEP_MAX_TRIANGLES", "1000000")
    doc = _faceted_doc()

    resp = client.get(f"/api/documents/{doc['id']}/step")
    assert resp.status_code == 200
    assert resp.text.startswith("ISO-10303-21;")
    assert resp.text.rstrip().endswith("END-ISO-10303-21;")
    assert int(resp.headers["x-forge-triangles"]) > 0


def test_budget_is_checked_before_building_entities(monkeypatch):
    """The guard must cost a meshing pass, not a full string build."""
    monkeypatch.setenv("FORGE_STEP_MAX_TRIANGLES", "10")
    doc = _faceted_doc()

    called = False
    original = document_analysis.stream_step_document

    def _spy(*args, **kwargs):
        nonlocal called
        called = True
        return original(*args, **kwargs)

    monkeypatch.setattr(document_analysis, "stream_step_document", _spy)
    resp = client.get(f"/api/documents/{doc['id']}/step")
    assert called and resp.status_code == 413


def test_triangle_count_matches_the_mesh():
    solid = _solid()
    assert triangle_count(solid) == len(Kernel.tessellate(solid).faces)
