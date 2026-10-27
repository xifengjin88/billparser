"""Download AWS's public price list files (the Price List bulk files) for billparse's price catalog (prices/).

No AWS account or login: plain HTTPS downloads from pricing.us-east-1.amazonaws.com. Only Python's standard library,
so it runs anywhere:

    uv run python scripts/aws/fetch_aws_price_files.py --out ~/aws-price-files                 # the current version
    uv run python scripts/aws/fetch_aws_price_files.py --out ~/aws-price-files --as-of 2025-10-15   # the version then
    uv run python scripts/aws/fetch_aws_price_files.py --out ~/aws-price-files --service AmazonEC2 --region us-east-1
    uv run python scripts/aws/fetch_aws_price_files.py --out ~/aws-price-files --savings-plans

How AWS publishes them (checked on 2026-10-26):
    /offers/v1.0/aws/index.json                       every service: versionIndexUrl, currentRegionIndexUrl, ...
    /offers/v1.0/aws/<Service>/index.json             every version: versionEffectiveBeginDate / EndDate ("" = current)
    /offers/v1.0/aws/<Service>/<version>/region_index.json   per region: .../<version>/<region>/index.json (or .csv)
    CSV: 5 header lines (FormatVersion, Disclaimer, Publication Date, Version, OfferCode), then the column names, then
         one row per price: SKU, OfferTermCode, RateCode, TermType, EffectiveDate, StartingRange, EndingRange, Unit,
         PricePerUnit, Currency, LeaseContractLength, PurchaseOption, OfferingClass, and the product attributes.
    /savingsPlan/v1.0/aws/<Offer>/current/index.json  Savings Plans versions (by publication date), per-region files;
         a row is a rate for a DiscountedSKU / DiscountedUsageType / DiscountedOperation (one region: ~300 MB of CSV).

What it writes (the files as AWS publishes them, gzipped; nothing renamed):
    offers/<Service>/<version>/<region>.csv.gz
    savings_plans/<Offer>/<version>/<region>.csv.gz          (with --savings-plans: the current version only)
    manifest.json   services, versions with their effective dates, regions, file sizes, rows, time

A file already downloaded is kept on the next run (a ".done" marker), so a stopped run continues where it stopped.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

BASE = "https://pricing.us-east-1.amazonaws.com"
SERVICES = ["AmazonEC2", "AmazonRDS", "AmazonElastiCache", "AmazonES", "AmazonRedshift"]
SAVINGS_PLANS = ["AWSComputeSavingsPlan"]          # also: AWSMachineLearningSavingsPlans, AWSDatabaseSavingsPlans
CHUNK = 4 * 1024 * 1024


class Progress:
    """What is downloading now and how far, printed every few seconds by a reporter thread."""

    def __init__(self, total_files: int):
        self.lock = threading.Lock()
        self.total_files, self.done_files, self.done_bytes = total_files, 0, 0
        self.active: dict[str, list[int]] = {}         # file label -> [bytes read, size (0 = unknown)]
        self.started = time.monotonic()

    def start(self, label: str, size: int) -> None:
        with self.lock:
            self.active[label] = [0, size]

    def add(self, label: str, n: int) -> None:
        with self.lock:
            self.active[label][0] += n

    def finish(self, label: str, ok: bool = True) -> None:
        with self.lock:
            read, _ = self.active.pop(label, [0, 0])
            self.done_files += 1
            self.done_bytes += read if ok else 0

    def skip(self) -> None:
        with self.lock:
            self.done_files += 1

    def line(self) -> str:
        with self.lock:
            now_bytes = self.done_bytes + sum(r for r, _ in self.active.values())
            elapsed = time.monotonic() - self.started
            rate = now_bytes / elapsed if elapsed else 0.0
            running = ", ".join(f"{k} {r / 1e6:,.0f} MB" + (f" {r / t:.0%}" if t else "")
                                for k, (r, t) in sorted(self.active.items()))
            left = self.total_files - self.done_files
            per_file = elapsed / self.done_files if self.done_files else None
            eta = f", about {_clock(per_file * left / max(1, len(self.active) or 1))} left" if per_file and left else ""
            return (f"[{_clock(elapsed)}] {self.done_files}/{self.total_files} files done, {now_bytes / 1e9:,.2f} GB "
                    f"of CSV at {rate / 1e6:,.1f} MB/s{eta}" + (f" | now: {running}" if running else ""))


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def report(progress: Progress, every: float, stop: threading.Event) -> None:
    while not stop.wait(every):
        print(progress.line(), flush=True)


def get_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.load(r)


def download(path: str, dest: Path, progress: Progress | None = None, label: str = "", tries: int = 4) -> dict:
    """Stream one CSV file to dest, gzipped; retried on network errors."""
    url = BASE + path
    for attempt in range(1, tries + 1):
        tmp = dest.with_suffix(".part")
        try:
            started, raw, rows = time.monotonic(), 0, 0
            with urllib.request.urlopen(url, timeout=300) as r, gzip.open(tmp, "wb", compresslevel=6) as out:
                if progress:
                    progress.start(label, int(r.headers.get("Content-Length") or 0))
                while (b := r.read(CHUNK)):
                    out.write(b)
                    raw += len(b)
                    rows += b.count(b"\n")
                    if progress:
                        progress.add(label, len(b))
            tmp.replace(dest)
            return {"url": url, "csv_bytes": raw, "gz_bytes": dest.stat().st_size, "rows": max(0, rows - 6),
                    "seconds": round(time.monotonic() - started, 1)}
        except OSError as e:
            tmp.unlink(missing_ok=True)
            if progress:
                progress.finish(label, ok=False)
            if attempt == tries:
                raise
            print(f"  retry {attempt} {url}: {e}", file=sys.stderr, flush=True)
            time.sleep(5 * attempt)
    raise RuntimeError("unreachable")


def versions_to_fetch(service: str, as_of: str | None) -> list[dict]:
    """One version: the one in effect on as_of (YYYY-MM-DD), or the current one. A weekly run takes the current
    version; a version already downloaded is skipped, so most weeks nothing is downloaded."""
    idx = get_json(f"/offers/v1.0/aws/{service}/index.json")
    for vid, v in idx["versions"].items():
        begin, end = (v.get("versionEffectiveBeginDate") or "")[:10], (v.get("versionEffectiveEndDate") or "")[:10]
        if (as_of is None and not end) or (as_of is not None and begin <= as_of and (not end or as_of < end)):
            return [{"version": vid, "begin": v.get("versionEffectiveBeginDate"), "end": end or None,
                     "current": not end,
                     "region_index": v["offerVersionUrl"].rsplit("/", 1)[0] + "/region_index.json"}]
    print(f"{service}: no version in effect on {as_of or 'today'}", file=sys.stderr)
    return []


def main(argv: list[str] | None = None) -> int:
    """argv: the command line's arguments (the worker's weekly price refresh calls it with its own)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--service", action="append", help=f"repeatable; default: {', '.join(SERVICES)}")
    ap.add_argument("--all-services", action="store_true", help="every service in AWS's price list index")
    ap.add_argument("--region", action="append", help="repeatable region code; default: every region")
    ap.add_argument("--as-of", help="the version in effect on this date (YYYY-MM-DD); default: the current one")
    ap.add_argument("--savings-plans", action="store_true", help="also the current Savings Plans files")
    ap.add_argument("--savings-plan-offer", action="append",
                    help="repeatable; default: every Savings Plans offer in AWS's index")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true", help="download again files that are already done")
    ap.add_argument("--progress-every", type=float, default=10.0, help="seconds between progress lines (0: none)")
    ap.add_argument("--dry-run", action="store_true", help="list the files and their count, download nothing")
    a = ap.parse_args(argv)
    started = datetime.now(UTC)
    want = set(a.region or [])

    print("finding the files to download", flush=True)
    index = get_json("/offers/v1.0/aws/index.json")
    services = sorted(index["offers"]) if a.all_services else (a.service or SERVICES)
    jobs, versions = [], {}
    for i, service in enumerate(services, 1):
        try:
            versions[service] = versions_to_fetch(service, a.as_of)
        except OSError as e:                         # a service whose index cannot be read is reported, not fatal
            print(f"  [{i}/{len(services)}] {service}: not read ({e})", file=sys.stderr, flush=True)
            continue
        before = len(jobs)
        for v in versions[service]:
            try:
                regions = {k: r["currentVersionUrl"] for k, r in get_json(v["region_index"])["regions"].items()}
            except OSError:                          # a service with no per-region files: one file for all regions
                regions = {"all-regions": v["region_index"].replace("/region_index.json", "/index.json")}
            v["regions"] = sorted(r for r in regions if not want or r in want or r == "all-regions")
            for region in v["regions"]:
                src = regions[region].replace("/index.json", "/index.csv")
                jobs.append((f"{service} {v['version']}", region, src,
                             a.out / "offers" / service / v["version"] / f"{region}.csv.gz"))
        print(f"  [{i}/{len(services)}] {service}: {len(jobs) - before} file(s)", flush=True)
    if a.savings_plans:
        sp_offers = a.savings_plan_offer or sorted({o["currentSavingsPlanIndexUrl"].split("/")[4]
                                                    for o in index["offers"].values()
                                                    if o.get("currentSavingsPlanIndexUrl")})
        for offer in sp_offers:
            print(f"  savings plans {offer}", flush=True)
            idx = get_json(f"/savingsPlan/v1.0/aws/{offer}/current/region_index.json")
            vid = idx["regions"][0]["versionUrl"].split("/")[5] if idx["regions"] else "current"
            versions[offer] = [{"version": vid, "publication_date": idx.get("publicationDate"), "current": True}]
            for r in idx["regions"]:
                if want and r["regionCode"] not in want:
                    continue
                src = r["versionUrl"].replace("/index.json", "/index.csv")
                jobs.append((f"{offer} {vid}", r["regionCode"], src,
                             a.out / "savings_plans" / offer / vid / f"{r['regionCode']}.csv.gz"))
    print(f"{len(jobs)} file(s) to download ({len(versions)} price lists) with {a.workers} worker(s)", flush=True)
    if a.dry_run:
        for name, region, src, _ in jobs:
            print(f"  {name:45} {region:22} {BASE + src}")
        return 0
    progress, stop = Progress(len(jobs)), threading.Event()
    if a.progress_every > 0:
        threading.Thread(target=report, args=(progress, a.progress_every, stop), daemon=True).start()

    results: dict = {}

    def one(job):
        name, region, src, dest = job
        done = dest.with_name(dest.name + ".done")
        if done.exists() and dest.exists() and not a.force:
            progress.skip()
            return {**json.loads(done.read_text()), "skipped": True}
        dest.parent.mkdir(parents=True, exist_ok=True)
        label = f"{name.split()[0]}/{region}"
        res = download(src, dest, progress, label)
        progress.finish(label)
        done.write_text(json.dumps(res))
        return res
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(one, j): j for j in jobs}
        for f in as_completed(futures):
            name, region = futures[f][:2]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001  one file failing must not stop the others; it is reported
                res = {"error": f"{type(e).__name__}: {e}"}
            results.setdefault(name, {})[region] = res
            mark = "skipped" if res.get("skipped") else ("ERROR " + res["error"] if "error" in res else
                                                         f"{res['rows']:,} rows, {res['gz_bytes'] / 1e6:,.0f} MB gz "
                                                         f"in {res['seconds']}s")
            print(f"{name:40} {region:18} {mark}", flush=True)
    stop.set()
    print(progress.line(), flush=True)

    errors = sum("error" in r for regs in results.values() for r in regs.values())
    manifest = {
        "fetched_at": started.isoformat(), "finished_at": datetime.now(UTC).isoformat(), "base": BASE,
        "as_of": a.as_of, "regions": a.region or "all", "versions": versions, "files": results,
        "totals": {"files": len(jobs), "rows": sum(r.get("rows", 0) for regs in results.values() for r in regs.values()),
                   "gz_bytes": sum(r.get("gz_bytes", 0) for regs in results.values() for r in regs.values()),
                   "errors": errors},
    }
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    print(f"wrote {a.out / 'manifest.json'}; {manifest['totals']}")
    return 1 if errors else 0


if __name__ == "__main__":
    shutil.disk_usage(".")                          # (fails early on a missing working directory)
    raise SystemExit(main())
