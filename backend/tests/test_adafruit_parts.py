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
