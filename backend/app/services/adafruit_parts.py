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


@lru_cache(maxsize=1)
def manifest():
    return json.loads((Path(__file__).parent / "data" / "adafruit.json").read_text())


@lru_cache(maxsize=1)
def _index():
    return {row["key"]: row for row in manifest()["parts"]}


def by_key(key):
    return _index().get(key)


def describe(row):
    revision = manifest()["revision"]
    return {"key": row["key"], "designation": row["designation"],
            "category": row["category"], "standard": "Adafruit", "material_id": "",
            "fidelity": "supplier", "geometry": "mesh" if row["path"].lower().endswith(".stl") else "exact",
            "size_mm": None, "source": "adafruit", "manufacturer": "Adafruit",
            "notes": "Original manufacturer geometry. Dimensions measured on import; mass not supplied.",
            "supplier_search": f"https://github.com/adafruit/Adafruit_CAD_Parts/blob/{revision}/{quote(row['path'], safe='/')}",
            "license": "MIT", "download_on_place": True, "removable": False}


def _verified(data, row):
    return len(data) == row["size"] and hashlib.sha1(
        b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == row["sha"]


def download(row):
    from app.db import DATA_DIR
    cache = DATA_DIR / "adafruit"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / row["sha"]
    if path.exists():
        data = path.read_bytes()
        if _verified(data, row):
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
