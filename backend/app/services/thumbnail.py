"""A picture of the part, for a library that currently shows a generic icon.

Two parts are indistinguishable in the library until one of them is opened.
Fixing that needs an image, and an image needs three decisions:

**What it is a picture of.** A *specific body* — so it is keyed on `mesh_hash`,
exactly like the mesh itself, and served with the same immutable caching. Keyed
on the document id it would show last week's part, which is worse than an icon
because it is confidently wrong. That identity only exists because P1 gave the
geometry a content hash; this could not have been built before it.

**Shaded, not a silhouette.** `services/drawing` already projects an outline
with no GPU and would have been cheaper, but a flat outline is not what someone
scanning a library recognises. This is the same painter's-algorithm shading
`features/sketch/PartBackdrop.tsx` uses on the sketch canvas, in numpy: project
the triangles, sort back to front, fill with a Lambert term modulated by depth.
No GPU, no browser, and it works for a document nobody has ever opened.

**The normal carries the tone, and depth only grades it** — the opposite of the
sketch backdrop, and for a reason worth writing down. The backdrop looks
*straight down a plane normal*, where every visible face is parallel and the
Lambert term is therefore useless, so depth is all it has. A three-quarter view
sees three differently-oriented sides, so orientation does the work. Leading
with depth here put a visible diagonal seam across every flat face, because
`tri_depth` is a per-triangle mean and the two triangles of one rectangle do not
share it.
"""

from __future__ import annotations

import io
import logging
from typing import Optional, Tuple

import numpy as np

_LOGGER = logging.getLogger(__name__)

SIZE = 256
MIME = "image/png"

# The library's panel colour, so a thumbnail sits in its card rather than on a
# white postage stamp.
_BACKGROUND = (24, 27, 33)
_MATERIAL = np.array([196, 205, 216], dtype=float)

# A three-quarter view with **Z up**, because the kernel is Z-up and that is how
# anyone looking at a part thinks of it. Taking the viewport's camera verbatim
# was the first attempt and lays the part on its side: three.js is Y-up, the
# viewport simply accepts that, and a boss on the top face ends up pointing at
# the camera where it cannot be seen at all.
_EYE = np.array([1.0, -1.5, 0.85], dtype=float)
_UP = np.array([0.0, 0.0, 1.0], dtype=float)

#: Named directions to look *from*, in kernel coordinates. The library card uses
#: `iso` and always has; the rest exist for review packets, where one view cannot
#: show a hole on the far side or a boss on top.
CAMERAS: dict = {
    "iso": (1.0, -1.5, 0.85),
    "iso_opposite": (-1.0, 1.5, 0.85),
    "top": (0.0, 0.0, 1.0),
    "bottom": (0.0, 0.0, -1.0),
    "front": (0.0, -1.0, 0.0),
    "right": (1.0, 0.0, 0.0),
}


