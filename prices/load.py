"""Load the AWS price files (scripts/aws/fetch_aws_price_files.py) into the price tables, as one snapshot, with history.

    load_snapshot(engine, download_dir)     every price list of the download's manifest.json

One file at a time: read with pyarrow (every column as text), split into products (one per SKU: the typed attributes
task 06 matches on, and every other attribute as JSON) and rates (one per rate code: on demand or reserved, one tier),
or Savings Plans rates. Units and values are normalized once per distinct value (prices.units, prices.normalize).
The rows go to staging tables (PostgreSQL: COPY), then one transaction:

  sanity     a price list may not lose more than 10% of its rates against the current snapshot, and a full load may
             not miss a price list the current snapshot has; else the snapshot is refused and nothing changes
  products   inserted, or updated when their attributes changed (attr_hash)
  history    an open rate whose price changed, or that the new files no longer have, is closed (effective_to); a new
             or changed rate is inserted from its EffectiveDate (or the load date). Only the price lists in the load
             are touched, so a partial load (one service) never closes another service's prices
  switch     the new snapshot is current, the old one previous
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from billparse.db import PriceSnapshotRow

from . import normalize as nz
from .units import normalize as unit_of

log = logging.getLogger(__name__)

MAX_DROP = 0.10                     # a price list losing more of its rates than this is refused
HEADER_LINES = 5                    # FormatVersion, Disclaimer, Publication Date, Version, OfferCode

# offer file columns -> price_product columns (the attributes task 06 matches on); every other product column is JSON
PRODUCT_COLUMNS = {"SKU": "sku", "serviceCode": "service_code", "Product Family": "product_family",
                   "Region Code": "region", "From Region Code": "from_region", "To Region Code": "to_region",
                   "Location": "location", "Instance Type": "instance_type", "Instance Family": "instance_family",
                   "Operating System": "operating_system", "Tenancy": "tenancy", "License Model": "license_model",
                   "Pre Installed S/W": "pre_installed_sw", "CapacityStatus": "capacity_status",
                   "Database Engine": "database_engine", "Deployment Option": "deployment_option",
                   "Cache Engine": "cache_engine", "usageType": "usage_type", "operation": "operation"}
# offer file columns that belong to a price, not to the product
RATE_COLUMNS = {"SKU", "OfferTermCode", "RateCode", "TermType", "PriceDescription", "EffectiveDate", "StartingRange",
                "EndingRange", "Unit", "PricePerUnit", "Currency", "LeaseContractLength", "PurchaseOption",
                "OfferingClass"}
TERM_TYPES = {"OnDemand": "on_demand", "Reserved": "reserved"}
PRODUCT_FIELDS = ["provider", *PRODUCT_COLUMNS.values(), "attributes", "attr_hash"]
RATE_FIELDS = ["provider", "service_code", "sku", "rate_code", "term_type", "lease_length", "purchase_option",
               "offering_class", "unit", "unit_raw", "unit_price", "price_raw", "begin_range", "end_range", "currency",
               "effective_date"]
PLAN_FIELDS = ["provider", "sku", "service_code", "plan_type", "lease_length", "purchase_option", "instance_family",
               "region", "description"]
SP_FIELDS = ["plan_sku", "discounted_sku", "unit", "rate", "currency", "effective_date"]


class Refused(RuntimeError):
    """The sanity check refused the snapshot: the current one stays."""


@dataclass
class LoadResult:
    snapshot_id: str
    status: str
    counts: dict[str, int] = field(default_factory=dict)     # rates per price list
    products: int = 0
    rates: int = 0
    sp_rates: int = 0
    closed: int = 0
    inserted: int = 0
    seconds: dict[str, float] = field(default_factory=dict)
    note: str | None = None


# ---------------------------------------------------------------- reading the files
def read_csv(path: Path) -> pa.Table:
    """One AWS price CSV (gzipped): the header lines skipped, every column as text, blanks as null."""
    with gzip.open(path, "rb") as fh:
        for _ in range(HEADER_LINES):
            fh.readline()
        names = next(csv.reader([fh.readline().decode("utf-8")]))
        unique = [n if names.count(n) == 1 else f"{n}_{i}" for i, n in enumerate(names)]
        return pacsv.read_csv(fh, read_options=pacsv.ReadOptions(column_names=unique, block_size=16 << 20),
                              parse_options=pacsv.ParseOptions(newlines_in_values=True),
                              convert_options=pacsv.ConvertOptions(
                                  column_types={n: pa.string() for n in unique}, null_values=[""],
                                  strings_can_be_null=True, quoted_strings_can_be_null=True))


def _units(raw: pd.Series) -> tuple[pd.Series, pd.Series]:
    """(normalized unit, factor) per row, computed once per distinct unit; an unknown unit stays as printed (lower
    case), factor 1."""
    table = {u: (unit_of(u) or (str(u).strip().lower(), 1.0)) for u in raw.dropna().unique()}
    unit = raw.map(lambda u: table[u][0] if u in table else "unit")
    factor = raw.map(lambda u: table[u][1] if u in table else 1.0).astype(float)
    return unit, factor


def _mapped(s: pd.Series, fn: Callable) -> pd.Series:
    table = {v: fn(v) for v in s.dropna().unique()}
    return s.map(table).astype(object).where(s.notna(), None)


def _number(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.where(~s.isin(["Inf", "inf", "Infinity"]), None), errors="coerce")


def offer_frames(table: pa.Table, service: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(products, rates) of one offer file."""
    df = table.to_pandas()
    df["serviceCode"] = df["serviceCode"] if "serviceCode" in df else service
    # products: the first row of each SKU; its attributes (product columns only, empty ones left out)
    first = df.drop_duplicates("SKU")
    prod = pd.DataFrame({out: (first[col] if col in first else None) for col, out in PRODUCT_COLUMNS.items()},
                        index=first.index)
    prod["provider"] = "aws"
    prod["service_code"] = prod["service_code"].fillna(service)
    extra = [c for c in first.columns if c not in RATE_COLUMNS and c not in PRODUCT_COLUMNS
             and first[c].notna().any()]
    prod["attributes"] = [json.dumps({k: v for k, v in zip(extra, row, strict=True) if isinstance(v, str)},
                                     separators=(",", ":"), ensure_ascii=False)
                          for row in first[extra].itertuples(index=False, name=None)]
    for col, fn in (("region", nz.region), ("from_region", nz.region), ("to_region", nz.region),
                    ("operating_system", nz.operating_system), ("tenancy", nz.tenancy),
                    ("license_model", nz.license_model), ("pre_installed_sw", nz.pre_installed_sw),
                    ("capacity_status", nz.capacity_status), ("instance_type", nz.instance_type)):
        prod[col] = _mapped(prod[col], fn) if prod[col].notna().any() else None
    prod["attr_hash"] = [hashlib.sha1("\x1f".join(v if isinstance(v, str) else "" for v in row).encode()).hexdigest()
                         for row in prod[[c for c in PRODUCT_FIELDS if c not in ("attr_hash",)]]
                         .itertuples(index=False, name=None)]
    # rates: every row
    unit, factor = _units(df["Unit"])
    price = pd.to_numeric(df["PricePerUnit"], errors="coerce")
    rates = pd.DataFrame({
        "provider": "aws", "service_code": df["serviceCode"].fillna(service), "sku": df["SKU"],
        "rate_code": df["RateCode"], "term_type": _mapped(df["TermType"], lambda v: TERM_TYPES.get(v, v.lower())),
        "lease_length": _mapped(df["LeaseContractLength"], nz.term) if "LeaseContractLength" in df else None,
        "purchase_option": _mapped(df["PurchaseOption"], nz.payment_option) if "PurchaseOption" in df else None,
        "offering_class": df["OfferingClass"] if "OfferingClass" in df else None,
        "unit": unit, "unit_raw": df["Unit"], "unit_price": (price / factor).round(12), "price_raw": price,
        "begin_range": _number(df["StartingRange"]), "end_range": _number(df["EndingRange"]),
        "currency": df["Currency"].fillna("USD"),
        "effective_date": df["EffectiveDate"].str.slice(0, 10)})
    return prod[PRODUCT_FIELDS], rates[RATE_FIELDS][rates["unit_price"].notna()]


