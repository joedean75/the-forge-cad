import hashlib

import httpx
import pytest

from app.services import adafruit_parts as ada, standard_parts, document_service as docs


def test_catalog_is_local_unique_and_paginated(monkeypatch):
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: pytest.fail("listing must not download"))
    rows = ada.manifest()["parts"]
    assert len(rows) == 484
    assert len({r["key"] for r in rows}) == len(rows)
    first = standard_parts.listing("catalog-reader", source="adafruit", limit=60)
    rest = standard_parts.listing("catalog-reader", source="adafruit", limit=60, offset=60)
    assert first["count"] == 484
    assert not {r["key"] for r in first["parts"]} & {r["key"] for r in rest["parts"]}
    assert all(r["source"] == "adafruit" and r["size_mm"] is None for r in first["parts"])
    assert standard_parts.listing("catalog-reader", source="adafruit", search="ESP32")["count"] > 0
    assert standard_parts.listing("catalog-reader", source="adafruit", offset=484)["parts"] == []


def test_download_verifies_and_reuses_cache(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    data = b"sample CAD bytes"
    sha = hashlib.sha1(b"blob "+str(len(data)).encode()+b"\0"+data).hexdigest()
    row = {"path": "test/test.step", "sha": sha, "size": len(data)}
    calls = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_bytes(self): return iter([data])

    def stream(*args, **kw):
        calls.append((args, kw))
        return Response()

    monkeypatch.setattr(httpx, "stream", stream)
    assert ada.download(row) == data
    assert ada.download(row) == data
    assert len(calls) == 1 and calls[0][1]["follow_redirects"] is False
    (tmp_path / "adafruit" / sha).write_bytes(b"corrupt")
    assert ada.download(row) == data
    assert len(calls) == 2
    with pytest.raises(ValueError, match="checksum"):
        ada.download({**row, "sha": "0"*40})


def test_repeated_placement_reuses_document_and_keeps_accounts_separate(monkeypatch):
    from app.cad_kernel.core.feature_tree import FeatureTree
    from app.cad_kernel.evaluator import brep
    from app.cad_kernel.io.step_brep import write_step_brep
    feature = FeatureTree.from_dict({"root": {"type": "BoxFeature", "params": {
        "size": {"x": .02, "y": .01, "z": .005}, "center": {"x": 0, "y": 0, "z": 0}}}})
    data = write_step_brep(brep.build_strict(feature.root)).encode()
    monkeypatch.setattr(ada, "download", lambda row: data)
    key = ada.manifest()["parts"][0]["key"]
    first = standard_parts.document_for_key(key, "ada-alice")
    assert standard_parts.document_for_key(key, "ada-alice") == first
    second = standard_parts.document_for_key(key, "ada-bob")
    assert second != first
    assert docs.get_document(first).owner_id == "ada-alice"
    assert docs.get_document(first).catalog_ref == key
    assert docs.get_document(first).derived.bbox is not None
    assert standard_parts.category_of(key, "ada-alice") == ada.by_key(key)["category"]
    from app.services import assembly_service, assembly_operations
    assembly = assembly_service.create_assembly(name="Adafruit test", owner_id="ada-alice")
    result = assembly_operations._insert_standard_part(assembly.id, {"key": key, "quantity": 2})
    assert result["part_id"] == first
    assert len(result["component_ids"]) == 2
    assert result["fidelity"] == "supplier"
    with pytest.raises(ValueError):
        standard_parts.document_for_key(key, "ada-alice", {"size": 3})


def test_unknown_key_does_not_fetch(monkeypatch):
    monkeypatch.setattr(ada, "download", lambda row: pytest.fail("unknown key downloaded"))
    assert standard_parts.document_for_key("adafruit:unknown", "reader") is None


def test_geometry_reports_what_the_import_will_actually_produce(monkeypatch):
    """`geometry` decides whether a caller can address, fillet or measure a face,
    so it may not be read off the filename.

    `import_service` voxelises a STEP when OpenCascade is absent, and OCP is an
    optional dependency — so inferring from the suffix told all 480 STEP rows
    they were `exact` on a deployment that has no exact kernel at all.
    """
    from app.cad_kernel.evaluator import brep

    step = next(r for r in ada.manifest()["parts"] if r["path"].lower().endswith(".step"))
    stl = next(r for r in ada.manifest()["parts"] if r["path"].lower().endswith(".stl"))

    monkeypatch.setattr(brep, "AVAILABLE", True)
    assert ada.describe(step)["geometry"] == "exact"
    assert ada.describe(stl)["geometry"] == "mesh"

    # No exact kernel: the STEP voxelises like everything else, and the row says
    # so rather than promising faces that will not exist.
    monkeypatch.setattr(brep, "AVAILABLE", False)
    described = ada.describe(step)
    assert described["geometry"] == "mesh"
    assert "not addressable" in described["notes"]


def test_every_listed_row_declares_a_geometry_the_type_allows():
    """`shared/types.ts` types it as `'exact' | 'mesh'`, and a field present on
    only some rows — or carrying a third value — is one a caller reads wrong."""
    rows = standard_parts.listing("catalog-reader", limit=1000)["parts"]
    assert rows
    assert {r["geometry"] for r in rows} <= {"exact", "mesh"}


def test_the_cache_stays_inside_its_byte_budget(tmp_path, monkeypatch):
    """The whole pinned catalog is 866 MB. Without a bound, an instance that
    places enough parts fills its disk — and every file here is re-downloadable
    and re-verified, so evicting one costs a fetch and never correctness."""
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ada, "CACHE_BYTES", 2048)

    def payload(n):
        body = bytes([n % 251]) * 1024
        sha = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
        return {"path": f"test/part{n}.step", "sha": sha, "size": len(body)}, body

    class Response:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_bytes(self): return iter([self.body])

    current = {}
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: Response(current["body"]))

    rows = []
    for n in range(6):
        row, body = payload(n)
        rows.append(row)
        current["body"] = body
        assert ada.download(row) == body

    cache = tmp_path / "adafruit"
    kept = sorted(p.name for p in cache.iterdir() if p.is_file())
    assert sum(p.stat().st_size for p in cache.iterdir()) <= 2048
    # The file just written is never the one evicted — it is what the caller is
    # about to import.
    assert rows[-1]["sha"] in kept
    # And an evicted part is recoverable rather than broken: re-downloading it
    # verifies against the same pinned checksum.
    current["body"] = payload(0)[1]
    assert ada.download(rows[0]) == payload(0)[1]


def test_a_file_larger_than_the_whole_budget_is_still_placeable(tmp_path, monkeypatch):
    """Refusing it would turn a 57 MB model into a part that can never be
    placed — the same call `blobs.MemoryStore.put` makes for an oversized blob."""
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ada, "CACHE_BYTES", 16)

    body = b"x" * 4096
    sha = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
    row = {"path": "test/big.step", "sha": sha, "size": len(body)}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_bytes(self): return iter([body])

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: Response())
    assert ada.download(row) == body
    assert (tmp_path / "adafruit" / sha).exists()
