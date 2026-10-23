"""Fetch Azure product codes from the public Retail Prices API.

No auth needed. The API returns one row per price (hundreds of thousands of rows),
but we only keep the distinct productId -> service mapping, which is far smaller.

Paging can take 20 to 60 minutes for the full catalog, so the script checkpoints
after every page and resumes where it stopped if interrupted.

Usage:
    python scripts/fetch_azure.py                      # full catalog
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


def fetch(out: Path, odata_filter: str | None, max_pages: int | None) -> None:
    ckpt = out.with_suffix(".checkpoint.json")
    products: dict[str, dict] = {}
    url: str | None = None
    params: dict | None = {"api-version": API_VERSION, "currencyCode": "USD"}
    if odata_filter:
        params["$filter"] = odata_filter

    if ckpt.exists():
        state = json.loads(ckpt.read_text())
        products, url, params = state["products"], state["next"], None
        print(f"Resuming: {len(products)} products so far")
        if url is None:
            print("Checkpoint says fetch already finished.")

    session = requests.Session()
    pages = 0
    while True:
        if url is None and params is None:
            break
        data = _get(session, url or API, params)
        params = None  # NextPageLink already carries the query string
        for item in data.get("Items", []):
            pid = item.get("productId")
            if pid and pid not in products:
                products[pid] = {k: item.get(k) for k in KEEP}
        url = data.get("NextPageLink")
        pages += 1
        ckpt.write_text(json.dumps({"products": products, "next": url}))
        if pages % 20 == 0:
            print(f"page {pages}: {len(products)} distinct products")
        if not url or (max_pages and pages >= max_pages):
            break

    out.write_text(json.dumps(products, indent=1))
    if not url:
        ckpt.unlink(missing_ok=True)
    print(f"Wrote {len(products)} products to {out}")


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
