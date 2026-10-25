"""The database: SQLModel tables and one engine per URL.

DATABASE_URL selects the database: PostgreSQL in Docker (postgresql+psycopg://user:pass@db:5432/name), a SQLite
file otherwise (<data dir>/billparse.db, which the CLI and the tests use). PostgreSQL gets its schema from the
Alembic migrations (migrations/), SQLite from create_all.

The large parts stay files beside the database (store.py): uploaded files, result rows (Parquet), the row model
and nightly reports.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any, ClassVar

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlmodel import Field, SQLModel, create_engine

JSON = sa.JSON().with_variant(JSONB(), "postgresql")     # jsonb on PostgreSQL, JSON text on SQLite


class DocumentRow(SQLModel, table=True):
    """One uploaded file and its latest result (the summary the pipeline returns)."""
    __tablename__: ClassVar[str] = "documents"
    sha256: str = Field(primary_key=True)
    name: str
    file_type: str | None = None
    kind: str | None = None                        # cost_lines | price_sheet | recommendations
    status: str = Field(index=True)                   # reconciled | unreconciled | needs_review | auxiliary
    #                                                   | unsupported | approved
    confidence: float | None = None
    rows: int = 0
    route: str | None = None                       # builtin | learned | mapper | llm_spec | llm_extract_*
    spec_id: str | None = None
    spec_version: int | None = None
    llm_calls: int = 0
    seconds: float | None = None
    total_billed: float | None = None
    currency: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    uploaded_at: float = Field(index=True)
    uploaded_by: str | None = None                 # the signed-in email of the file's first upload (AUTH=google)
    parsed_at: float | None = None
    reviewed_at: float | None = None
    reviewed_by: str | None = None
    reviewer_note: str | None = Field(default=None, sa_type=sa.Text)
    approved: dict[str, Any] | None = Field(default=None, sa_type=JSON)   # verdict + analysis after review


class SpecRow(SQLModel, table=True):
    """A learned spec: which version is current. Built-in specs are files (specs/builtin) and never rows."""
    __tablename__: ClassVar[str] = "spec"
    id: str = Field(primary_key=True)
    current_version: int
    trust: str | None = None                       # candidate | verified (of the current version)
    updated_at: float


class SpecVersionRow(SQLModel, table=True):
    """Every saved version of a learned spec, never overwritten (history and rollback)."""
    __tablename__: ClassVar[str] = "spec_version"
    id: str = Field(primary_key=True)
    version: int = Field(primary_key=True)
    spec: dict[str, Any] = Field(sa_type=JSON)
    created_at: float


class SpecDocRow(SQLModel, table=True):
    """A file a spec parsed and was accepted on: the regression set of the spec."""
    __tablename__: ClassVar[str] = "spec_doc"
    spec_id: str = Field(primary_key=True)
    sha256: str = Field(primary_key=True)
    rows: int
    total: float
    confidence: float


class SpecFingerprintRow(SQLModel, table=True):
    """A layout fingerprint a spec parsed (learned specs are found again by these)."""
    __tablename__: ClassVar[str] = "spec_fingerprint"
    spec_id: str = Field(primary_key=True)
    hash: str = Field(primary_key=True)
    fingerprint: dict[str, Any] = Field(sa_type=JSON)


class JournalRow(SQLModel, table=True):
    """Append-only logs: `records` (one per parse), `labels` (row classifier data), `review` (the review queue)."""
    __tablename__: ClassVar[str] = "journal"
    __table_args__ = (sa.Index("journal_stream_sha", "stream", "sha256"),)
    id: int | None = Field(default=None, primary_key=True)
    stream: str
    sha256: str | None = None
    ts: float
    data: dict[str, Any] = Field(sa_type=JSON)


class AgentJobRow(SQLModel, table=True):
    """A layout the agent reviews (agent.py): one job per layout key, so a layout goes to the LLM once unless retried."""
    __tablename__: ClassVar[str] = "agent_job"
    id: int | None = Field(default=None, primary_key=True)
    layout_key: str = Field(unique=True)             # layout:<fingerprint> or repair:<spec>@v<version>:<sha>
    kind: str                                        # new | repair
    sha256: str                                      # the file the spec is written for
    name: str
    status: str = Field(index=True)                  # queued | running | proved | unproved | limit | rejected | error
    spec_id: str | None = None                       # the spec improved (repair), then the spec saved
    spec_version: int | None = None
    rounds: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    note: str | None = Field(default=None, sa_type=sa.Text)
    error: str | None = Field(default=None, sa_type=sa.Text)
    report: dict[str, Any] | None = Field(default=None, sa_type=JSON)   # the code's checks on the best spec
    attempts: int = 0
    created: float
    updated: float
    started: float | None = None
    seconds: float | None = None


class AppUserRow(SQLModel, table=True):
    """A person admitted to the web app: a Google account of the allowed domain that gave a valid invite code once."""
    __tablename__: ClassVar[str] = "app_users"
    email: str = Field(primary_key=True)              # lower case
    name: str | None = None
    invite: str | None = None                      # which invite code admitted them: its label, never the code
    admitted_at: float
    last_login: float | None = None


def default_url(data_dir: Path) -> str:
    return os.environ.get("DATABASE_URL") or f"sqlite:///{Path(data_dir) / 'billparse.db'}"


@functools.lru_cache(maxsize=16)
def engine_for(url: str) -> Engine:
    """One engine (connection pool) per database URL; SQLite tables are created on first use."""
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
        SQLModel.metadata.create_all(engine)
    else:
        engine = create_engine(url, pool_pre_ping=True)   # PostgreSQL: the schema comes from the migrations
    return engine
