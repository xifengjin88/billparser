#!/usr/bin/env bash
# Nightly: refresh raw catalogs, rebuild, and swap in atomically.
# Cron example: 0 2 * * * cd /opt/svcres && ./scripts/refresh_catalog.sh >> logs/refresh.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/raw

echo "[$(date -u +%FT%TZ)] refresh start"
python scripts/fetch_azure.py --out data/raw/azure_products.json
python scripts/fetch_aws.py   --out data/raw/aws_services.json
if [[ -n "${GCP_API_KEY:-}" ]]; then
  python scripts/fetch_gcp.py --out data/raw/gcp_services.json
else
  echo "GCP_API_KEY not set, keeping previous gcp_services.json"
fi

# Build to a temp file, sanity check, then rename (atomic on the same filesystem)
python scripts/build_catalog.py --out data/catalog.msgpack.new
python - <<'PY'
import sys
sys.path.insert(0, ".")
from pathlib import Path
from svcres.catalog import Catalog
new = Catalog.load(Path("data/catalog.msgpack.new"))
old_p = Path("data/catalog.msgpack")
if old_p.exists():
    old = Catalog.load(old_p)
    if len(new.codes) < 0.9 * len(old.codes):   # a partial fetch should never replace a good catalog
        sys.exit(f"New catalog shrank: {len(new.codes)} codes vs {len(old.codes)}. Keeping old one.")
print("sanity check ok", new.stats())
PY
mv data/catalog.msgpack.new data/catalog.msgpack
echo "[$(date -u +%FT%TZ)] refresh done"
