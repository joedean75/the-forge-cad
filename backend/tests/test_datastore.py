"""Durable datastore guarantees — what file-JSON + in-memory dicts could not do.

Proves the production properties the persistence migration exists to provide:
durability beyond the process, indexed tenant scoping, a lossless legacy import,
and concurrent-write safety.
"""

import json
import threading

from sqlalchemy import create_engine, select

from app import db
from app.services import persistence


def test_managed_postgres_urls_are_normalized_to_psycopg():
    # Render/Heroku/Fly hand out postgres:// or postgresql://; both must be pinned
    # to the installed psycopg v3 dialect, and sqlite must pass through untouched.
    assert db._normalize("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert db._normalize("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert db._normalize("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert db._normalize("sqlite:///x.db") == "sqlite:///x.db"


def test_record_survives_a_fresh_engine():
    # Write through the normal API, then read via a brand-new engine pointed at
    # the same database — data is on disk, not merely in a process dict.
    persistence.save_account("durable-1", {"hello": "world", "n": 42})

    fresh = create_engine(db.DATABASE_URL, future=True)
    with fresh.connect() as conn:
        row = conn.execute(
            select(db.Record.data).where(db.Record.kind == "account", db.Record.id == "durable-1")
        ).first()
    fresh.dispose()

    assert row is not None
    assert json.loads(row[0])["n"] == 42


def test_document_owner_id_is_an_indexed_column():
    # Ownership is a first-class column (indexable / RLS-able), not buried in JSON.
    persistence.save_document("doc-owned", {"owner_id": "acct-xyz", "name": "Part"})
    with db.SessionLocal() as s:
        rec = s.get(db.Record, {"kind": "document", "id": "doc-owned"})
    assert rec.owner_id == "acct-xyz"
    # owner_id carries a DB index so tenant-scoped queries stay fast at scale.
    assert any(col.name == "owner_id" for idx in db.Record.__table__.indexes for col in idx.columns)


def test_migrate_json_files_is_lossless_and_idempotent(tmp_path, monkeypatch):
    # A legacy documents/<id>.json is imported exactly once.
    monkeypatch.setattr(persistence, "DATA_DIR", tmp_path)
    docs_dir = tmp_path / "documents"
    docs_dir.mkdir()
    (docs_dir / "legacy-doc.json").write_text(
        json.dumps({"owner_id": "legacy-owner", "name": "Old Part"})
    )

    first = persistence.migrate_json_files()
    assert first == 1
    loaded = persistence.load_document("legacy-doc")
    assert loaded["name"] == "Old Part"

    # Re-running imports nothing (already present).
    assert persistence.migrate_json_files() == 0


def test_concurrent_writes_do_not_corrupt():
    # Many threads writing at once must all land — the file-JSON layer could lose
    # or truncate records under this; the DB serializes them safely.
    def write(i):
        persistence.save_candidate(f"conc-{i}", {"i": i})

    threads = [threading.Thread(target=write, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(25):
        assert persistence.load_candidate(f"conc-{i}") == {"i": i}
