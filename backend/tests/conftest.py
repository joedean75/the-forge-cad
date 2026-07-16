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
