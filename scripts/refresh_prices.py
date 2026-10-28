"""Load a download of the AWS price files or of the Azure retail prices into the price catalog tables, as one snapshot.

    python scripts/aws/fetch_aws_price_files.py --all-services --savings-plans --out ~/aws-price-files
    python scripts/refresh_prices.py                             # PRICE_FILES_DIR (default ~/aws-price-files)
    python scripts/refresh_prices.py --only AmazonRDS --partial  # one price list; the others stay as they are
    python scripts/azure/fetch_azure_retail_prices.py --out ~/azure-price-files
    python scripts/refresh_prices.py --azure                     # AZURE_PRICE_FILES_DIR (default ~/azure-price-files)
    python scripts/refresh_prices.py --azure --only "Virtual Machines"

The download's manifest.json says which provider it is, so --dir loads either.
The database is DATABASE_URL (billparse's own database; the tables come from the migrations: make migrate).
Unchanged prices are not written again: only new, changed and removed prices change rows. A load that loses more than
10% of a price list's rates is refused, and the current snapshot stays.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlmodel import create_engine  # noqa: E402

from billparse.db import default_url  # noqa: E402
from billparse.settings import data_dir, load_env  # noqa: E402
from prices.load import Refused, load_snapshot  # noqa: E402


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=None,
                    help="the download folder (with manifest.json); default: PRICE_FILES_DIR (~/aws-price-files), "
                         "with --azure AZURE_PRICE_FILES_DIR (~/azure-price-files)")
    ap.add_argument("--azure", action="store_true", help="the Azure download folder by default")
    ap.add_argument("--database-url", default=None, help="default: DATABASE_URL")
    ap.add_argument("--only", action="append",
                    help="repeatable: load only this price list (AmazonEC2, ...; Azure: a service name)")
    ap.add_argument("--skip", action="append", help="repeatable: load every price list but this one")
    ap.add_argument("--partial", action="store_true", help="do not refuse the price lists the load does not have")
    ap.add_argument("--snapshot-id", help="default: the download date")
    ap.add_argument("--progress-every", type=int, default=100, help="print a line every N files")
    a = ap.parse_args()

    if a.dir is None:
        a.dir = Path(os.environ.get("AZURE_PRICE_FILES_DIR", "~/azure-price-files") if a.azure
                     else os.environ.get("PRICE_FILES_DIR", "~/aws-price-files"))
    only = a.only
    if a.skip:
        manifest = json.loads((a.dir.expanduser() / "manifest.json").read_text())
        azure = manifest.get("provider") == "azure"          # Azure's files are by service name (with spaces)
        names = {n if azure else n.split()[0] for n in manifest["files"]}
        only = sorted((set(only) if only else names) - set(a.skip))
    engine = create_engine(a.database_url or default_url(data_dir()))   # the tables come from the migrations
    started, n = time.monotonic(), 0

    def progress(line: str) -> None:
        nonlocal n
        n += 1
        if n % a.progress_every == 0:
            print(f"{time.monotonic() - started:7.0f}s  {n:,} files  {line}", flush=True)

    try:
        res = load_snapshot(engine, a.dir.expanduser(), a.snapshot_id, partial=a.partial or bool(only),
                            only=only, on_file=progress)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"snapshot {res.snapshot_id} is {res.status}: {res.products:,} products, {res.rates:,} rates, "
          f"{res.sp_rates:,} savings plans rates; {res.closed:,} closed, {res.inserted:,} inserted; "
          f"seconds {res.seconds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
