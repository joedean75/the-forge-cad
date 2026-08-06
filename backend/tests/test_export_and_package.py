"""Getting the part out of the building.

The product's thesis is model → make, and the last step is a file somebody else
can open. The catalog could produce none: `GET /{id}/step` existed, so a human
could export and an agent could not.

Three claims are tested here — the export is addressable rather than inline, the
PDF is a real PDF of the same sheet the SVG shows, and a package is pinned to a
*revision* rather than to whatever the document happens to be right now.
"""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from main import app
from app.cad_kernel.io import pdf as pdf_io
from app.services import artifacts, document_service as docs, drawing, release_package

client = TestClient(app)


def _doc(prompt: str = "box 0.1x0.06x0.02 m") -> str:
    resp = client.post("/api/documents", json={"prompt": prompt, "resolution": 64})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _op(doc_id: str, op: str, **args) -> dict:
    resp = client.post(f"/api/documents/{doc_id}/op?envelope=1",
                       json={"op": op, "args": args})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ok(doc_id: str, op: str, **args) -> dict:
    env = _op(doc_id, op, **args)
    assert env["ok"], env["error"]
    return env["result"]


# --- export is in the catalog ----------------------------------------------

@pytest.mark.parametrize("fmt", sorted(artifacts.FORMATS))
def test_every_declared_format_writes_a_file(fmt):
    doc_id = _doc()
    result = _ok(doc_id, "export", format=fmt)
    assert result["bytes"] > 0
    assert result["url"].endswith(result["hash"])
    assert result["filename"].endswith(f".{artifacts.FORMATS[fmt].extension}")


def test_the_result_is_a_url_not_the_bytes():
    """Inlining an export is the mistake P1 spent a sprint undoing for the mesh,
    and a STEP body is larger than the mesh was."""
    result = _ok(_doc(), "export", format="step")
    assert "/artifact/" in result["url"]
    # Nothing in the envelope may carry the file itself.
    assert all(not isinstance(v, (bytes, bytearray)) for v in result.values())
    assert len(json.dumps(result)) < 400


def test_exporting_the_same_body_twice_returns_the_same_url():
    """Content addressing, so a retry is free and a package is cheap to rebuild."""
    doc_id = _doc()
    first = _ok(doc_id, "export", format="stl")
    second = _ok(doc_id, "export", format="stl")
    assert first["url"] == second["url"]


def test_an_edit_moves_the_url():
    doc_id = _doc()
    before = _ok(doc_id, "export", format="stl")["hash"]
    root = client.get(f"/api/documents/{doc_id}").json()["feature_tree"]["root"]["id"]
    _ok(doc_id, "set_feature_params", feature_id=root,
        params={"size": {"x": 0.25, "y": 0.06, "z": 0.02}})
    assert _ok(doc_id, "export", format="stl")["hash"] != before


def test_an_unknown_format_is_refused_by_the_schema():
    env = _op(_doc(), "export", format="parasolid")
    assert env["error"]["code"] == "BAD_ARG"
    assert env["error"]["arg"] == "format"


def test_the_artifact_endpoint_is_immutable_and_named():
    doc_id = _doc()
    result = _ok(doc_id, "export", format="step")
    resp = client.get(result["url"])
    assert resp.status_code == 200
    assert "immutable" in resp.headers["cache-control"]
    assert "private" in resp.headers["cache-control"]
    assert result["filename"] in resp.headers["content-disposition"]
    # STEP is written in millimetres with an explicit unit context by both
    # writers — invisible until somebody else opens the file.
    assert "MILLI" in resp.text


def test_a_hash_this_document_does_not_have_is_a_404():
    assert client.get(f"/api/documents/{_doc()}/artifact/deadbeefdeadbeef").status_code == 404


# --- the PDF ---------------------------------------------------------------

def test_the_drawing_renders_as_a_real_pdf():
    payload = drawing.render_pdf(_doc())
    assert payload.startswith(b"%PDF-1.4")
    assert payload.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in payload and b"/Type /Page" in payload
    # One page, and a cross-reference table that names every object in it.
    assert payload.count(b"/Type /Page\n") + payload.count(b"/Type /Page ") == 1


