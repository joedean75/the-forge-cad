"""The pre-production hardening pass.

Each test here is a hole that was open, written so it stays shut. They are
grouped by what an attacker gets, not by which file changed.
"""

import os

import pytest
from fastapi.testclient import TestClient

from main import app
from app import body_limit, rate_limit
from app.services import accounts, caller, document_service as docs, persistence

client = TestClient(app)


# ------------------------------------------------------- request body size --

def test_an_oversized_json_body_is_refused_before_it_is_buffered():
    """`POST /op` buffered whatever it was handed. The operation surface is
    reachable by anyone who can open a document, so an unbounded body is remote
    memory exhaustion rather than untidiness."""
    doc = client.post("/api/documents", json={"name": "limit"}).json()
    oversized = "x" * (body_limit.DEFAULT_MAX_BYTES + 1024)
    resp = client.post(f"/api/documents/{doc['id']}/op",
                       json={"op": "list_features", "args": {"pad": oversized}})
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == "BAD_ARG"
    assert "too large" in resp.json()["detail"]["message"]


def test_a_normal_body_is_untouched():
    doc = client.post("/api/documents", json={"name": "limit"}).json()
    resp = client.post(f"/api/documents/{doc['id']}/op",
                       json={"op": "list_features", "args": {}})
    assert resp.status_code == 200


def test_uploads_get_the_larger_cap_and_json_routes_do_not():
    """A STEP import is legitimately tens of MB and an operation call is not.
    One number for both either refuses real files or leaves the JSON surface
    open."""
    assert body_limit.limit_for("/api/documents/import") == body_limit.UPLOAD_MAX_BYTES
    assert body_limit.limit_for("/api/documents/meta/supplier-parts") \
        == body_limit.UPLOAD_MAX_BYTES
    assert body_limit.limit_for("/api/documents/x/op") == body_limit.DEFAULT_MAX_BYTES
    assert body_limit.DEFAULT_MAX_BYTES < body_limit.UPLOAD_MAX_BYTES


