"""Image-to-CAD: silhouette tracing, and the endpoint around it.

The vision-LLM path needs credentials, so these cover the deterministic tracer —
which is the path that runs for everyone.
"""

import io
import math

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from main import app
from app.cad_kernel.geom.meshing import mesh_volume
from app.cad_kernel.kernel import Kernel
from app.services import image_to_cad as itc


client = TestClient(app)


def _png(draw_fn, size=(400, 300), bg=255, mode="L") -> bytes:
    img = Image.new(mode, size, bg)
    draw_fn(ImageDraw.Draw(img))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _plate_png() -> bytes:
    """A 300x200 dark plate on a light sheet, with two round holes."""
    def draw(d):
        d.rectangle([50, 50, 349, 249], fill=0)
        d.ellipse([100, 120, 140, 160], fill=255)
        d.ellipse([260, 120, 300, 160], fill=255)
    return _png(draw)


# ------------------------------------------------------------------ masking --

def test_dark_subject_on_light_background():
    mask = itc._load_mask(_plate_png())
    assert mask.dtype == bool
    # 300x200 plate minus two 20px-radius holes.
    expected = 300 * 200 - 2 * math.pi * 20 ** 2
    assert mask.sum() == pytest.approx(expected, rel=0.05)


def test_light_subject_on_dark_background_is_not_inverted():
    """Corners decide which side is background, so contrast polarity is free."""
    data = _png(lambda d: d.rectangle([50, 50, 349, 249], fill=255), bg=0)
    mask = itc._load_mask(data)
    assert mask.sum() == pytest.approx(300 * 200, rel=0.05)


