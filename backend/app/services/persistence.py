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
import re
from pathlib import Path
from typing import List, Optional

from sqlalchemy import delete as sql_delete, select

from app.db import DATA_DIR, Record, SessionLocal, engine, init_db

# Kinds (row namespaces in the `records` table).
_ACCOUNT = "account"
_DOCUMENT = "document"
_ASSEMBLY_DOC = "assembly_doc"  # parts-composition assembly
_ORDER = "order"  # manufacturing orders / RFQs
_META = "meta"  # one-time migration markers etc.
_TRAJECTORY = "trajectory"  # recorded agent interaction sequences (training data)
_JOB = "job"  # durable background work (see services/jobs.py)

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

    Returns dicts with the listing fields — id, owner_id, name, provenance,
    updated_at, collaborators, plus what the library card shows about the part
    itself (lifecycle state, material, mass, revision letters and the geometry's
    hash) — without ever deserializing full document payloads (feature trees,
    meshes, version history) into Python. The card gained four fields; the
    listing query stays one pass over the rows.
    """
    from sqlalchemy import text

    if engine.dialect.name == "sqlite":
        sql = text(
            "SELECT id, owner_id,"
            " json_extract(data, '$.name'),"
            " json_extract(data, '$.provenance'),"
            " json_extract(data, '$.updated_at'),"
            " json_extract(data, '$.created_at'),"
            " json_extract(data, '$.source_prompt'),"
            " json_extract(data, '$.collaborators'),"
            " json_extract(data, '$.lifecycle_state'),"
            " json_extract(data, '$.material_id'),"
            " json_extract(data, '$.derived.mass'),"
            " json_extract(data, '$.derived.mesh_hash'),"
            " json_extract(data, '$.revisions')"
            " FROM records WHERE kind = :kind"
        )
    else:  # postgres
        sql = text(
            "SELECT id, owner_id,"
            " data::json ->> 'name',"
            " data::json ->> 'provenance',"
            " data::json ->> 'updated_at',"
            " data::json ->> 'created_at',"
            " data::json ->> 'source_prompt',"
            " data::json ->> 'collaborators',"
            " data::json ->> 'lifecycle_state',"
            " data::json ->> 'material_id',"
            " data::json #>> '{derived,mass}',"
            " data::json #>> '{derived,mesh_hash}',"
            " data::json ->> 'revisions'"
            " FROM records WHERE kind = :kind"
        )
    def _json(value, fallback):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return fallback
        return value if value is not None else fallback

    out: List[dict] = []
    with SessionLocal() as s:
        for (rec_id, owner_id, name, provenance, updated_at, created_at, prompt,
             collaborators, state, material_id, mass, mesh_hash,
             revisions) in s.execute(sql, {"kind": _DOCUMENT}):
            revs = _json(revisions, []) or []
            out.append(
                {
                    "id": rec_id,
                    "owner_id": owner_id,
                    "name": name or "Untitled Part",
                    "provenance": provenance or "manual",
                    "updated_at": updated_at or "",
                    "created_at": created_at or updated_at or "",
                    "source_prompt": prompt,
                    "collaborators": _json(collaborators, []) or [],
                    "lifecycle_state": state or "draft",
                    "material_id": material_id,
                    "mass": float(mass) if mass is not None else None,
                    "mesh_hash": mesh_hash,
                    # Only the latest letter reaches the card; the full list is
                    # behind the history drawer.
                    "revision": (revs[-1] or {}).get("letter") if revs else None,
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


def list_trajectories() -> List[str]:
    return _list(_TRAJECTORY)


def save_trajectory(trajectory_id: str, trajectory: dict) -> None:
    _save(_TRAJECTORY, trajectory_id, trajectory, owner_id=trajectory.get("owner_id"))


def load_trajectory(trajectory_id: str) -> Optional[dict]:
    return _load(_TRAJECTORY, trajectory_id)


def delete_trajectory(trajectory_id: str) -> bool:
    return _delete(_TRAJECTORY, trajectory_id)


def list_trajectories_for_document(document_id: str) -> List[dict]:
    """Trajectories that built one document, filtered in SQL."""
    return _rows_by_json_field(_TRAJECTORY, "doc_id", document_id)


# ---------------------------------------------------------------------------
# Background jobs
#
# A job is a row like everything else, which is what makes its status and result
# survive a restart. `claim_job` is the one function here that is not a plain
# read or write: it is the queue's compare-and-set, and it has to be in this
# file because which lock enforces it is a property of the dialect.
# ---------------------------------------------------------------------------

def save_job(job_id: str, job: dict, owner_id: Optional[str] = None) -> None:
    _save(_JOB, job_id, job, owner_id=owner_id)


def load_job(job_id: str) -> Optional[dict]:
    return _load(_JOB, job_id)


def delete_job(job_id: str) -> bool:
    return _delete(_JOB, job_id)


def list_job_rows() -> List[dict]:
    """Every job row. The table holds tens of these, not millions.

    Retention prunes finished jobs, so the working set is the queue plus a day
    of outcomes. If that ever stops being true the fix is a status index, not a
    cache: a stale view of the queue is a job run twice.
    """
    with SessionLocal() as s:
        rows = s.scalars(select(Record.data).where(Record.kind == _JOB)).all()
    return [json.loads(r) for r in rows]


def list_jobs_for_subject(subject_id: str) -> List[dict]:
    """Jobs against one document or assembly, filtered in SQL."""
    return _rows_by_json_field(_JOB, "subject_id", subject_id)


def claim_job(job_id: str, *, expect_status: str, patch: dict) -> Optional[dict]:
    """Apply `patch` to a job, but only while its status is still `expect_status`.

    The queue's whole mutual exclusion. Two workers both list the queue, both
    see the same oldest job, and both try to take it — exactly one may win, or
    the job runs twice.

    The read and the write are in one transaction. Under SQLite that is enough
    on its own: there is a single writer, and the loser's `UPDATE` waits for the
    winner's commit and then sees `running`. Under Postgres, where two
    transactions can read concurrently, the row is locked with `FOR UPDATE`
    first — without it both would read `queued`, both would write, and the
    second would silently overwrite the first worker's name.

    Returns the updated row to the winner and `None` to everybody else.
    """
    with SessionLocal() as s:
        if engine.dialect.name != "sqlite":
            s.execute(
                select(Record).where(Record.kind == _JOB, Record.id == job_id)
                .with_for_update())
        row = s.get(Record, {"kind": _JOB, "id": job_id})
        if row is None:
            return None
        data = json.loads(row.data)
        if data.get("status") != expect_status:
            return None
        data.update(patch)
        row.data = json.dumps(data)
        s.commit()
        return data


#: A bare JSON key. `field` names a path and so cannot be a bound parameter —
#: it is interpolated into the statement — which makes it the one argument here
#: that must never come from a request.
_JSON_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _rows_by_json_field(kind: str, field: str, value: str) -> List[dict]:
    """Rows of one kind whose top-level JSON `field` equals `value`.

    Filtered in SQL rather than by loading every row and testing in Python — a
    document row carries its whole feature tree and version history.

    `kind` and `value` are bound; `field` is interpolated, because a JSON path
    is not a value any driver will bind. Every caller passes a literal today, so
    this is a rule rather than a fix — but the failure mode if one ever forwards
    a request field is SQL injection, and the check costs a regex. Verified: the
    field ``catalog_ref') OR 1=1--`` reaches the statement without it.
    """
    from sqlalchemy import text

    if not _JSON_FIELD.match(field):
        raise ValueError(f"invalid JSON field name: {field!r}")

    if engine.dialect.name == "sqlite":
        sql = text(f"SELECT data FROM records WHERE kind = :kind"
                   f" AND json_extract(data, '$.{field}') = :value")
    else:  # postgres
        sql = text(f"SELECT data FROM records WHERE kind = :kind"
                   f" AND data::json ->> '{field}' = :value")
    with SessionLocal() as s:
        rows = s.execute(sql, {"kind": kind, "value": value}).scalars().all()
    return [json.loads(r) for r in rows]


def list_orders_for_document(document_id: str) -> List[dict]:
    """Orders for one document, filtered in SQL rather than loading every row."""
    return _rows_by_json_field(_ORDER, "document_id", document_id)


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
    # Candidate / generated-assembly folders are deliberately absent: those
    # record kinds were removed with the legacy pipeline, so importing them
    # would only create rows nothing can read.
    mapping = {
        _ACCOUNT: DATA_DIR / "accounts",
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
