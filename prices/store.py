"""The price catalog's bulk lookup: many keys in, the price in effect on each key's date out, in one SQL statement.

    catalog = get_catalog()                       # once per process
    found = catalog.lookup(keys)                  # keys: a DataFrame, one row per distinct key (see lookup)
    sp = catalog.lookup_savings_plans(keys)

How a key finds its price, the first way that matches wins:
  1 rate_code    the exact price (a CUR line's pricing/RateCode)
  2 sku          the SKU's on-demand price (or the reserved one asked for), first tier
  3 usage        usage type + operation (a CUR line without SKU), the product's price; capacity reservations have
                 their own usage types (UnusedBox:, Reservation:), and when a usage type has several products
                 (an Outpost's), the "Used" one
  4 attributes   instance type + region (+ OS, tenancy, license, software, engine, deployment when given): an invoice
                 line with no SKU. Give service_code too: EC2, WorkSpaces, EMR, GameLift, Outposts, ... share instance
                 types. EC2 capacity reservation prices are never taken here (only "Used"), nor RDS "Unused" ones
                 when the instance's own product fits.
A key that several prices fit at its best way (attributes that leave the OS open, say) gets no price: candidates
says how many there were, so task 06 can report it instead of guessing.
"""

from __future__ import annotations

import threading

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from billparse.db import PriceSnapshotRow, default_url, engine_for
from billparse.settings import data_dir

from . import normalize as nz

KEY_COLUMNS = ["key_id", "provider", "as_of", "rate_code", "sku", "service_code", "usage_type", "operation", "region",
               "instance_type", "operating_system", "tenancy", "license_model", "pre_installed_sw", "database_engine",
               "deployment_option", "term_type", "lease_length", "purchase_option", "offering_class", "usage_alt"]
NORMALIZE = {"region": nz.region, "instance_type": nz.instance_type, "operating_system": nz.operating_system,
             "tenancy": nz.tenancy, "license_model": nz.license_model, "pre_installed_sw": nz.pre_installed_sw,
             "lease_length": nz.term, "purchase_option": nz.payment_option,
             "offering_class": lambda v: str(v).strip().lower()}
OPTIONAL = ["operating_system", "tenancy", "license_model", "pre_installed_sw", "database_engine", "deployment_option"]

_DATE = "(r.effective_from <= k.as_of OR r.first_version) AND (r.effective_to IS NULL OR r.effective_to > k.as_of)"
_TERM = ("r.term_type = k.term_type AND (k.lease_length IS NULL OR r.lease_length = k.lease_length) "
         "AND (k.purchase_option IS NULL OR r.purchase_option = k.purchase_option) "
         "AND (k.offering_class IS NULL OR r.offering_class = k.offering_class)")
_TIER0 = "(r.begin_range IS NULL OR r.begin_range = 0)"
_USED = "(p.capacity_status IS NULL OR p.capacity_status = 'Used')"
_SERVICE = "(k.service_code IS NULL OR p.service_code = k.service_code)"
# a product without an attribute (EC2 has no database engine) is not excluded by it; the products that match more of
# the key's attributes exactly win
_SCORE = "(" + " + ".join(f"CASE WHEN p.{c} = k.{c} THEN 1 ELSE 0 END" for c in OPTIONAL) + ")"
_NOT_UNUSED = "(p.usage_type IS NULL OR p.usage_type NOT LIKE '%Unused%')"     # RDS has no capacity status
_ATTRS = ", ".join(f"p.{c} AS p_{c}" for c in OPTIONAL)            # what tells the candidates apart
_NO_ATTRS = ", ".join(f"NULL AS p_{c}" for c in OPTIONAL)
_OUT = ("k.key_id, {prio} AS priority, '{how}' AS matched_by, {pref} AS preferred, {score} AS score, r.id AS rate_id, r.sku, r.rate_code, r.term_type, "
        "r.unit, r.unit_price, r.currency, r.effective_from, r.effective_from > k.as_of AS before_history, "
        "r.begin_range, r.end_range, {attrs}")
