"""Download Azure's public retail prices (the Azure Retail Prices API) for billparse's price catalog (prices/).

No Azure account, no key: plain HTTPS GETs of https://prices.azure.com/api/retail/prices. Only Python's standard
library, so it runs anywhere:

    uv run python scripts/azure/fetch_azure_retail_prices.py --out ~/azure-price-files            # the default services
    uv run python scripts/azure/fetch_azure_retail_prices.py --out ~/azure-price-files --service "Virtual Machines" \
        --region eastus --region westeurope
    uv run python scripts/azure/fetch_azure_retail_prices.py --out ~/azure-price-files --all-services
    uv run python scripts/azure/fetch_azure_retail_prices.py --out /tmp/x --service Storage --region eastus \
        --filter "skuName eq 'P10 LRS'"                                          # one more OData clause

How the API answers (checked on 2026-10-27):
    GET /api/retail/prices?$filter=serviceName eq 'Virtual Machines' and armRegionName eq 'eastus'
    {"BillingCurrency": "USD", "CustomerEntityId": "Default", "CustomerEntityType": "Retail", "Count": 1000,
     "NextPageLink": "...&$skip=1000" | null, "Items": [...]}       at most 1000 items per page (about 590 KB of JSON)
    an item: currencyCode, tierMinimumUnits, retailPrice, unitPrice, armRegionName, location, effectiveStartDate,
             effectiveEndDate (some: a scheduled end), meterId, meterName, productId, skuId, productName, skuName,
             serviceName, serviceId, serviceFamily, unitOfMeasure ("1 Hour", "1/Month", "1 GB", "10K", ...),
             type (Consumption | DevTestConsumption | Reservation), reservationTerm ("1 Year", "3 Years"; a
             reservation's retailPrice is the price of the whole term), isPrimaryMeterRegion, armSkuName
    no total count; a $skip of 1,000,000 or more answers 400 ("greater than the total count 1000000"), so one query
    gives at most 1M items (the unfiltered list has more: a full page at $skip=999000), and a query that would need
    more is an error here: ask per service and region. Bursts answer 429 (retried here, after Retry-After)
    A full download (estimate, not run): more than 1M items, so more than 1,000 requests of about 590 KB (600 MB+ of
    JSON, about 50 MB gzip), 20-40 minutes with the rate limit.

What it writes (the items as the API gives them, one JSON object per line, gzipped):
    <service>/<region>.jsonl.gz       (<region> "all-regions" when no region is given)
    manifest.json   provider "azure", the filters, per service and region: rows, requests, bytes, time

A file already downloaded is kept on the next run (a ".done" marker), and a file stopped part way continues at its
last page saved (<file>.part and <file>.resume), so a stopped run continues where it stopped.
While a file downloads, a line every --progress-every seconds (default 10; 0: none) says how far it is: the API gives
no total, so rows, pages and MB so far, and the rate.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BASE = "https://prices.azure.com/api/retail/prices"
# the services that most bills spend on; --all-services: every service the API has (no serviceName filter)
SERVICES = ["Virtual Machines", "Storage", "SQL Database", "Azure App Service", "Azure Kubernetes Service",
            "Azure Cosmos DB", "Azure Database for PostgreSQL", "Azure Database for MySQL", "Redis Cache",
            "Bandwidth", "Virtual Network", "Load Balancer", "Azure Monitor", "Log Analytics", "Functions",
            "Backup", "Azure Synapse Analytics", "Azure Data Factory v2", "Application Gateway", "Azure Firewall"]
ALL = "all-services"
ALL_REGIONS = "all-regions"
NO_REGION = "no-region"              # the file of the items whose armRegionName is empty
MAX_SKIP = 1_000_000               # the API answers 400 to a $skip of 1,000,000 or more: a query's items stop there


def _quote(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def odata_filter(service: str | None, region: str | None, extra: str | None = None) -> str:
    parts = []
    if service:
        parts.append(f"serviceName eq {_quote(service)}")
    if region is not None:                          # "": the items without a region (global meters)
        parts.append(f"armRegionName eq {_quote(region)}")
    if extra:
        parts.append(f"({extra})")
    return " and ".join(parts)


def first_url(flt: str, currency: str | None = None, api_version: str | None = None) -> str:
    q = {}
    if api_version:
        q["api-version"] = api_version
    if currency:
        q["currencyCode"] = _quote(currency)
    if flt:
        q["$filter"] = flt
    return BASE + ("?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote) if q else "")


def get_page(url: str, tries: int = 8, pause: float = 1.0) -> tuple[dict, int]:
    """(the page, its bytes). Retried on network errors and 429/5xx, waiting Retry-After or longer each time."""
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                body = r.read()
            return json.loads(body), len(body)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries:
                raise
            wait = float(e.headers.get("Retry-After") or 0) or min(60.0, pause * 2 ** attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == tries:
                raise
            wait = min(60.0, pause * 2 ** attempt)
            print(f"  retry {attempt} {url}: {e}", file=sys.stderr, flush=True)
        time.sleep(wait)
    raise RuntimeError("unreachable")


class Capped(RuntimeError):
    """A query has more items than the API serves (MAX_SKIP); regions: the armRegionName values seen until then."""

    def __init__(self, regions: set[str]):
        super().__init__(f"the API serves at most {MAX_SKIP:,} items per query and this one has more: split it "
                         "(--service, --region)")
        self.regions = regions


def fetch(flt: str, dest: Path, currency: str | None = None, api_version: str | None = None,
          pause: float = 0.2, progress_every: float = 0, label: str = "") -> dict:
    """Every page of one filter, NextPageLink after NextPageLink, to dest (gzip JSON Lines). Raises Capped when the
    query has more items than the API serves. progress_every: seconds between two progress lines (0: none).
    Each page is appended to <dest>.part as its own gzip member, then <dest>.resume records the next page's URL and
    the .part's size: a run stopped mid-file (Ctrl-C, a network error) continues at that page next time, the .part
    cut back to the last page recorded."""
    tmp, state = dest.with_name(dest.name + ".part"), dest.with_name(dest.name + ".resume")
    url: str | None = first_url(flt, currency, api_version)
    rows, requests, raw, before = 0, 0, 0, 0.0
    regions: set[str] = set()
    if tmp.exists() and state.exists():
        st = json.loads(state.read_text())
        url, rows, requests, raw, before = st["url"], st["rows"], st["requests"], st["json_bytes"], st["seconds"]
        regions = set(st["regions"])
        with open(tmp, "r+b") as f:
            f.truncate(st["size"])                     # a page written after the last record is asked again
        print(f"    {label}: continuing at {rows:,} rows ({requests} pages)", flush=True)
    else:
        tmp.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
    started = time.monotonic()
    shown = started
    try:
        while url:
            page, n = get_page(url)
            requests += 1
            raw += n
            items = page.get("Items") or []
            with gzip.open(tmp, "at", encoding="utf-8", compresslevel=6) as out:
                for item in items:
                    out.write(json.dumps(item, separators=(",", ":"), ensure_ascii=False) + "\n")
                    regions.add(item.get("armRegionName") or "")
            rows += len(items)
            url = page.get("NextPageLink")
            skip = re.search(r"[?&]\$skip=(\d+)", url or "")
            if skip and int(skip.group(1)) >= MAX_SKIP:
                tmp.unlink(missing_ok=True)
                state.unlink(missing_ok=True)
                raise Capped(regions)
            now = time.monotonic()
            if url:
                part = state.with_name(state.name + ".tmp")
                part.write_text(json.dumps({"url": url, "rows": rows, "requests": requests, "json_bytes": raw,
                                            "seconds": before + now - started, "regions": sorted(regions),
                                            "size": tmp.stat().st_size}))
                part.replace(state)
            if progress_every and url and now - shown >= progress_every:
                shown = now
                print(f"    {label}: {rows:,} rows, {requests} pages, {raw / 1e6:,.1f} MB, {before + now - started:,.0f}s "
                      f"({rows / max(before + now - started, 1e-9):,.0f} rows/s)", flush=True)
            if url and pause:
                time.sleep(pause)                      # gentle: the API answers 429 to bursts
    except Capped:
        raise
    except BaseException:
        if not state.exists():                         # nothing recorded yet: nothing to continue from
            tmp.unlink(missing_ok=True)
        raise
    if not tmp.exists():                               # a query with no items
        with gzip.open(tmp, "wt", encoding="utf-8"):
            pass
    tmp.replace(dest)
    state.unlink(missing_ok=True)
    return {"filter": flt, "rows": rows, "requests": requests, "json_bytes": raw, "gz_bytes": dest.stat().st_size,
            "seconds": round(before + time.monotonic() - started, 1)}


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "_"


def main(argv: list[str] | None = None) -> int:
    """argv: the command line's arguments (the worker's weekly price refresh calls it with its own)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--service", action="append", help=f"repeatable serviceName; default: {', '.join(SERVICES)}")
    ap.add_argument("--all-services", action="store_true", help="every service (no serviceName filter: one stream)")
    ap.add_argument("--region", action="append", help="repeatable armRegionName (eastus); default: every region")
    ap.add_argument("--filter", help="one more OData clause, and-ed to each request (skuName eq 'P10 LRS')")
    ap.add_argument("--currency", help="currencyCode (default: the API's, USD)")
    ap.add_argument("--api-version", help="api-version (default: none, the API's current GA answer)")
    ap.add_argument("--pause", type=float, default=0.2, help="seconds between two pages")
    ap.add_argument("--progress-every", type=float, default=10,
                    help="seconds between two progress lines while a file downloads (0: none)")
    ap.add_argument("--force", action="store_true", help="download again files that are already done")
    ap.add_argument("--dry-run", action="store_true", help="print the requests' first URLs, download nothing")
    a = ap.parse_args(argv)
    started = datetime.now(UTC)
    t0 = time.monotonic()
    services = [None] if a.all_services else (a.service or SERVICES)
    regions = a.region or [None]
    jobs = [(s, r) for s in services for r in regions]
    print(f"{len(jobs)} download(s)", flush=True)
    if a.dry_run:
        for s, r in jobs:
            print(f"  {s or ALL:35} {r or ALL_REGIONS:20} {first_url(odata_filter(s, r, a.filter), a.currency, a.api_version)}")
        return 0
    results: dict[str, dict] = {}
    i = 0
    while i < len(jobs):
        service, region = jobs[i]
        i += 1
        name, reg = service or ALL, (ALL_REGIONS if region is None else region or NO_REGION)
        dest = a.out / slug(name) / f"{slug(reg)}.jsonl.gz"
        done = dest.with_name(dest.name + ".done")
        if done.exists() and dest.exists() and not a.force:
            res = {**json.loads(done.read_text()), "skipped": True}
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if a.progress_every:
                print(f"[{i}/{len(jobs)}] {name:35} {reg:20} ...", flush=True)
            try:
                res = fetch(odata_filter(service, region, a.filter), dest, a.currency, a.api_version, a.pause,
                            a.progress_every, f"{name} {reg}")
                done.write_text(json.dumps(res))
            except Capped as e:
                if region is not None:
                    res = {"error": f"Capped: {e}"}
                else:                    # every region at once is too many: one query per region seen instead
                    jobs.extend((service, r) for r in sorted(e.regions))
                    print(f"[{i}/{len(jobs)}] {name:35} more than {MAX_SKIP:,} items: {len(e.regions)} regions "
                          "one by one", flush=True)
                    continue
            except Exception as e:  # noqa: BLE001  one download failing must not stop the others; it is reported
                res = {"error": f"{type(e).__name__}: {e}"}
        res["path"] = f"{slug(name)}/{slug(reg)}.jsonl.gz"
        results.setdefault(name, {})[reg] = res
        mark = "skipped" if res.get("skipped") else ("ERROR " + res["error"] if "error" in res else
                                                     f"{res['rows']:,} rows in {res['requests']} requests, "
                                                     f"{res['gz_bytes'] / 1e6:,.1f} MB gz, {res['seconds']}s")
        print(f"[{i}/{len(jobs)}] {name:35} {reg:20} {mark}  (total {time.monotonic() - t0:,.0f}s)", flush=True)
    files = [r for regs in results.values() for r in regs.values()]
    errors = sum("error" in r for r in files)
    manifest = {
        "provider": "azure", "source": "azure-retail-prices", "fetched_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(), "base": BASE, "filter": a.filter, "currency": a.currency,
        "api_version": a.api_version, "services": services if not a.all_services else ALL,
        "regions": a.region or "all", "files": results,
        "totals": {"files": len(files), "rows": sum(r.get("rows", 0) for r in files),
                   "requests": sum(r.get("requests", 0) for r in files),
                   "json_bytes": sum(r.get("json_bytes", 0) for r in files),
                   "gz_bytes": sum(r.get("gz_bytes", 0) for r in files), "errors": errors},
    }
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    print(f"wrote {a.out / 'manifest.json'}; {manifest['totals']}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