def test_the_pdf_carries_the_text_the_dxf_deliberately_drops():
    """A DXF goes into another tool that re-dimensions anyway; a PDF is read by
    a person, and a sheet with no figures on it says nothing."""
    import re
    import zlib

    payload = drawing.render_pdf(_doc())
    # One content stream, and the compressed bytes may themselves contain
    # b"stream\n" — so this matches the object framing, not the token.
    body = re.search(rb"/FlateDecode >>\nstream\n(.*)\nendstream", payload, re.S)
    content = zlib.decompress(body.group(1))
    assert b"/F1" in content and b"Tj" in content
    assert b"(FRONT)" in content
    assert b"(100.0)" in content          # the overall dimension figure


def test_the_stylesheet_is_parsed_rather_than_restated():
    """A dash pattern written twice will one day be dashed in the SVG and solid
    in the PDF, and nobody notices until a shop reads a hidden line as an edge."""
    styles = drawing._class_styles()
    assert styles["hidden"]["stroke-dasharray"] == "2 1.4"
    assert styles["outline"]["stroke-width"] == "0.5"


def test_a_named_colour_is_not_silently_black():
    assert pdf_io._rgb("white") == (1.0, 1.0, 1.0)
    assert pdf_io._rgb("#1a1f27")[0] == pytest.approx(26 / 255)


def test_a_pdf_string_escapes_what_would_end_it():
    assert pdf_io._pdf_string("a(b)c\\d") == rb"(a\(b\)c\\d)"


# --- the release package ---------------------------------------------------

def test_a_package_needs_a_released_revision():
    """A package pinned to 'now' is not a release, it is a screenshot of a
    moving target."""
    env = _op(_doc(), "release_package")
    assert env["error"]["code"] == "PRECONDITION"
    assert "release_revision" in env["error"]["message"]


def test_an_unknown_revision_letter_lists_the_ones_that_exist():
    doc_id = _doc()
    _ok(doc_id, "release_revision", notes="first")
    env = _op(doc_id, "release_package", revision="Z")
    assert env["error"]["code"] == "BAD_ARG"
    assert "A" in env["error"]["message"]


def test_the_package_contains_everything_a_supplier_needs():
    doc_id = _doc()
    _ok(doc_id, "set_material", material_id="al-6061")
    _ok(doc_id, "set_spec", requirements=[
        {"kind": "bounding_box", "axis": "x", "value": 100, "tolerance": 1},
        {"kind": "watertight"},
    ])
    _ok(doc_id, "release_revision", notes="first release")

    built = _ok(doc_id, "release_package")
    assert built["revision"] == "A"

    archive = zipfile.ZipFile(io.BytesIO(client.get(built["url"]).content))
    names = archive.namelist()
    assert "manifest.json" in names and "bom.csv" in names and "spec-report.json" in names
    assert any(n.endswith(".step") for n in names)
    assert any(n.endswith(".pdf") for n in names)
    assert any(n.endswith(".dxf") for n in names)

    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["revision"] == "A"
    assert manifest["material"] == "Aluminum 6061-T6"
    # The score is the row that says the part is not merely a valid solid but
    # the part that was asked for.
    assert manifest["spec_score"] == 1.0
    assert json.loads(archive.read("spec-report.json"))["passed"] == 2


def test_a_package_without_a_spec_says_so_rather_than_scoring_nothing():
    doc_id = _doc()
    _ok(doc_id, "release_revision")
    built = _ok(doc_id, "release_package")
    archive = zipfile.ZipFile(io.BytesIO(client.get(built["url"]).content))
    report = json.loads(archive.read("spec-report.json"))
    assert report["score"] is None
    assert "no design spec" in report["unavailable"]


