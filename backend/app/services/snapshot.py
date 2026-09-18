"""A picture of what was actually built, for the agent that built it.

Deterministic checks decide pass or fail; they do not catch a part that is
valid, measures correctly and is the wrong shape. A live run reported "a
120 x 80 x 6 plate with four holes" over a body measuring 50 x 30 x 6 — every
check it ran passed, because none of them asked whether the result looked like
the request. A rendered view is the cheapest thing that does.

Three decisions worth stating:

**Review resolution, not display resolution.** The renderer is a painter's
algorithm in Python, one polygon call per triangle, so cost tracks the triangle
count. A document meshed at 128 for the viewport is re-tessellated at
``REVIEW_RESOLUTION`` here: a review only has to show shape, and an assembly of
ten parts at display resolution is seconds per view.

**512 pixels, not the library card's 256.** A card only has to be recognisable.
Spotting a missing hole or a mirrored bracket needs the larger frame, and a PNG
at this size is roughly 50 to 150 KB, a third more again as base64, which is
what each attached view costs on every remaining planner turn.

**An assembly is rendered placed.** Each component's mesh is carried into
assembly space by its own transform — the scale-then-rotate-then-translate order
`interference` uses — because the question a review answers about an assembly is
where the parts ended up, which is exactly what a per-part picture cannot show.
"""

from __future__ import annotations

import base64
import logging
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np

from app.services import thumbnail

_LOGGER = logging.getLogger(__name__)

#: Tessellation resolution for a review render. Shape, not surface finish.
REVIEW_RESOLUTION = 64

#: Pixels on a side. See the module docstring for why this is not 256.
REVIEW_PIXELS = 512

#: The most views one packet may carry, whatever a caller asks for.
MAX_VIEWS = 3

#: What a review shows when the caller names nothing. One view answers "is this
#: the right shape" for a single part; an assembly needs the plan and the
#: elevation too, because a part in the wrong place is invisible from one angle.
DEFAULT_CAMERAS = {"document": ("iso",), "assembly": ("iso", "top", "front")}


def _document_mesh(doc_id: str) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    from app.cad_kernel.kernel import Kernel
    from app.services import document_service as docs, feature_ops

    doc = docs.get_document(doc_id)
    if doc is None or not feature_ops.has_solid(doc.feature_tree):
        # The evaluator hands back a 1 mm stand-in for a body-less tree, which
        # renders as a confident little cube. No picture is the honest answer.
        return None
    solid = docs.get_solid(doc_id, resolution=REVIEW_RESOLUTION)
    mesh = Kernel.tessellate(solid)
    return np.asarray(mesh.vertices, dtype=float), np.asarray(mesh.faces, dtype=np.int64)


def _assembly_mesh(assembly_id: str) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    from app.cad_kernel.evaluator.sdf import _euler_matrix
    from app.services import assembly_service

    asm = assembly_service.get_assembly(assembly_id)
    if asm is None:
        return None
    vertices: List[np.ndarray] = []
    faces: List[np.ndarray] = []
    offset = 0
    # A component is a *reference*, so one part document routinely appears many
    # times — four identical motors on an airframe are one body meshed four
    # times without this.
    meshed: dict = {}
    for component in asm.components:
        if component.missing:
            continue
        if component.part_id not in meshed:
            meshed[component.part_id] = _document_mesh(component.part_id)
        part = meshed[component.part_id]
        if part is None:
            continue
        local, triangles = part
        t = component.transform
        rotation = _euler_matrix(t.rotate_deg.x, t.rotate_deg.y, t.rotate_deg.z)
        placed = (rotation @ ((t.scale or 1.0) * local).T).T + np.array(
            [t.translate.x, t.translate.y, t.translate.z], dtype=float)
        vertices.append(placed)
        faces.append(triangles + offset)
        offset += len(placed)
    if not vertices:
        return None
    return np.concatenate(vertices), np.concatenate(faces)


def mesh_for(subject: str, subject_id: str) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """The triangles a review should look at, or ``None`` when there are none."""
    return (_assembly_mesh(subject_id) if subject == "assembly"
            else _document_mesh(subject_id))


def cameras_for(subject: str, requested: Optional[Sequence[str]] = None) -> List[str]:
    """The named views to render, bounded and de-duplicated in the order given."""
    names: Iterable[str] = requested or DEFAULT_CAMERAS.get(subject, ("iso",))
    chosen: List[str] = []
    for name in names:
        key = str(name).strip().lower()
        if key in thumbnail.CAMERAS and key not in chosen:
            chosen.append(key)
    return (chosen or list(DEFAULT_CAMERAS.get(subject, ("iso",))))[:MAX_VIEWS]


def packet(subject: str, subject_id: str, cameras: Optional[Sequence[str]] = None,
           pixels: int = REVIEW_PIXELS) -> List[dict]:
    """Rendered views of the current body: ``[{camera, png, direction, up}]``.

    Never raises: a review that cannot be rendered is reported as no views, and
    the caller says so rather than failing the run over a picture.
    """
    try:
        mesh = mesh_for(subject, subject_id)
    except Exception as exc:  # noqa: BLE001 — a missing picture is not a failure
        _LOGGER.info("snapshot mesh unavailable for %s %s: %s", subject, subject_id, exc)
        return []
    if mesh is None:
        return []
    vertices, faces = mesh
    size = max(128, min(int(pixels or REVIEW_PIXELS), 1024))
    views: List[dict] = []
    for name in cameras_for(subject, cameras):
        direction = thumbnail.CAMERAS[name]
        try:
            png = thumbnail.render(vertices, faces, size=size, eye=direction)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.info("snapshot render failed (%s): %s", name, exc)
            continue
        if png:
            _, up, _ = thumbnail._basis(direction)
            views.append({"camera": name, "png": png,
                          "direction": [round(float(v), 4) for v in direction],
                          "up": [round(float(v), 4) for v in up]})
    return views


def store_views(subject: str, subject_id: str, views: List[dict]) -> List[dict]:
    """File each view as an immutable artifact and describe where it is.

    URLs, never bytes. An operation result is trimmed into the planner's
    transcript (`copilot._trim_result` caps a string at 2000 characters), so a
    base64 PNG returned from a handler would be truncated into garbage and cost
    the whole transcript to carry. The copilot attaches real images by calling
    `packet` directly; everyone else fetches the URL.
    """
    from app.services import artifacts

    stored: List[dict] = []
    for view in views:
        record = artifacts.store(subject_id, artifacts.Artifact(
            view["png"], "image/png", f"{view['camera']}.png"))
        url = (f"/api/assemblies/{subject_id}/artifact/{record['hash']}"
               if subject == "assembly" else record["url"])
        stored.append({"camera": view["camera"], "url": url,
                       "bytes": record["bytes"], "direction": view["direction"],
                       "up": view["up"]})
    return stored


def data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()
