"""Exported files as addressable resources, not as inline payloads.

The catalog could not produce a file. `GET /api/documents/{id}/step` existed, so
a human could export and an agent could not — the last place in the system where
the UI could do something the action space could not describe.

**The result of an export is a URL.** Inlining the bytes is the mistake P1 spent
a sprint undoing for the mesh, and a STEP body is *larger* than the mesh was: a
plain plate exports to 1.4 MB of B-rep text, which base64s to 1.9 MB riding on
an operation response that an agent then puts in a prompt. The URL is keyed by
the **content** hash, so it inherits the mesh endpoint's whole contract:
immutable, cached forever, and a 404 rather than different bytes under a digest
a client has been told to keep.

Two things follow from content addressing that are worth stating:

- **Exporting the same body twice returns the same URL.** That is what makes a
  release package cheap to re-request and an agent's retry free.
- **The digest is of the file, not of the document.** Two documents that happen
  to evaluate to identical geometry export to different URLs because the STEP
  header carries the part name — which is correct: they are different files.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Dict, NamedTuple, Optional

from app.services.op_errors import BadArgument, PreconditionFailed

_LOGGER = logging.getLogger(__name__)


class Artifact(NamedTuple):
    payload: bytes
    media_type: str
    filename: str


class _Format(NamedTuple):
    media_type: str
    extension: str
    description: str


#: What `export` can write, and what each one is *for*. The description reaches
#: the operation's schema, because "step | stl | dxf | svg | pdf" tells a model
#: nothing about which of them a shop can quote from.
FORMATS: Dict[str, _Format] = {
    "step": _Format("application/step", "step",
                    "exact B-rep solid — what another CAD package opens and edits"),
    "stl": _Format("model/stl", "stl",
                   "triangle mesh — what a 3D printer's slicer takes"),
    "pdf": _Format("application/pdf", "pdf",
                   "the dimensioned drawing sheet, as a shop receives it"),
    "dxf": _Format("image/vnd.dxf", "dxf",
                   "the drawing's geometry on layers — what CAM and drafting tools read"),
    "svg": _Format("image/svg+xml", "svg",
                   "the drawing sheet as a web image, to look at in a browser"),
}

_CACHE_SIZE = 64


class _LruCache(dict):
    """A dict that forgets its oldest entry past a cap."""

    def __init__(self, cap: int):
        super().__init__()
        self._cap = cap

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        while len(self) > self._cap:
            del self[next(iter(self))]


# Exports are big and re-requested rarely, so this is deliberately smaller than
# the thumbnail cache and larger than one: a release package asks for four
# formats of the same body in a row.
_ARTIFACTS: Dict[str, Artifact] = _LruCache(_CACHE_SIZE)


def _key(doc_id: str, digest: str) -> str:
    return f"{doc_id}:{digest}"


def store(doc_id: str, artifact: Artifact) -> Dict[str, object]:
    """Keep an exported file under its content hash and describe where it is."""
    digest = hashlib.sha256(artifact.payload).hexdigest()[:16]
    _ARTIFACTS[_key(doc_id, digest)] = artifact
    return {
        "url": f"/api/documents/{doc_id}/artifact/{digest}",
        "hash": digest,
        "bytes": len(artifact.payload),
        "filename": artifact.filename,
        "media_type": artifact.media_type,
    }


def fetch(doc_id: str, digest: str) -> Optional[Artifact]:
    return _ARTIFACTS.get(_key(doc_id, digest))


def _safe_name(name: str, extension: str) -> str:
    """A filename a browser and a shop's file server will both accept."""
    cleaned = "".join(c if (c.isalnum() or c in "-_ ") else "-" for c in (name or "part"))
    cleaned = "-".join(cleaned.split()) or "part"
    return f"{cleaned[:60]}.{extension}"


def build(doc_id: str, fmt: str, *, resolution: int = 128,
          spec: Optional[dict] = None, name: Optional[str] = None) -> Artifact:
    """Render one export format for a document. Raises a typed failure, never 500."""
    from app.services import document_service as docs

    fmt = str(fmt or "").lower()
    if fmt not in FORMATS:
        raise BadArgument(
            f"cannot export {fmt!r}; formats are: {', '.join(sorted(FORMATS))}",
            arg="format")

    doc = docs.get_document(doc_id)
    if doc is None:
        from app.services.op_errors import NoSuchDocument

        raise NoSuchDocument(f"no document {doc_id!r}")
    meta = FORMATS[fmt]
    filename = _safe_name(name or doc.name, meta.extension)

    from app.services import instrumentation

    try:
        with instrumentation.span(f"export.{fmt}"):
            payload = _render(doc_id, fmt, resolution=resolution, spec=spec)
    except (BadArgument, PreconditionFailed):
        raise
    except Exception as exc:  # noqa: BLE001
        _LOGGER.info("export %s of %s failed: %s", fmt, doc_id, exc, exc_info=True)
        raise PreconditionFailed(
            f"the body could not be exported as {fmt}: {exc}") from exc

    if not payload:
        raise PreconditionFailed(
            f"the {fmt} export came back empty — the document has no body to export")
    instrumentation.gauge(f"export.{fmt}.bytes", len(payload))
    return Artifact(payload, meta.media_type, filename)


def _render(doc_id: str, fmt: str, *, resolution: int, spec: Optional[dict]) -> bytes:
    from app.cad_kernel.io.stl import write_stl_binary
    from app.cad_kernel.kernel import Kernel
    from app.services import document_analysis, document_service as docs, drawing

    if fmt == "step":
        # Exact when OCC can build the tree, faceted otherwise — the same
        # decision `GET /{id}/step` makes, and both writers emit millimetres
        # with an explicit SI_UNIT context.
        return document_analysis.export_step_document(doc_id, resolution=resolution).encode("utf-8")
    if fmt == "stl":
        solid = docs.get_solid(doc_id, resolution=resolution)
        doc = docs.get_document(doc_id)
        return write_stl_binary(Kernel.tessellate(solid), name=(doc.name or "part")[:60])
    if fmt == "pdf":
        return drawing.render_pdf(doc_id, spec=spec)
    if fmt == "dxf":
        return drawing.render_dxf(doc_id, spec=spec).encode("utf-8")
    return drawing.generate_drawing(doc_id, spec=spec)["svg"].encode("utf-8")


def export(doc_id: str, fmt: str, *, resolution: int = 128,
           spec: Optional[dict] = None, name: Optional[str] = None) -> Dict[str, object]:
    """Build an export and return where to fetch it — the operation's result."""
    artifact = build(doc_id, fmt, resolution=resolution, spec=spec, name=name)
    return {"format": fmt, "description": FORMATS[fmt].description,
            **store(doc_id, artifact)}
