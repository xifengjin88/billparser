"""Task 06: price matching. Every AWS usage line looked up in the price catalog (the task 05 fixture, loaded into the
test store's SQLite database): its level, assumptions and missing key columns, the list price and list cost where the
file printed none, the rules R1-R3, the same results on the normal path, the large path and after roll-ups, and
reprice without parsing again."""

from __future__ import annotations

import csv
import gzip
import json
import shutil
import time
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import billparse.analyze as analyze_mod
import billparse.contract as contract_mod
from billparse import Pipeline, Settings, api, webapi
from billparse.attributes import extract
from billparse.db import engine_for
from billparse.export import to_schema
from billparse.pricing import KeyCache, PriceDefaults, infer_discount, match_prices
from billparse.schema import Document
from prices.load import load_snapshot
from prices.store import PriceCatalog

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "samples" / "files"
FIXTURE = ROOT / "tests" / "fixtures" / "prices" / "download"
LINUX = "5G4TA8Z4MUKE6MJB"                      # m5.xlarge Linux Shared us-east-1: $0.192 per hour
LINUX_RATE = f"{LINUX}.JRTCKXETXF.6YS6EN2CT7"
RDS = "C86H957YE4QF4727"                        # db.m5.large MySQL Single-AZ: $0.171 per hour
S3 = "WP9ANXZGBYYSGJEA"                         # S3 standard storage: $0.023 per GB-month, first tier
TINY = "0.000001"                               # LARGE_FILE_MB: every file is large


# ---------------------------------------------------------------- helpers
@pytest.fixture
def catalog(tmp_path) -> PriceCatalog:
    engine = engine_for(f"sqlite:///{tmp_path / 'prices.db'}")
    load_snapshot(engine, FIXTURE)
    return PriceCatalog(engine)


def _pipe(tmp_path: Path, name: str = "store", prices: bool = True) -> Pipeline:
    raw = tmp_path / "data" / "raw"
    if not raw.exists():
        raw.mkdir(parents=True)
        for f in (ROOT / "data" / "raw").glob("*.json"):
            shutil.copy(f, raw / f.name)
    analyze_mod._RESOLVER = None
    pipe = Pipeline(Settings(store_dir=tmp_path / name, catalog_dir=tmp_path / "data", use_llm=False,
                             use_agent=False))
    if prices:
        load_snapshot(pipe.store.engine, FIXTURE)
    return pipe


def _cur(tmp_path: Path, list_cost: bool = True) -> Path:
    """The legacy CUR sample with the fixture's real m5.xlarge SKU and rate code (the sample's are made up), and one
    more line identified only by its usage type and operation. list_cost=False: without the public on-demand
    columns, so the list cost comes from the catalog."""
    df = pd.read_csv(FILES / "aws_cur_legacy_full.csv.gz", dtype=str, keep_default_na=False)
    for col in ("product/sku", "pricing/RateCode"):
        df[col] = df[col].str.replace("RVS26QBX9XP34QN6", LINUX).str.replace("JRTCKXETXFVB2RYV", LINUX)
    m5 = df[df["lineItem/LineItemType"].eq("Usage") & df["lineItem/UsageType"].str.contains("m5.xlarge")]
    extra = m5.iloc[[0]].copy()
    extra["identity/LineItemId"], extra["product/sku"], extra["pricing/RateCode"] = "li-usage-only", "", ""
    extra["lineItem/UsageType"], extra["lineItem/Operation"] = "BoxUsage:m5.xlarge", "RunInstances"
    df = pd.concat([df, extra], ignore_index=True)
    if not list_cost:
        df = df.drop(columns=[c for c in df if c.startswith("pricing/publicOnDemand")])
    path = tmp_path / ("cur.csv.gz" if list_cost else "cur_nolist.csv.gz")
    df.to_csv(path, index=False, compression="gzip")
    return path


def _doc(pipe: Pipeline, sha: str) -> Document:
    return Document.model_validate_json(pipe.store.part(sha, "doc"))


def _list_total(doc: Document) -> Decimal:
    return sum((c.list_cost.value for c in doc.charges if c.list_cost), Decimal(0))


