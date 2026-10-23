"""Fetch AWS service codes and display names.

Step 1 (no auth): the public Price List offer index lists every service code
(AmazonEC2, AmazonS3, ...).

Step 2 (optional, needs AWS credentials with pricing:GetAttributeValues):
the Price List Query API returns the human-readable name for each code
("Amazon Elastic Compute Cloud"), which is what invoices usually show.
Without credentials, names fall back to the seed list and the code itself.

Usage:
    python scripts/fetch_aws.py
    python scripts/fetch_aws.py --no-names       # skip step 2
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests

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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("data/raw/aws_services.json"))
    ap.add_argument("--no-names", action="store_true")
    a = ap.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)

    codes = fetch_codes()
    print(f"{len(codes)} service codes")
    names = {} if a.no_names else fetch_names(codes)
    out = {c: {"names": names.get(c, [])} for c in codes}
    a.out.write_text(json.dumps(out, indent=1))
    print(f"Wrote {a.out} ({sum(1 for v in out.values() if v['names'])} with names)")
