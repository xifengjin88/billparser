"""Fetch AWS list prices with the AWS Price List query API, for billparse's price catalog (prices/). Read only.

Needs AWS credentials (any account: the price APIs are free and read public prices) and boto3. Run it from an
environment that has both, for example the reserved-ai server directory:

    cd /Users/xifengjin/worktrees/reserved-ai/export-public-catalog/server
    uv run python /Users/xifengjin/projects/billparse/scripts/aws/fetch_aws_prices.py --out ~/aws-prices
    uv run python .../fetch_aws_prices.py --out ~/aws-prices --service AmazonEC2 --region us-east-1
    uv run python .../fetch_aws_prices.py --out ~/aws-prices --savings-plans

What it writes (raw AWS answers, nothing renamed; billparse maps them):
    products/<ServiceCode>/<region>.jsonl.gz   one object per product: GetProducts' PriceList entry (product, its
                                               attributes, and its OnDemand and Reserved terms with their prices)
    savings_plans/<region>.jsonl.gz            DescribeSavingsPlansOfferingRates results (with --savings-plans)
    manifest.json                              time, account, services, regions, counts, filters

It never writes to AWS. A finished file is kept when you run it again (a ".done" marker), so a stopped run continues
where it stopped; --force fetches again. Throttling is retried (adaptive mode). The query API returns today's prices:
past prices come from the bulk price files' version index (a later step), not from this script.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.config import Config

# the services billparse prices first (task 05/06): compute, databases, caches, search, warehouse
SERVICES = ["AmazonEC2", "AmazonRDS", "AmazonElastiCache", "AmazonES", "AmazonRedshift"]
# no default filter: a TERM_MATCH on an attribute drops every product without it (capacitystatus=Used drops EBS,
# data transfer and NAT from EC2); billparse's lookup chooses the "Used" price where the attribute applies
DEFAULT_FILTERS: dict[str, dict[str, str]] = {}
SP_PRODUCTS = ["EC2", "Fargate", "Lambda", "SageMaker", "RDS"]   # Savings Plans rate products
RETRY = Config(retries={"max_attempts": 12, "mode": "adaptive"}, read_timeout=120)


def pricing_client():
    return boto3.client("pricing", region_name="us-east-1", config=RETRY)   # the API lives in us-east-1


def regions_of(client, service: str) -> list[str]:
    """The region codes the service has prices in."""
    out, token = [], None
    while True:
        kw = {"ServiceCode": service, "AttributeName": "regionCode", "MaxResults": 100}
        if token:
            kw["NextToken"] = token
        page = client.get_attribute_values(**kw)
        out += [v["Value"] for v in page.get("AttributeValues", [])]
        token = page.get("NextToken")
        if not token:
            return sorted(out)


def fetch_products(service: str, region: str, filters: dict[str, str], path: Path) -> dict:
    """Every product of a service in a region, one JSON object per line."""
    client = pricing_client()
    flt = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    flt += [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()]
    tmp = path.with_suffix(".part")
    n, started = 0, time.monotonic()
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        for page in client.get_paginator("get_products").paginate(ServiceCode=service, Filters=flt,
                                                                  FormatVersion="aws_v1",
                                                                  PaginationConfig={"PageSize": 100}):
            for item in page["PriceList"]:
                fh.write((item if isinstance(item, str) else json.dumps(item, separators=(",", ":"))) + "\n")
                n += 1
    tmp.replace(path)
    return {"rows": n, "seconds": round(time.monotonic() - started, 1), "bytes": path.stat().st_size}


def fetch_savings_plans(region: str, path: Path) -> dict:
    """Savings Plans rates for one region (every plan type, term and payment option)."""
    client = boto3.client("savingsplans", region_name="us-east-1", config=RETRY)
    tmp = path.with_suffix(".part")
    n, token, started = 0, None, time.monotonic()
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        while True:
            kw = {"products": SP_PRODUCTS, "filters": [{"name": "region", "values": [region]}], "maxResults": 1000}
            if token:
                kw["nextToken"] = token
            page = client.describe_savings_plans_offering_rates(**kw)
            for rate in page.get("searchResults", []):
                fh.write(json.dumps(rate, separators=(",", ":"), default=str) + "\n")
                n += 1
            token = page.get("nextToken")
            if not token:
                break
    tmp.replace(path)
    return {"rows": n, "seconds": round(time.monotonic() - started, 1), "bytes": path.stat().st_size}


def run(jobs: list[tuple[str, str, Path, callable]], workers: int, force: bool) -> dict:
    results: dict = {}

    def one(job):
        name, region, path, fn = job
        done = path.with_name(path.name + ".done")
        if done.exists() and path.exists() and not force:
            return name, region, {**json.loads(done.read_text()), "skipped": True}
        path.parent.mkdir(parents=True, exist_ok=True)
        res = fn(path)
        done.write_text(json.dumps(res))
        return name, region, res
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, j): j for j in jobs}
        for f in as_completed(futures):
            name, region = futures[f][:2]
            try:
                _, _, res = f.result()
            except Exception as e:  # noqa: BLE001  one region failing must not stop the others; it is reported
                res = {"error": f"{type(e).__name__}: {e}"}
            results.setdefault(name, {})[region] = res
            mark = "skipped" if res.get("skipped") else ("ERROR " + res["error"] if "error" in res else
                                                         f"{res['rows']:,} rows in {res['seconds']}s")
            print(f"{name:20} {region:16} {mark}", flush=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--service", action="append", help=f"repeatable; default: {', '.join(SERVICES)}")
    ap.add_argument("--region", action="append", help="repeatable region code; default: every region the service has")
    ap.add_argument("--filter", action="append", default=[], metavar="FIELD=VALUE",
                    help="extra TERM_MATCH filter for every service (e.g. operatingSystem=Linux)")
    ap.add_argument("--no-default-filters", action="store_true", help="keep EC2 capacity-reservation prices too")
    ap.add_argument("--savings-plans", action="store_true", help="also fetch Savings Plans rates")
    ap.add_argument("--workers", type=int, default=4, help="parallel requests (the API throttles above a few)")
    ap.add_argument("--force", action="store_true", help="fetch again files that are already done")
    a = ap.parse_args()

    sts = boto3.client("sts")
    try:
        account = sts.get_caller_identity()["Account"]
    except Exception as e:  # noqa: BLE001  a clear message instead of a botocore trace
        print(f"No usable AWS credentials ({e}). Log in first (aws sso login / AWS_PROFILE).", file=sys.stderr)
        return 2
    services = a.service or SERVICES
    extra = dict(f.split("=", 1) for f in a.filter)
    client = pricing_client()
    started = datetime.now(UTC)

    jobs = []
    for service in services:
        regions = a.region or regions_of(client, service)
        filters = {**({} if a.no_default_filters else DEFAULT_FILTERS.get(service, {})), **extra}
        for region in regions:
            path = a.out / "products" / service / f"{region}.jsonl.gz"
            jobs.append((service, region, path, lambda p, s=service, r=region, f=filters: fetch_products(s, r, f, p)))
    if a.savings_plans:
        sp_regions = a.region or sorted({r for _, r, _, _ in jobs})
        for region in sp_regions:
            path = a.out / "savings_plans" / f"{region}.jsonl.gz"
            jobs.append(("SavingsPlans", region, path, lambda p, r=region: fetch_savings_plans(r, p)))
    print(f"{len(jobs)} file(s) to fetch with {a.workers} worker(s), account {account}", flush=True)
    results = run(jobs, a.workers, a.force)

    manifest = {
        "fetched_at": started.isoformat(), "finished_at": datetime.now(UTC).isoformat(), "account": account,
        "api": "pricing:GetProducts (FormatVersion aws_v1), savingsplans:DescribeSavingsPlansOfferingRates",
        "services": services, "regions": a.region or "all", "filters": {"default": None if a.no_default_filters
                                                                         else DEFAULT_FILTERS, "extra": extra},
        "results": results,
        "totals": {name: sum(r.get("rows", 0) for r in regs.values()) for name, regs in results.items()},
        "errors": sum("error" in r for regs in results.values() for r in regs.values()),
    }
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {a.out / 'manifest.json'}; totals {manifest['totals']}; errors {manifest['errors']}")
    return 1 if manifest["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
