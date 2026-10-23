"""Merge raw fetch outputs + seeds into data/catalog.msgpack.

    python scripts/build_catalog.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from svcres.catalog import build  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    ap.add_argument("--out", type=Path, default=Path("data/catalog.msgpack"))
    a = ap.parse_args()
    cat = build(a.raw_dir)
    cat.save(a.out)
    print(f"Wrote {a.out} ({a.out.stat().st_size / 1e6:.2f} MB): {cat.stats()}")
