"""Durable persistence for accounts, documents, candidates and assemblies.

Backed by the SQLAlchemy datastore in `app.db` (SQLite in dev, Postgres in
production via `DATABASE_URL`). The public functions keep the same signatures the
services already call, so swapping the backing store from loose JSON files to a
transactional database required no changes upstream.

Each save is a single committed transaction, so a process crash or concurrent
writer can no longer corrupt or lose a record the way half-written JSON could.
Legacy `backend/data/*/*.json` files are imported once on startup (`migrate_json_files`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from sqlalchemy import delete as sql_delete, select

from app.db import DATA_DIR, Record, SessionLocal, init_db

# Kinds (row namespaces in the `records` table).
_ACCOUNT = "account"
_CANDIDATE = "candidate"
_ASSEMBLY = "assembly"
_DOCUMENT = "document"


# ---------------------------------------------------------------------------
# Generic row CRUD
# ---------------------------------------------------------------------------

def _save(kind: str, rec_id: str, data: dict, owner_id: Optional[str] = None) -> None:
    payload = json.dumps(data)
    with SessionLocal() as s:
        row = s.get(Record, {"kind": kind, "id": rec_id})
        if row is None:
            s.add(Record(kind=kind, id=rec_id, owner_id=owner_id, data=payload))
        else:
            row.data = payload
            row.owner_id = owner_id
        s.commit()


def _load(kind: str, rec_id: str) -> Optional[dict]:
    with SessionLocal() as s:
        row = s.get(Record, {"kind": kind, "id": rec_id})
        return json.loads(row.data) if row else None


def _list(kind: str) -> List[str]:
    with SessionLocal() as s:
        return list(s.scalars(select(Record.id).where(Record.kind == kind)).all())


def _delete(kind: str, rec_id: str) -> bool:
    with SessionLocal() as s:
        result = s.execute(
            sql_delete(Record).where(Record.kind == kind, Record.id == rec_id)
        )
        s.commit()
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def save_account(account_id: str, account: dict) -> None:
    _save(_ACCOUNT, account_id, account)


def load_account(account_id: str) -> Optional[dict]:
    return _load(_ACCOUNT, account_id)


def list_accounts() -> List[str]:
    return _list(_ACCOUNT)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def save_candidate(candidate_id: str, candidate: dict) -> None:
    _save(_CANDIDATE, candidate_id, candidate)


def load_candidate(candidate_id: str) -> Optional[dict]:
    return _load(_CANDIDATE, candidate_id)


def list_candidates() -> List[str]:
    return _list(_CANDIDATE)


# ---------------------------------------------------------------------------
# Assemblies
# ---------------------------------------------------------------------------

def save_assembly(assembly_id: str, assembly: dict) -> None:
    _save(_ASSEMBLY, assembly_id, assembly)


def load_assembly(assembly_id: str) -> Optional[dict]:
    return _load(_ASSEMBLY, assembly_id)


def list_assemblies() -> List[str]:
    return _list(_ASSEMBLY)


# ---------------------------------------------------------------------------
# Documents (owner-scoped)
# ---------------------------------------------------------------------------

def save_document(document_id: str, document: dict) -> None:
    _save(_DOCUMENT, document_id, document, owner_id=document.get("owner_id"))


def load_document(document_id: str) -> Optional[dict]:
    return _load(_DOCUMENT, document_id)


def list_documents() -> List[str]:
    return _list(_DOCUMENT)


def delete_document(document_id: str) -> bool:
    return _delete(_DOCUMENT, document_id)


# ---------------------------------------------------------------------------
# One-time migration from the legacy JSON files
# ---------------------------------------------------------------------------

def migrate_json_files() -> int:
    """Import any legacy `data/<kind>s/*.json` records into the database.

    Idempotent: a record already present in the DB is left untouched. Returns the
    number of records imported. Legacy files are kept (not deleted) as a backup.
    """
    init_db()
    mapping = {
        _ACCOUNT: DATA_DIR / "accounts",
        _CANDIDATE: DATA_DIR / "candidates",
        _ASSEMBLY: DATA_DIR / "assemblies",
        _DOCUMENT: DATA_DIR / "documents",
    }
    imported = 0
    for kind, folder in mapping.items():
        if not folder.exists():
            continue
        for path in folder.glob("*.json"):
            if _load(kind, path.stem) is not None:
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            owner_id = data.get("owner_id") if kind == _DOCUMENT else None
            _save(kind, path.stem, data, owner_id=owner_id)
            imported += 1
    return imported
