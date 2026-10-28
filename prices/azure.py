"""Azure's retail prices (scripts/azure/fetch_azure_retail_prices.py: gzip JSON Lines, one API item per line) as rows of
the same price tables as AWS's (prices.load stages them and applies the same snapshot, history and sanity rules).

A product is one meter of one SKU: sku = "<meterId>/<skuId>/<meterName>" (a reservation has its own skuId; a few
meters print two names). Its columns: service_code = serviceName, product_family = serviceFamily, region =
armRegionName, location, instance_type = armSkuName, usage_type = the meter ID (a line's MeterId), meter_name,
product_name, sku_name, and for Virtual Machines the meter's operating system (a product name with "Windows" is the
Windows meter, else the Linux one; Microsoft's naming) and license model; productId, skuId, serviceId and
isPrimaryMeterRegion in attributes.

A rate is one price of a product: rate_code = "<sku>/<type>/<reservationTerm>/<tierMinimumUnits>"; term_type
on_demand (Consumption), dev_test (DevTestConsumption) or reserved (Reservation, lease_length 1yr / 3yr / 5yr);
begin_range = tierMinimumUnits and end_range the next tier's; effective_date = effectiveStartDate. The unit is the
unit of measure's block normalized (prices.units.azure_unit): "1 Hour" -> per hour, "10K" -> per request (price /
10,000), "1 GB/Month" -> per GB-month, "1/Month" -> per count-month. A reservation's retailPrice is the price of the
whole term for one unit: it is stored per hour (price / the term's hours), price_raw and unit_raw keep the term's.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pandas as pd

from billparse.attributes import SEP, _azure_os

from . import normalize as nz
from .units import azure_unit

PROVIDER = "azure"
SOURCE = "azure-retail-prices"
ALL_SERVICES = "all-services"         # the fetcher's --all-services files: every service in one (counted per service)
TERM_TYPES = {"Consumption": "on_demand", "DevTestConsumption": "dev_test", "Reservation": "reserved"}
TERM_HOURS = {"1yr": 8760.0, "3yr": 3 * 8760.0, "5yr": 5 * 8760.0}
PRODUCT_ATTRIBUTES = ["productId", "skuId", "serviceId", "isPrimaryMeterRegion"]
BATCH_ROWS = 200_000
VM = "Virtual Machines"


def price_list(service: str) -> str:
    """The name of a service's price list in a snapshot's counts: "azure:Virtual Machines"."""
    return f"{PROVIDER}:{service}"


def files(download_dir: Path) -> Iterator[tuple[str, str, Path]]:
    """(service, region, path) of every file of the download's manifest.json (a failed one left out)."""
    manifest = json.loads((download_dir / "manifest.json").read_text())
    for service, regions in manifest["files"].items():
        for region, res in regions.items():
            if "error" not in res:
                yield service, region, download_dir / res["path"]


def read_batches(path: Path, rows: int = BATCH_ROWS) -> Iterator[pd.DataFrame]:
    """One file's items, in batches of rows (a file is one service in one region: VMs in eastus are about 20,000
    items, one batch; the API lists a meter's tiers together, so a batch boundary rarely splits them)."""
    batch: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                batch.append(json.loads(line))
            if len(batch) >= rows:
                yield pd.DataFrame(batch)
                batch = []
    if batch:
        yield pd.DataFrame(batch)


def _text(df: pd.DataFrame, col: str) -> pd.Series:
    s = df[col] if col in df else pd.Series(None, index=df.index, dtype=object)
    s = s.astype(object)
    return s.where(s.notna() & s.astype(str).str.strip().ne(""), None)


def _os(product: str | None, service: str | None) -> tuple[str | None, str | None]:
    if service != VM or not isinstance(product, str):
        return None, None
    os_, lic, _ = _azure_os(f"{SEP}{product}{SEP}")
    return os_, lic


