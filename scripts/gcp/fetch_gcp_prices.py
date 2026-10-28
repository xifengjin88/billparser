"""Download GCP's public list prices (the Cloud Billing Catalog API) for billparse's price catalog (prices/).

The API needs an API key (a Google Cloud project with the Cloud Billing API enabled; the key needs no other role).
Set it as GCP_BILLING_API_KEY; without it the script stops with a message. Only Python's standard library:

    GCP_BILLING_API_KEY=... uv run python scripts/gcp/fetch_gcp_prices.py --out ~/gcp-price-files
    uv run python scripts/gcp/fetch_gcp_prices.py --out ~/gcp-price-files --service 6F81-5844-456A   # Compute Engine
    uv run python scripts/gcp/fetch_gcp_prices.py --out ~/gcp-price-files --all-services   # also Maps, marketplace
    uv run python scripts/gcp/fetch_gcp_prices.py --out ~/gcp-price-files --dry-run        # the services, no SKUs

How Google publishes them (https://cloud.google.com/billing/docs/reference/rest, checked on 2026-10-27):
    GET /v1/services?pageSize=5000&pageToken=...           services[]: name, serviceId, displayName,
                                                           businessEntityName (businessEntities/GCP | /Maps | ...)
    GET /v1/services/<serviceId>/skus?currencyCode=USD&pageSize=5000&pageToken=...
                                                           skus[]: skuId, description, category {serviceDisplayName,
                                                           resourceFamily, resourceGroup, usageType}, serviceRegions,
                                                           pricingInfo[] {effectiveTime, pricingExpression {usageUnit,
                                                           usageUnitDescription, baseUnit, baseUnitConversionFactor,
                                                           tieredRates[] {startUsageAmount, unitPrice {currencyCode,
                                                           units, nanos}}}}, geoTaxonomy
    Both pages end when nextPageToken is empty. Without startTime/endTime the latest prices come back.

What it writes:
    skus/<serviceId>.jsonl.gz    one SKU per line, as the API returns it
    manifest.json                "provider": "gcp", the services, per file the SKU count and size, time, errors
A service already downloaded today is kept on the next run (a ".done" marker), so a stopped run continues.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

BASE = "https://cloudbilling.googleapis.com/v1"
KEY_ENV = "GCP_BILLING_API_KEY"
GCP = "businessEntities/GCP"                # Google Cloud's own services (not Maps, not the marketplace)
PAGE = 5000


def get_json(path: str, params: dict, key: str, tries: int = 4) -> dict:
    """One API page; retried on network errors and on 429 / 5xx. The key goes in the query (never printed)."""
    url = f"{BASE}{path}?{urllib.parse.urlencode({**params, 'key': key})}"
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries:
                raise RuntimeError(f"{path}: HTTP {e.code} {e.reason}") from None
        except OSError as e:
            if attempt == tries:
                raise RuntimeError(f"{path}: {type(e).__name__}") from None
        print(f"  retry {attempt} {path}", file=sys.stderr, flush=True)
        time.sleep(5 * attempt)
    raise RuntimeError("unreachable")


def pages(path: str, field: str, key: str, params: dict | None = None):
    """Every item of a paged list, following nextPageToken."""
    token = ""
    while True:
        got = get_json(path, {**(params or {}), "pageSize": PAGE, **({"pageToken": token} if token else {})}, key)
        yield from got.get(field) or []
        token = got.get("nextPageToken") or ""
        if not token:
            return


def fetch_service(service_id: str, dest: Path, key: str, currency: str) -> dict:
    """Every SKU of one service into dest (gzip JSON Lines), written to a temporary file first."""
    started, n = time.monotonic(), 0
    tmp = dest.with_suffix(".part")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as out:
            for sku in pages(f"/services/{service_id}/skus", "skus", key, {"currencyCode": currency}):
                out.write(json.dumps(sku, separators=(",", ":"), ensure_ascii=False) + "\n")
                n += 1
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"skus": n, "gz_bytes": dest.stat().st_size, "seconds": round(time.monotonic() - started, 1)}


def main(argv: list[str] | None = None) -> int:
    """argv: the command line's arguments (the worker's weekly price refresh calls it with its own)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--service", action="append", help="repeatable: a service ID (6F81-5844-456A) or display name "
                                                       "(Compute Engine); default: every Google Cloud service")
    ap.add_argument("--all-services", action="store_true", help="every service, also Maps and the marketplace")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true", help="download again services that are already done")
    ap.add_argument("--dry-run", action="store_true", help="list the services, download no SKU")
    a = ap.parse_args(argv)
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        print(f"{KEY_ENV} is not set: the Cloud Billing Catalog API needs an API key (a Google Cloud project with "
              "the Cloud Billing API enabled). Set it in .env, then run this again.", file=sys.stderr)
        return 2
    started = datetime.now(UTC)

    print("listing the services", flush=True)
    services = list(pages("/services", "services", key))
    if a.service:
        want = {s.lower() for s in a.service}
        chosen = [s for s in services if s["serviceId"].lower() in want or (s.get("displayName") or "").lower() in want]
        missing = want - {s["serviceId"].lower() for s in chosen} - {(s.get("displayName") or "").lower() for s in chosen}
        if missing:
            print(f"no such service: {', '.join(sorted(missing))}", file=sys.stderr)
    elif a.all_services:
        chosen = services
    else:
        chosen = [s for s in services if s.get("businessEntityName") == GCP]
    print(f"{len(chosen)} of {len(services)} services to download with {a.workers} worker(s)", flush=True)
    if a.dry_run:
        for s in chosen:
            print(f"  {s['serviceId']}  {s.get('displayName')}")
        return 0

    (a.out / "skus").mkdir(parents=True, exist_ok=True)
    day = started.date().isoformat()
    results: dict[str, dict] = {}

    def one(s: dict) -> dict:
        dest = a.out / "skus" / f"{s['serviceId']}.jsonl.gz"
        done = dest.with_name(dest.name + ".done")
        if done.exists() and dest.exists() and not a.force:
            got = json.loads(done.read_text())
            if got.get("day") == day:
                return {**got, "skipped": True}
        res = {**fetch_service(s["serviceId"], dest, key, a.currency), "day": day}
        done.write_text(json.dumps(res))
        return res

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(one, s): s for s in chosen}
        for f in as_completed(futures):
            s = futures[f]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001  one service failing must not stop the others; it is reported
                res = {"error": str(e)}
            res.update(file=f"skus/{s['serviceId']}.jsonl.gz", display_name=s.get("displayName"))
            results[s["serviceId"]] = res
            mark = "skipped" if res.get("skipped") else ("ERROR " + res["error"] if "error" in res else
                                                         f"{res['skus']:,} SKUs in {res['seconds']}s")
            print(f"{s['serviceId']}  {(s.get('displayName') or '')[:40]:40} {mark}", flush=True)

    errors = sum("error" in r for r in results.values())
    manifest = {
        "provider": "gcp", "fetched_at": started.isoformat(), "finished_at": datetime.now(UTC).isoformat(),
        "base": BASE, "currency": a.currency,
        "services": {s["serviceId"]: {"display_name": s.get("displayName"),
                                      "business_entity": s.get("businessEntityName")} for s in chosen},
        "files": results,
        "totals": {"files": len(results), "skus": sum(r.get("skus", 0) for r in results.values()),
                   "gz_bytes": sum(r.get("gz_bytes", 0) for r in results.values()), "errors": errors},
    }
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    print(f"wrote {a.out / 'manifest.json'}; {manifest['totals']}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
