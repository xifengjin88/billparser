"""Load the providers' code lists (the fetch scripts' JSON) into the table service_product, then rebuild the service
catalog. Each provider's list replaces its old one; an empty or missing file changes nothing.

    python scripts/fetch_azure.py --out data/raw/azure_products.json     # every Azure product (public, no key)
    python scripts/load_service_products.py                              # data/raw: azure, aws and gcp
    python scripts/load_service_products.py --provider azure --raw-dir data/raw

The database is DATABASE_URL (the tables come from the migrations: make migrate).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billparse.db import default_url, engine_for  # noqa: E402
from billparse.service_catalog import RAW_FILES, load_products, products_from_json, rebuild  # noqa: E402
from billparse.settings import catalog_dir, data_dir, load_env  # noqa: E402


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    ap.add_argument("--provider", action="append", choices=list(RAW_FILES), help="repeatable; default: all three")
    ap.add_argument("--no-rebuild", action="store_true", help="do not rebuild CATALOG_DIR/catalog.msgpack")
    a = ap.parse_args()
    engine = engine_for(default_url(data_dir()))
    providers = tuple(a.provider or RAW_FILES)
    found = products_from_json(a.raw_dir, providers)
    for p in providers:
        path = a.raw_dir / RAW_FILES[p]
        n = load_products(engine, p, found.get(p) or [], fetched_at=path.stat().st_mtime if path.exists() else None)
        print(f"{p}: {n:,} rows" if n else f"{p}: nothing loaded ({path} missing or empty); the old list stays")
    if not a.no_rebuild:
        print(f"service catalog rebuilt: {rebuild(engine, catalog_dir())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
