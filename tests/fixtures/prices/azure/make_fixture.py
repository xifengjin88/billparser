"""Download the Azure price-catalog test fixture from the public Azure Retail Prices API (no key, no customer data).

    uv run python tests/fixtures/prices/azure/make_fixture.py

Writes tests/fixtures/prices/azure/download/ (the layout and manifest.json of
scripts/azure/fetch_azure_retail_prices.py) with a few products of eastus, each from one small filtered request:
the VM sizes the Azure samples use (D4s v5, D8s v5, E8ds v5, B2ms: Linux and Windows, Spot, Low Priority, DevTest
and 1 and 3 year reservations), Premium SSD P10 disks (unit 1/Month), the Hot LRS block blob meters (1 GB/Month with
tiers, 10K operations), Bandwidth (tiers) and the AKS uptime SLA.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).parent / "download"
REGION = "eastus"
SIZES = ["Standard_D4s_v5", "Standard_D8s_v5", "Standard_E8ds_v5", "Standard_B2ms"]
PARTS = {
    "Virtual Machines": " or ".join(f"armSkuName eq '{s}'" for s in SIZES),
    "Storage": "skuName eq 'P10 LRS' or (productName eq 'General Block Blob v2' and skuName eq 'Hot LRS')",
    "Bandwidth": None,
    "Azure Kubernetes Service": "meterName eq 'Standard Uptime SLA'",
}


def _fetcher():
    path = ROOT / "scripts" / "azure" / "fetch_azure_retail_prices.py"
    spec = importlib.util.spec_from_file_location("fetch_azure_retail_prices", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                          # type: ignore[union-attr]
    return mod


def main() -> None:
    f = _fetcher()
    shutil.rmtree(OUT, ignore_errors=True)
    files = {}
    for service, extra in PARTS.items():
        rel = f"{f.slug(service)}/{REGION}.jsonl.gz"
        (OUT / rel).parent.mkdir(parents=True, exist_ok=True)
        res = f.fetch(f.odata_filter(service, REGION, extra), OUT / rel)
        files[service] = {REGION: {"path": rel, "rows": res["rows"], "requests": res["requests"],
                                   "filter": res["filter"]}}
        print(service, res["rows"], "rows")
        time.sleep(2)
    manifest = {"provider": "azure", "source": "azure-retail-prices", "fetched_at": "2026-10-27T00:00:00+00:00",
                "base": f.BASE, "files": files}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
