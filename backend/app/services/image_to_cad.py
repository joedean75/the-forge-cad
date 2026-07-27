"""Generate CAD geometry from an image.

Two engines, same shape as text-to-CAD: an optional vision LLM produces a full
feature tree when one is configured, and a deterministic **silhouette tracer**
handles everything credential-free.

The tracer is not a fallback in the apologetic sense — it is the right tool for
the common case. A logo, a gasket, a faceplate, a bracket photographed against a
contrasting background: all of those are a 2D outline that wants extruding, and
tracing them is exact and instant where a language model would only guess. It
recovers holes too, so a flange traced from a photo comes out with its bolt
circle rather than as a blank disc.

What it deliberately does not attempt is 3D reconstruction from a single view.
An image carries no depth, so the extrusion thickness is a parameter rather than
something inferred.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.ops.booleans import BooleanFeature
from app.cad_kernel.ops.sketch import ExtrudeFeature
from app.services import llm_client as llm

_LOGGER = logging.getLogger(__name__)

# Longest in-plane dimension of the traced part, in metres, when the caller does
# not say. 100 mm is a plate you can hold.
DEFAULT_WIDTH_M = 0.10
DEFAULT_DEPTH_M = 0.004

# Contour simplification, in pixels. Tight enough to keep lettering legible,
# loose enough that a 2000-px photo doesn't become a 20k-vertex polygon.
_SIMPLIFY_PX = 1.5
# Contours enclosing less than this share of the outline's area are noise.
_MIN_AREA_FRACTION = 0.002
_MAX_POLYGON_POINTS = 400

SUPPORTED_FORMATS = ("png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff")


class ImageError(ValueError):
    """The image could not be read, or carries no traceable shape."""


def _load_mask(data: bytes) -> np.ndarray:
    """Decode `data` into a boolean foreground mask.

    Transparency wins when present — a PNG logo's alpha channel *is* the
    silhouette, and thresholding its colours instead would drop white glyphs.
    Otherwise the image is thresholded against its own mid-range, with the
    corners deciding which side is background so a dark part on a light sheet
    and a light part on a dark sheet both come out the same way.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ImageError("image support is unavailable on this server") from exc

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise ImageError("could not read that image") from exc

    # Downscale very large images: tracing accuracy is set by the simplification
    # tolerance, not the pixel count, and a 6000-px photo is pure cost.
    if max(img.size) > 1600:
        img.thumbnail((1600, 1600))

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        alpha = np.asarray(img.convert("RGBA"))[:, :, 3]
        if alpha.min() < 250:  # transparency actually carries information
            return alpha > 128

    grey = np.asarray(img.convert("L"), dtype=float)
    if grey.size == 0:
        raise ImageError("that image is empty")

    try:
        from skimage.filters import threshold_otsu

        level = float(threshold_otsu(grey))
    except Exception:
        level = float(grey.mean())

    mask = grey < level
    # Corners are almost always background; if they read as foreground, invert.
    h, w = mask.shape
    corners = [mask[0, 0], mask[0, w - 1], mask[h - 1, 0], mask[h - 1, w - 1]]
    if sum(bool(c) for c in corners) >= 3:
        mask = ~mask
    return mask


def _polygon_area(points: np.ndarray) -> float:
    x, y = points[:, 0], points[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _point_in_polygon(point: np.ndarray, polygon: np.ndarray) -> bool:
    """Ray cast; used only to decide whether a contour is a hole in another."""
    x, y = float(point[0]), float(point[1])
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1
            if x < xin:
                inside = not inside
    return inside


def trace_contours(mask: np.ndarray) -> Tuple[np.ndarray, List[np.ndarray]]:
    """Return ``(outer, holes)`` polygons in pixel coordinates.

    The largest contour is the part; anything enclosed by it is a hole. Contours
    that are neither (separate blobs elsewhere in the frame) are dropped — one
    image makes one part, and silently unioning stray marks would be worse than
    ignoring them.
    """
    from skimage import measure

    padded = np.pad(mask.astype(float), 1)
    raw = measure.find_contours(padded, 0.5)
    polys: List[np.ndarray] = []
    for contour in raw:
        simplified = measure.approximate_polygon(contour, tolerance=_SIMPLIFY_PX)
        if len(simplified) < 4:
            continue
        # find_contours yields (row, col); flip to (x, y) with y up.
        pts = np.column_stack([simplified[:, 1] - 1.0, mask.shape[0] - (simplified[:, 0] - 1.0)])
        if np.allclose(pts[0], pts[-1]):
            pts = pts[:-1]
        if len(pts) >= 3:
            polys.append(pts)

    if not polys:
        raise ImageError("no shape found in that image — try higher contrast")

    polys.sort(key=_polygon_area, reverse=True)
    outer = polys[0]
    outer_area = _polygon_area(outer)
    if outer_area <= 0:
        raise ImageError("no shape found in that image — try higher contrast")

    holes = [
        p for p in polys[1:]
        if _polygon_area(p) / outer_area >= _MIN_AREA_FRACTION and _point_in_polygon(p[0], outer)
    ]
    return outer, holes


def _decimate(points: np.ndarray, limit: int = _MAX_POLYGON_POINTS) -> np.ndarray:
    """Cap vertex count — the SDF evaluates every edge for every sample point."""
    if len(points) <= limit:
        return points
    idx = np.linspace(0, len(points) - 1, limit).astype(int)
    return points[idx]


def _to_metres(points: np.ndarray, scale: float, centre: np.ndarray) -> List[List[float]]:
    scaled = (points - centre) * scale
    return [[round(float(x), 6), round(float(y), 6)] for x, y in scaled]


def trace_to_tree(
    data: bytes,
    width_m: float = DEFAULT_WIDTH_M,
    depth_m: float = DEFAULT_DEPTH_M,
) -> FeatureTree:
    """Trace an image's silhouette into an extruded solid, holes included."""
    if width_m <= 0 or depth_m <= 0:
        raise ImageError("width and depth must be positive")

    mask = _load_mask(data)
    outer, holes = trace_contours(mask)

    lo = outer.min(axis=0)
    hi = outer.max(axis=0)
    span = float(max(hi - lo))
    if span <= 0:
        raise ImageError("that image traced to a degenerate outline")
    scale = width_m / span
    centre = (lo + hi) / 2.0

    body: Any = ExtrudeFeature(
        name="Traced profile",
        profile={"type": "polygon", "points": _to_metres(_decimate(outer), scale, centre)},
        plane="xy",
        offset=-depth_m / 2.0,
        depth=depth_m,
    )

    if holes:
        cutters = [
            ExtrudeFeature(
                name=f"Hole {i + 1}",
                profile={"type": "polygon", "points": _to_metres(_decimate(h), scale, centre)},
                plane="xy",
                # Overshoot the body so the cut is clean through.
                offset=-depth_m,
                depth=depth_m * 3.0,
            )
            for i, h in enumerate(holes)
        ]
        body = BooleanFeature(name="Traced part", operation="subtract", children=[body, *cutters])

    return FeatureTree(body)


# ---------------------------------------------------------------------------
# Vision LLM path
# ---------------------------------------------------------------------------

def _data_uri(data: bytes, filename: str) -> str:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "png").lower()
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext if ext in SUPPORTED_FORMATS else 'png'}"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


