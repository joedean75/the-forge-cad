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

Since Sprint 4 the bytes live in `services/blobs` rather than in an LRU here, so
a URL is valid on whichever instance serves the fetch and survives a restart.
Nothing about the contract changed: the digest is still the name, the store
still 404s rather than returning different bytes, and the caller still
regenerates on a miss.
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

def _key(digest: str) -> str:
    """A blob key inside the document's namespace.

    Prefixed, because the same namespace holds the document's meshes and
    thumbnails: a mesh hash and an export hash are both hex and could otherwise
    collide into each other's slot. Which they never would in practice, and
    "never in practice" is how content-addressed stores serve the wrong bytes.
    """
    return f"export-{digest}"


def store(doc_id: str, artifact: Artifact) -> Dict[str, object]:
    """Keep an exported file under its content hash and describe where it is.

    The bytes go to `services/blobs`, which on a configured deployment means a
    shared volume rather than this process's memory. That is what makes the URL
    valid on the instance that did not build it — content addressing already
    made a cross-instance fetch *safe*, in the sense that it could only 404, and
    this is what makes it work.
    """
    from app.services import blobs

    digest = hashlib.sha256(artifact.payload).hexdigest()[:16]
    blobs.put(doc_id, _key(digest),
              blobs.Blob(artifact.payload, artifact.media_type, artifact.filename))
    return {
        "url": f"/api/documents/{doc_id}/artifact/{digest}",
        "hash": digest,
        "bytes": len(artifact.payload),
        "filename": artifact.filename,
        "media_type": artifact.media_type,
    }


def fetch(doc_id: str, digest: str) -> Optional[Artifact]:
    from app.services import blobs

    blob = blobs.get(doc_id, _key(digest))
    return Artifact(blob.payload, blob.media_type, blob.filename) if blob else None


#: G-code is written by the slicer and the CAM planner, and it used to ride in
#: `CamResult.gcode_url` as a base64 data URI — a megabyte of text inlined into
#: the document, stripped on persist, and therefore *lost on every reload* even
#: though the toolpath that produced it had not changed. It is a generated file
#: like any other, so it gets a content-hash URL like any other.
GCODE_MEDIA_TYPE = "text/plain"


def store_gcode(doc_id: str, gcode: str, name: str) -> str:
    """File a G-code program as an artifact and return its URL."""
    payload = gcode.encode("utf-8")
    return str(store(doc_id, Artifact(payload, GCODE_MEDIA_TYPE,
                                      _safe_name(name, "gcode")))["url"])


def gcode_text(doc_id: str, url: Optional[str]) -> Optional[str]:
    """Read a G-code program back from whichever form its URL is in.

    Two forms, because documents persisted before this carried the program
    inline as `data:text/plain;base64,…` and a live process may still be holding
    one. A reader that understood only the new form would 404 on a part sliced
    before a deploy — which is the sort of breakage that makes people distrust
    the whole feature rather than blame the migration.
    """
    if not url:
        return None
    if url.startswith("data:"):
        import base64

        try:
            return base64.b64decode(url.split(",", 1)[1]).decode("utf-8")
        except (IndexError, ValueError):
            return None
    digest = url.rstrip("/").rsplit("/", 1)[-1]
    artifact = fetch(doc_id, digest)
    return artifact.payload.decode("utf-8") if artifact else None


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
