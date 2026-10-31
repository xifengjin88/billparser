"""The service catalog (svcres: what a bill's service names and codes resolve to), from the database: the code lists the
providers publish (table service_product), the products of the prices loaded (price_product), and the aliases
(table service_synonym). Rebuilt after every price load (the weekly refresh, make prices) and every code-list load
(make service-products); each process loads it again when the file changes.

    load_products(engine, "azure", rows, "azure-retail-prices")   # replace a provider's code list
    rebuild(engine, catalog_dir())        # catalog_dir/catalog.msgpack; catalog_dir/raw/*.json: the last fallback

Per provider, the rows the catalog is built from, in this order (a later one wins for the same product):
    service_product   the provider's whole list (scripts/fetch_*.py output, loaded by scripts/load_service_products.py)
    price_product     the products of the prices loaded: azure productId, productName, serviceName, serviceId,
                      serviceFamily; aws the service code and its serviceName (the most used first); gcp the service
                      ID and its display name
A provider with neither comes from the fetch scripts' JSON in catalog_dir/raw, as before.

The built-in aliases (svcres.seeds: AWS short names, Azure aliases, Marketplace sellers) are added to service_synonym
once, source `seed` (a journal entry, stream `synonym_seed`, records it): they can then be seen and changed like the
learned ones, and a label a person already has is never overwritten.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from svcres.catalog import Catalog, build
from svcres.normalize import code_key, norm
from svcres.seeds import AWS_SEED, AZURE_ALIASES, MARKETPLACE_SELLERS

from .db import JournalRow, ServiceProductRow, ServiceSynonymRow, engine_for
from .db import price_product as P

log = logging.getLogger(__name__)
SP = ServiceProductRow.__table__
SYN = ServiceSynonymRow.__table__
J = JournalRow.__table__
SEED_STREAM = "synonym_seed"
SOURCES = {"azure": "azure-retail-prices", "aws": "aws-price-list", "gcp": "gcp-billing-catalog"}
RAW_FILES = {"azure": "azure_products.json", "aws": "aws_services.json", "gcp": "gcp_services.json"}


def _attr(name: str) -> sa.ColumnElement:
    return P.c.attributes[name].as_string()


def _insert(engine: Engine, table: sa.Table):
    return postgresql.insert(table) if engine.dialect.name == "postgresql" else sqlite.insert(table)


# ---------------------------------------------------------------- the providers' code lists
def products_from_json(raw_dir: Path, providers: tuple[str, ...] = tuple(RAW_FILES)) -> dict[str, list[dict]]:
    """The fetch scripts' JSON (scripts/fetch_azure.py, fetch_aws.py, fetch_gcp.py) as service_product rows."""
    out: dict[str, list[dict]] = {}
    for provider in providers:
        path = Path(raw_dir) / RAW_FILES[provider]
        if not path.is_file():
            continue
        data = json.loads(path.read_text())
        if provider == "azure":
            out[provider] = [{"product_id": pid, "product_name": p.get("productName") or "",
                              "service_id": p.get("serviceId"), "service_name": p.get("serviceName"),
                              "family": p.get("serviceFamily")}
                             for pid, p in data.items() if p.get("serviceName")]
        elif provider == "aws":
            out[provider] = [{"product_id": code, "product_name": name, "service_id": code, "service_name": name}
                             for code, v in data.items() for name in ((v or {}).get("names") or [""])]
        else:
            out[provider] = [{"product_id": sid, "product_name": s.get("displayName") or "", "service_id": sid,
                              "service_name": s.get("displayName")}
                             for sid, s in data.items() if s.get("displayName")]
    return out


def load_products(engine: Engine, provider: str, rows: list[dict], source: str | None = None,
                  fetched_at: float | None = None) -> int:
    """Replace the provider's code list with rows (one transaction: readers see the old list or the new one).
    Returns how many rows it has now. An empty download replaces nothing (a failed fetch must not empty the list)."""
    if not rows:
        return 0
    now = fetched_at or time.time()
    seen, clean = set(), []
    for r in rows:
        key = (str(r["product_id"]), str(r.get("product_name") or ""))
        if key[0] and key not in seen:
            seen.add(key)
            clean.append({"provider": provider, "product_id": key[0], "product_name": key[1],
                          "service_id": r.get("service_id"), "service_name": r.get("service_name"),
                          "family": r.get("family"), "attributes": r.get("attributes"),
                          "source": source or SOURCES.get(provider, "manual"), "fetched_at": now})
    with engine.begin() as c:
        c.execute(SP.delete().where(SP.c.provider == provider))
        for i in range(0, len(clean), 5000):
            c.execute(SP.insert(), clean[i:i + 5000])
    log.info("service_product: %d %s rows", len(clean), provider)
    return len(clean)


