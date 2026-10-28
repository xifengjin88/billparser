"""Record a correction: map a raw label to a service key. Takes effect immediately, in the database's
service_synonym table (DATABASE_URL from .env, else the SQLite file in PARSER_DATA_DIR), as `python -m billparse synonym`.

    python scripts/add_synonym.py "VM Compute Hrs" "azure:Virtual Machines"
    python scripts/add_synonym.py "Cloud Hosting" "aws:Amazon Elastic Compute Cloud" --provider aws
    python scripts/add_synonym.py --search "virtual mach"     # find service keys
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billparse.settings import load_env  # noqa: E402
from billparse.synonyms import synonym_store  # noqa: E402
from svcres.catalog import Catalog  # noqa: E402
from svcres.normalize import norm  # noqa: E402
from svcres.resolver import Resolver  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("raw", nargs="?")
    ap.add_argument("service_key", nargs="?")
    ap.add_argument("--provider", choices=["azure", "aws", "gcp"])
    ap.add_argument("--search")
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    a = ap.parse_args()
    load_env()
    cat = Catalog.load(a.data_dir / "catalog.msgpack")

    if a.search:
        q = norm(a.search)
        hits = [k for k in cat.services if q in norm(k)]
        print("\n".join(sorted(hits)[:50]) or "no matches")
        sys.exit()
    if not (a.raw and a.service_key):
        sys.exit("Usage: add_synonym.py RAW SERVICE_KEY [--provider p]")
    Resolver(cat, a.data_dir, synonyms=synonym_store(a.data_dir)).add_synonym(a.raw, a.service_key, a.provider)
    print(f"Saved: {a.raw!r} -> {a.service_key}")