def _basis(eye=None) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Right/up/forward for a camera looking from ``eye`` at the origin.

    **The world up has to be swapped when the camera is on the Z axis.** A `top`
    view looks straight down, `cross(up, forward)` is then the zero vector, and
    dividing by its length fills every pixel with NaN — a blank picture that
    reports no error, which is the one failure mode a review render must not
    have.
    """
    eye = _EYE if eye is None else np.asarray(eye, dtype=float)
    norm = float(np.linalg.norm(eye))
    forward = eye / (norm or 1.0)
    world_up = _UP if abs(float(forward @ _UP)) < 0.99 else np.array([0.0, 1.0, 0.0])
    right = np.cross(world_up, forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    return right, up, forward


def render(vertices: np.ndarray, faces: np.ndarray, size: int = SIZE,
           eye=None) -> Optional[bytes]:
    """A shaded PNG of a mesh, or ``None`` if there is nothing to draw.

    ``eye`` is the direction to look from, defaulting to the three-quarter view
    the library card uses. A name from `CAMERAS` is resolved to its direction.
    """
    from PIL import Image, ImageDraw

    vertices = np.asarray(vertices, dtype=float)
    faces = np.asarray(faces, dtype=np.int64)
    if vertices.size == 0 or faces.size == 0:
        return None

    if isinstance(eye, str):
        eye = CAMERAS.get(eye) or _EYE
    right, up, forward = _basis(eye)
    centre = (vertices.min(axis=0) + vertices.max(axis=0)) / 2.0
    local = vertices - centre

    # Orthographic: the part is scaled to fit rather than perspective-projected,
    # so a 5 mm bracket and a 500 mm plate fill the same frame.
    u = local @ right
    v = local @ up
    depth = local @ forward
    span = max(float(u.max() - u.min()), float(v.max() - v.min()))
    if span <= 0:
        return None
    scale = (size * 0.82) / span
    px = (u - (u.max() + u.min()) / 2.0) * scale + size / 2.0
    py = size / 2.0 - (v - (v.max() + v.min()) / 2.0) * scale

    tri_depth = depth[faces].mean(axis=1)
    # Painter's algorithm: furthest first, so nearer triangles overwrite. A depth
    # buffer would be more correct and needs a per-pixel loop in Python.
    order = np.argsort(-tri_depth)

    corners = np.stack([px[faces], py[faces]], axis=-1)          # (F, 3, 2)
    a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]
    # Screen-space normal sign: a triangle wound clockwise in screen space faces
    # away. Back faces are dropped rather than drawn, which roughly halves the
    # work and removes the interior showing through open silhouettes.
    facing = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])

    world = vertices[faces]
    normals = np.cross(world[:, 1] - world[:, 0], world[:, 2] - world[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    lengths[lengths == 0] = 1.0
    normals /= lengths[:, None]
    # Two lights and a floor, so a face turned away from the single key light is
    # dim rather than black and the three visible sides of a box read differently.
    key = np.array([0.4, -0.8, 0.45]); key /= np.linalg.norm(key)
    fill = np.array([-0.7, 0.2, 0.3]); fill /= np.linalg.norm(fill)
    lit = np.clip(0.72 * np.abs(normals @ key) + 0.28 * np.abs(normals @ fill), 0.0, 1.0)

    near, far = float(tri_depth.max()), float(tri_depth.min())
    spread = (near - far) or 1.0
    # Orientation carries the tone; depth only grades it, and only slightly.
    # The first version had that the other way round and put a diagonal seam
    # across every flat face — `tri_depth` is a per-triangle mean, so the two
    # triangles of one rectangle get different values however coplanar they are.
    # Coplanar triangles do share a normal, which is what makes leading with the
    # Lambert term seam-free by construction.
    shade = (0.30 + 0.70 * lit) * (0.93 + 0.07 * ((tri_depth - far) / spread))

    image = Image.new("RGB", (size, size), _BACKGROUND)
    draw = ImageDraw.Draw(image)
    for index in order:
        if facing[index] <= 0:
            continue
        colour = tuple(int(v) for v in np.clip(_MATERIAL * shade[index], 0, 255))
        draw.polygon([tuple(p) for p in corners[index]], fill=colour, outline=colour)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def for_document(doc_id: str, mesh_hash: str) -> Optional[bytes]:
    """The thumbnail for a document's geometry, if that hash is its geometry.

    Refuses a hash the document does not have, for the same reason the mesh
    endpoint does: the response is cached forever under that hash, so answering
    with a different picture poisons every cache that sees it.
    """
    from app.cad_kernel.kernel import Kernel
    from app.services import document_service as docs

    cached = docs.cached_thumbnail(doc_id, mesh_hash)
    if cached is not None:
        return cached

    doc = docs.get_document(doc_id)
    if doc is None or (doc.derived.mesh_hash or "") != mesh_hash:
        return None
    # A document with no body evaluates to a 1 mm stand-in, which scales to fit
    # and renders as a confident little cube — the exact starting block the
    # datum-plane template exists to stop showing people. The icon is the honest
    # picture of a part nobody has modelled yet.
    from app.services import feature_ops

    if not feature_ops.has_solid(doc.feature_tree):
        return None
    try:
        solid = docs.get_solid(doc_id, resolution=doc.derived.resolution or 128)
        mesh = Kernel.tessellate(solid)
        payload = render(np.asarray(mesh.vertices), np.asarray(mesh.faces))
    except Exception as exc:  # noqa: BLE001 — a missing picture is not a failure
        _LOGGER.info("thumbnail unavailable for %s: %s", doc_id, exc)
        return None
    if payload is not None:
        docs.cache_thumbnail(doc_id, mesh_hash, payload)
    return payload
