"""Read the details a line's text prints: instance type, database engine, OS, deployment, pricing model, usage type,
quantity, unit, unit price, region code.

Bills print these inside descriptions ("$0.7424 per RDS db.m7g.xlarge Multi-AZ instance hour (or partial hour)
running MySQL 1,176 Hrs", "Az SQL DB Sgl/El Pool Gen Pur-Comp G4-1 vCore", "Standard_D4s_v5"). Each extractor
only fills a field that is empty, so a column the file has always wins. `extract(rows)` works on any route's rows.
"""

from __future__ import annotations

import os
import re

import numpy as np
import pandas as pd

from .transforms import map_unique

# instance types: AWS (with RDS, ElastiCache, OpenSearch, Redshift prefixes), Azure VM sizes, GCP machine types
AWS_TYPE = re.compile(r"\b((?:db|cache|search|ra3|dc2|ds2)\.)?([a-z]\d[a-z0-9-]*|[a-z]{1,3}\d[a-z0-9-]*)\."
                      r"(nano|micro|small|medium|large|xlarge|\d{1,2}xlarge|metal(?:-\d+xl)?)(?:\.search|\.elasticsearch)?\b")
AZURE_SIZE = re.compile(r"\b(Standard|Basic)_[A-Z]\w*\b")
GCP_TYPE = re.compile(r"\b([a-z]\d[a-z]?|n1|n2d?|e2|c2d?|c3d?|m[123]|a[23]|t2[ad]|g2|h3)-(standard|highmem|highcpu|"
                      r"megamem|ultramem|custom|micro|small|medium)(-\d+)?(-\d+)?\b")
ENGINES = [
    ("Aurora PostgreSQL", r"aurora[\s-]*postgre(?:s|sql)"), ("Aurora MySQL", r"aurora[\s-]*mysql"),
    ("Aurora", r"\baurora\b"), ("PostgreSQL", r"postgre(?:s|sql)"), ("MySQL", r"\bmysql\b"),
    ("MariaDB", r"maria\s*db"), ("Oracle", r"\boracle\b"), ("SQL Server", r"sql\s*server|\bmssql\b"),
    ("Db2", r"\bdb2\b"), ("Redis", r"\bredis\b"), ("Valkey", r"\bvalkey\b"), ("Memcached", r"memcached"),
    ("MongoDB", r"mongo\s*db|documentdb"), ("Cassandra", r"cassandra|keyspaces"),
    ("Azure SQL", r"\b(?:az(?:ure)? )?sql db\b|sql database"), ("Cosmos DB", r"cosmos\s*db"),
]
OSES = [("Windows", r"\bwindows\b|\bwin\b"), ("RHEL", r"\brhel\b|red\s*hat"), ("SUSE", r"\bsuse\b|\bsles\b"),
        ("Ubuntu", r"\bubuntu\b"), ("Linux", r"\blinux\b|linux/unix")]
DEPLOYMENT = [("Multi-AZ", r"multi[\s-]*az"), ("Single-AZ", r"single[\s-]*az")]
PRICING = [("Savings Plan", r"savings\s*plan"), ("Reserved", r"\breserved\b|\bri\b|reservation"),
           ("Spot", r"\bspot\b"), ("On Demand", r"on[\s-]*demand"), ("Commitment", r"\bcommitment\b|\bcud\b")]
# AWS usage types: a region prefix and a usage name ("USE1-BoxUsage:m5.xlarge", "APS3-TimedStorage-ByteHrs")
USAGE_TYPE = re.compile(r"\b((?:[A-Z]{2,4}\d?(?:-[A-Z]{2,4}\d?)?-)?(?:BoxUsage|TimedStorage|DataTransfer|Requests|"
                        r"EBS|NatGateway|LoadBalancer|InstanceUsage|Multi-AZUsage|HeavyUsage|SpotUsage|"
                        r"CW|S3|Lambda|DataProcessing|VendedLog|AWS-(?:In|Out)-Bytes|[A-Za-z]+-(?:Bytes|Hours|Hrs))"
                        r"[\w:.\-]*)")
