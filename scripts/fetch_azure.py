"""Fetch Azure product codes from the public Retail Prices API.
No auth needed. The API returns one row per price (millions of rows), but we only keep the distinct
productId -> service mapping, which is far smaller.

The API refuses to page past 1,000,000 rows ($skip=1000000 answers HTTP 400), so one unfiltered listing never
finishes. The script fetches one service family at a time; a family that reaches the limit is fetched again one
region at a time. Each listing saves a checkpoint after every page and resumes where it stopped.

Usage:
    python scripts/fetch_azure.py                      # every family (20 to 60 minutes)
    python scripts/fetch_azure.py --filter "serviceFamily eq 'Compute'"
    python scripts/fetch_azure.py --max-pages 5        # quick smoke test
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

API = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"
KEEP = ("productId", "productName", "serviceName", "serviceId", "serviceFamily")
PAGE_LIMIT = 1000                     # 1,000 pages x 1,000 rows: the API's $skip limit
FAMILIES = [
    "Compute", "Databases", "AI + Machine Learning", "Analytics", "Azure Communication Services", "Networking",
    "Storage", "Security", "Developer Tools", "Azure Stack", "Management and Governance", "Microsoft Syntex",
    "Other", "Internet of Things", "Gaming", "Integration", "Containers", "Power Platform", "Telecommunications",
    "Data", "Azure Security", "Blockchain", "Azure Arc", "Web", "Microsoft 365 Copilot", "Quantum Computing",
    "Windows Virtual Desktop", "Mixed Reality", "Windows 365", "Azure Kubernetes Service",
]


class SkipLimit(Exception):
    """The listing is longer than the API pages."""


def listing(session: requests.Session, ckpt: Path, odata_filter: str | None, max_pages: int | None,
            regions: set[str]) -> dict[str, dict]:
    """Every product of one filtered listing; also collects the regions seen (to split a listing that is too long)."""
    products: dict[str, dict] = {}
    url: str | None = None
    params: dict | None = {"api-version": API_VERSION, "currencyCode": "USD"}
    if odata_filter:
        params["$filter"] = odata_filter
    if ckpt.exists():
        state = json.loads(ckpt.read_text())
        products, url, params = state["products"], state["next"], None
        regions.update(state.get("regions", []))
        if url is None:
            return products
        if "$skip=" in url and int(url.split("$skip=")[1].split("&")[0]) >= PAGE_LIMIT * 1000:
            raise SkipLimit(odata_filter or "all")
    pages = 0
    while True:
        data = _get(session, url or API, params)
        params = None                 # NextPageLink already carries the query string
        for item in data.get("Items", []):
            pid = item.get("productId")
            if pid and pid not in products:
                products[pid] = {k: item.get(k) for k in KEEP}
            if item.get("armRegionName"):
                regions.add(item["armRegionName"])
        url = data.get("NextPageLink")
        pages += 1
        ckpt.write_text(json.dumps({"products": products, "next": url, "regions": sorted(regions)}))
        if pages % 50 == 0:
            print(f"    page {pages}: {len(products)} products")
        if not url or (max_pages and pages >= max_pages):
            return products
        if "$skip=" in url and int(url.split("$skip=")[1].split("&")[0]) >= PAGE_LIMIT * 1000:
            raise SkipLimit(odata_filter or "all")


def fetch(out: Path, odata_filter: str | None, max_pages: int | None) -> None:
    session = requests.Session()
    folder = out.with_suffix(".parts")
    folder.mkdir(parents=True, exist_ok=True)
    products: dict[str, dict] = {}
    filters = [odata_filter] if odata_filter else [f"serviceFamily eq '{f}'" for f in FAMILIES]
    for flt in filters:
        name = flt.split("'")[1] if "'" in flt else "filter"
        regions: set[str] = set()
        try:
            got = listing(session, folder / f"{_safe(name)}.json", flt, max_pages, regions)
            print(f"{name}: {len(got)} products")
        except SkipLimit:
            print(f"{name}: longer than the API pages; fetching it region by region ({len(regions)} regions seen)")
            got = json.loads((folder / f"{_safe(name)}.json").read_text())["products"]
            for region in sorted(regions) + [""]:
                part = f"{flt} and armRegionName eq '{region}'"
                ckpt = folder / f"{_safe(name)}--{_safe(region) or 'global'}.json"
                try:
                    got.update(listing(session, ckpt, part, max_pages, set()))
                except SkipLimit:
                    got.update(json.loads(ckpt.read_text())["products"])     # the pages it did read
                    print(f"  {name} {region}: still too long; kept the first {PAGE_LIMIT} pages")
            print(f"{name}: {len(got)} products")
        products.update(got)
    out.write_text(json.dumps(products, indent=1))
    print(f"Wrote {len(products)} products to {out}")


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s)


def _get(session: requests.Session, url: str, params: dict | None, tries: int = 6) -> dict:
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            wait = min(2 ** attempt, 60)
            print(f"  retry in {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError(f"Giving up on {url}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("data/raw/azure_products.json"))
    ap.add_argument("--filter", dest="odata_filter")
    ap.add_argument("--max-pages", type=int)
    a = ap.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    fetch(a.out, a.odata_filter, a.max_pages)
