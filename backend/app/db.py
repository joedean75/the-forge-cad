"""Durable datastore — SQLAlchemy engine + session, one config away from Postgres.

The source of truth for accounts, documents, candidates and assemblies moves off
volatile in-memory dicts + loose JSON files onto a real transactional database:

  * **Dev / tests / CI** → embedded SQLite (`sqlite:///backend/data/daps.db`),
    WAL-mode for concurrent readers, zero infrastructure.
  * **Production** → point `DATABASE_URL` at Postgres
    (`postgresql+psycopg://user:pass@host/db`) for MVCC concurrent writers,
    connection pooling, TLS and horizontal scale — no code change.

Each aggregate is stored as one row: `(kind, id)` primary key, an indexed
`owner_id` for tenant-scoped queries, and the Pydantic/dataclass payload as JSON.
This keeps the existing serialization while giving durability, atomic
transactions, and concurrent-access safety that the file-JSON layer lacked.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from sqlalchemy import String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _default_url() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DATA_DIR / 'daps.db'}"


def _normalize(url: str) -> str:
    """Coerce managed-Postgres URLs to the installed driver.

    Hosts like Render/Heroku/Fly hand out `postgres://…` or `postgresql://…`,
    which SQLAlchemy maps to the (uninstalled) psycopg2 dialect. We ship psycopg
    v3, so pin the dialect explicitly.
    """
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _normalize(os.environ.get("DATABASE_URL") or _default_url())
_IS_SQLITE = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    future=True,
    echo=False,
    # SQLite is accessed from FastAPI's threadpool; allow cross-thread use.
    connect_args={"check_same_thread": False} if _IS_SQLITE else {},
    # Postgres: recycle stale pooled connections rather than erroring.
    pool_pre_ping=not _IS_SQLITE,
)


if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")     # concurrent readers alongside a writer
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")    # wait out a competing writer, don't crash
        cur.close()


class Base(DeclarativeBase):
    pass


class Record(Base):
    """A single stored aggregate, keyed by (kind, id)."""

    __tablename__ = "records"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    data: Mapped[str] = mapped_column(Text)


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables if they do not exist. Idempotent."""
    Base.metadata.create_all(engine)