def test_alpha_channel_wins_over_colour():
    """A white glyph on transparency must trace, not vanish into the background."""
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([50, 50, 149, 149], fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    mask = itc._load_mask(buf.getvalue())
    assert mask.sum() == pytest.approx(100 * 100, rel=0.05)


def test_unreadable_data_is_a_clean_error():
    with pytest.raises(itc.ImageError):
        itc._load_mask(b"definitely not an image")


# ------------------------------------------------------------------ tracing --

def test_outer_contour_and_holes_are_separated():
    outer, holes = itc.trace_contours(itc._load_mask(_plate_png()))
    assert len(outer) == 4, "a rectangle should simplify to four corners"
    assert len(holes) == 2


def test_blank_image_has_nothing_to_trace():
    with pytest.raises(itc.ImageError):
        itc.trace_contours(itc._load_mask(_png(lambda d: None)))


def test_specks_are_ignored_but_real_holes_are_not():
    def draw(d):
        d.rectangle([50, 50, 349, 249], fill=0)
        d.ellipse([100, 120, 140, 160], fill=255)  # a real hole
        d.ellipse([300, 230, 302, 232], fill=255)  # a 2px speck
    _, holes = itc.trace_contours(itc._load_mask(_png(draw)))
    assert len(holes) == 1


def test_a_separate_blob_is_not_unioned_in():
    """One image makes one part; stray marks elsewhere are dropped, not merged."""
    def draw(d):
        d.rectangle([50, 50, 200, 249], fill=0)
        d.rectangle([300, 200, 340, 240], fill=0)  # unrelated mark
    outer, holes = itc.trace_contours(itc._load_mask(_png(draw)))
    assert holes == []
    assert outer[:, 0].max() < 260, "the outline should be the large blob only"


# ----------------------------------------------------------------- geometry --

def test_traced_solid_matches_the_drawn_plate():
    tree, source = itc.generate_from_image(_plate_png(), "plate.png", width_mm=150, depth_mm=5)
    assert source == "traced"

    solid = Kernel.evaluate_tree(tree, resolution=110)
    mesh = Kernel.tessellate(solid)
    volume = abs(mesh_volume(mesh.vertices, mesh.faces))

    # 150 x 100 x 5 mm plate, less two 20 mm-diameter holes.
    expected = (0.150 * 0.100 * 0.005) - 2 * (math.pi * 0.010 ** 2 * 0.005)
    assert volume == pytest.approx(expected, rel=0.05)


def test_requested_width_sets_the_scale_and_aspect_is_preserved():
    tree, _ = itc.generate_from_image(_plate_png(), "plate.png", width_mm=150, depth_mm=5)
    mesh = Kernel.tessellate(Kernel.evaluate_tree(tree, resolution=90))
    size = (mesh.vertices.max(axis=0) - mesh.vertices.min(axis=0)) * 1000.0
    assert size[0] == pytest.approx(150.0, abs=1.0)
    assert size[1] == pytest.approx(100.0, abs=2.0)   # 300:200 aspect
    assert size[2] == pytest.approx(5.0, abs=0.5)


def test_a_shape_with_no_holes_needs_no_boolean():
    data = _png(lambda d: d.rectangle([50, 50, 349, 249], fill=0))
    tree, _ = itc.generate_from_image(data, "plate.png")
    assert tree.root.to_dict()["type"] == "ExtrudeFeature"


def test_holes_produce_a_subtract():
    tree, _ = itc.generate_from_image(_plate_png(), "plate.png")
    root = tree.root
    assert root.to_dict()["type"] == "BooleanFeature"
    assert root.operation == "subtract"


def test_polygon_vertex_count_is_capped():
    """The SDF tests every edge per sample, so a photo must not become 20k edges."""
    import numpy as np

    dense = np.column_stack([np.arange(1000.0), np.arange(1000.0)])
    assert len(itc._decimate(dense)) <= itc._MAX_POLYGON_POINTS


@pytest.mark.parametrize("bad", [(0, 4), (100, 0), (-10, 4)])
def test_non_positive_sizes_are_rejected(bad):
    width, depth = bad
    with pytest.raises(itc.ImageError):
        itc.trace_to_tree(_plate_png(), width_m=width / 1000.0, depth_m=depth / 1000.0)


# ----------------------------------------------------------------- endpoint --

def test_endpoint_builds_an_editable_document():
    resp = client.post(
        "/api/documents/from-image",
        files={"file": ("plate.png", _plate_png(), "image/png")},
        data={"width_mm": "150", "depth_mm": "5", "name": "Traced plate"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-forge-source"] == "traced"

    doc = resp.json()
    assert doc["name"] == "Traced plate"
    assert "/mesh/" in doc["derived"]["mesh_url"]
    bbox = doc["derived"]["bbox"]
    assert (bbox["max"]["x"] - bbox["min"]["x"]) * 1000 == pytest.approx(150.0, abs=1.0)


def test_endpoint_names_the_document_after_the_file():
    resp = client.post(
        "/api/documents/from-image",
        files={"file": ("gasket-v2.png", _plate_png(), "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "gasket-v2"


def test_endpoint_rejects_a_non_image_with_a_useful_message():
    resp = client.post(
        "/api/documents/from-image",
        files={"file": ("x.png", b"not an image", "image/png")},
    )
    assert resp.status_code == 422
    assert "image" in resp.json()["detail"].lower()


def test_endpoint_rejects_an_empty_upload():
    resp = client.post(
        "/api/documents/from-image",
        files={"file": ("empty.png", b"", "image/png")},
    )
    assert resp.status_code == 422


def test_traced_document_is_editable_like_any_other():
    """The point of tracing over importing: the result is a real feature tree."""
    from app.services import cad_operations as ops

    doc = client.post(
        "/api/documents/from-image",
        files={"file": ("plate.png", _plate_png(), "image/png")},
    ).json()

    before = ops.dispatch("bounding_box", doc["id"], {})["size_mm"]["z"]
    ops.dispatch("add_primitive", doc["id"], {
        "shape": "cylinder", "operation": "union",
        "params": {"radius": 0.005, "height": 0.03, "center": {"x": 0, "y": 0, "z": 0}},
    })
    after = ops.dispatch("bounding_box", doc["id"], {})["size_mm"]["z"]
    assert after > before
