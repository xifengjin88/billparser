"""Fetch AWS service codes and names into the table service_product (the service catalog's AWS list), then rebuild
the service catalog.

Codes (no auth): the public Price List offer index lists every service code (AmazonEC2, AmazonS3, ...).
Names, the first source that has them:
    the Price List Query API (needs boto3 and AWS credentials with pricing:GetAttributeValues)
    the AWS price files already loaded (their serviceName: AWS's own names, no credentials)
    the built-in names (svcres.seeds)

    uv run --with boto3 python scripts/fetch_aws.py          # the database is DATABASE_URL
    python scripts/fetch_aws.py --no-names                    # no Query API: price files and built-in names
    python scripts/fetch_aws.py --json data/raw/aws_services.json   # also write the JSON
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billparse.db import default_url, engine_for  # noqa: E402
from billparse.service_catalog import aws_price_names, aws_rows, load_products, rebuild  # noqa: E402
from billparse.settings import catalog_dir, data_dir, load_env  # noqa: E402
from svcres.seeds import AWS_SEED  # noqa: E402

INDEX = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json"


def fetch_codes() -> list[str]:
    r = requests.get(INDEX, timeout=60)
    r.raise_for_status()
    return sorted(r.json()["offers"].keys())


def fetch_names(codes: list[str]) -> dict[str, list[str]]:
    try:
        import boto3  # optional dependency
        client = boto3.client("pricing", region_name="us-east-1")
    except Exception as e:  # noqa: BLE001
        print(f"Skipping names (boto3 or credentials unavailable: {e})")
        return {}

    names: dict[str, list[str]] = {}
    for i, code in enumerate(codes, 1):
        try:
            vals: list[str] = []
            kwargs = {"ServiceCode": code, "AttributeName": "servicename"}
            while True:
                resp = client.get_attribute_values(**kwargs)
                vals += [v["Value"] for v in resp.get("AttributeValues", [])]
                if "NextToken" not in resp:
                    break
                kwargs["NextToken"] = resp["NextToken"]
            if vals:
                names[code] = vals
        except Exception as e:  # noqa: BLE001  some codes have no servicename attribute
            print(f"  {code}: {e.__class__.__name__}")
        if i % 25 == 0:
            print(f"{i}/{len(codes)} codes")
    return names


def main(argv: list[str] | None = None) -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-names", action="store_true", help="skip the Query API")
    ap.add_argument("--json", type=Path, help="also write the codes and names as JSON (code -> {names})")
    ap.add_argument("--out", type=Path, help=argparse.SUPPRESS)             # the old name of --json
    ap.add_argument("--no-db", action="store_true", help="do not write the database")
    ap.add_argument("--no-rebuild", action="store_true", help="do not rebuild CATALOG_DIR/catalog.msgpack")
    a = ap.parse_args(argv)
    codes = fetch_codes()
    print(f"{len(codes)} service codes")
    names = {} if a.no_names else fetch_names(codes)
    engine = None if a.no_db and not (a.json or a.out) else engine_for(default_url(data_dir()))
    sources = {"query api": sum(1 for c in codes if names.get(c))}
    if engine is not None:
        price = aws_price_names(engine)
        missing = [c for c in codes if not names.get(c) and price.get(c)]
        for c in missing:
            names[c] = price[c]
        sources["price files"] = len(missing)
    seeded = [c for c in codes if not names.get(c) and AWS_SEED.get(c, (None, []))[1]]
    for c in seeded:
        names[c] = AWS_SEED[c][1][:1]
    sources["built-in"] = len(seeded)
    sources["none"] = sum(1 for c in codes if not names.get(c))
    print(f"names from: {sources}")
    out = {c: {"names": names.get(c, [])} for c in codes}
    if a.json or a.out:
        path = a.json or a.out
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=1))
        print(f"Wrote {path}")
    if not a.no_db:
        n = load_products(engine, "aws", aws_rows({c: v["names"] for c, v in out.items()}), "aws-price-list")
        print(f"service_product: {n} AWS rows")
        if not a.no_rebuild:
            print(f"service catalog rebuilt: {rebuild(engine, catalog_dir())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