QUERY = " UNION ALL ".join([
    f"SELECT {_OUT.format(prio=1, how='rate_code', pref='true', score=0, attrs=_NO_ATTRS)} FROM price_keys k JOIN price_rate r "
    f"ON r.rate_code = k.rate_code AND {_DATE} WHERE k.rate_code IS NOT NULL",
    f"SELECT {_OUT.format(prio=2, how='sku', pref='true', score=0, attrs=_NO_ATTRS)} FROM price_keys k JOIN price_rate r "
    f"ON r.provider = k.provider AND r.sku = k.sku AND {_TERM} AND {_TIER0} AND {_DATE} WHERE k.sku IS NOT NULL",
    *(f"SELECT {_OUT.format(prio=3, how='usage', pref=_USED, score=_SCORE, attrs=_ATTRS)} FROM price_keys k "
      f"JOIN price_product p ON p.provider = k.provider AND p.usage_type = k.{col} "
      f"AND (k.operation IS NULL OR p.operation = k.operation) "
      f"AND (k.region IS NULL OR p.region IS NULL OR p.region = k.region) AND {_SERVICE} "
      + "".join(f"AND (k.{c} IS NULL OR p.{c} IS NULL OR p.{c} = k.{c}) " for c in OPTIONAL) +
      f"JOIN price_rate r ON r.provider = p.provider AND r.sku = p.sku AND {_TERM} AND {_TIER0} AND {_DATE} "
      f"WHERE k.{col} IS NOT NULL"
      for col in ("usage_type", "usage_alt")),      # two equalities (an OR would not use the index)
    f"SELECT {_OUT.format(prio=4, how='attributes', pref=_NOT_UNUSED, score=_SCORE, attrs=_ATTRS)} FROM price_keys k JOIN price_product p "
    f"ON p.provider = k.provider AND p.instance_type = k.instance_type AND p.region = k.region AND {_USED} "
    f"AND {_SERVICE} "
    + "".join(f"AND (k.{c} IS NULL OR p.{c} IS NULL OR p.{c} = k.{c}) " for c in OPTIONAL)
    + f"JOIN price_rate r ON r.provider = p.provider AND r.sku = p.sku AND {_TERM} AND {_TIER0} AND {_DATE} "
    f"WHERE k.instance_type IS NOT NULL AND k.region IS NOT NULL",
])
SP_KEYS = ["key_id", "as_of", "discounted_sku", "plan_type", "lease_length", "purchase_option"]
SP_QUERY = ("SELECT k.key_id, r.id AS sp_rate_id, p.sku AS plan_sku, p.plan_type, p.lease_length, p.purchase_option, "
            "r.unit, r.rate, r.currency, r.effective_from, r.effective_from > k.as_of AS before_history FROM sp_keys k "
            "JOIN price_sp_rate r ON r.discounted_sku = k.discounted_sku AND " + _DATE + " "
            "JOIN price_sp_plan p ON p.sku = r.plan_sku AND (k.plan_type IS NULL OR p.plan_type = k.plan_type) "
            "AND (k.lease_length IS NULL OR p.lease_length = k.lease_length) "
            "AND (k.purchase_option IS NULL OR p.purchase_option = k.purchase_option)")