def test_the_cap_holds_without_a_content_length_header():
    """`Transfer-Encoding: chunked` carries no length, so the header check alone
    is bypassable by any client willing to stream."""
    doc = client.post("/api/documents", json={"name": "limit"}).json()
    payload = b'{"op":"list_features","args":{"pad":"' \
        + b"x" * (body_limit.DEFAULT_MAX_BYTES + 1024) + b'"}}'

    def chunks():
        for i in range(0, len(payload), 65536):
            yield payload[i:i + 65536]

    resp = client.post(f"/api/documents/{doc['id']}/op", content=chunks(),
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 413


# ------------------------------------------------------------ rate limiting --

def test_the_operation_endpoint_is_rate_limited_per_route_not_per_document():
    """Keying a limit on the concrete path made it per-document: a caller only
    had to walk their own documents to get a fresh bucket every time."""
    rate_limit.reset()
    prev = rate_limit._ENABLED
    rate_limit._ENABLED = True
    try:
        a = client.post("/api/documents", json={"name": "rl-a"}).json()
        b = client.post("/api/documents", json={"name": "rl-b"}).json()
        codes = []
        # Alternate documents; a per-path bucket would never trip.
        for i in range(340):
            doc_id = (a if i % 2 else b)["id"]
            codes.append(client.post(f"/api/documents/{doc_id}/op",
                                     json={"op": "list_features", "args": {}}).status_code)
        assert 429 in codes, "the operation endpoint accepted 340 calls unthrottled"
        # And it is a *limit*, not a wall: real traffic gets through first.
        assert codes[:200] == [200] * 200
    finally:
        rate_limit._ENABLED = prev
        rate_limit.reset()


def test_the_limiter_reads_the_route_template():
    class _Req:
        def __init__(self, path):
            self.scope = {"route": type("R", (), {"path": "/api/documents/{doc_id}/op"})()}
            self.url = type("U", (), {"path": path})()
    assert rate_limit._route_key(_Req("/api/documents/abc/op")) \
        == "/api/documents/{doc_id}/op"


# ------------------------------------------------------ cross-tenant leaks --

def _account(email):
    try:
        return accounts.create_account(email, "password123")
    except accounts.AuthError:
        return accounts.authenticate_password(email, "password123")


def _auth(acc):
    return {"Authorization": f"Bearer {accounts.issue_token(acc)}"}


def test_a_collaborator_does_not_see_the_owners_imported_parts():
    """`list_standard_parts` returns half the caller's own catalog. Scoped to
    the *document* owner it disclosed the designations, manufacturers and part
    numbers of everything the owner had imported to anyone they shared a part
    with."""
    from app.services import supplier_parts

    alice, bob = _account("leak-alice@forge.test"), _account("leak-bob@forge.test")
    secret = client.post("/api/documents", json={"name": "alice part"},
                         headers=_auth(alice)).json()
    supplier_parts.record(document_id=secret["id"], owner_id=alice.id,
                          designation="CONFIDENTIAL X9 actuator",
                          manufacturer="SkunkWorks", part_number="X9-001",
                          source="manufacturer", category="hardware")

    shared = client.post("/api/documents", json={"name": "shared"},
                         headers=_auth(alice)).json()
    add = client.post(f"/api/documents/{shared['id']}/collaborators",
                      json={"email": bob.email, "role": "editor"}, headers=_auth(alice))
    assert add.status_code == 200, add.text

    seen = client.post(f"/api/documents/{shared['id']}/op?envelope=1",
                       json={"op": "list_standard_parts", "args": {"limit": 250}},
                       headers=_auth(bob)).json()
    assert seen["ok"], seen
    names = " ".join(p["designation"] for p in seen["result"]["parts"])
    assert "CONFIDENTIAL" not in names
    assert "SkunkWorks" not in str(seen["result"]["parts"])
    # Alice still sees her own on the same document.
    mine = client.post(f"/api/documents/{shared['id']}/op?envelope=1",
                       json={"op": "list_standard_parts", "args": {"limit": 250}},
                       headers=_auth(alice)).json()
    assert "CONFIDENTIAL X9 actuator" in \
        " ".join(p["designation"] for p in mine["result"]["parts"])


def test_a_supplier_row_never_resolves_for_another_account():
    from app.services import standard_parts, supplier_parts

    alice, bob = _account("row-alice@forge.test"), _account("row-bob@forge.test")
    doc = client.post("/api/documents", json={"name": "alice part"},
                      headers=_auth(alice)).json()
    row = supplier_parts.record(document_id=doc["id"], owner_id=alice.id,
                                designation="private widget", source="other")
    assert supplier_parts.by_key(row["key"], bob.id) is None
    assert standard_parts.document_for_key(row["key"], owner_id=bob.id) is None
    assert standard_parts.document_for_key(row["key"], owner_id=alice.id) == doc["id"]


def test_the_acting_account_is_scoped_and_restored():
    assert caller.acting_account_id() is None
    with caller.acting_as("a"):
        assert caller.acting_account_id() == "a"
        with caller.acting_as("b"):
            assert caller.acting_account_id() == "b"
        assert caller.acting_account_id() == "a"
    assert caller.acting_account_id() is None


# ------------------------------------------------------------- SQL identity --

def test_a_json_field_name_cannot_carry_sql():
    """`field` names a JSON path, so it is interpolated rather than bound. Every
    caller passes a literal; without this check the first one that forwards a
    request field is an injection. The payload below reached the statement."""
    with pytest.raises(ValueError):
        persistence._rows_by_json_field("document", "catalog_ref') OR 1=1--", "x")
    with pytest.raises(ValueError):
        persistence._rows_by_json_field("document", "a.b", "x")
    # The real callers still work.
    assert persistence._rows_by_json_field("document", "catalog_ref", "nope") == []


def test_every_json_field_caller_passes_a_literal():
    """The guard above turns a dynamic field into a 500. This is the check that
    nothing is relying on one."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    dynamic = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name != "_rows_by_json_field" or len(node.args) < 2:
                continue
            if not isinstance(node.args[1], ast.Constant):
                dynamic.append(f"{path.name}:{node.lineno}")
    assert not dynamic, f"non-literal JSON field name: {dynamic}"


# --------------------------------------------------------- production boot --

def test_enforced_auth_without_a_signing_secret_refuses_to_boot():
    """Not a warning. The fallback secret is per-*process*, so with more than one
    worker a session is valid or 401 depending on which one answers — flakiness
    nobody diagnoses from a log line at boot."""
    import main

    # Save and restore the two keys this touches. `os.environ.clear()` was the
    # first attempt and it takes the whole suite down with it: `DATABASE_URL`
    # and `FORGE_DATA_DIR` are set by conftest before the app imports, and any
    # later test that re-opens the datastore reads them again — so a dozen
    # persistence tests failed depending on the order pytest happened to pick.
    prev = {k: os.environ.get(k)
            for k in ("FORGE_JWT_SECRET", "FORGE_ALLOW_INSECURE")}
    try:
        os.environ.pop("FORGE_JWT_SECRET", None)
        os.environ.pop("FORGE_ALLOW_INSECURE", None)
        import app.security as security

        security.AUTH_REQUIRED = True
        with pytest.raises(RuntimeError, match="FORGE_JWT_SECRET"):
            main._check_production_posture()

        # …and the documented escape hatch works.
        os.environ["FORGE_ALLOW_INSECURE"] = "1"
        main._check_production_posture()

        # A configured secret is simply fine.
        del os.environ["FORGE_ALLOW_INSECURE"]
        os.environ["FORGE_JWT_SECRET"] = "0" * 64
        main._check_production_posture()
    finally:
        security.AUTH_REQUIRED = False
        for key, value in prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ------------------------------------------------------------------- XSS --

def _drawable_document():
    doc = client.post("/api/documents", json={"name": "xss"}).json()
    client.post(f"/api/documents/{doc['id']}/op",
                json={"op": "add_primitive", "args": {"shape": "box", "params": {
                    "size": {"x": .05, "y": .03, "z": .01}}}})
    return doc["id"]


def test_a_section_label_cannot_inject_markup_into_the_drawing():
    """Stored XSS, and the worst-placed one in the product.

    `DrawingSpec.sections[].label` is caller-supplied and was interpolated raw
    into the view label — the one text node in the whole writer that skipped
    `_esc`. `MakePanel` renders the SVG with `dangerouslySetInnerHTML`; a
    `<script>` inserted that way does not run, but `<image onerror=…>` does, and
    the session JWT is in localStorage. An attacker needs only edit access to a
    document the victim later opens.
    """
    doc_id = _drawable_document()
    payload = '</text><image href=x onerror="alert(1)"/><text>'
    svg = client.post(f"/api/documents/{doc_id}/drawing",
                      json={"views": ["front"],
                            "sections": [{"plane": "xz", "label": payload}]}).json()["svg"]
    assert payload not in svg
    assert "<image" not in svg
    assert "&lt;/text&gt;" in svg          # escaped, not stripped — still legible
    # No raw markup may survive anywhere a label reaches.
    assert 'onerror="alert' not in svg


def test_the_escaper_covers_attributes_as_well_as_text():
    from app.services.drawing import _esc

    assert _esc('<a href="x">') == "&lt;a href=&quot;x&quot;&gt;"
    assert _esc("it's & so") == "it&#39;s &amp; so"


def test_a_javascript_link_is_not_stored_on_a_supplier_part():
    """`source_url` is typed by the importer and comes back out as an `href`."""
    from app.services import supplier_parts

    doc = client.post("/api/documents", json={"name": "link"}).json()
    row = supplier_parts.record(document_id=doc["id"], owner_id=None,
                                designation="widget", source="other",
                                source_url="javascript:alert(document.cookie)")
    assert row["source_url"] == ""
    assert "javascript:" not in supplier_parts.describe(row)["supplier_search"]

    ok = supplier_parts.record(document_id=doc["id"], owner_id=None,
                               designation="widget2", source="other",
                               source_url="https://example.com/part")
    assert ok["source_url"] == "https://example.com/part"
