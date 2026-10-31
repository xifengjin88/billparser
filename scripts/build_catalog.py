"""Build data/catalog.msgpack: the price catalog's lists (DATABASE_URL, when it has prices), the fetch scripts' JSON
(data/raw) for the rest, and the seeds. A price load (make prices, the weekly refresh) does this by itself.

    python scripts/build_catalog.py               # the price catalog's lists and data/raw
    python scripts/build_catalog.py --raw-only    # data/raw only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billparse.db import default_url, engine_for  # noqa: E402
from billparse.service_catalog import catalog_rows  # noqa: E402
from billparse.settings import data_dir, load_env  # noqa: E402
from svcres.catalog import build  # noqa: E402

if __name__ == "__main__":
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    ap.add_argument("--out", type=Path, default=Path("data/catalog.msgpack"))
    ap.add_argument("--raw-only", action="store_true", help="ignore the price catalog (DATABASE_URL)")
    a = ap.parse_args()
    cat = build(a.raw_dir, None if a.raw_only else catalog_rows(engine_for(default_url(data_dir()))))
    cat.save(a.out)
    print(f"Wrote {a.out} ({a.out.stat().st_size / 1e6:.2f} MB): {cat.stats()}")