def _keys_frame(keys: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    k = keys.copy()
    for c in columns:
        if c not in k:
            k[c] = None
    k["provider"] = k.get("provider", pd.Series("aws", index=k.index)).fillna("aws")
    if "term_type" in columns:
        k["term_type"] = k["term_type"].fillna("on_demand")
    k["as_of"] = pd.to_datetime(k["as_of"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "usage_alt" in columns:          # us-east-1's instance usage types have no region prefix in the price list
        u = k["usage_type"].astype(object)
        k["usage_alt"] = u.where(u.astype(str).str.startswith("USE1-"), None).map(
            lambda v: v[5:] if isinstance(v, str) else None)
    for c, fn in NORMALIZE.items():
        if c in k and c in columns:
            vals = {v: fn(v) for v in k[c].dropna().unique()}
            k[c] = k[c].map(vals).astype(object).where(k[c].notna(), None)
    k = k[columns].astype(object)
    return k.where(k.notna(), None)


def _key_ids(found: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    """The key ids as the caller gave them (the keys table holds text)."""
    if not found.empty:
        found["key_id"] = found["key_id"].map(dict(zip(keys["key_id"].astype(str), keys["key_id"], strict=True)))
    return found


def _typed(found: pd.DataFrame) -> pd.DataFrame:
    """The same types on every database (PostgreSQL gives Decimal and True, SQLite float and 1): prices are float,
    flags bool, counts int, so quantity * unit_price works the same in tests and in production."""
    for c in ("unit_price", "rate"):
        if c in found:
            found[c] = pd.to_numeric(found[c], errors="coerce").astype(float)
    for c in ("before_history", "tiered"):
        if c in found:
            found[c] = found[c].fillna(False).astype(bool)
    if "candidates" in found:
        found["candidates"] = found["candidates"].astype(int)
    return found


class PriceCatalog:
    def __init__(self, engine: Engine):
        self.engine = engine

    @classmethod
    def load(cls, url: str | None = None) -> PriceCatalog:
        return cls(engine_for(url or default_url(data_dir())))

    @property
    def version(self) -> str | None:
        """The current snapshot's id, or None when nothing is loaded."""
        snap = PriceSnapshotRow.__table__
        with self.engine.connect() as conn:
            return conn.execute(sa.select(snap.c.id).where(snap.c.status == "current")).scalar_one_or_none()

    def _run(self, temp: str, columns: list[str], frame: pd.DataFrame, sql: str) -> pd.DataFrame:
        with self.engine.begin() as conn:
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS {temp}")
            conn.exec_driver_sql(f"CREATE TEMP TABLE {temp} ({', '.join(c + ' TEXT' for c in columns)})")
            if conn.dialect.name == "postgresql":
                raw = conn.connection.driver_connection
                with raw.cursor() as cur, cur.copy(f"COPY {temp} ({', '.join(columns)}) FROM STDIN") as cp:
                    for row in frame.itertuples(index=False, name=None):
                        cp.write_row(row)
                conn.exec_driver_sql(f"ANALYZE {temp}")
            else:
                conn.execute(sa.text(f"INSERT INTO {temp} VALUES ({', '.join(':' + c for c in columns)})"),
                             frame.to_dict("records"))
            rows = conn.execute(sa.text(sql)).mappings().all()
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS {temp}")
        return pd.DataFrame(rows)

    def lookup(self, keys: pd.DataFrame) -> pd.DataFrame:
        """keys: one row per distinct key: key_id, as_of (a date), and any of provider (default aws), rate_code, sku,
        service_code (AmazonEC2: a CUR line's product code), usage_type, operation, region, instance_type, operating_system, tenancy, license_model, pre_installed_sw,
        database_engine, deployment_option, term_type (default on_demand), lease_length, purchase_option,
        offering_class (standard | convertible).
        Returns one row per key that found a price: key_id, matched_by, candidates, unit_price (None when
        candidates > 1), unit, currency, sku, rate_code, rate_id, effective_from, before_history (the date is before
        the oldest price the catalog knows: the price is that oldest one), tiered (the price has more tiers),
        differs (candidates > 1: the attributes that tell the candidates apart, "operating_system, license_model").
        Keys that found nothing are absent."""
        frame = _keys_frame(keys, KEY_COLUMNS)
        found = _key_ids(self._run("price_keys", KEY_COLUMNS, frame, QUERY), keys)
        if found.empty:
            return found
        found["preferred"] = found["preferred"].astype(bool)          # SQLite gives 0/1
        best = found[found["priority"] == found.groupby("key_id")["priority"].transform("min")]
        # a usage type that capacity reservations share (Outposts): the "Used" product, when the key has one
        best = best[best["preferred"] | ~best.groupby("key_id")["preferred"].transform("any")]
        best = best[best["score"] == best.groupby("key_id")["score"].transform("max")]
        g = best.groupby("key_id")
        best = best.assign(candidates=g["unit_price"].transform("nunique"), tiered=best["end_range"].notna())
        varies = pd.DataFrame({c: g[f"p_{c}"].transform("nunique").gt(1) for c in OPTIONAL}, index=best.index)
        best = best.assign(differs=varies.apply(lambda r: ", ".join(c for c, v in r.items() if v) or None, axis=1))
        out = best.drop_duplicates("key_id").copy()
        out.loc[out["candidates"] > 1, ["unit_price", "rate_code", "rate_id", "sku"]] = None
        out.loc[out["candidates"] <= 1, "differs"] = None
        return _typed(out.drop(columns=["priority", "preferred", "score", "begin_range", "end_range",
                                        *(f"p_{c}" for c in OPTIONAL)]).reset_index(drop=True))

    def lookup_savings_plans(self, keys: pd.DataFrame) -> pd.DataFrame:
        """keys: key_id, as_of, discounted_sku, and any of plan_type, lease_length, purchase_option. Returns every
        Savings Plans rate that applies (one per plan type, term and payment option the key leaves open)."""
        frame = _keys_frame(keys, SP_KEYS)
        return _typed(_key_ids(self._run("sp_keys", SP_KEYS, frame, SP_QUERY), keys))


_CATALOGS: dict[str, PriceCatalog] = {}
_LOCK = threading.Lock()


def get_catalog(url: str | None = None) -> PriceCatalog:
    """One catalog per database, shared by the threads of a process (like analyze.get_resolver)."""
    key = url or default_url(data_dir())
    with _LOCK:
        if key not in _CATALOGS:
            _CATALOGS[key] = PriceCatalog(engine_for(key))
        return _CATALOGS[key]

