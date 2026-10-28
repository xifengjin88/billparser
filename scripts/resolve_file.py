"""Resolve the service column of a CSV/XLSX/JSON file.

    python scripts/resolve_file.py invoice.csv --code-col "Product code" --name-col "Service"
    python scripts/resolve_file.py usage.xlsx --name-col MeterCategory --provider azure

Writes <input>.resolved.csv with service, provider, category, method, score columns,
and prints unresolved values with their top candidates for review.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billparse.settings import load_env  # noqa: E402
from billparse.synonyms import synonym_store  # noqa: E402
from svcres.catalog import Catalog  # noqa: E402
from svcres.llm_fallback import make_llm_fallback  # noqa: E402
from svcres.resolver import Resolver  # noqa: E402


def read_any(path: Path, sheet: str | None) -> pd.DataFrame:
    suf = path.suffix.lower()
    if suf in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, sheet_name=sheet or 0, dtype=str)
    if suf == ".json":
        return pd.read_json(path, dtype=False)
    return pd.read_csv(path, dtype=str, low_memory=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", type=Path)
    ap.add_argument("--code-col")
    ap.add_argument("--name-col")
    ap.add_argument("--provider", choices=["azure", "aws", "gcp"])
    ap.add_argument("--sheet")
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    if not (a.code_col or a.name_col):
        sys.exit("Give --code-col and/or --name-col")

    load_env()
    t0 = time.perf_counter()
    cat = Catalog.load(a.data_dir / "catalog.msgpack")
    fb = None if a.no_llm else make_llm_fallback(cat)
    res = Resolver(cat, a.data_dir, fallback=fb, synonyms=synonym_store(a.data_dir))  # the database's
    t_load = time.perf_counter() - t0

    df = read_any(a.file, a.sheet)
    t1 = time.perf_counter()
    # Codes first; fall back to the name column where the code is missing or unknown.
    by_code = res.resolve_many(df[a.code_col].unique(), a.provider) if a.code_col else {}
    by_name = res.resolve_many(df[a.name_col].unique(), a.provider) if a.name_col else {}

    def pick(row):
        for col, table in ((a.code_col, by_code), (a.name_col, by_name)):
            if col and isinstance(row[col], str):
                r = table.get(row[col].strip())
                if r and r.service_key:
                    return r
        return None

    picked = df.apply(pick, axis=1)
    for f in ("service", "provider", "category", "method", "score"):
        df[f"resolved_{f}"] = picked.map(lambda r, f=f: getattr(r, f) if r else None)
    t_res = time.perf_counter() - t1

    out = a.file.with_suffix(".resolved.csv")
    df.to_csv(out, index=False)

    n = len(df)
    ok = df["resolved_service"].notna().sum()
    uniq = len(by_code) + len(by_name)
    print(f"Catalog load {t_load*1000:.0f} ms | resolve {n} rows ({uniq} unique values) in {t_res*1000:.0f} ms")
    print(f"Resolved {ok}/{n} rows ({ok/n:.1%}). Methods: "
          f"{df['resolved_method'].value_counts(dropna=False).to_dict()}")
    unresolved = [r for r in [*by_code.values(), *by_name.values()] if not r.service_key]
    if unresolved:
        print("\nUnresolved (add with scripts/add_synonym.py):")
        for r in unresolved[:30]:
            top = ", ".join(f"{k} ({s:.0f})" for k, s in (r.candidates or [])[:3]) or "no candidates"
            print(f"  {r.raw!r:45} -> {top}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