REGION_PREFIX = re.compile(r"\b(USE1|USE2|USW1|USW2|CAN1|CAN2|EUC1|EUC2|EUW1|EUW2|EUW3|EUN1|EUS1|EUS2|APN1|APN2|APN3|"
                           r"APS1|APS2|APS3|APS4|APS5|APS6|APE1|SAE1|AFS1|MES1|MEC1|ILC1|MXC1)-")
REGION_NAME = re.compile(r"^(US East|US West|Canada|EU|Europe|Asia Pacific|South America|Africa|Middle East|Israel|"
                         r"Mexico|AWS GovCloud)\s*\(([^)]+)\)$|^(Any|Global)$")
PRICE = re.compile(r"(?:USD|\$|€|£)\s?\$?\s?(\d+(?:\.\d+)?)\s+per\b", re.I)
# the block of units a price is for: "per 1,000 requests", "per 1 million", "per million", "per 10000"
BLOCK = re.compile(r"\s+(?:(\d{1,3}(?:,\d{3})+|\d+)\s*)?(million|billion|thousand)?\b", re.I)
SCALE_WORDS = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
QTY_UNIT = re.compile(r"(?<![\w.$])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s+([A-Za-z][\w/()-]*(?:[ -][A-Za-z][\w/()-]*)?)\s*$")
UNIT_WORDS = re.compile(r"^(hrs?|hours?|gb|gb-mo|gb-month|tb|requests?|events?|ios|lcu-hrs?|acu-hrs?|vcpu-hours?|"
                        r"units?|instances?|calls?|queries|messages|objects|keys|secrets|metrics|alarms|dashboards|"
                        r"api calls|invocations|lambda-gb-second|gb-second|seconds?|minutes?|days?|months?|users?|"
                        r"ru|wcu|rcu|nodes?|vcpu|cpu|transitions?|count|each|mo)\b", re.I)

NEW_FIELDS = ("instance_type", "database_engine", "operating_system", "deployment_option", "pricing_model",
              "usage_type")


def _first(pairs: list[tuple[str, str]], text: str) -> str | None:
    t = text.lower()
    return next((name for name, rx in pairs if re.search(rx, t)), None)


def instance_type(text: str) -> str | None:
    m = AWS_TYPE.search(text)
    if m:
        return m.group(0)
    m = AZURE_SIZE.search(text) or GCP_TYPE.search(text)
    return m.group(0) if m else None


def from_text(text: str) -> dict:
    """Every detail one text prints."""
    out: dict = {}
    if not text:
        return out
    if (it := instance_type(text)):
        out["instance_type"] = it
    if (e := _first(ENGINES, text)):
        out["database_engine"] = e
    if (o := _first(OSES, text)):
        out["operating_system"] = o
    if (d := _first(DEPLOYMENT, text)):
        out["deployment_option"] = d
    if (pm := _first(PRICING, text)):
        out["pricing_model"] = pm
    if (u := USAGE_TYPE.search(text)):
        out["usage_type"] = u.group(1)
    if (p := PRICE.search(text)):
        size = 1.0
        b = BLOCK.match(text, p.end())
        if b and (b.group(1) or b.group(2)):
            size = float((b.group(1) or "1").replace(",", "")) * SCALE_WORDS.get((b.group(2) or "").lower(), 1.0)
        # the price of one unit, so quantity x unit price is the cost; the printed form is kept beside it
        out["unit_price"] = float(p.group(1)) / size
        if size != 1.0 and b:
            out["price_per"] = (p.group(0) + b.group(0)).strip()
    if (q := QTY_UNIT.search(text)) and UNIT_WORDS.match(q.group(2)):
        out["quantity"] = float(q.group(1).replace(",", ""))
        out["unit"] = q.group(2)
    if (r := REGION_PREFIX.search(text)):
        out["region_code"] = r.group(1)
    return out


