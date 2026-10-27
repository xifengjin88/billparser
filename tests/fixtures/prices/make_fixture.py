"""Cut the price-catalog test fixture from a download of the public AWS price files (no customer data).

    python tests/fixtures/prices/make_fixture.py ~/aws-price-files

Writes tests/fixtures/prices/download/ (the layout and manifest.json of scripts/aws/fetch_aws_price_files.py) with a
few products of us-east-1: m5.xlarge Linux (plain, SQL Std, Ent and Web, an unused capacity reservation), m5.xlarge
Windows (license included, BYOL, license billed apart, SQL Std), AWSOutposts' m5.xlarge, an RDS MySQL db.m5.large, an
S3 storage price with tiers, and the Compute Savings Plans rates of the Linux m5.xlarge.
"""

from __future__ import annotations

import csv
import gzip
import json
import sys
from pathlib import Path

OUT = Path(__file__).parent / "download"
OFFERS = {
    # m5.xlarge Shared Used: Linux plain and with SQL Std / Ent / Web; Windows plain, BYOL, license billed apart
    # (:box), with SQL Std, and SQL Std :box; Linux's unused capacity reservation
    "AmazonEC2": {"5G4TA8Z4MUKE6MJB", "EHFDZFP2EH9BD4DX", "E47BRDCTED8CBHEU", "TSKNSGPA2KR5BHCN",
                  "HXGQ45BFM9J2FJ3F", "8AW2DYRV3M9N5RRZ", "AWJMJ73C9RDQFD5N", "XX43P9XZN7A2HHHK", "7EQYX3EPSQG98BNK",
                  "E9RWRGDUV5X76FUB"},
    "AWSOutposts": {"WYFNYKVVU5FD662T"},                # an Outpost m5.xlarge: the usage type of EC2's
    "AmazonRDS": {"C86H957YE4QF4727", "C54NGRMKCQEBBF3B"},
    "AmazonS3": {"WP9ANXZGBYYSGJEA"},
}
SP_DISCOUNTED = {"5G4TA8Z4MUKE6MJB"}


def cut(src: Path, dst: Path, keep) -> int:
    with gzip.open(src, "rt", encoding="utf-8", newline="") as fh:
        head = [fh.readline() for _ in range(5)]
        reader = csv.reader(fh)
        names = next(reader)
        rows = [r for r in reader if keep(dict(zip(names, r, strict=False)))]
    dst.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(dst, "wt", encoding="utf-8", newline="") as fh:
        fh.writelines(head)
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(names)
        w.writerows(rows)
    return len(rows)


def main(src: Path) -> None:
    files = {}
    for offer, skus in OFFERS.items():
        version = sorted(p.name for p in (src / "offers" / offer).iterdir() if p.is_dir())[-1]
        n = cut(src / "offers" / offer / version / "us-east-1.csv.gz", OUT / "offers" / offer / version / "us-east-1.csv.gz",
                lambda r, s=skus: r["SKU"] in s)
        files[f"{offer} {version}"] = {"us-east-1": {"rows": n}}
    offer = "AWSComputeSavingsPlan"
    version = sorted(p.name for p in (src / "savings_plans" / offer).iterdir() if p.is_dir())[-1]
    n = cut(src / "savings_plans" / offer / version / "us-east-1.csv.gz",
            OUT / "savings_plans" / offer / version / "us-east-1.csv.gz", lambda r: r["DiscountedSKU"] in SP_DISCOUNTED)
    files[f"{offer} {version}"] = {"us-east-1": {"rows": n}}
    manifest = {"fetched_at": "2026-10-27T00:00:00+00:00", "files": files}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(files, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser())
