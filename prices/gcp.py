"""GCP public list prices (the Cloud Billing Catalog API, scripts/gcp/fetch_gcp_prices.py) for the price tables.

A download is a folder with manifest.json ("provider": "gcp") and one gzip JSON Lines file per service: one SKU per
line, as services.skus.list returns it. prices.load.load_snapshot reads it like the AWS files, with the same
snapshot, history and sanity rules; this module only turns SKUs into rows:

  product   one per skuId: service_code = the service ID (6F81-5844-456A, what a GCP export prints as service.id),
            product_family = category.resourceFamily, usage_type = category.usageType (OnDemand, Preemptible,
            Commit1Yr, ...), region = the one serviceRegion (None when the SKU lists several: they are in
            attributes.regions), and the rest as JSON: description, resource group, regions, the units
  rate      one per tier of the latest pricingInfo: rate_code "<skuId>.t<startUsageAmount>", price = units + nanos,
            term_type from usageType (on_demand | spot | committed_use, lease 1yr/3yr), effective_date = its
            effectiveTime

Each SKU is priced in its usageUnit ("h", "GiBy.mo"); the export prints the same unit in words
(usage.pricing_unit: "hour", "gibibyte month") or the base unit (usage.unit: "seconds", "byte-seconds"), which
baseUnitConversionFactor converts (base_factor).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pandas as pd

from .units import normalize as unit_of

PROVIDER = "gcp"
SOURCE = "gcp-billing-catalog"
PREFIX = "gcp:"                          # a GCP price list's name in price_snapshot.counts: gcp:<service ID>
BATCH = 20_000                           # SKUs per batch
TERMS = {"ondemand": ("on_demand", None), "preemptible": ("spot", None)}
# usageUnit codes the unit words do not already name: (normalized unit, factor of one such unit)
CODES = {"h": ("hour", 1.0), "s": ("hour", 1 / 3600), "min": ("hour", 1 / 60), "d": ("hour", 24.0),
         "giby.mo": ("gb_month", 1.0), "tiby.mo": ("gb_month", 1024.0), "miby.mo": ("gb_month", 1 / 1024),
         "giby": ("gb", 1.0), "tiby": ("gb", 1024.0), "miby": ("gb", 1 / 1024), "by": ("gb", 1 / 2**30),
         "count": ("request", 1.0)}
BASE_WORDS = {"s": "second", "by": "byte", "by.s": "byte second", "count": "count", "h": "hour"}


def price_list(service_id: str) -> str:
    return f"{PREFIX}{service_id}"


def download_files(download_dir: Path) -> Iterator[tuple[str, str, str, Path]]:
    """("gcp", price list, "global", path) of every service file in the manifest."""
    manifest = json.loads((download_dir / "manifest.json").read_text())
    for service_id, res in manifest["files"].items():
        if "error" not in res:
            yield "gcp", price_list(service_id), "global", download_dir / res["file"]


def read_batches(path: Path, batch: int = BATCH) -> Iterator[list[dict]]:
    """The SKUs of one service file, in batches."""
    out: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
                if len(out) >= batch:
                    yield out
                    out = []
    if out:
        yield out


def money(m: dict | None) -> float | None:
    """Money {units: "1", nanos: 500000000} -> 1.5."""
    if not m:
        return None
    return int(m.get("units") or 0) + int(m.get("nanos") or 0) / 1e9


def usage_unit(code: str | None, words: str | None) -> tuple[str, float]:
    """(normalized unit, factor) of a SKU's usage unit: from its words ("hour", "gibibyte month"), else its code
    ("h", "GiBy.mo"), else the words (or code) as printed in lower case, factor 1 (a line printing the same words
    still matches)."""
    for v in (words, code):
        if v and (got := unit_of(v)):
            return got
    if code and code.strip().lower() in CODES:
        return CODES[code.strip().lower()]
    return (words or code or "unit").strip().lower(), 1.0


def _words(v: object) -> str:
    """'byte-seconds' and 'By.s' and 'byte second' alike: lower case, words, no plural s."""
    s = str(v).strip().lower()
    s = BASE_WORDS.get(s, s)
    return " ".join(w[:-1] if len(w) > 2 and w.endswith("s") else w for w in re.split(r"[\s\-.]+", s) if w)


def base_factor(attrs: dict, line_unit: object) -> tuple[str, float] | None:
    """A line counted in the SKU's base unit (usage.unit "seconds", "byte-seconds"): (the price's normalized unit, the
    factor of one line unit in it) = usage unit factor / baseUnitConversionFactor. None when the line's unit is not
    the base unit."""
    base, words, conv = attrs.get("base_unit"), attrs.get("base_unit_description"), attrs.get("base_unit_conversion_factor")
    if not conv or line_unit is None or not str(line_unit).strip():
        return None
    mine = _words(line_unit)
    if mine not in {_words(v) for v in (base, words) if v}:
        return None
    unit, factor = usage_unit(attrs.get("usage_unit"), attrs.get("usage_unit_description"))
    return unit, factor / float(conv)


def _term(usage_type: str | None) -> tuple[str, str | None]:
    s = (usage_type or "").strip()
    if (m := re.fullmatch(r"commit(\d+)(yr|mo)", s.lower())):
        n = int(m[1]) / (12 if m[2] == "mo" else 1)
        return "committed_use", f"{n:g}yr"
    return TERMS.get(s.lower(), (s.lower() or "on_demand", None))


def _start(v: object) -> str:
    x = float(v or 0)
    return str(int(x)) if x.is_integer() else repr(x)


def sku_frames(skus: list[dict], service_id: str, product_fields: list[str],
               rate_fields: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(products, rates) of a batch of SKUs, in the load's staging columns."""
    prods, rates = [], []
    for s in skus:
        sku = s.get("skuId")
        if not sku:
            continue
        cat = s.get("category") or {}
        regions = list(s.get("serviceRegions") or [])
        infos = [p for p in s.get("pricingInfo") or [] if p.get("pricingExpression")]
        info = max(infos, key=lambda p: p.get("effectiveTime") or "") if infos else {}
        expr = info.get("pricingExpression") or {}
        attrs = {"description": s.get("description"), "service_display_name": cat.get("serviceDisplayName"),
                 "resource_group": cat.get("resourceGroup"), "regions": regions,
                 "usage_unit": expr.get("usageUnit"), "usage_unit_description": expr.get("usageUnitDescription"),
                 "base_unit": expr.get("baseUnit"), "base_unit_description": expr.get("baseUnitDescription"),
                 "base_unit_conversion_factor": expr.get("baseUnitConversionFactor"),
                 "display_quantity": expr.get("displayQuantity"),
                 "service_provider_name": s.get("serviceProviderName"),
                 "geo_taxonomy": (s.get("geoTaxonomy") or {}).get("type")}
        attrs = {k: v for k, v in attrs.items() if v not in (None, "", [])}
        prod = {f: None for f in product_fields}
        prod.update(provider=PROVIDER, sku=sku, service_code=service_id, product_family=cat.get("resourceFamily"),
                    region=regions[0] if len(regions) == 1 else None, usage_type=cat.get("usageType"),
                    attributes=json.dumps(attrs, separators=(",", ":"), ensure_ascii=False, sort_keys=True))
        prod["attr_hash"] = hashlib.sha1("\x1f".join(prod[f] if isinstance(prod[f], str) else ""
                                                     for f in product_fields if f != "attr_hash").encode()).hexdigest()
        prods.append(prod)
        term_type, lease = _term(cat.get("usageType"))
        unit, factor = usage_unit(expr.get("usageUnit"), expr.get("usageUnitDescription"))
        tiers = expr.get("tieredRates") or []
        for i, t in enumerate(tiers):
            price = money(t.get("unitPrice"))
            if price is None:
                continue
            nxt = tiers[i + 1].get("startUsageAmount") if i + 1 < len(tiers) else None
            rates.append({"provider": PROVIDER, "service_code": service_id, "sku": sku,
                          "rate_code": f"{sku}.t{_start(t.get('startUsageAmount'))}", "term_type": term_type,
                          "lease_length": lease, "purchase_option": None, "offering_class": None, "unit": unit,
                          "unit_raw": expr.get("usageUnit"), "unit_price": round(price / factor, 12),
                          "price_raw": price, "begin_range": float(t.get("startUsageAmount") or 0),
                          "end_range": None if nxt is None else float(nxt),
                          "currency": (t.get("unitPrice") or {}).get("currencyCode") or "USD",
                          "effective_date": (info.get("effectiveTime") or "")[:10] or None})
    return (pd.DataFrame(prods, columns=product_fields),
            pd.DataFrame(rates, columns=rate_fields))