# ---------------------------------------------------------------- the catalog
def catalog_rows(engine: Engine) -> dict[str, list[dict]]:
    """provider -> rows for svcres.build: the code lists first, then the price catalog's products (which win);
    empty when the tables are missing (a store without the migrations)."""
    aws_name = _attr("serviceName")                 # one expression in SELECT and GROUP BY (PostgreSQL compares them)
    prices = {
        "azure": sa.select(_attr("productId").label("productId"), P.c.product_name.label("productName"),
                           P.c.service_code.label("serviceName"), _attr("serviceId").label("serviceId"),
                           P.c.product_family.label("serviceFamily"))
                   .where(P.c.provider == "azure").distinct(),
        "aws": sa.select(P.c.service_code.label("code"), aws_name.label("name"), sa.func.count().label("n"))
                 .where(P.c.provider == "aws").group_by(P.c.service_code, aws_name)
                 .order_by(P.c.service_code, sa.func.count().desc()),
        "gcp": sa.select(P.c.service_code.label("code"), _attr("service_display_name").label("name"))
                 .where(P.c.provider == "gcp").distinct(),
    }
    out: dict[str, list[dict]] = {p: [] for p in prices}
    try:
        with engine.connect() as conn:
            for r in conn.execute(sa.select(SP).order_by(SP.c.provider, SP.c.id)):
                if r.provider == "azure":
                    out["azure"].append({"productId": r.product_id, "productName": r.product_name or None,
                                         "serviceName": r.service_name, "serviceId": r.service_id,
                                         "serviceFamily": r.family})
                elif r.provider in out:
                    out[r.provider].append({"code": r.product_id, "name": r.product_name or None})
            for provider, q in prices.items():
                out[provider] += [dict(r._mapping) for r in conn.execute(q)]
    except DBAPIError as e:                         # no tables (a store without the migrations): JSON only
        log.warning("service catalog: the database could not be read (%s); the JSON lists only", type(e).__name__)
        return {}
    return out


def rows_for_url(url: str | None) -> dict[str, list[dict]]:
    """catalog_rows of the database at url; empty when there is none or it cannot be opened (a new SQLite store)."""
    if not url:
        return {}
    try:
        return catalog_rows(engine_for(url))
    except DBAPIError as e:
        log.warning("service catalog: %s could not be opened (%s); the JSON lists only", url.split("@")[-1],
                    type(e).__name__)
        return {}


def rebuild(engine: Engine, catalog_dir: Path) -> dict:
    """Build catalog_dir/catalog.msgpack from the database (and catalog_dir/raw for the rest), add the built-in
    aliases to service_synonym once; returns the catalog's stats."""
    catalog_dir.mkdir(parents=True, exist_ok=True)
    cat = build(catalog_dir / "raw", catalog_rows(engine))
    cat.save(catalog_dir / "catalog.msgpack")
    stats = cat.stats()
    try:
        stats["seeded_synonyms"] = seed_synonyms(engine, cat)
    except DBAPIError as e:
        log.warning("service catalog: the built-in aliases were not added (%s)", type(e).__name__)
    log.info("service catalog rebuilt: %s", stats)
    return stats


# ---------------------------------------------------------------- the built-in aliases
def seed_rows(cat: Catalog) -> list[dict]:
    """The built-in aliases as service_synonym rows, for the services this catalog has."""
    rows = []
    for code, (_category, aliases) in AWS_SEED.items():
        key = cat.codes.get(f"aws:{code_key(code)}")
        rows += [("aws", a, key) for a in aliases if key]
    for name, aliases in AZURE_ALIASES.items():
        if f"azure:{name}" in cat.services:
            rows += [("azure", a, f"azure:{name}") for a in aliases]
    for provider, sellers in MARKETPLACE_SELLERS.items():
        if f"{provider}:Marketplace" in cat.services:
            rows += [(provider, s, f"{provider}:Marketplace") for s in sellers]
    seen, out = set(), []
    for scope, alias, key in rows:
        label = norm(alias)
        if label and (scope, label) not in seen:
            seen.add((scope, label))
            out.append({"scope": scope, "label": label, "service_key": key})
    return out


def seed_synonyms(engine: Engine, cat: Catalog) -> int:
    """The built-in aliases into service_synonym, once (source seed; a label already there keeps its row). Returns
    how many were added (0 when done before)."""
    with engine.begin() as c:
        if c.execute(sa.select(J.c.id).where(J.c.stream == SEED_STREAM).limit(1)).first():
            return 0
        now = time.time()
        rows = [dict(r, source="seed", created=now, updated=now, updated_by=None) for r in seed_rows(cat)]
        before = c.execute(sa.select(sa.func.count()).select_from(SYN)).scalar() or 0
        if rows:
            c.execute(_insert(engine, SYN).values(rows).on_conflict_do_nothing(index_elements=[SYN.c.scope,
                                                                                               SYN.c.label]))
        added = (c.execute(sa.select(sa.func.count()).select_from(SYN)).scalar() or 0) - before
        c.execute(J.insert().values(stream=SEED_STREAM, ts=now, data={"aliases": len(rows), "added": added}))
    log.info("service_synonym: %d built-in aliases added", added)
    return added