def frames(df: pd.DataFrame, service: str, product_fields: list[str],
           rate_fields: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(products, rates) of a batch of items, with the columns of prices.load's staging tables."""
    df = df.copy()
    meter, sku_id, meter_name = _text(df, "meterId"), _text(df, "skuId"), _text(df, "meterName")
    df = df[meter.notna() & sku_id.notna()]
    if df.empty:
        return pd.DataFrame(columns=product_fields), pd.DataFrame(columns=rate_fields)
    meter, sku_id, meter_name = meter[df.index], sku_id[df.index], meter_name[df.index]
    df["_sku"] = meter + "/" + sku_id + "/" + meter_name.fillna("")
    svc = _text(df, "serviceName").fillna(service)
    df["_service"] = svc
    # products: the first item of each SKU
    first = df.drop_duplicates("_sku")
    product = _text(first, "productName")
    os_lic = {k: _os(*k) for k in set(zip(product, first["_service"], strict=True))}
    pairs = [os_lic[k] for k in zip(product, first["_service"], strict=True)]
    prod = pd.DataFrame({c: None for c in product_fields}, index=first.index)
    prod["provider"] = PROVIDER
    prod["sku"] = first["_sku"]
    prod["service_code"] = first["_service"]
    prod["product_family"] = _text(first, "serviceFamily")
    region = _text(first, "armRegionName")
    prod["region"] = region.map(lambda v: v.lower() if isinstance(v, str) else None)
    prod["location"] = _text(first, "location")
    prod["instance_type"] = _text(first, "armSkuName").map(nz.instance_type)
    prod["operating_system"] = [p[0] for p in pairs]
    prod["license_model"] = [nz.license_model(p[1]) for p in pairs]
    prod["usage_type"] = meter[first.index]
    prod["meter_name"] = meter_name[first.index]
    prod["product_name"] = product
    prod["sku_name"] = _text(first, "skuName")
    attrs = [c for c in PRODUCT_ATTRIBUTES if c in first]
    prod["attributes"] = [json.dumps({k: v for k, v in zip(attrs, row, strict=True) if v is not None and v == v},
                                     separators=(",", ":"), ensure_ascii=False, default=str)
                          for row in first[attrs].itertuples(index=False, name=None)]
    hashed = [c for c in product_fields if c != "attr_hash"]
    prod["attr_hash"] = [hashlib.sha1("\x1f".join(v if isinstance(v, str) else "" for v in row).encode()).hexdigest()
                         for row in prod[hashed].itertuples(index=False, name=None)]
    # rates: every item
    typ = _text(df, "type").fillna("Consumption")
    term = _text(df, "reservationTerm")
    lease = term.map(lambda v: nz.term(v) if isinstance(v, str) else None)
    tier = pd.to_numeric(df["tierMinimumUnits"] if "tierMinimumUnits" in df else 0, errors="coerce").fillna(0.0)
    price = pd.to_numeric(df["retailPrice"], errors="coerce")
    uom = _text(df, "unitOfMeasure")
    table = {u: (azure_unit(u) or (str(u).strip().lower(), 1.0)) for u in uom.dropna().unique()}
    unit = uom.map(lambda u: table[u][0] if u in table else "unit")
    factor = uom.map(lambda u: table[u][1] if u in table else 1.0).astype(float)
    reserved = typ.eq("Reservation")
    hours = lease.map(lambda v: TERM_HOURS.get(v) if isinstance(v, str) else None).astype(float)
    per_term = reserved & unit.eq("hour") & hours.notna()
    unit_price = (price / factor).where(~per_term, price / hours)
    group = df["_sku"] + "\x1f" + typ + "\x1f" + term.fillna("")
    rates = pd.DataFrame({
        "provider": PROVIDER, "service_code": df["_service"], "sku": df["_sku"],
        "rate_code": group.str.replace("\x1f", "/") + "/" + tier.map(lambda v: f"{v:g}"),
        "term_type": typ.map(lambda v: TERM_TYPES.get(v, v.lower())), "lease_length": lease,
        "purchase_option": None, "offering_class": None,
        "unit": unit, "unit_raw": uom.where(~per_term, term), "unit_price": unit_price.round(12), "price_raw": price,
        "begin_range": tier, "end_range": None,
        "currency": _text(df, "currencyCode").fillna("USD"),
        "effective_date": _text(df, "effectiveStartDate").map(lambda v: v[:10] if isinstance(v, str) else None)})
    # a tier ends where the next one of the same price starts
    order = rates.assign(_g=group).sort_values(["_g", "begin_range"])
    nxt = order.groupby("_g")["begin_range"].shift(-1)
    rates["end_range"] = nxt.reindex(rates.index)
    return prod[product_fields], rates[rate_fields][rates["unit_price"].notna()]
