"""Write the GCP price-catalog test fixture: a few SKUs, hand-written in the Cloud Billing Catalog API's JSON format
(services.skus.list), in the layout of scripts/gcp/fetch_gcp_prices.py. Prices are made up near the public ones; no
API call, no customer data.

    python tests/fixtures/prices/gcp/make_fixture.py

Compute Engine: the two SKU IDs of samples/files/gcp_export_full.json (an N2 and an E2 core, on demand), a Spot N2
core, an N2 1-year commitment, an N2 RAM SKU (priced per gibibyte hour, exported in byte-seconds). Cloud Storage: a
multi-region storage SKU (per gibibyte month). Networking: an egress SKU with tiers, the first one free.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

OUT = Path(__file__).parent / "download"
WHEN = "2026-10-01T07:00:00Z"
AMERICAS = ["us-central1", "us-east1", "us-east4", "us-west1"]
COMPUTE, STORAGE, NETWORK = "6F81-5844-456A", "95FF-2EF5-5EA1", "E505-1604-58F8"
SERVICES = {COMPUTE: "Compute Engine", STORAGE: "Cloud Storage", NETWORK: "Networking"}


def money(price: float) -> dict:
    units = int(price)
    return {"currencyCode": "USD", "units": str(units), "nanos": round((price - units) * 1e9)}


def sku(service: str, sku_id: str, description: str, family: str, group: str, usage: str, regions: list[str],
        unit: str, unit_words: str, base: str, base_words: str, factor: float,
        tiers: list[tuple[float, float]]) -> dict:
    return {
        "name": f"services/{service}/skus/{sku_id}", "skuId": sku_id, "description": description,
        "category": {"serviceDisplayName": SERVICES[service], "resourceFamily": family, "resourceGroup": group,
                     "usageType": usage},
        "serviceRegions": regions,
        "pricingInfo": [{"summary": "", "effectiveTime": WHEN, "currencyConversionRate": 1,
                         "pricingExpression": {"usageUnit": unit, "displayQuantity": 1, "usageUnitDescription": unit_words,
                                               "baseUnit": base, "baseUnitDescription": base_words,
                                               "baseUnitConversionFactor": factor,
                                               "tieredRates": [{"startUsageAmount": s, "unitPrice": money(p)}
                                                               for s, p in tiers]},
                         "aggregationInfo": {"aggregationLevel": "ACCOUNT", "aggregationInterval": "MONTHLY",
                                             "aggregationCount": 1}}],
        "serviceProviderName": "Google",
        "geoTaxonomy": {"type": "MULTI_REGIONAL" if len(regions) > 1 else "REGIONAL", "regions": regions}}


HOUR = ("h", "hour", "s", "second", 3600)
SKUS = {
    COMPUTE: [
        sku(COMPUTE, "CP-COMPUTEENGINE-VMIMAGE-N2-STANDARD-4", "N2 Instance Core running in Americas", "Compute", "CPU",
            "OnDemand", AMERICAS, *HOUR, [(0, 0.031611)]),
        sku(COMPUTE, "D973-5D65-BAB2", "E2 Instance Core running in Americas", "Compute", "CPU", "OnDemand", AMERICAS,
            *HOUR, [(0, 0.021811)]),
        sku(COMPUTE, "2D3A-6B1E-9F0C", "Spot Preemptible N2 Instance Core running in Americas", "Compute", "CPU",
            "Preemptible", AMERICAS, *HOUR, [(0, 0.007650)]),
        sku(COMPUTE, "4C1E-7A2B-0F11", "Commitment v1: N2 Cpu in Americas for 1 Year", "Compute", "CPU", "Commit1Yr",
            AMERICAS, *HOUR, [(0, 0.019915)]),
        sku(COMPUTE, "F449-33EC-A5EF", "N2 Instance Ram running in Americas", "Compute", "RAM", "OnDemand", AMERICAS,
            "GiBy.h", "gibibyte hour", "By.s", "byte second", 3865470566400, [(0, 0.004237)]),
    ],
    STORAGE: [
        sku(STORAGE, "E5F0-6A5D-7BAD", "Standard Storage US Multi-region", "Storage", "MultiRegionalStorage",
            "OnDemand", ["us"], "GiBy.mo", "gibibyte month", "By.s", "byte second", 2821703270400000,
            [(0, 0.026)]),
    ],
    NETWORK: [
        sku(NETWORK, "9DE9-9092-B3BC", "Network Internet Egress from Americas to Americas", "Network", "PremiumInternetEgress",
            "OnDemand", ["us-central1"], "GiBy", "gibibyte", "By", "byte", 1073741824,
            [(0, 0.0), (1, 0.12), (1024, 0.11), (10240, 0.08)]),
    ],
}


def write(out: Path = OUT, skus: dict[str, list[dict]] = SKUS, fetched_at: str = "2026-10-27T09:00:00+00:00") -> None:
    """The download: skus/<service ID>.jsonl.gz and manifest.json (also used by the tests to write edited copies)."""
    files = {}
    for service, rows in skus.items():
        path = out / "skus" / f"{service}.jsonl.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.GzipFile(path, "wb", mtime=0) as fh:            # the same bytes on every run
            fh.write("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode())
        files[service] = {"file": f"skus/{service}.jsonl.gz", "display_name": SERVICES.get(service, service),
                          "skus": len(rows), "gz_bytes": path.stat().st_size}
    manifest = {"provider": "gcp", "fetched_at": fetched_at, "finished_at": fetched_at,
                "base": "https://cloudbilling.googleapis.com/v1", "currency": "USD",
                "services": {s: {"display_name": SERVICES.get(s, s), "business_entity": "businessEntities/GCP"}
                             for s in skus},
                "files": files, "totals": {"files": len(files), "skus": sum(len(r) for r in skus.values()), "errors": 0}}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    write()
    print(f"wrote {OUT}")