def sp_frames(table: pa.Table, offer: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(plans, rates) of one Savings Plans file: a plan once per SKU, a rate per plan and covered SKU."""
    df = table.to_pandas()
    df = df[df["DiscountedSKU"].notna()]
    first = df.drop_duplicates("SKU")
    lease = (first["LeaseContractLength"].fillna("") + " " + first["LeaseContractLengthUnit"].fillna("")).str.strip()
    plans = pd.DataFrame({
        "provider": "aws", "sku": first["SKU"], "service_code": first["ServiceCode"].fillna(offer),
        "plan_type": first["Product Family"], "lease_length": _mapped(lease, nz.term),
        "purchase_option": _mapped(first["PurchaseOption"], nz.payment_option),
        "instance_family": first["Instance Family"], "region": _mapped(first["Location"].where(
            first["Location Type"] == "AWS Region"), nz.region), "description": first["Description"]})
    unit, factor = _units(df["Unit"])
    rates = pd.DataFrame({
        "plan_sku": df["SKU"], "discounted_sku": df["DiscountedSKU"], "unit": unit,
        "rate": (pd.to_numeric(df["DiscountedRate"], errors="coerce") / factor).round(12),
        "currency": df["Currency"].fillna("USD"), "effective_date": df["EffectiveDate"].str.slice(0, 10)})
    return plans[PLAN_FIELDS], rates[rates["rate"].notna()][SP_FIELDS]


def download_files(download_dir: Path) -> Iterator[tuple[str, str, str, Path]]:
    """(kind, price list, region, path) of every file in the download's manifest.json: kind offers | savings_plans."""
    manifest = json.loads((download_dir / "manifest.json").read_text())
    for name, regions in manifest["files"].items():
        offer, version = name.split()
        kind = "savings_plans" if (download_dir / "savings_plans" / offer).exists() else "offers"
        for region, res in regions.items():
            if "error" not in res:
                yield kind, offer, region, download_dir / kind / offer / version / f"{region}.csv.gz"


# ---------------------------------------------------------------- staging
STAGE = {"stage_product": PRODUCT_FIELDS, "stage_rate": RATE_FIELDS, "stage_plan": PLAN_FIELDS, "stage_sp": SP_FIELDS}


def _create_stage(conn: Connection) -> None:
    pg = conn.dialect.name == "postgresql"
    for name, cols in STAGE.items():
        conn.exec_driver_sql(f"DROP TABLE IF EXISTS {name}")
        typed = ", ".join(f"{c} {'NUMERIC' if c in ('unit_price', 'price_raw', 'begin_range', 'end_range', 'rate') else 'TEXT'}"
                          for c in cols)
        conn.exec_driver_sql(f"CREATE {'UNLOGGED' if pg else 'TEMP'} TABLE {name} "
                             f"(n {'BIGSERIAL' if pg else 'INTEGER PRIMARY KEY AUTOINCREMENT'}, {typed})")


def _copy(conn: Connection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    cols = STAGE[table]
    if conn.dialect.name == "postgresql":
        buf = pa.BufferOutputStream()
        pacsv.write_csv(pa.Table.from_pandas(df[cols], preserve_index=False), buf,
                        pacsv.WriteOptions(include_header=False))
        raw = conn.connection.driver_connection
        with raw.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv)") as cp:
            cp.write(buf.getvalue().to_pybytes())
    else:
        rows = df[cols].astype(object).where(df[cols].notna(), None).to_dict("records")
        conn.execute(sa.text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
                     rows)


# ---------------------------------------------------------------- the load
def load_snapshot(engine: Engine, download_dir: Path, snapshot_id: str | None = None, partial: bool = False,
                  only: Iterable[str] | None = None, on_file: Callable[[str], None] | None = None) -> LoadResult:
    """Load every file of a download (only: these price lists) as one snapshot. partial: only the price lists in it
    are compared and changed (a full load also refuses a price list that is missing); a partial load on the day of
    the current snapshot adds to it. Raises Refused when the sanity check fails."""
    download_dir = Path(download_dir)
    manifest = json.loads((download_dir / "manifest.json").read_text())
    sid = snapshot_id or manifest["fetched_at"][:10]
    res = LoadResult(sid, "loading")
    t0 = time.monotonic()
    with engine.begin() as conn:
        snap = PriceSnapshotRow.__table__
        conn.execute(snap.delete().where(snap.c.id == sid, snap.c.status.in_(["loading", "refused"])))
        if conn.execute(sa.select(snap.c.id).where(snap.c.id == sid,
                                                   snap.c.status != "current" if partial else sa.true())).first():
            raise Refused(f"snapshot {sid} is already loaded")
    with engine.connect() as conn:
        _create_stage(conn)
        conn.commit()
        wanted = set(only) if only else None
        for kind, offer, region, path in download_files(download_dir):
            if wanted is not None and offer not in wanted:
                continue
            table = read_csv(path)
            if kind == "offers":
                prod, rates = offer_frames(table, offer)
                _copy(conn, "stage_product", prod)
                _copy(conn, "stage_rate", rates)
                res.counts[offer] = res.counts.get(offer, 0) + len(rates)
            else:
                plans, sp = sp_frames(table, offer)
                _copy(conn, "stage_plan", plans)
                _copy(conn, "stage_sp", sp)
                res.counts[offer] = res.counts.get(offer, 0) + len(sp)
            conn.commit()
            if on_file:
                on_file(f"{offer} {region}: {table.num_rows:,} rows")
        res.seconds["read_and_stage"] = round(time.monotonic() - t0, 1)
        t1 = time.monotonic()
        try:
            _apply(conn, res, sid, partial)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            for name in STAGE:
                conn.exec_driver_sql(f"DROP TABLE IF EXISTS {name}")
                conn.exec_driver_sql(f"DROP TABLE IF EXISTS {name}_one")
            conn.commit()
        res.seconds["apply"] = round(time.monotonic() - t1, 1)
    return res


def _upsert(conn: Connection, target: str, stage: str, fields: list[str], update: str, p: dict) -> None:
    """Insert the staged rows of a SKU table (the first row of a SKU wins); on conflict: DO UPDATE SET update."""
    pick = ", ".join("CAST(attributes AS JSONB)" if c == "attributes" and conn.dialect.name == "postgresql" else c
                     for c in fields)
    conn.execute(sa.text(
        f"INSERT INTO {target} ({', '.join(fields)}, first_snapshot, changed_snapshot) "
        f"SELECT {pick}, :sid, :sid FROM {stage} WHERE n IN (SELECT min(n) FROM {stage} GROUP BY provider, sku) "
        f"ON CONFLICT (provider, sku) DO UPDATE SET {update}"), p)


def _unique(conn: Connection, stage: str, keys: list[str]) -> str:
    """The staged rates, one per key: the stage itself when no key repeats (a unique index proves it, and the joins
    use it), else a copy with the first row of each key."""
    try:
        with conn.begin_nested():
            conn.exec_driver_sql(f"CREATE UNIQUE INDEX {stage}_key ON {stage} ({', '.join(keys)})")
        if conn.dialect.name == "postgresql":
            conn.exec_driver_sql(f"ANALYZE {stage}")
        return stage
    except sa.exc.IntegrityError:
        pass
    one = f"{stage}_one"
    conn.exec_driver_sql(f"DROP TABLE IF EXISTS {one}")
    conn.exec_driver_sql(f"CREATE TEMP TABLE {one} AS SELECT * FROM {stage} WHERE n IN "
                         f"(SELECT min(n) FROM {stage} GROUP BY {', '.join(keys)})")
    conn.exec_driver_sql(f"CREATE INDEX {one}_key ON {one} ({', '.join(keys)})")
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(f"ANALYZE {one}")
    return one


def _apply(conn: Connection, res: LoadResult, sid: str, partial: bool) -> None:
    snap = PriceSnapshotRow.__table__
    current = conn.execute(sa.select(snap.c.id, snap.c.counts).where(snap.c.status == "current")).first()
    prev = dict(current.counts or {}) if current else {}
    problems = []
    for name, n in prev.items():
        if name not in res.counts:
            if not partial:
                problems.append(f"{name} is missing ({n:,} rates before)")
        elif res.counts[name] < (1 - MAX_DROP) * n:
            problems.append(f"{name}: {res.counts[name]:,} rates, {n:,} before")
    if problems:
        conn.execute(snap.insert().values(id=sid, status="refused", source="aws-price-files", counts=res.counts,
                                          created_at=time.time(), note="; ".join(problems)[:4000]))
        conn.commit()
        res.status, res.note = "refused", "; ".join(problems)
        raise Refused(res.note)
    p = {"sid": sid}
    x = conn.exec_driver_sql
    pg = conn.dialect.name == "postgresql"
    t = time.monotonic()

    def step(name: str) -> None:
        nonlocal t
        res.seconds[name] = round(time.monotonic() - t, 1)
        t = time.monotonic()

    if pg:
        for name in STAGE:
            x(f"ANALYZE {name}")
    # products and plans: new, or changed; the first row of a SKU wins
    upd = ", ".join(f"{c} = excluded.{c}" for c in PRODUCT_FIELDS if c not in ("provider", "sku"))
    _upsert(conn, "price_product", "stage_product", PRODUCT_FIELDS,
            f"{upd}, changed_snapshot = excluded.changed_snapshot WHERE price_product.attr_hash <> excluded.attr_hash", p)
    upd = ", ".join(f"{c} = excluded.{c}" for c in PLAN_FIELDS if c not in ("provider", "sku"))
    _upsert(conn, "price_sp_plan", "stage_plan", PLAN_FIELDS, f"{upd}, changed_snapshot = excluded.changed_snapshot", p)
    step("products")
    for target, stage, fields, keys, price, scope in (
            ("price_rate", "stage_rate", RATE_FIELDS, ["rate_code"], "unit_price",
             "r.service_code IN (SELECT DISTINCT service_code FROM stage_rate)"),
            ("price_sp_rate", "stage_sp", SP_FIELDS, ["plan_sku", "discounted_sku"], "rate",
             "r.plan_sku IN (SELECT sku FROM price_sp_plan WHERE service_code IN "
             "(SELECT DISTINCT service_code FROM stage_plan))")):
        one = _unique(conn, stage, keys)
        step(f"{target}_dedupe")
        on = " AND ".join(f"s.{k} = r.{k}" for k in keys)
        # close: open rates of the loaded price lists whose price changed or that the files no longer have
        x("DROP TABLE IF EXISTS rate_close")
        conn.execute(sa.text(
            f"CREATE TEMP TABLE rate_close AS SELECT r.id, {', '.join('r.' + k for k in keys)}, CASE WHEN "
            f"s.{keys[0]} IS NOT NULL AND s.effective_date > r.effective_from THEN s.effective_date ELSE :sid END "
            f"AS close_date FROM {target} r LEFT JOIN {one} s ON {on} WHERE r.effective_to IS NULL AND {scope} "
            f"AND (s.{keys[0]} IS NULL OR s.{price} <> r.{price} OR s.currency <> r.currency)"), p)
        closed = x("SELECT count(*) FROM rate_close").scalar_one()
        if pg:
            x("ANALYZE rate_close")
        x(f"UPDATE {target} SET effective_to = (SELECT c.close_date FROM rate_close c WHERE c.id = {target}.id) "
          f"WHERE id IN (SELECT id FROM rate_close)")
        step(f"{target}_close")
        # insert: rates with no open row (new, or just closed); from the close date, else the file's effective date
        out = ", ".join(f for f in fields if f != "effective_date")
        sel = ", ".join(f"s.{f}" for f in fields if f != "effective_date")
        on_c = " AND ".join(f"c.{k} = s.{k}" for k in keys)
        inserted = conn.execute(sa.text(
            f"INSERT INTO {target} ({out}, effective_from, effective_to, first_snapshot, first_version) "
            f"SELECT {sel}, COALESCE(c.close_date, s.effective_date, :sid), NULL, :sid, "
            f"c.{keys[0]} IS NULL AND NOT EXISTS (SELECT 1 FROM {target} r WHERE {on}) FROM {one} s "
            f"LEFT JOIN rate_close c ON {on_c} "
            f"WHERE NOT EXISTS (SELECT 1 FROM {target} r WHERE {on} AND r.effective_to IS NULL)"), p).rowcount
        step(f"{target}_insert")
        res.closed += closed
        res.inserted += inserted
        n = x(f"SELECT count(*) FROM {one}").scalar_one()
        if target == "price_rate":
            res.rates = n
        else:
            res.sp_rates = n
        x("DROP TABLE IF EXISTS rate_close")
    res.products = x("SELECT count(*) FROM (SELECT DISTINCT provider, sku FROM stage_product) t").scalar_one()
    counts = res.counts if not partial or not prev else {**prev, **res.counts}
    note = (f"{res.products:,} products, {res.rates:,} rates, {res.sp_rates:,} savings plans rates; "
            f"{res.closed:,} closed, {res.inserted:,} inserted")
    if current and current.id == sid:               # a partial load on the same day adds to the current snapshot
        old = conn.execute(sa.select(snap.c.note).where(snap.c.id == sid)).scalar_one() or ""
        conn.execute(snap.update().where(snap.c.id == sid).values(counts=counts, note=f"{old}\n{note}"[-4000:]))
    else:
        conn.execute(snap.update().where(snap.c.status == "current").values(status="previous"))
        conn.execute(snap.insert().values(id=sid, status="current", source="aws-price-files", counts=counts,
                                          created_at=time.time(), note=note))
    res.status = "current"
