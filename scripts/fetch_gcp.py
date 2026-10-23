"""Fetch GCP billing services from the Cloud Billing Catalog API.

Needs an API key with the Cloud Billing API enabled (no billing account access needed):
    export GCP_API_KEY=...
    python scripts/fetch_gcp.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

API = "https://cloudbilling.googleapis.com/v1/services"


def fetch(key: str) -> dict[str, dict]:
    services: dict[str, dict] = {}
    token = None
    while True:
        params = {"key": key, "pageSize": 5000}
        if token:
            params["pageToken"] = token
        r = requests.get(API, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        for s in data.get("services", []):
            services[s["serviceId"]] = {"displayName": s.get("displayName")}
        token = data.get("nextPageToken")
        if not token:
            return services


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("data/raw/gcp_services.json"))
    a = ap.parse_args()
    key = os.environ.get("GCP_API_KEY")
    if not key:
        sys.exit("Set GCP_API_KEY")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    svcs = fetch(key)
    a.out.write_text(json.dumps(svcs, indent=1))
    print(f"Wrote {len(svcs)} services to {a.out}")
