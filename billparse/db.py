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
    checks: dict[str, Any] | None = Field(default=None, sa_type=JSON)   # the price checks' statuses (task 07)


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


# ---------------------------------------------------------------- price catalog (prices/)
# Provider list prices, loaded from the providers' price files (scripts/aws/fetch_aws_price_files.py, prices/load.py)
# with their history. A product is a SKU and its attributes; a rate is one price of it (on demand or reserved, one
# tier); a Savings Plans rate is the plan's rate for one discounted SKU. Dates are YYYY-MM-DD text; a price applies
# from effective_from (included) to effective_to (excluded, None while current). Every index is one way task 06 looks a
# price up (prices/store.py).
class PriceSnapshotRow(SQLModel, table=True):
    """One load of the price files. One snapshot is current; a refused one is never current."""
    __tablename__: ClassVar[str] = "price_snapshot"
    id: str = Field(primary_key=True)                  # the load date, YYYY-MM-DD
    status: str = Field(index=True)                    # loading | current | previous | refused
    source: str                                        # aws-price-files (the download folder's manifest)
    counts: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)   # rows per price list
    created_at: float
    note: str | None = Field(default=None, sa_type=sa.Text)


def _history() -> list[sa.Column]:
    """A price is written once and changed only when the provider changes it: then effective_to is set (the load
    never rewrites unchanged rows). AWS prints EffectiveDate as a recent date for most prices, so the first version
    of a price is used for earlier dates too (first_version)."""
    return [sa.Column("effective_from", sa.String(10), nullable=False),
            sa.Column("effective_to", sa.String(10), nullable=True),
            sa.Column("first_snapshot", sa.String(10), nullable=False),
            # no older version is known: the price applies to earlier dates too (the files give no start before it)
            sa.Column("first_version", sa.Boolean, nullable=False, server_default=sa.false())]


price_product = sa.Table(
    "price_product", SQLModel.metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
    sa.Column("provider", sa.String, nullable=False),
    sa.Column("sku", sa.String, nullable=False),
    sa.Column("service_code", sa.String, nullable=False),
    sa.Column("product_family", sa.String),
    sa.Column("region", sa.String),                   # a code: us-east-1
    sa.Column("from_region", sa.String),              # data transfer: from and to
    sa.Column("to_region", sa.String),
    sa.Column("location", sa.String),                 # as printed: US East (N. Virginia)
    sa.Column("instance_type", sa.String),
    sa.Column("instance_family", sa.String),
    sa.Column("operating_system", sa.String),
    sa.Column("tenancy", sa.String),
    sa.Column("license_model", sa.String),
    sa.Column("pre_installed_sw", sa.String),
    sa.Column("capacity_status", sa.String),
    sa.Column("database_engine", sa.String),
    sa.Column("deployment_option", sa.String),
    sa.Column("cache_engine", sa.String),
    sa.Column("usage_type", sa.String),
    sa.Column("operation", sa.String),
    sa.Column("attributes", JSON),                     # every other attribute the file prints for the product
    sa.Column("attr_hash", sa.String(40), nullable=False),   # of every column above: changed -> updated
    sa.Column("first_snapshot", sa.String(10), nullable=False),
    sa.Column("changed_snapshot", sa.String(10), nullable=False),
    sa.UniqueConstraint("provider", "sku", name="price_product_sku"),               # a CUR line's product/sku
    # the lookups by usage type and by attributes read only these indexes (INCLUDE: the filter columns and the
    # SKU), never the wide rows: about 120 products share an instance type and region (OS, tenancy, license, ...)
    sa.Index("price_product_usage", "provider", "usage_type", "operation",
             postgresql_include=["region", "capacity_status", "service_code", "sku"]),
    sa.Index("price_product_instance", "provider", "instance_type", "region",
             postgresql_include=["operating_system", "tenancy", "license_model", "pre_installed_sw",
                                 "capacity_status", "database_engine", "deployment_option", "service_code",
                                 "usage_type", "sku"]),
)

price_rate = sa.Table(
    "price_rate", SQLModel.metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
    sa.Column("provider", sa.String, nullable=False),
    sa.Column("service_code", sa.String, nullable=False),
    sa.Column("sku", sa.String, nullable=False),
    sa.Column("rate_code", sa.String, nullable=False),   # a CUR line's pricing/RateCode
    sa.Column("term_type", sa.String, nullable=False),   # on_demand | reserved
    sa.Column("lease_length", sa.String),                # 1yr | 3yr
    sa.Column("purchase_option", sa.String),             # no_upfront | partial_upfront | all_upfront
    sa.Column("offering_class", sa.String),              # standard | convertible
    sa.Column("unit", sa.String, nullable=False),        # normalized (prices.units): hour, gb_month, request, ...
    sa.Column("unit_raw", sa.String),                    # as printed: Hrs, GB-Mo, Quantity
    sa.Column("unit_price", sa.Numeric(30, 12), nullable=False),   # per one normalized unit
    sa.Column("price_raw", sa.Numeric(30, 12), nullable=False),    # as printed, per unit_raw
    sa.Column("begin_range", sa.Numeric(30, 6)),         # a tier: usage from begin_range to end_range (None: no end)
    sa.Column("end_range", sa.Numeric(30, 6)),
    sa.Column("currency", sa.String(3), nullable=False),
    *_history(),
    sa.Index("price_rate_code", "rate_code", "effective_from"),                      # a line's rate code, by date
    sa.Index("price_rate_sku", "provider", "sku", "term_type", "effective_from"),     # a line's SKU, by date
)

price_sp_plan = sa.Table(
    "price_sp_plan", SQLModel.metadata,                          # one row per Savings Plan offering (its SKU)
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("provider", sa.String, nullable=False),
    sa.Column("sku", sa.String, nullable=False),
    sa.Column("service_code", sa.String, nullable=False),        # ComputeSavingsPlans, DatabaseSavingsPlans, ...
    sa.Column("plan_type", sa.String, nullable=False),           # ComputeSavingsPlans | EC2InstanceSavingsPlans | ...
    sa.Column("lease_length", sa.String, nullable=False),        # 1yr | 3yr
    sa.Column("purchase_option", sa.String, nullable=False),     # no_upfront | partial_upfront | all_upfront
    sa.Column("instance_family", sa.String),                     # EC2 Instance Savings Plans only
    sa.Column("region", sa.String),                              # a code; None: every region
    sa.Column("description", sa.Text),
    sa.Column("first_snapshot", sa.String(10), nullable=False),
    sa.Column("changed_snapshot", sa.String(10), nullable=False),
    sa.UniqueConstraint("provider", "sku", name="price_sp_plan_sku"),
)

price_sp_rate = sa.Table(
    "price_sp_rate", SQLModel.metadata,                          # 16M rows: only what is not in the plan or product
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
    sa.Column("plan_sku", sa.String, nullable=False),            # price_sp_plan.sku
    sa.Column("discounted_sku", sa.String, nullable=False),      # the on-demand SKU it covers (price_product.sku)
    sa.Column("unit", sa.String, nullable=False),
    sa.Column("rate", sa.Numeric(30, 12), nullable=False),       # per one normalized unit
    sa.Column("currency", sa.String(3), nullable=False),
    *_history(),
    sa.Index("price_sp_discounted", "discounted_sku", "effective_from"),   # a covered line's SKU (rate code: plan.sku)
)


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
