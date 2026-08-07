"""Shared test fixtures.

Tests run in the credential-free mode (rule-based parser) so geometry generation
is deterministic and offline — no live LLM calls, even though a real key may be
present in the environment/.env for the running app.
"""

# Point the datastore at an isolated throwaway SQLite file BEFORE any app module
# imports `app.db` (which reads DATABASE_URL at import time), so the suite never
# touches the developer's real database.
import os
import tempfile

_TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "forge_pytest.db")
for _suffix in ("", "-wal", "-shm"):
    try:
        os.remove(_TEST_DB_PATH + _suffix)
    except FileNotFoundError:
        pass
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
# Point legacy-JSON migration at an empty directory: without this, any lifespan-
# managed TestClient imports the developer's real backend/data/*.json (gigabytes,
# and its owner_id=None documents leak into every tenant's listing).
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="forge_pytest_data_")
os.environ["FORGE_DATA_DIR"] = _TEST_DATA_DIR
# Rate limiting is exercised explicitly in test_security.py; keep it off for the
# rest of the suite, which signs up/logs in many times from one client IP.
os.environ.setdefault("FORGE_RATE_LIMIT", "0")

import pytest

from app.db import init_db
from app.services import llm_client

init_db()


@pytest.fixture(autouse=True)
def _disable_llm(monkeypatch):
    monkeypatch.setattr(llm_client, "LLM_API_KEY", None)


def base_box(client, doc_id, size=(0.1, 0.1, 0.1), center=(0.0, 0.0, 0.0)) -> str:
    """Give a blank document a base box and return that box's feature id.

    A blank document is three datum planes and no body, so a test that wants
    something to cut into has to make it. Tests used to resize the template's
    100 mm placeholder — the starting block the datum-plane template replaced —
    which meant `feature_tree["root"]["id"]` happened to name a box. It now
    names the union the planes sit in, and resizing that does nothing at all.
    """
    resp = client.post(f"/api/documents/{doc_id}/op", json={
        "op": "add_primitive",
        "args": {"shape": "box", "params": {
            "size": {"x": size[0], "y": size[1], "z": size[2]},
            "center": {"x": center[0], "y": center[1], "z": center[2]}}}})
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]["feature_id"]


def body_id(tree) -> str:
    """The id of the first feature in `tree` that makes material.

    `feature_tree["root"]["id"]` used to be exactly this, because a blank
    document *was* a box. The root is now the union the datum planes sit in, and
    setting a size on it does nothing — silently, which is why every test that
    relied on the old shape needed this rather than a rename.
    """
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services.feature_ops import BODYLESS_TYPES, is_boolean

    for node in walk_nodes(tree):
        if node.get("type") not in BODYLESS_TYPES and not is_boolean(node):
            return node["id"]
    raise AssertionError("this tree has no body")

