"""Pinned, MIT-licensed Adafruit CAD; lazy verified downloads for internal design."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import quote

import httpx

PREFIX = "adafruit:"
SOURCE = {"id": "adafruit", "label": "Adafruit", "search": "",
          "note": "Original Adafruit CAD, MIT licensed; downloads on first placement"}

#: Suffixes OpenCascade reads as exact solids. Everything else voxelises, and
#: so does one of these when OCC is absent — see `_geometry`.
_EXACT_SUFFIXES = ("step", "stp", "iges", "igs")


def _bytes_env(name: str, default_mb: int) -> int:
    try:
        return int(float(os.getenv(name, "") or default_mb) * 1024 * 1024)
    except ValueError:
        return default_mb * 1024 * 1024


#: How much downloaded CAD may sit on disk.
#:
#: The whole pinned catalog is 866 MB — 484 files averaging 1.8 MB, the largest
#: 57 MB — so an instance that places enough parts fills its disk without a
#: bound. `services/blobs` bounds *bytes* rather than an entry count for exactly
#: this shape of workload, and a file count would be no budget at all when the
#: files are two orders of magnitude apart in size.
#:
#: Eviction is always safe here, which is what makes a budget the right answer
#: rather than a refusal: every file is re-downloadable and re-verified against
#: the pinned checksum, so a deleted one costs a fetch and never correctness —
#: the same property that lets `blobs` treat a missing blob as recoverable.
CACHE_BYTES = _bytes_env("FORGE_ADAFRUIT_CACHE_MB", 256)


@lru_cache(maxsize=1)
def manifest():
    return json.loads((Path(__file__).parent / "data" / "adafruit.json").read_text())


@lru_cache(maxsize=1)
def _index():
    return {row["key"]: row for row in manifest()["parts"]}


def by_key(key):
    return _index().get(key)


def _geometry(path):
    """What placing this row will actually produce — not what its suffix implies.

    `import_service.import_document` is the authority and says so itself:
    "STEP/IGES import exactly when OpenCascade is present; everything else — and
    a STEP file OCC cannot read — goes through the voxel path." OCP is optional
    here, so reading the suffix alone told all 480 STEP rows they were `exact`
    on a deployment with no exact kernel at all — and `geometry` is the field
    that decides whether a caller can address, fillet or measure a face.
    `supplier_parts` already derives it from what the import produced rather
    than from a filename.

    With OCC present this is still a prediction, because a STEP file OCC cannot
    read falls back to voxels. But it is then wrong for one file rather than for
    the entire catalog, and the placed document is the authority either way.
    """
    from app.cad_kernel.evaluator import brep

    suffix = path.lower().rsplit(".", 1)[-1]
    if suffix in _EXACT_SUFFIXES:
        return "exact" if brep.AVAILABLE else "mesh"
    return "mesh"


def describe(row):
    revision = manifest()["revision"]
    geometry = _geometry(row["path"])
    return {"key": row["key"], "designation": row["designation"],
            "category": row["category"], "standard": "Adafruit", "material_id": "",
            "fidelity": "supplier", "geometry": geometry,
            "size_mm": None, "source": "adafruit", "manufacturer": "Adafruit",
            "notes": "Original manufacturer geometry. Dimensions measured on import; "
                     "mass not supplied."
                     + ("" if geometry == "exact" else
                        " Imports as a mesh on this deployment, so its faces and "
                        "edges are not addressable."),
            "supplier_search": f"https://github.com/adafruit/Adafruit_CAD_Parts/blob/{revision}/{quote(row['path'], safe='/')}",
            "license": "MIT", "download_on_place": True, "removable": False}


def _verified(data, row):
    return len(data) == row["size"] and hashlib.sha1(
        b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == row["sha"]


def _sweep(cache, keep):
    """Evict least-recently-used downloads until the cache is inside its budget.

    `keep` is the file just written — it is what the caller is about to import,
    and a sweep that evicted it would re-download the same bytes on the next
    placement of the same part. A single file larger than the whole budget is
    kept for the same reason `blobs.MemoryStore.put` keeps an oversized blob:
    refusing it would turn a 57 MB model into a part that can never be placed.

    In-flight temporaries are skipped by name. Another thread may be writing one,
    and unlinking it would fail that download rather than free anything durable.
    """
    entries = []
    try:
        for entry in cache.iterdir():
            if not entry.is_file() or entry.name.startswith("download-"):
                continue
            try:
                info = entry.stat()
            except OSError:       # vanished under us; nothing to account for
                continue
            entries.append((info.st_mtime, info.st_size, entry))
    except OSError:
        return

    total = sum(size for _, size, _ in entries)
    for _, size, entry in sorted(entries, key=lambda row: row[0]):
        if total <= CACHE_BYTES:
            break
        if entry == keep:
            continue
        try:
            entry.unlink()
        except OSError:
            continue
        total -= size


def download(row):
    from app.db import DATA_DIR
    cache = DATA_DIR / "adafruit"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / row["sha"]
    if path.exists():
        data = path.read_bytes()
        if _verified(data, row):
            # Touch it: the sweep evicts by mtime, so without this a part placed
            # every day ages out behind one placed once, which is the difference
            # between an LRU and a FIFO.
            try:
                os.utime(path, None)
            except OSError:
                pass
            return data
    # Only a packaged catalog entry constructs the URL. Callers cannot supply
    # arbitrary URLs, redirects or file paths through the placement operation.
    url = ("https://raw.githubusercontent.com/adafruit/Adafruit_CAD_Parts/"
           f"{manifest()['revision']}/{quote(row['path'], safe='/')}")
    try:
        with httpx.stream("GET", url, timeout=45.0, follow_redirects=False) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > row["size"]:
                    raise ValueError("Adafruit download exceeded its recorded size")
        data = bytes(data)
        if not _verified(data, row):
            raise ValueError("Adafruit file did not match the pinned catalog checksum")
    except httpx.HTTPError as exc:
        raise ValueError("Adafruit download unavailable. Check the server connection and try Place again.") from exc
    fd, temporary = tempfile.mkstemp(dir=cache, prefix="download-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    _sweep(cache, path)
    return data


def document_for_key(key, owner_id=None):
    from app.services import document_service as docs, import_service, standard_parts
    row = by_key(key)
    if row is None:
        return None
    # Reuse the application's per-key serialization and account-scoped lookup.
    with docs.writing(f"catalog:{owner_id}:{key}"):
        found = standard_parts._existing(key, owner_id)
        if found and docs.get_document(found) is not None:
            return found
        doc = import_service.import_document(
            filename=row["path"], data=download(row), name=row["designation"],
            owner_id=owner_id, units="auto", resolution=64)
        doc.catalog_ref = key
        doc.provenance = "imported"
        docs._persist(doc)
        return doc.id
