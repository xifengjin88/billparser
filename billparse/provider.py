"""The document's provider (aws, azure, gcp) from the file's own evidence, for the lines that do not say.

A line's provider comes from its spec (a built-in export's) or from its resolved service. A reseller export or an
invoice often has neither on most lines (meter names do not resolve to a service), and pricing chooses its catalog
by the line's provider. Evidence, each with a weight:
    the file name           "azure", "aws", "gcp" (and their vendors' names) as a word
    the headers             names one provider uses (MeterName, lineItem/, sku.id, ...)
    the values              resource IDs (/subscriptions/..., arn:aws:...), region names (eastus, us-east-1, us-central1)
    the resolved lines      the providers of the lines whose service resolved
The provider with the most weight wins when it has at least MIN_SCORE and twice the next one's: a multi-cloud
invoice (AWS and Azure lines) says nothing, and its lines keep their own.
"""

from __future__ import annotations

import re
from collections import Counter

import pandas as pd

MIN_SCORE = 2.0
NAME = {"aws": r"aws|amazon", "azure": r"azure|microsoft|msft", "gcp": r"gcp|google|bigquery"}
HEADERS = {
    "azure": r"meter ?(name|id|category|sub ?category|region)|subscription ?(id|name|guid)|resource ?group|tenant ?id|"
             r"resource ?location|pay ?g ?price|billing ?currency ?code|cost ?in ?billing ?currency|"
             r"exchange ?rate ?pricing ?to ?billing|benefit ?(id|name)|pc ?to ?bc ?exchange ?rate",
    "aws": r"line ?item/|product/|pricing/|reservation/|savings ?plan/|bill/|usage ?type|linked ?account|"
           r"payer ?account|rate ?code|unblended|blended",
    "gcp": r"project\.?(id|name|number)|sku\.?(id|description)|billing ?account ?id|service\.description|"
           r"cost ?type|invoice\.month|usage\.pricing ?unit",
}
VALUES = {"azure": re.compile(r"^/subscriptions/", re.I), "aws": re.compile(r"^arn:aws", re.I)}
REGIONS = {
    "azure": re.compile(r"^(east|west|north|south|central|northcentral|southcentral|westcentral)us\d?$|"
                        r"^(west|north)europe$|^uk(south|west)$|^(japan|korea|australia|canada|brazil|france|germany|"
                        r"switzerland|norway|sweden|uae|india|southafrica|qatar|israel|italy|poland|spain|mexico)\w+$|"
                        r"^(east|southeast)asia$", re.I),
    "aws": re.compile(r"^(us|eu|ap|ca|sa|me|af|il|mx)-(east|west|north|south|central|northeast|southeast)-\d$", re.I),
    "gcp": re.compile(r"^(us|europe|asia|australia|northamerica|southamerica|me|africa)-[a-z]+\d$", re.I),
}


def _norm(h: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(h)).lower().replace("_", " ").strip()


def _share(col: pd.Series, rx: re.Pattern) -> float:
    vals = col.dropna().astype(str).head(500)
    vals = vals[vals.str.strip().ne("")]
    return float(vals.str.match(rx).mean()) if len(vals) else 0.0


def detect_provider(rows: pd.DataFrame, headers: list[str], name: str) -> dict | None:
    """{"value": provider, "from": [what said so]}, or None when the evidence is too little or mixed."""
    score: Counter = Counter()
    why: dict[str, list[str]] = {p: [] for p in NAME}
    stem = re.sub(r"[_.\-]+", " ", str(name or "")).lower()
    for p, rx in NAME.items():
        if re.search(rf"\b({rx})\b", stem):
            score[p] += 2.0
            why[p].append("file name")
    for p, rx in HEADERS.items():
        hits = [h for h in headers if re.search(rf"\b({rx})", _norm(h))]
        if hits:
            score[p] += min(3.0, float(len(hits)))
            why[p].append("headers " + ", ".join(hits[:6]))
    if "resource_id" in rows:
        for p, rx in VALUES.items():
            if _share(rows["resource_id"], rx) > 0.5:
                score[p] += 2.0
                why[p].append("resource IDs")
    if "region" in rows:
        for p, rx in REGIONS.items():
            if _share(rows["region"], rx) > 0.5:
                score[p] += 1.0
                why[p].append("region names")
    if "provider" in rows:
        known = rows["provider"].dropna().astype(str).str.lower()
        known = known[known.isin(list(NAME))]
        if len(known):
            top, n = Counter(known).most_common(1)[0]
            if n / len(known) >= 0.9:
                score[top] += 2.0 if len(known) >= 10 else 1.0
                why[top].append(f"{n:,} of {len(known):,} lines with a resolved service")
    if not score:
        return None
    (best, s1), *rest = score.most_common()
    s2 = rest[0][1] if rest else 0.0
    if s1 < MIN_SCORE or s1 < 2 * s2:
        return None
    return {"value": best, "from": why[best]}


def fill_provider(rows: pd.DataFrame, found: dict | None) -> int:
    """The lines without a provider get the document's; lines that have one keep it. Returns how many were filled."""
    if not found or rows.empty:
        return 0
    if "provider" not in rows:
        rows["provider"] = None
    empty = rows["provider"].isna() | rows["provider"].astype(str).str.strip().eq("")
    rows.loc[empty, "provider"] = found["value"]
    return int(empty.sum())
