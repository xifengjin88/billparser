"""Benchmark a large AWS CUR 2.0 parse: time, rows per second and peak memory (task 02).

    uv run python scripts/bench_large.py --rows 1000000                 # generate, then parse in a subprocess
    uv run python scripts/bench_large.py --rows 1000000 --entry bytes   # the in-memory path (Pipeline.parse_bytes)

The file is synthetic and realistic: cost = quantity x rate, several services, instance types and accounts, and a
share of Reserved Instance and Savings Plan rows with their negations and fees (as in task 01's sample). It is
written to a temp directory and deleted afterwards; nothing is committed. The parse runs in a child process, so its
peak RSS is its own.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import random
import resource
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COLUMNS = ["bill_invoice_id", "bill_billing_period_start_date", "line_item_usage_account_id",
           "line_item_line_item_type", "line_item_usage_start_date", "line_item_usage_end_date",
           "line_item_product_code", "line_item_usage_type", "line_item_operation", "line_item_usage_amount",
           "line_item_unblended_rate", "line_item_unblended_cost", "line_item_currency_code",
           "line_item_line_item_description", "line_item_resource_id", "product_product_name",
           "product_region_code", "product_instance_type", "pricing_unit", "pricing_public_on_demand_cost",
           "reservation_reservation_a_r_n", "reservation_effective_cost", "savings_plan_savings_plan_a_r_n",
           "savings_plan_savings_plan_effective_cost"]
ITEMS = [("AmazonEC2", "Amazon Elastic Compute Cloud", "BoxUsage:{t}", "RunInstances", "Hrs",
          {"m5.large": 0.096, "m5.xlarge": 0.192, "c5.2xlarge": 0.34, "r6g.large": 0.1008}),
         ("AmazonEC2", "Amazon Elastic Compute Cloud", "EBS:VolumeUsage.gp3", "CreateVolume-Gp3", "GB-Mo",
          {"": 0.08 / 730}),
         ("AmazonRDS", "Amazon Relational Database Service", "InstanceUsage:{t}", "CreateDBInstance:0002", "Hrs",
          {"db.r6g.large": 0.25, "db.m6g.xlarge": 0.336}),
         ("AmazonS3", "Amazon Simple Storage Service", "TimedStorage-ByteHrs", "StandardStorage", "GB-Mo",
          {"": 0.023 / 730}),
         ("AWSLambda", "AWS Lambda", "Request", "Invoke", "Requests", {"": 0.0000002}),
         ("AmazonCloudWatch", "AmazonCloudWatch", "CW:Requests", "GetMetricData", "Requests", {"": 0.00001})]
REGIONS = [("us-east-1", "USE1"), ("us-west-2", "USW2"), ("eu-west-1", "EU")]
RI = "arn:aws:ec2:us-east-1:111122223333:reserved-instances/1d3fbc13-f181-4c40-9dd6-12b345678de9"
SP = "arn:aws:savingsplans::111122223333:savingsplan/7a2c1b9e-0d55-4f0e-9c1a-3b5e2f8d1c44"


WIDE = 100                      # --wide: product attribute and tag columns a real CUR has (about 125 columns in all)


def _wide_values(rnd: random.Random, n: int) -> list[list[str]]:
    """Some sets of values for the extra columns: most are empty on a line, as in a real CUR."""
    words = ["Linux", "Shared", "Used", "No License required", "NA", "Yes", "No", "Compute Instance", "EBS only",
             "Up to 10 Gigabit", "Intel Xeon Platinum 8175", "64-bit", "3.1 GHz", "8 GiB", "4", "2", "standard"]
    out = []
    for _ in range(n):
        out.append([rnd.choice(words) if rnd.random() < 0.35 else "" for _ in range(WIDE)])
    return out


def generate(path: Path, rows: int, seed: int = 7, wide: bool = False) -> float:
    """Write a gzip CUR 2.0 CSV with `rows` lines (hourly lines over one month). Returns the billed total. wide: 100
    more columns (product attributes and tags) that the spec does not read."""
    rnd = random.Random(seed)
    extra_cols = [f"product_attribute_{i:03d}" for i in range(WIDE // 2)] + \
        [f"resource_tags_user_tag_{i:03d}" for i in range(WIDE - WIDE // 2)] if wide else []
    extra_rows = _wide_values(rnd, 64) if wide else [[]]
    start = date(2026, 9, 1)
    total = 0.0
    with gzip.open(path, "wt", newline="", compresslevel=6) as f:
        w = csv.writer(f)
        w.writerow(COLUMNS + extra_cols)
        for i in range(rows):
            d = start + timedelta(days=i % 30)
            code, name, ut, op, unit, types = ITEMS[i % len(ITEMS)]
            t = rnd.choice(list(types))
            region, prefix = REGIONS[i % len(REGIONS)]
            usage_type = f"{prefix}-{ut.format(t=t)}"
            rate = types[t]
            q = {"Hrs": 1.0, "GB-Mo": round(rnd.uniform(1, 500), 4), "Requests": float(rnd.randint(1, 100000))}[unit]
            kind = "Usage"
            roll = rnd.random()
            if code in ("AmazonEC2", "AmazonRDS") and unit == "Hrs":
                if roll < 0.15:
                    kind = "DiscountedUsage"
                elif roll < 0.30:
                    kind = "SavingsPlanCoveredUsage"
                elif roll < 0.33:
                    usage_type = usage_type.replace("BoxUsage", "SpotUsage")
                    rate = round(rate * 0.3, 6)
            cost = round(q * rate, 8)
            row = {"bill_invoice_id": "EUINUS26-123456", "bill_billing_period_start_date": "2026-09-01T00:00:00Z",
                   "line_item_usage_account_id": f"1111222233{i % 7:02d}", "line_item_line_item_type": kind,
                   "line_item_usage_start_date": f"{d}T{i % 24:02d}:00:00Z",
                   "line_item_usage_end_date": f"{d}T{i % 24:02d}:59:59Z", "line_item_product_code": code,
                   "line_item_usage_type": usage_type, "line_item_operation": op, "line_item_usage_amount": q,
                   "line_item_unblended_rate": rate, "line_item_unblended_cost": cost, "line_item_currency_code": "USD",
                   "line_item_line_item_description": f"${rate} per {unit} {t}".strip(),
                   "line_item_resource_id": f"i-{i % 5000:08x}" if unit == "Hrs" else "",
                   "product_product_name": name, "product_region_code": region, "product_instance_type": t,
                   "pricing_unit": unit, "pricing_public_on_demand_cost": cost}
            if kind == "DiscountedUsage":
                row.update(line_item_unblended_rate=0.0, line_item_unblended_cost=0.0, reservation_reservation_a_r_n=RI,
                           reservation_effective_cost=round(cost * 0.6, 8))
                cost = 0.0
            elif kind == "SavingsPlanCoveredUsage":
                row.update(savings_plan_savings_plan_a_r_n=SP, savings_plan_savings_plan_effective_cost=round(cost * 0.7, 8))
            w.writerow([row.get(c, "") for c in COLUMNS] + extra_rows[i % len(extra_rows)])
            total += cost
            if kind == "SavingsPlanCoveredUsage":            # its negation, as the next line
                neg = dict(row, line_item_line_item_type="SavingsPlanNegation", line_item_unblended_cost=-cost,
                           savings_plan_savings_plan_effective_cost="", pricing_public_on_demand_cost="")
                w.writerow([neg.get(c, "") for c in COLUMNS] + extra_rows[i % len(extra_rows)])
                total -= cost
    return round(total, 2)


def catalog_dir() -> Path:
    """A service catalog built once and kept between runs (building it is not part of a parse)."""
    d = Path(tempfile.gettempdir()) / "billparse-bench-catalog"
    if not (d / "catalog.msgpack").exists():
        sys.path.insert(0, str(ROOT))
        from svcres.catalog import build
        d.mkdir(exist_ok=True)
        build(ROOT / "data" / "raw").save(d / "catalog.msgpack")
    return d


def child(path: str, entry: str) -> None:
    """Parse one file and print one JSON line: seconds, rows, status, peak RSS."""
    sys.path.insert(0, str(ROOT))
    from billparse import Pipeline, Settings
    from billparse.analyze import get_resolver
    data_dir = Path(tempfile.mkdtemp())
    catalog = catalog_dir()
    pipe = Pipeline(Settings(store_dir=data_dir / "store", catalog_dir=catalog, use_llm=False, use_agent=False))
    get_resolver(catalog)                               # loaded before the measurement: its memory is the baseline
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()
    p = Path(path)
    if entry == "bytes":
        r = pipe.parse_bytes(p.read_bytes(), p.name, agent=False)
    else:
        r = pipe.parse_path(p, p.name, agent=False)
    secs = time.perf_counter() - t0
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024     # bytes on macOS, KB on Linux
    print(json.dumps({"seconds": round(secs, 1), "rows": r.get("rows"), "status": r.get("status"),
                      "completeness": r.get("completeness"), "total": (r.get("analysis") or {}).get("total_billed"),
                      "route": (r.get("spec") or {}).get("route"), "peak_mb": round(peak * scale / 1e6),
                      "baseline_mb": round(base * scale / 1e6), "above_baseline_mb": round((peak - base) * scale / 1e6)}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--entry", choices=["path", "bytes"], default="path")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--keep", type=Path, help="keep the generated file here")
    ap.add_argument("--wide", action="store_true", help=f"{WIDE} more columns the spec does not read")
    a = ap.parse_args()
    if a.child:
        return child(a.child, a.entry)
    catalog_dir()
    tmp = Path(tempfile.mkdtemp())
    f = (a.keep or tmp) / f"cur2-{a.rows}{'-wide' if a.wide else ''}.csv.gz"
    t0 = time.perf_counter()
    expected = generate(f, a.rows, wide=a.wide) if not f.exists() else None
    gen = time.perf_counter() - t0
    with gzip.open(f, "rb") as g:
        raw = sum(len(b) for b in iter(lambda: g.read(8 << 20), b""))
    out = subprocess.run([sys.executable, __file__, "--child", str(f), "--entry", a.entry], capture_output=True,
                         text=True, cwd=ROOT, env={**os.environ, "BILLPARSE_USE_LLM": "0", "AGENT_ENABLED": "0"})
    if out.returncode != 0:
        sys.exit(f"parse failed ({out.returncode}): {out.stderr[-3000:]}")
    res = json.loads(out.stdout.strip().splitlines()[-1])
    res.update(entry=a.entry, file_rows=a.rows, columns=len(COLUMNS) + (WIDE if a.wide else 0), gz_mb=round(f.stat().st_size / 1e6, 1), csv_mb=round(raw / 1e6, 1),
               rows_per_s=round((res["rows"] or 0) / res["seconds"]) if res["seconds"] else None,
               expected_total=expected, generate_s=round(gen, 1))
    print(json.dumps(res, indent=1))
    if not a.keep:
        f.unlink()


if __name__ == "__main__":
    main()