def _quantity(text: str) -> dict:
    q = QTY_UNIT.search(text) if text else None
    if q and UNIT_WORDS.match(q.group(2)):
        return {"quantity": float(q.group(1).replace(",", "")), "unit": q.group(2)}
    return {}


SEP = "\x1f"                    # joins a line's texts into one key (never printed in a bill)


def _found(texts: list[str]) -> dict:
    """What one line's texts print: the joined text, then a quantity and unit that end a text of their own ("...
    4,216.07 Hrs"), the description's last (in the joined text the headings come after it)."""
    found = from_text(" ".join(texts))
    for t in reversed(texts):
        if (q := _quantity(t)):
            found = {**found, **q}
    return found


def extract(rows: pd.DataFrame, warnings: list[str] | None = None) -> list[str]:
    """Fill empty attribute columns from each line's text (description, service label, context path). Returns the
    fields it filled. More than MAX_UNIQUE_TEXTS distinct texts (a resource ID used as the description): the step is
    skipped, with a note in warnings."""
    if rows.empty:
        return []
    def text_of(c: str) -> pd.Series:
        if c not in rows:
            return pd.Series([""] * len(rows), index=rows.index, dtype=object)
        return map_unique(rows[c], lambda v: "" if v is None or pd.isna(v) else str(v))
    sources = [text_of(c) for c in ("description", "service_raw", "product_code", "_path")]
    # once per distinct combination of the texts: exports repeat the same few texts on millions of lines
    key = sources[0]
    for t in sources[1:]:
        key = key + SEP + t
    codes, uniques = pd.factorize(key)
    limit = int(os.environ.get("MAX_UNIQUE_TEXTS", "500000"))
    if len(uniques) > limit:
        if warnings is not None:
            warnings.append(f"Details (instance type, engine, ...) were not read from the line texts: {len(uniques):,} "
                            f"distinct texts, more than MAX_UNIQUE_TEXTS ({limit:,})")
        return []
    found_unique = [_found(u.split(SEP)) for u in uniques]
    # a tax line has no usage: its quantity and price are the charge's, not its own
    taxed = rows["charge_type"].eq("Tax") if "charge_type" in rows else pd.Series(False, index=rows.index)
    filled = []
    for f in (*NEW_FIELDS, "unit_price", "price_per", "quantity", "unit"):
        per_key = np.array([d.get(f) for d in found_unique] + [None], dtype=object)
        vals = pd.Series(per_key[codes], index=rows.index).infer_objects()   # the type a map would infer
        if f in ("unit_price", "price_per", "quantity", "unit"):
            vals = vals.where(~taxed, None)
        if vals.notna().any():
            if f not in rows:
                rows[f] = None
            empty = rows[f].isna()
            if empty.any():
                rows.loc[empty, f] = vals[empty]
                filled.append(f)
    return filled


def normalize_regions(rows: pd.DataFrame) -> int:
    """A region column of printed names ("US East (N. Virginia)", "East US 2") becomes codes ("us-east-1",
    "eastus2"); the printed name is kept in region_name. Values that are no known region stay as printed. Returns
    the number of rows changed."""
    if rows.empty or "region" not in rows:
        return 0
    from svcres.regions import lookup
    codes = {v: (hit[1] if (hit := lookup(v)) else None) for v in rows["region"].dropna().astype(str).unique()}
    named = map_unique(rows["region"], lambda v: codes.get(str(v)) if v is not None and not pd.isna(v) else None)
    change = named.notna() & named.ne(rows["region"])
    if not change.any():
        return 0
    if "region_name" not in rows:
        rows["region_name"] = None
    keep = change & rows["region_name"].isna()
    rows.loc[keep, "region_name"] = rows.loc[keep, "region"]
    rows.loc[change, "region"] = named[change]
    return int(change.sum())


def region_of(label: str) -> str | None:
    """A region printed as a heading: "US East (N. Virginia)", "Canada (Central)", "Any"."""
    return label.strip() if REGION_NAME.match(label.strip()) else None