def _lines(**cols) -> pd.DataFrame:
    """Usage lines of AWS, as a parse leaves them (charge type, provider, a date)."""
    n = max(len(v) for v in cols.values() if isinstance(v, list))
    base = {"charge_type": "Usage", "provider": "aws", "charge_start": "2026-10-27", "region": "us-east-1",
            "service_code": "AmazonEC2", "quantity": 10.0, "unit": "Hrs", "billed_cost": 1.0}
    return pd.DataFrame({**{k: [v] * n for k, v in base.items()}, **cols})


def _edit(download: Path, offer: str, change) -> None:
    (path,) = (download / "offers" / offer).glob("*/us-east-1.csv.gz")
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        head = [fh.readline() for _ in range(5)]
        reader = csv.DictReader(fh)
        names, rows = reader.fieldnames, [change(dict(r)) for r in reader]
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        fh.writelines(head)
        w = csv.DictWriter(fh, names, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- levels
def test_cur_lines_match_exactly(tmp_path):
    pipe = _pipe(tmp_path)
    r = pipe.parse_file(_cur(tmp_path))
    rows = pipe.store.get_rows(r["sha256"])
    m5 = rows[rows["usage_type"].astype(str).str.contains("m5.xlarge") & rows["charge_type"].eq("Usage")]
    assert set(m5["price_match_level"]) == {"exact"}
    assert (m5["catalog_sku_id"] == LINUX).all()
    usage_only = rows[rows["usage_type"].eq("BoxUsage:m5.xlarge")]
    assert usage_only["price_match_level"].tolist() == ["exact"] and usage_only["sku_id"].isna().all()
    assert r["pricing"]["catalog_version"] == "2026-10-27"


def test_covered_usage_gets_a_list_price_and_keeps_its_coverage(tmp_path):
    pipe = _pipe(tmp_path)
    r = pipe.parse_file(_cur(tmp_path, list_cost=False))
    rows = pipe.store.get_rows(r["sha256"])
    covered = rows[rows["line_type_raw"].eq("SavingsPlanCoveredUsage")]
    assert len(covered) and (covered["coverage"] == "savings_plan").all()
    assert set(covered["price_match_level"]) == {"exact"}
    assert pd.to_numeric(covered["list_cost"]).gt(0).all()
    negation = rows[rows["line_type_raw"].eq("SavingsPlanNegation")]
    assert negation["price_match_level"].isna().all()                  # not usage: not priced


def test_invoice_lines(catalog):
    rows = _lines(description=["$0.192 per On Demand Linux m5.xlarge Instance Hour",
                               "$0.376 per On Demand Windows m5.xlarge Instance Hour",
                               "Amazon EC2 m5.xlarge Instance Hour"],
                  billed_cost=[1.92, 3.76, 1.92])
    extract(rows)
    match_prices(rows, catalog)
    linux, windows, no_os = (rows.loc[i] for i in range(3))
    assert linux["price_match_level"] == "assumed" and linux["catalog_sku_id"] == LINUX
    assert linux["price_assumptions"] == "tenancy=Shared (text); pre_installed_sw=NA (text)"
    assert float(linux["list_cost"]) == pytest.approx(1.92) and bool(linux["_list_cost_inferred"])
    assert windows["price_match_level"] == "ambiguous" and "license_model" in windows["price_missing"]
    assert pd.isna(windows["list_cost"])
    assert no_os["price_match_level"] in ("ambiguous", "unmatched") and "operating_system" in no_os["price_missing"]
    again = _lines(description=["Amazon EC2 m5.xlarge Instance Hour"])
    extract(again)
    stats = match_prices(again, catalog, PriceDefaults(operating_system="Linux"))
    assert again.loc[0, "price_match_level"] == "assumed" and again.loc[0, "catalog_sku_id"] == LINUX
    assert "operating_system=Linux (request)" in again.loc[0, "price_assumptions"]
    assert stats.summary()["defaults"] == {"operating_system": "Linux"}


def test_the_parser_columns_are_not_changed_by_assumptions(catalog):
    rows = _lines(description=["$0.192 per On Demand Linux m5.xlarge Instance Hour"])
    extract(rows)
    match_prices(rows, catalog, PriceDefaults(license_model="No License required"))
    parsed = rows.reindex(columns=["tenancy", "pre_installed_sw", "license_model"]).loc[0]
    assert parsed.isna().all()                                   # the assumptions are only in the lookup key


@pytest.mark.parametrize("name", ["azure_actual.csv", "focus_gcp.parquet", "gcp_export.json"])
def test_azure_and_gcp_lines_have_no_catalog(tmp_path, name):
    pipe = _pipe(tmp_path)
    r = pipe.parse_file(FILES / name)
    rows = pipe.store.get_rows(r["sha256"])
    usage = rows["charge_type"].isna() | rows["charge_type"].eq("Usage")
    assert set(rows.loc[usage, "price_match_level"].dropna()) == {"no_catalog"}
    assert set(r["pricing"]["levels"]) == {"no_catalog"} and r["pricing"]["match_rate"]["sure"] is None


# ---------------------------------------------------------------- units, dates
def test_units(catalog):
    rows = _lines(sku_id=[LINUX, S3, LINUX], service_code=["AmazonEC2", "AmazonS3", "AmazonEC2"],
                  quantity=[3600.0, 100.0, 5.0], unit=["seconds", "GB-Mo", "Requests"])
    match_prices(rows, catalog)
    seconds, storage, wrong = (rows.loc[i] for i in range(3))
    assert seconds["price_match_level"] == "exact" and float(seconds["list_cost"]) == pytest.approx(0.192)
    assert float(seconds["list_unit_price"]) == pytest.approx(0.192 / 3600)
    assert storage["price_match_level"] == "exact" and float(storage["list_cost"]) == pytest.approx(2.3)
    assert "tiered" in storage["price_assumptions"]
    assert wrong["price_match_level"] == "unit_mismatch" and wrong["price_missing"] == "unit"
    assert pd.isna(wrong["list_cost"])


def test_a_line_without_unit_takes_the_price_unit_only_when_exact(catalog):
    rows = _lines(sku_id=[LINUX, None], description=[None, "$0.192 per On Demand Linux m5.xlarge Instance Hour"],
                  unit=[None, None])
    extract(rows)
    match_prices(rows, catalog)
    assert rows.loc[0, "price_match_level"] == "exact"
    assert rows.loc[1, "price_match_level"] == "unit_mismatch"


def test_dates_and_history(tmp_path):
    engine = engine_for(f"sqlite:///{tmp_path / 'prices.db'}")
    load_snapshot(engine, FIXTURE)
    download = tmp_path / "download"
    shutil.copytree(FIXTURE, download)

    def raise_linux(r: dict) -> dict:
        if r["RateCode"] == LINUX_RATE:
            r["PricePerUnit"], r["EffectiveDate"] = "0.2000000000", "2026-11-01T00:00:00Z"
        return r
    _edit(download, "AmazonEC2", raise_linux)
    load_snapshot(engine, download, snapshot_id="2026-11-03")
    rows = _lines(sku_id=[LINUX] * 3, charge_start=["2020-01-15", "2026-10-31", "2026-11-02"])
    match_prices(rows, PriceCatalog(engine))
    assert [float(v) for v in rows["list_unit_price"]] == pytest.approx([0.192, 0.192, 0.2])
    assert "before_history" in rows.loc[0, "price_assumptions"]
    assert pd.isna(rows.loc[2, "price_assumptions"]) or "before_history" not in rows.loc[2, "price_assumptions"]


def test_a_line_without_date_takes_the_billing_period(catalog):
    rows = _lines(sku_id=[LINUX], charge_start=[None])
    stats = match_prices(rows, catalog, period_start="2026-10-01")
    assert rows.loc[0, "price_match_level"] == "exact" and not stats.warnings
    rows = _lines(sku_id=[LINUX], charge_start=[None])
    stats = match_prices(rows, catalog)
    assert any("as of today" in w for w in stats.warnings)


# ---------------------------------------------------------------- rules
def test_r1_a_printed_list_price_is_never_overwritten(catalog):
    rows = _lines(sku_id=[LINUX], list_unit_price=[0.5], list_cost=[5.0])
    match_prices(rows, catalog)
    assert (float(rows.loc[0, "list_unit_price"]), float(rows.loc[0, "list_cost"])) == (0.5, 5.0)
    assert not bool(rows.loc[0, "_list_cost_inferred"]) and rows.loc[0, "price_match_level"] == "exact"


def _priced_lines(ratios) -> pd.DataFrame:
    skus = [(LINUX, "AmazonEC2", "Hrs", 0.192), (RDS, "AmazonRDS", "Hrs", 0.171), (S3, "AmazonS3", "GB-Mo", 0.023)]
    out = []
    for i, r in enumerate(ratios):
        sku, code, unit, price = skus[i % 3]
        q = 10.0 + i
        out.append({"sku_id": sku, "service_code": code, "unit": unit, "quantity": q, "billed_cost": q * price * r,
                    "coverage": "on_demand"})
    return _lines(**{k: [o[k] for o in out] for k in out[0]})


def test_r2_a_steady_discount_is_inferred(catalog):
    rng = np.random.default_rng(1)
    rows = _priced_lines(0.9 * (1 + rng.uniform(-0.002, 0.002, 30)))
    stats = match_prices(rows, catalog)
    discount = infer_discount(stats)
    assert discount == pytest.approx(0.10, abs=0.003)
    from billparse.pricing import apply_discount
    assert apply_discount(rows, discount) == 30
    assert rows["_contracted_cost_inferred"].all()
    assert np.allclose(rows["contracted_cost"].astype(float), rows["list_cost"].astype(float) * (1 - discount))
    assert stats.summary(discount)["inferred_discount"]["estimate"] is True


def test_r2_random_ratios_infer_nothing(catalog):
    rows = _priced_lines(np.random.default_rng(2).uniform(0.5, 0.99, 30))
    assert infer_discount(match_prices(rows, catalog)) is None


def test_r2_needs_three_services(catalog):
    rows = _priced_lines([0.9] * 30)
    rows = rows[rows["service_code"].ne("AmazonS3")].reset_index(drop=True)
    assert infer_discount(match_prices(rows, catalog)) is None


def test_r3_far_below_list_price_is_one_warning_and_coverage_stays(catalog):
    rows = _lines(sku_id=[LINUX] * 3, billed_cost=[0.0, 0.1, 1.92], coverage=["unknown"] * 3)
    stats = match_prices(rows, catalog)
    warnings = stats.summary()["warnings"]
    assert any(w.startswith("2 lines cost far below list price") for w in warnings)
    assert rows["coverage"].tolist() == ["unknown"] * 3
    assert "effective_cost" not in rows


def test_no_catalog_parses_with_a_warning(tmp_path):
    pipe = _pipe(tmp_path, prices=False)
    r = pipe.parse_file(_cur(tmp_path))
    assert r["status"] in ("reconciled", "unreconciled")
    assert any("No price catalog" in w for w in r["pricing"]["warnings"])
    assert not r["pricing"]["levels"]


def test_a_catalog_error_never_fails_the_parse(tmp_path, monkeypatch):
    pipe = _pipe(tmp_path)

    def broken(self, keys):
        raise RuntimeError("database gone")
    monkeypatch.setattr(PriceCatalog, "lookup", broken)
    r = pipe.parse_file(_cur(tmp_path))
    assert r["status"] in ("reconciled", "unreconciled")
    assert any("Price matching failed" in w for w in r["pricing"]["warnings"])
    rows = pipe.store.get_rows(r["sha256"])
    assert rows["price_match_level"].isna().all()


# ---------------------------------------------------------------- the same everywhere
def _run(tmp_path: Path, monkeypatch, mode: str, list_cost: bool = False) -> tuple[dict, Decimal, Pipeline]:
    pipe = _pipe(tmp_path, mode)
    with monkeypatch.context() as m:
        if mode == "large":
            m.setenv("LARGE_FILE_MB", TINY)
            m.setenv("CHUNK_ROWS", "3")
        if mode == "rolled":
            m.setattr(contract_mod, "MAX_CHARGES", 2)
        r = pipe.parse_file(_cur(tmp_path, list_cost=list_cost))
    return r, _list_total(_doc(pipe, r["sha256"])), pipe


def test_the_same_on_the_normal_path_the_large_path_and_after_roll_ups(tmp_path, monkeypatch):
    normal, total, _ = _run(tmp_path, monkeypatch, "normal")
    large, large_total, _ = _run(tmp_path, monkeypatch, "large")
    rolled, rolled_total, _ = _run(tmp_path, monkeypatch, "rolled")
    assert large.get("large") and not normal.get("large")
    assert normal["pricing"] == large["pricing"] == rolled["pricing"]
    assert total > 0 and total == large_total == rolled_total
    assert normal["pricing"]["levels"]["exact"]["lines"] > 0


# ---------------------------------------------------------------- reprice
@pytest.mark.parametrize("mode", ["normal", "large"])
def test_reprice(tmp_path, monkeypatch, mode):
    r, before, pipe = _run(tmp_path, monkeypatch, mode)
    with pipe.store.engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE price_rate SET unit_price = unit_price * 2 WHERE sku = '{LINUX}'")
    rows_before = len(pipe.store.get_rows(r["sha256"]))
    again = pipe.reprice(r["sha256"])
    after = _list_total(_doc(pipe, r["sha256"]))
    assert after > before
    assert again["spec"] == r["spec"] and again["rows"] == r["rows"] and again["status"] == r["status"]
    assert len(pipe.store.get_rows(r["sha256"])) == rows_before
    assert again["pricing"]["levels"] == r["pricing"]["levels"]
    third = pipe.reprice(r["sha256"])                        # the same catalog: the same list cost
    assert _list_total(_doc(pipe, r["sha256"])) == after and third["pricing"] == again["pricing"]


def test_reprice_with_new_defaults(tmp_path):
    pipe = _pipe(tmp_path)
    r = pipe.parse_file(_cur(tmp_path))
    again = pipe.reprice(r["sha256"], PriceDefaults(tenancy="Shared"))
    assert again["pricing"]["defaults"] == {"tenancy": "Shared"}
    assert pipe.reprice(r["sha256"])["pricing"]["defaults"] == {"tenancy": "Shared"}     # kept


# ---------------------------------------------------------------- the contract and the API
def test_the_contract_carries_the_price_fields(tmp_path):
    pipe = _pipe(tmp_path)
    r = pipe.parse_file(_cur(tmp_path, list_cost=False))
    doc = _doc(pipe, r["sha256"])
    out = to_schema(doc)
    assert out.schema_version == "1.8" and out.pricing is not None
    priced = [c for c in doc.charges if c.price_match_level]
    assert priced and all(c.price_match_level.origin == "inferred" and c.price_match_level.source == "price_catalog"
                          for c in priced)
    filled = [c for c in priced if c.list_cost and c.list_cost.source == "price_catalog"]
    assert filled and all(c.list_cost.origin == "inferred" for c in filled)
    assert "list" not in doc.cost_types                     # the catalog's list cost is not the file's


@pytest.mark.parametrize("name", sorted(p.name for p in FILES.iterdir() if p.is_file()))
def test_every_sample_validates_and_pricing_changes_no_parse(tmp_path, name):
    """The contract validates with prices, and the route, status, rows and billed total are those of a parse without
    a catalog."""
    priced = _pipe(tmp_path, "priced")
    r = priced.parse_file(FILES / name)
    if priced.store.part(r["sha256"], "doc"):
        to_schema(_doc(priced, r["sha256"]))
    plain = _pipe(tmp_path, "plain", prices=False).parse_file(FILES / name)
    assert (r["status"], r["spec"], r["rows"]) == (plain["status"], plain["spec"], plain["rows"])
    assert (r.get("analysis") or {}).get("total_billed") == (plain.get("analysis") or {}).get("total_billed")


def test_post_parse_returns_the_price_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLPARSE_USE_LLM", "0")
    pipe = _pipe(tmp_path)
    webapi._pipeline.cache_clear()
    api.app.dependency_overrides[webapi.get_store] = lambda: pipe.store
    api.app.dependency_overrides[webapi.get_pipeline] = lambda: pipe
    try:
        client = TestClient(api.app)
        with open(_cur(tmp_path, list_cost=False), "rb") as fh:
            res = client.post("/parse?assume_os=Linux", files={"file": ("cur.csv.gz", fh)})
        assert res.status_code == 200, res.text
        doc = res.json()["document"]
        assert doc["schema_version"] == "1.8" and doc["pricing"]["defaults"] == {"operating_system": "Linux"}
        assert any(c.get("price_match_level") for c in doc["charges"])
        sha = res.json()["sha256"]
        task = client.post(f"/documents/{sha}/reprice", json={"tenancy": "Shared"}).json()
        deadline = time.time() + 30
        while task["state"] == "running" and time.time() < deadline:
            time.sleep(0.05)
            task = client.get(f"/parse/tasks/{sha}").json()
        assert task["state"] == "done", task
        view = client.get(f"/documents/{sha}/view").json()
        assert view["pricing"]["defaults"] == {"tenancy": "Shared"}
        assert any(c["price_match_level"] for c in view["charges"])
    finally:
        api.app.dependency_overrides.clear()
        webapi._pipeline.cache_clear()


# ---------------------------------------------------------------- performance
def test_500k_lines_with_2000_keys(catalog, capsys):
    n, k = 500_000, 2_000
    rng = np.random.default_rng(3)
    pick = rng.integers(0, k, n)
    rows = pd.DataFrame({
        "charge_type": "Usage", "provider": "aws", "region": "us-east-1", "service_code": "AmazonEC2",
        "usage_type": np.where(pick < 10, "BoxUsage:m5.xlarge", "BoxUsage:x" + pick.astype(str)),
        "operation": "RunInstances", "quantity": 1.0, "unit": "Hrs", "billed_cost": 0.192,
        "charge_start": np.where(rng.integers(0, 2, n) == 0, "2026-10-01", "2026-10-30")})
    cache = KeyCache()
    t = time.perf_counter()
    stats = match_prices(rows, catalog, cache=cache)
    seconds = time.perf_counter() - t
    with capsys.disabled():
        print(f"\n  priced {n:,} lines with {k:,} distinct keys in {seconds:.2f} s ({stats.lookups:,} lookups)")
    assert seconds < 10
    assert stats.levels["exact"][0] > 0 and json.dumps(stats.summary())


# ---------------------------------------------------------------- region default, undated lines
def test_assume_region_prices_lines_without_a_region(catalog):
    rows = _lines(description=["$0.192 per On Demand Linux m5.xlarge Instance Hour"], region=[None])
    extract(rows)
    match_prices(rows, catalog)
    assert rows.loc[0, "price_match_level"] == "unmatched" and rows.loc[0, "price_missing"] == "region"
    rows = _lines(description=["$0.192 per On Demand Linux m5.xlarge Instance Hour"], region=[None])
    extract(rows)
    match_prices(rows, catalog, PriceDefaults(region="us-east-1"))
    assert rows.loc[0, "price_match_level"] == "assumed" and rows.loc[0, "catalog_sku_id"] == LINUX
    assert "region=us-east-1 (request)" in rows.loc[0, "price_assumptions"]
    assert pd.isna(rows.loc[0, "region"])                        # the line's own column stays as parsed


class _Day:
    """pricing's datetime module with a fixed today."""

    def __init__(self, day: str):
        import datetime as real
        self.date = type("date", (), {"today": staticmethod(lambda: real.date.fromisoformat(day))})


def test_a_reprice_prices_undated_lines_as_of_the_first_date(tmp_path, monkeypatch):
    import billparse.pricing as pricing_mod
    pipe = _pipe(tmp_path)
    monkeypatch.setattr(pricing_mod, "dt", _Day("2026-10-27"))
    r = pipe.parse_file(FILES / "reseller_invoice.pdf")
    assert r["pricing"]["undated_as_of"] == "2026-10-27"
    monkeypatch.setattr(pricing_mod, "dt", _Day("2027-03-01"))
    again = pipe.reprice(r["sha256"])
    assert again["pricing"]["undated_as_of"] == "2026-10-27"
    assert any("the date of the first pricing" in w for w in again["pricing"]["warnings"])