_VISION_INSTRUCTION = (
    "The user supplied an image of a part. Model it as a kernel feature tree using the same "
    "schema and units (metres) as a text prompt. Read dimensions off the image only if they are "
    "written on it; otherwise choose sensible proportions and state them by the geometry you emit. "
    "If the image is a flat outline (a logo, gasket, faceplate), model it as an extrusion."
)


def llm_tree_from_image(data: bytes, filename: str, prompt: Optional[str] = None) -> Optional[dict]:
    """Ask a vision-capable LLM for a feature tree. None if unavailable or unparseable."""
    if not llm.is_configured():
        return None

    text = _VISION_INSTRUCTION if not prompt else f"{_VISION_INSTRUCTION}\n\nUser note: {prompt}"
    messages = [
        {"role": "system", "content": llm._FEATURE_TREE_SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": _data_uri(data, filename)}},
        ]},
    ]
    try:
        content = llm._call_llm(messages, timeout=90.0, max_tokens=3000)
    except Exception as exc:
        # A text-only model rejects the image part; that is a normal outcome here.
        _LOGGER.info("Vision feature-tree request failed (%s); tracing instead", exc)
        return None
    return llm._parse_tree_response(content)


def generate_from_image(
    data: bytes,
    filename: str = "image.png",
    prompt: Optional[str] = None,
    width_mm: float = DEFAULT_WIDTH_M * 1000.0,
    depth_mm: float = DEFAULT_DEPTH_M * 1000.0,
) -> Tuple[FeatureTree, str]:
    """Return ``(tree, source)`` for an image.

    Tries a vision LLM when one is configured, and traces the silhouette
    otherwise or if the model's tree does not evaluate.
    """
    if not data:
        raise ImageError("no image data")

    tree_dict = llm_tree_from_image(data, filename, prompt)
    if tree_dict is not None:
        try:
            tree = FeatureTree.from_dict(tree_dict)
            tree.root.evaluate()
            return tree, "llm-vision"
        except Exception as exc:
            _LOGGER.info("Vision tree did not evaluate (%s); tracing instead", exc)

    return trace_to_tree(data, width_m=width_mm / 1000.0, depth_m=depth_mm / 1000.0), "traced"


def create_document_from_image(
    data: bytes,
    filename: str = "image.png",
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    width_mm: float = DEFAULT_WIDTH_M * 1000.0,
    depth_mm: float = DEFAULT_DEPTH_M * 1000.0,
    resolution: int = 96,
    owner_id: Optional[str] = None,
):
    """Trace or model an image into a new editable document."""
    from app.services import document_service

    tree, source = generate_from_image(
        data, filename=filename, prompt=prompt, width_mm=width_mm, depth_mm=depth_mm
    )

    base = filename.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    doc = document_service.create_document(name=name or stem or "Image part", owner_id=owner_id)
    updated = document_service.update_document(
        doc.id, feature_tree=tree.to_dict(), resolution=min(128, max(64, resolution))
    )
    if updated is None:  # pragma: no cover — we just created it
        raise ImageError("Document creation failed.")
    # The result is an ordinary editable tree, so it is text_to_cad-like
    # provenance rather than "imported" (which means an opaque voxel body).
    updated.provenance = "text_to_cad"
    document_service._persist(updated)
    return updated, source
