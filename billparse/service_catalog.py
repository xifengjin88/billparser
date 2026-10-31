"""The service catalog (svcres: what a bill's service names and codes resolve to), built from the price catalog's own
tables, so the mapping is as new as the prices: rebuilt after every price load (the weekly refresh, make prices).

    rebuild(engine, catalog_dir())        # catalog_dir/catalog.msgpack; catalog_dir/raw/*.json is the fallback

Per provider, the distinct rows of price_product (one query each):
    azure  productId, productName, serviceName (service_code), serviceId, serviceFamily (product_family)
    aws    the service code and its name (the price files' serviceName), the most used name first
    gcp    the service ID and its display name
A provider whose prices are not loaded comes from the fetch scripts' JSON (scripts/fetch_*.py), as before.
"""

from __future__ import annotations

import logging
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from svcres.catalog import build

from .db import engine_for
from .db import price_product as P

log = logging.getLogger(__name__)


def _attr(name: str) -> sa.ColumnElement:
    return P.c.attributes[name].as_string()


def catalog_rows(engine: Engine) -> dict[str, list[dict]]:
    """provider -> rows (see the module doc); empty when the price tables are missing or empty."""
    aws_name = _attr("serviceName")                 # one expression in SELECT and GROUP BY (PostgreSQL compares them)
    queries = {
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
    out: dict[str, list[dict]] = {}
    try:
        with engine.connect() as conn:
            for provider, q in queries.items():
                out[provider] = [dict(r._mapping) for r in conn.execute(q)]
    except DBAPIError as e:                         # no price tables (a store without the migrations): JSON only
        log.warning("service catalog: the price catalog could not be read (%s); the JSON lists only",
                    type(e).__name__)
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
    """Build catalog_dir/catalog.msgpack from the price catalog (and catalog_dir/raw for the rest); returns its stats.
    Workers load it again when the file changes (analyze.get_resolver)."""
    catalog_dir.mkdir(parents=True, exist_ok=True)
    cat = build(catalog_dir / "raw", catalog_rows(engine))
    cat.save(catalog_dir / "catalog.msgpack")
    stats = cat.stats()
    log.info("service catalog rebuilt: %s", stats)
    return stats
