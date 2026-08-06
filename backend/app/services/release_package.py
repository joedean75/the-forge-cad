"""Everything a supplier needs, in one file, pinned to a revision letter.

`release_revision` already cuts an immutable lettered snapshot and
`verify_against_spec` already scores the body against what it was supposed to
be. Those are the two halves of a handover and nothing put them in an envelope:
a shop received a STEP file exported from whatever the document happened to be
that afternoon.

**The package is built from the revision, not from the document.** A package
pinned to "now" is not a release, it is a screenshot of a moving target — and
because the revision carries a feature tree rather than geometry, building it
means *evaluating that tree*. Everything downstream (the drawing, the spec
report, the exports) is keyed by document id, so the snapshot is materialised as
an ephemeral document with an id of `{doc_id}@{letter}` and torn down after.
`document_service.EPHEMERAL` is what keeps that out of the datastore; the guard
is in `_persist` because the evaluate path persists from four different places.

**It is a job, not a request.** A drawing render is ~2.9 s, the STEP export is a
build and the spec report meshes the body — a package is a fan-out of every slow
thing in the system, and doing it inline means timing out at the worst possible
moment. Same shape as the optimize path: post to start, poll a job id.

The archive is a plain uncompressed-directory `zip` (deflated), because the
things in it are already the interchange formats — no proprietary container, and
a supplier can open it with nothing installed.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_LOGGER = logging.getLogger(__name__)

#: What goes in, in the order a person opens them. A format that fails is
#: *recorded* rather than fatal: a body the drawing pipeline cannot trace still
#: has a STEP file worth sending, and a package that refuses to exist because
#: one of five renderers tripped is worse than one that says which is missing.
CONTENTS = ("step", "pdf", "dxf", "stl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _revision(doc, letter: Optional[str]):
    """The revision to build from — the named letter, or the latest released."""
    from app.services.op_errors import PreconditionFailed

    if not doc.revisions:
        raise PreconditionFailed(
            "this document has no released revision; call release_revision first — "
            "a package built from the current document is not a release")
    if letter:
        found = next((r for r in doc.revisions if r.letter == letter), None)
        if found is None:
            known = ", ".join(r.letter for r in doc.revisions)
            from app.services.op_errors import BadArgument

            raise BadArgument(f"no revision {letter!r}; this document has: {known}",
                              arg="revision")
        return found
    return doc.revisions[-1]


class _Snapshot:
    """The revision's tree, evaluated as a document, for the length of a `with`."""

    def __init__(self, doc, revision, resolution: int):
        self.doc = doc
        self.revision = revision
        self.resolution = resolution
        self.scratch = None

    def __enter__(self):
        import copy

        from app.models.schemas import DerivedCache
        from app.services import document_service as docs

        scratch = self.doc.model_copy(deep=True)
        scratch.id = f"{self.doc.id}@{self.revision.letter}"
        scratch.feature_tree = copy.deepcopy(self.revision.feature_tree)
        # The revision is geometry, not history: a snapshot carries no derived
        # cache and no version list, and reusing the document's would report the
        # *current* body's mass against the released letter.
        scratch.derived = DerivedCache(mass=0.0, volume=0.0)
        scratch.version_history = []
        docs.EPHEMERAL.add(scratch.id)
        docs.DOCUMENTS[scratch.id] = scratch
        docs.evaluate_document(scratch, resolution=self.resolution, persist=False)
        self.scratch = scratch
        return scratch

    def __exit__(self, *exc):
        from app.services import document_service as docs

        if self.scratch is not None:
            docs.DOCUMENTS.pop(self.scratch.id, None)
            docs.EPHEMERAL.discard(self.scratch.id)
            docs._SOLID_CACHE.pop(self.scratch.id, None)
        return False


def _spec_report(scratch) -> Dict[str, Any]:
    """The score against the spec, or an honest note that there is none.

    This is the row that says the part is not merely a valid solid but the part
    that was asked for, so its absence is stated rather than left to be inferred
    from a missing file.
    """
    from app.services import design_spec

    try:
        return design_spec.verify(scratch)
    except design_spec.SpecError as exc:
        return {"score": None, "checks": [], "unavailable": str(exc)}