def test_the_package_is_of_the_revision_not_of_the_current_document():
    """The trap this whole design exists for: edit the part after releasing and
    the package must still be the released body."""
    doc_id = _doc()
    _ok(doc_id, "release_revision", notes="A")
    released_bytes = _ok(doc_id, "export", format="stl")["bytes"]

    root = client.get(f"/api/documents/{doc_id}").json()["feature_tree"]["root"]["id"]
    _ok(doc_id, "set_feature_params", feature_id=root,
        params={"size": {"x": 0.4, "y": 0.06, "z": 0.02}})

    built = _ok(doc_id, "release_package")
    manifest = built["manifest"]
    assert manifest["revision"] == "A"
    # Said explicitly, because a document that has moved on is the normal case.
    assert manifest["document_matches_revision"] is False
    assert manifest["bbox_mm"]["x"] == pytest.approx(100.0, abs=1.0)

    archive = zipfile.ZipFile(io.BytesIO(client.get(built["url"]).content))
    stl = next(n for n in archive.namelist() if n.endswith(".stl"))
    assert len(archive.read(stl)) == released_bytes


def test_building_a_package_leaves_no_phantom_document():
    """The revision is materialised as a document so every reader downstream
    works unchanged. It must not reach the datastore or the library."""
    doc_id = _doc()
    _ok(doc_id, "release_revision")
    before = {d.id for d in docs.list_documents()}
    _ok(doc_id, "release_package")
    assert {d.id for d in docs.list_documents()} == before
    assert not [k for k in docs.DOCUMENTS if "@" in k]
    assert not docs.EPHEMERAL


def test_a_format_that_cannot_be_written_is_recorded_not_fatal(monkeypatch):
    """A body the drawing pipeline cannot trace still has a STEP file worth
    sending."""
    doc_id = _doc()
    _ok(doc_id, "release_revision")

    real = artifacts.build

    def flaky(document_id, fmt, **kw):
        if fmt == "pdf":
            raise RuntimeError("no sheet today")
        return real(document_id, fmt, **kw)

    monkeypatch.setattr(artifacts, "build", flaky)
    manifest = release_package.build(doc_id)["manifest"]
    assert [m["format"] for m in manifest["unavailable"]] == ["pdf"]
    assert "step" in [c["format"] for c in manifest["contents"]]


def test_the_package_job_polls_to_done():
    doc_id = _doc()
    _ok(doc_id, "release_revision")
    started = client.post(f"/api/documents/{doc_id}/package", json={}).json()
    # TestClient runs background tasks before returning, so the job is already
    # resolved; the contract under test is the shape, not the timing.
    job = client.get(f"/api/documents/{doc_id}/package/{started['id']}").json()
    assert job["status"] == "done", job["message"]
    assert job["result"]["revision"] == "A"
    assert client.get(f"/api/documents/{doc_id}/package/nope").status_code == 404


def test_an_order_carries_the_release_package():
    """'Get it made' used to send a process, a quantity and a document id — a
    supplier receiving that has a price and no geometry."""
    doc_id = _doc()
    _ok(doc_id, "set_material", material_id="al-6061")
    _ok(doc_id, "release_revision", notes="A")
    quoted = client.post(f"/api/documents/{doc_id}/quotes", json={"quantity": 1}).json()
    process = quoted["quotes"][0]["process"]

    order = client.post(f"/api/documents/{doc_id}/orders",
                        json={"process": process, "quantity": 1}).json()
    assert order["package"]["revision"] == "A"
    assert "step" in order["package"]["contents"]
    assert client.get(order["package"]["url"]).status_code == 200


def test_an_order_on_an_unreleased_part_says_why_it_has_no_package():
    """A prototype is a real thing to order; refusing it would invent a process
    requirement the shop never asked for."""
    doc_id = _doc()
    quoted = client.post(f"/api/documents/{doc_id}/quotes", json={"quantity": 1}).json()
    order = client.post(f"/api/documents/{doc_id}/orders",
                        json={"process": quoted["quotes"][0]["process"], "quantity": 1}).json()
    assert order["package"] is None
    assert "released revision" in order["package_note"]
