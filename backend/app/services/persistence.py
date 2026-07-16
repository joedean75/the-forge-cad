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

from app.db import DATA_DIR, Record, SessionLocal, engine, init_db

# Kinds (row namespaces in the `records` table).
_ACCOUNT = "account"
_CANDIDATE = "candidate"
_ASSEMBLY = "assembly"  # legacy generated AssemblyResult (trusses/frames)
_DOCUMENT = "document"
_ASSEMBLY_DOC = "assembly_doc"  # parts-composition assembly
_ORDER = "order"  # manufacturing orders / RFQs
_META = "meta"  # one-time migration markers etc.

_MIGRATION_MARKER = "json_files_migrated"
_MESH_STRIP_MARKER = "document_meshes_stripped_v1"


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


def list_document_summaries() -> List[dict]:
    """Lightweight listing fields for every document, extracted in SQL.

    Returns dicts with id/owner_id/name/provenance/updated_at/collaborators
    without ever deserializing full document payloads (feature trees, meshes,
    version history) into Python.
    """
    from sqlalchemy import text

    if engine.dialect.name == "sqlite":
        sql = text(
            "SELECT id, owner_id,"
            " json_extract(data, '$.name'),"
            " json_extract(data, '$.provenance'),"
            " json_extract(data, '$.updated_at'),"
            " json_extract(data, '$.collaborators')"
            " FROM records WHERE kind = :kind"
        )
    else:  # postgres
        sql = text(
            "SELECT id, owner_id,"
            " data::json ->> 'name',"
            " data::json ->> 'provenance',"
            " data::json ->> 'updated_at',"
            " data::json ->> 'collaborators'"
            " FROM records WHERE kind = :kind"
        )
    out: List[dict] = []
    with SessionLocal() as s:
        for rec_id, owner_id, name, provenance, updated_at, collaborators in s.execute(
            sql, {"kind": _DOCUMENT}
        ):
            if isinstance(collaborators, str):
                try:
                    collaborators = json.loads(collaborators)
                except ValueError:
                    collaborators = []
            out.append(
                {
                    "id": rec_id,
                    "owner_id": owner_id,
                    "name": name or "Untitled Part",
                    "provenance": provenance or "manual",
                    "updated_at": updated_at or "",
                    "collaborators": collaborators or [],
                }
            )
    return out


# ---------------------------------------------------------------------------
# Assembly documents (parts composition)
# ---------------------------------------------------------------------------

def save_assembly_doc(assembly_id: str, assembly: dict) -> None:
    _save(_ASSEMBLY_DOC, assembly_id, assembly, owner_id=assembly.get("owner_id"))


def load_assembly_doc(assembly_id: str) -> Optional[dict]:
    return _load(_ASSEMBLY_DOC, assembly_id)


def list_assembly_docs() -> List[str]:
    return _list(_ASSEMBLY_DOC)


def delete_assembly_doc(assembly_id: str) -> bool:
    return _delete(_ASSEMBLY_DOC, assembly_id)


# ---------------------------------------------------------------------------
# Manufacturing orders (RFQs)
# ---------------------------------------------------------------------------

def save_order(order_id: str, order: dict) -> None:
    _save(_ORDER, order_id, order, owner_id=order.get("owner_id"))


def load_order(order_id: str) -> Optional[dict]:
    return _load(_ORDER, order_id)


def list_orders() -> List[str]:
    return _list(_ORDER)


# ---------------------------------------------------------------------------
# One-time migration from the legacy JSON files
# ---------------------------------------------------------------------------

def migrate_json_files() -> int:
    """Import any legacy `data/<kind>s/*.json` records into the database.

    Runs once per database: a marker row is written on completion, so later
    startups skip the (potentially multi-gigabyte) directory walk entirely.
    Records already present are left untouched — presence is checked against an
    id-only index scan, never by loading row payloads. Legacy files are kept
    (not deleted) as a backup.
    """
    init_db()
    if _load(_META, _MIGRATION_MARKER) is not None:
        return 0
    mapping = {
        _ACCOUNT: DATA_DIR / "accounts",
        _CANDIDATE: DATA_DIR / "candidates",
        _ASSEMBLY: DATA_DIR / "assemblies",
        _DOCUMENT: DATA_DIR / "documents",
    }
    with SessionLocal() as s:
        existing = set(
            s.execute(select(Record.kind, Record.id).where(Record.kind.in_(mapping.keys()))).all()
        )
    imported = 0
    for kind, folder in mapping.items():
        if not folder.exists():
            continue
        for path in folder.glob("*.json"):
            if (kind, path.stem) in existing:
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            owner_id = data.get("owner_id") if kind == _DOCUMENT else None
            _save(kind, path.stem, data, owner_id=owner_id)
            imported += 1
    _save(_META, _MIGRATION_MARKER, {"imported": imported})
    return imported


def strip_document_meshes() -> int:
    """One-time slimming pass: drop persisted derived meshes from document rows.

    Meshes are derived data (re-evaluated on demand), but historical rows carry
    multi-MB base64 STLs that make every read and listing expensive. Marker-gated
    so the full-table rewrite happens exactly once per database.
    """
    if _load(_META, _MESH_STRIP_MARKER) is not None:
        return 0
    stripped = 0
    for doc_id in _list(_DOCUMENT):
        data = _load(_DOCUMENT, doc_id)
        if not data:
            continue
        derived = data.get("derived") or {}
        if derived.get("mesh_url") or derived.get("step_url"):
            derived["mesh_url"] = None
            derived["step_url"] = None
            data["derived"] = derived
            _save(_DOCUMENT, doc_id, data, owner_id=data.get("owner_id"))
            stripped += 1
    _save(_META, _MESH_STRIP_MARKER, {"stripped": stripped})
    return stripped