def _bom(scratch) -> List[Dict[str, Any]]:
    """The parts list for one part: itself, its material and what it weighs.

    A single-line BOM looks redundant until it is the line a purchasing system
    reads. Priced with the same `materials` cost model the assembly BOM uses —
    material cost, explicitly not a quote.
    """
    from app.services import materials

    material = materials.get_material(scratch.material_id)
    mass = float(scratch.derived.mass or 0.0)
    return [{
        "item": 1,
        "name": scratch.name,
        "quantity": 1,
        "material": material.name,
        "unit_mass_g": round(mass * 1000, 2),
        "ext_material_cost_usd": round(mass * material.cost_per_kg, 2),
    }]


def _bom_csv(lines: List[Dict[str, Any]]) -> str:
    import csv

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Item", "Name", "Qty", "Material", "Unit Mass (g)",
                     "Ext Material Cost (USD)"])
    for line in lines:
        writer.writerow([line["item"], line["name"], line["quantity"], line["material"],
                         line["unit_mass_g"], line["ext_material_cost_usd"]])
    return buf.getvalue()


def build(doc_id: str, *, revision: Optional[str] = None, resolution: int = 128,
          drawing_spec: Optional[dict] = None) -> Dict[str, Any]:
    """Assemble the release package. Returns the stored artifact's descriptor."""
    from app.services import artifacts, document_service as docs
    from app.services.op_errors import NoSuchDocument

    doc = docs.get_document(doc_id)
    if doc is None:
        raise NoSuchDocument(f"no document {doc_id!r}")
    rev = _revision(doc, revision)

    with _Snapshot(doc, rev, resolution) as scratch:
        included: List[Dict[str, Any]] = []
        missing: List[Dict[str, str]] = []
        files: Dict[str, bytes] = {}

        for fmt in CONTENTS:
            try:
                artifact = artifacts.build(scratch.id, fmt, resolution=resolution,
                                           spec=drawing_spec,
                                           name=f"{doc.name}-rev{rev.letter}")
            except Exception as exc:  # noqa: BLE001 — recorded, never fatal
                missing.append({"format": fmt, "reason": str(exc)})
                continue
            files[artifact.filename] = artifact.payload
            included.append({"format": fmt, "filename": artifact.filename,
                             "bytes": len(artifact.payload)})

        report = _spec_report(scratch)
        bom_lines = _bom(scratch)
        bbox = scratch.derived.bbox
        manifest = {
            "document_id": doc.id,
            "name": doc.name,
            # The letter is the point of the whole exercise: a package that does
            # not say which revision it is cannot be checked against a drawing.
            "revision": rev.letter,
            "revision_label": rev.label,
            "released_at": rev.released_at,
            "notes": rev.notes,
            "lifecycle_state": doc.lifecycle_state,
            "packaged_at": _now(),
            # Said explicitly, because a document that has moved on since the
            # release is the normal case and the package is *not* of it.
            "document_matches_revision": docs._hash_tree(doc.feature_tree)
            == docs._hash_tree(rev.feature_tree),
            "material": bom_lines[0]["material"],
            "mass_g": bom_lines[0]["unit_mass_g"],
            "volume_mm3": round(float(scratch.derived.volume or 0.0) * 1e9, 2),
            "bbox_mm": None if bbox is None else {
                "x": round((bbox.max.x - bbox.min.x) * 1000, 3),
                "y": round((bbox.max.y - bbox.min.y) * 1000, 3),
                "z": round((bbox.max.z - bbox.min.z) * 1000, 3),
            },
            "spec_score": report.get("score"),
            "contents": included,
            "unavailable": missing,
            "units": "STEP and the drawing are in millimetres",
        }

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            for filename, payload in files.items():
                archive.writestr(filename, payload)
            archive.writestr("spec-report.json", json.dumps(report, indent=2))
            archive.writestr("bom.csv", _bom_csv(bom_lines))
        payload = buffer.getvalue()

    name = artifacts._safe_name(f"{doc.name}-rev{rev.letter}", "zip")
    stored = artifacts.store(doc_id, artifacts.Artifact(payload, "application/zip", name))
    return {"revision": rev.letter, "manifest": manifest, **stored}
