"""GCP public list prices (task 12): the Cloud Billing Catalog download loaded into the price tables with history and
the sanity check, and GCP lines matched by SKU ID. The fixture is hand-written in the API's JSON format
(tests/fixtures/prices/gcp/make_fixture.py); no API call."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa

from billparse import jobs, worker
from billparse.db import engine_for
from billparse.pricing import match_prices
from prices.load import Refused, load_snapshot
from prices.store import PriceCatalog

ROOT = Path(__file__).resolve().parents[1]
AWS = ROOT / "tests" / "fixtures" / "prices" / "download"
GCP = ROOT / "tests" / "fixtures" / "prices" / "gcp" / "download"
sys.path.insert(0, str(GCP.parent))
import make_fixture as fx  # noqa: E402
from test_jobs import _drain, store  # noqa: E402, F401  (the job tests' store fixture)
from test_pricing import _pipe  # noqa: E402

N2 = "BB77-5FDA-4F5A"                         # the samples' SKU IDs: $0.031611 and $0.021811 per hour
E2 = "D973-5D65-BAB2"
SPOT, COMMIT, RAM = "2D3A-6B1E-9F0C", "4C1E-7A2B-0F11", "F449-33EC-A5EF"
STORAGE, EGRESS = "E5F0-6A5D-7BAD", "9DE9-9092-B3BC"
COMPUTE = "6F81-5844-456A"


@pytest.fixture
def engine(tmp_path):
    return engine_for(f"sqlite:///{tmp_path / 'prices.db'}")


@pytest.fixture
def catalog(engine):
    load_snapshot(engine, AWS)
    load_snapshot(engine, GCP)
    return PriceCatalog(engine)


def _download(tmp_path: Path, change=None, drop: set[str] | None = None, services: set[str] | None = None,
              fetched_at: str = "2026-11-03T09:00:00+00:00") -> Path:
    """An edited copy of the fixture: change(sku) edits a SKU in place, drop removes SKUs, services keeps those."""
    skus = copy.deepcopy(fx.SKUS)
    out = {}
    for service, rows in skus.items():
        if services is not None and service not in services:
            continue
        rows = [r for r in rows if r["skuId"] not in (drop or set())]
        for r in rows:
            if change:
                change(r)
        out[service] = rows
    dest = tmp_path / f"gcp-{len(list(tmp_path.glob('gcp-*')))}"
    fx.write(dest, out, fetched_at)
    return dest


def _set_price(sku_id: str, price: float, when: str):
    def change(r: dict) -> None:
        if r["skuId"] == sku_id:
            r["pricingInfo"][0]["effectiveTime"] = when
            r["pricingInfo"][0]["pricingExpression"]["tieredRates"][0]["unitPrice"] = fx.money(price)
    return change


def _keys(**cols) -> pd.DataFrame:
    k = pd.DataFrame(cols)
    k["key_id"] = range(len(k))
    k["provider"] = "gcp"
    k["term_type"] = "any"
    return k


def _lines(**cols) -> pd.DataFrame:
    """GCP usage lines, as a parse of a billing export leaves them."""
    n = max(len(v) for v in cols.values() if isinstance(v, list))
    base = {"charge_type": "Usage", "provider": "gcp", "charge_start": "2026-10-27", "region": "us-east1",
            "service_code": COMPUTE, "quantity": 24.0, "unit": "hour", "billed_cost": 1.0}
    return pd.DataFrame({**{k: [v] * n for k, v in base.items()}, **cols})


# ---------------------------------------------------------------- the load
def test_load_counts_rows_and_attributes(engine):
    res = load_snapshot(engine, GCP)
    assert res.status == "current" and res.snapshot_id == "2026-10-27"
    assert res.products == 9 and res.rates == 13
    assert res.counts == {"gcp:6F81-5844-456A": 5, "gcp:95FF-2EF5-5EA1": 1, "gcp:E505-1604-58F8": 4,
                          "gcp:24E6-581D-38E5": 2, "gcp:9662-B51E-5089": 1}
    with engine.connect() as conn:
        p = conn.execute(sa.text("SELECT * FROM price_product WHERE sku = :s"), {"s": N2}).mappings().one()
        r = conn.execute(sa.text("SELECT * FROM price_rate WHERE sku = :s"), {"s": COMMIT}).mappings().one()
        snap = conn.execute(sa.text("SELECT source FROM price_snapshot")).scalar_one()
    attrs = json.loads(p["attributes"]) if isinstance(p["attributes"], str) else p["attributes"]
    assert (p["provider"], p["service_code"], p["product_family"], p["usage_type"]) == ("gcp", COMPUTE, "Compute",
                                                                                       "OnDemand")
    assert p["region"] is None and attrs["regions"] == fx.AMERICAS        # several regions: in the attributes
    assert attrs["usage_unit"] == "h" and attrs["base_unit_conversion_factor"] == 3600
    assert (r["term_type"], r["lease_length"], r["unit"], r["effective_from"]) == ("committed_use", "1yr", "hour",
                                                                                   "2026-10-01")
    assert float(r["unit_price"]) == pytest.approx(0.019915) and snap == "gcp-billing-catalog"


def test_aws_and_gcp_share_the_snapshot_and_leave_each_other_alone(engine, tmp_path):
    load_snapshot(engine, AWS)
    load_snapshot(engine, GCP)                               # the same day: added to the AWS snapshot
    with engine.connect() as conn:
        snap = conn.execute(sa.text("SELECT id, status, source, counts FROM price_snapshot")).mappings().all()
    assert len(snap) == 1 and snap[0]["source"] == "aws-price-files, gcp-billing-catalog"
    with pytest.raises(Refused, match="already loaded"):
        load_snapshot(engine, GCP)                           # a second full GCP load that day
    # next week's full AWS load: the GCP price lists are not missing, and stay open
    aws = tmp_path / "aws"
    shutil.copytree(AWS, aws)
    res = load_snapshot(engine, aws, snapshot_id="2026-11-03")
    assert res.status == "current" and (res.closed, res.inserted) == (0, 0)
    with engine.connect() as conn:
        counts = conn.execute(sa.text("SELECT counts FROM price_snapshot WHERE status = 'current'")).scalar_one()
        open_gcp = conn.execute(sa.text("SELECT count(*) FROM price_rate WHERE provider = 'gcp' "
                                        "AND effective_to IS NULL")).scalar_one()
    counts = json.loads(counts) if isinstance(counts, str) else counts
    assert counts["gcp:6F81-5844-456A"] == 5 and counts["AmazonEC2"] > 0 and open_gcp == 13
    # and that week's GCP load adds to it
    assert load_snapshot(engine, _download(tmp_path)).status == "current"


def test_a_second_load_of_the_same_skus_writes_nothing(engine, tmp_path):
    load_snapshot(engine, GCP)
    res = load_snapshot(engine, _download(tmp_path))
    assert (res.closed, res.inserted) == (0, 0)


def test_history_closes_a_changed_price(catalog, engine, tmp_path):
    res = load_snapshot(engine, _download(tmp_path, _set_price(N2, 0.0333, "2026-11-01T08:00:00Z")))
    assert (res.closed, res.inserted) == (1, 1)
    found = catalog.lookup(_keys(as_of=["2026-10-15", "2026-11-02", "2026-09-01"], sku=[N2] * 3))
    got = found.set_index("key_id")["unit_price"].to_dict()
    assert got[0] == pytest.approx(0.031611) and got[1] == pytest.approx(0.0333)
    assert got[2] == pytest.approx(0.031611)                 # before the history: the oldest price, said so
    assert found.set_index("key_id").loc[2, "before_history"]


def test_a_load_that_loses_most_of_a_service_is_refused(catalog, engine, tmp_path):
    with pytest.raises(Refused, match="gcp:6F81-5844-456A"):
        load_snapshot(engine, _download(tmp_path, drop={N2, E2, SPOT, COMMIT}))
    with pytest.raises(Refused, match="gcp:E505-1604-58F8 is missing"):
        load_snapshot(engine, _download(tmp_path, services={COMPUTE, fx.STORAGE}))
    assert catalog.version == "2026-10-27"
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM price_rate WHERE effective_to IS NOT NULL")).scalar_one() == 0


# ---------------------------------------------------------------- lookup
def test_sku_lookup_takes_the_skus_own_price_whatever_its_term(catalog):
    found = catalog.lookup(_keys(as_of=["2026-10-27"] * 4, sku=[N2, SPOT, COMMIT, "NO-SUCH-SKU"])).set_index("key_id")
    assert list(found.index) == [0, 1, 2]
    assert found["unit_price"].tolist() == pytest.approx([0.031611, 0.00765, 0.019915])
    assert found["term_type"].tolist() == ["on_demand", "spot", "committed_use"]
    assert set(found["matched_by"]) == {"sku"} and set(found["candidates"]) == {1}


def test_tiered_rates_take_the_first_paid_tier(catalog):
    row = catalog.lookup(_keys(as_of=["2026-10-27"], sku=[EGRESS])).iloc[0]
    assert row["unit_price"] == pytest.approx(0.12) and row["tiered"] and row["unit"] == "gb"
    assert row["rate_code"] == f"{EGRESS}.t1"


def test_an_aws_key_never_reaches_a_gcp_price(catalog):
    k = _keys(as_of=["2026-10-27"], sku=[N2]).assign(provider="aws", term_type=None)
    assert catalog.lookup(k).empty


# ---------------------------------------------------------------- matching
def test_gcp_lines_match_their_sku_exactly(catalog):
    rows = _lines(sku_id=[N2, E2, SPOT, COMMIT], quantity=[24.0, 24.0, 10.0, 24.0])
    stats = match_prices(rows, catalog)
    assert rows["price_match_level"].tolist() == ["exact"] * 4
    assert rows["catalog_sku_id"].tolist() == [N2, E2, SPOT, COMMIT]
    assert pd.to_numeric(rows["list_cost"]).tolist() == pytest.approx([24 * 0.031611, 24 * 0.021811, 10 * 0.00765,
                                                                       24 * 0.019915])
    assert stats.levels["exact"][0] == 4


def test_committed_use_covered_lines_get_the_on_demand_list_price(catalog):
    rows = _lines(sku_id=[N2], coverage=["committed_use"], effective_cost=[0.4])
    match_prices(rows, catalog)
    assert rows.loc[0, "price_match_level"] == "exact" and rows.loc[0, "coverage"] == "committed_use"
    assert float(rows.loc[0, "list_cost"]) == pytest.approx(24 * 0.031611)
    assert float(rows.loc[0, "price_ratio"]) == pytest.approx(0.4 / 24 / 0.031611)


def test_units_are_converted_to_the_prices(catalog):
    rows = _lines(sku_id=[N2, RAM, STORAGE, EGRESS, N2],
                  unit=["seconds", "byte-seconds", "gibibyte month", "gibibyte", "gibibyte month"],
                  quantity=[86400.0, 16 * 2**30 * 3600 * 24, 100.0, 50.0, 1.0])
    match_prices(rows, catalog)
    assert rows["price_match_level"].tolist() == ["exact"] * 4 + ["unit_mismatch"]
    lst = pd.to_numeric(rows["list_cost"])
    assert lst[0] == pytest.approx(24 * 0.031611)                        # seconds -> hours
    assert lst[1] == pytest.approx(16 * 24 * 0.004237)                   # byte-seconds -> gibibyte hours (base unit)
    assert lst[2] == pytest.approx(100 * 0.026) and lst[3] == pytest.approx(50 * 0.12)
    assert rows.loc[3, "price_assumptions"] == "tiered" and rows.loc[4, "price_missing"] == "unit"


def test_what_a_gcp_line_lacks(catalog):
    rows = _lines(sku_id=[None, "AAAA-BBBB", "AAAA-BBBB"], service_code=[COMPUTE, COMPUTE, "152E-C115-5142"],
                  instance_type=["n2-standard-4", None, None])
    match_prices(rows, catalog)
    assert rows["price_match_level"].tolist() == ["unmatched"] * 3
    assert rows["price_missing"].tolist() == ["sku_id", "no_such_product", "service_not_loaded"]


def test_without_gcp_prices_gcp_lines_stay_no_catalog(engine):
    load_snapshot(engine, AWS)
    rows = _lines(sku_id=[N2])
    match_prices(rows, PriceCatalog(engine))
    assert rows.loc[0, "price_match_level"] == "no_catalog"


def test_the_gcp_export_sample_matches(tmp_path):
    pipe = _pipe(tmp_path)
    load_snapshot(pipe.store.engine, GCP)
    r = pipe.parse_file(ROOT / "samples" / "files" / "gcp_export_full.json")
    rows = pipe.store.get_rows(r["sha256"])
    usage = rows[rows["charge_type"].eq("Usage")]
    assert len(usage) == 6 and set(usage["price_match_level"]) == {"exact"}
    assert set(usage["catalog_sku_id"]) == {N2, E2} and pd.to_numeric(usage["list_cost"]).gt(0).all()
    assert r["pricing"]["levels"] == {"exact": {"lines": 6, "cost": 186.0}}
    checks = {c["name"]: c["status"] for c in r["checks"]}
    assert checks["catalog_grounding"] != "skip"                         # GCP lines are price-checked now


# ---------------------------------------------------------------- fetcher and the weekly job
def _fetcher():
    path = ROOT / "scripts" / "gcp" / "fetch_gcp_prices.py"
    spec = importlib.util.spec_from_file_location("fetch_gcp_prices", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_fetcher_needs_its_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GCP_BILLING_API_KEY", raising=False)
    assert _fetcher().main(["--out", str(tmp_path / "out")]) == 2
    assert "GCP_BILLING_API_KEY is not set" in capsys.readouterr().err and not (tmp_path / "out").exists()


def test_the_fetcher_pages_and_writes_a_loadable_download(tmp_path, monkeypatch, engine):
    """The API answered from the fixture, two SKUs per page: services, then SKUs per service, by nextPageToken."""
    mod = _fetcher()
    services = [{"name": f"services/{s}", "serviceId": s, "displayName": n, "businessEntityName": mod.GCP}
                for s, n in fx.SERVICES.items()]
    services.append({"name": "services/MAPS", "serviceId": "MAPS", "displayName": "Maps",
                     "businessEntityName": "businessEntities/Maps"})
    calls = []

    def get_json(path: str, params: dict, key: str, tries: int = 4) -> dict:
        calls.append((path, params.get("pageToken")))
        assert key == "test-key"
        items = services if path == "/services" else fx.SKUS[path.split("/")[2]]
        start = int(params.get("pageToken") or 0)
        nxt = start + 2 if start + 2 < len(items) else ""
        return {"services" if path == "/services" else "skus": items[start:start + 2], "nextPageToken": str(nxt)}
    monkeypatch.setattr(mod, "get_json", get_json)
    monkeypatch.setenv("GCP_BILLING_API_KEY", "test-key")
    out = tmp_path / "gcp-price-files"
    assert mod.main(["--out", str(out), "--workers", "1"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["provider"] == "gcp" and set(manifest["files"]) == set(fx.SERVICES)    # not Maps
    assert manifest["files"][COMPUTE]["skus"] == 5 and ("/services/6F81-5844-456A/skus", "4") in calls
    assert load_snapshot(engine, out).rates == 13


def test_the_price_job_skips_gcp_without_a_key(store, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("PRICE_PROVIDERS", "gcp")
    monkeypatch.delenv("GCP_BILLING_API_KEY", raising=False)
    job, _ = jobs.enqueue(store, "prices", "maintenance", name="prices", dedupe_key="prices:test")
    assert _drain(store) == ["done"]
    assert jobs.get(store, job["id"])["result"] == {"skipped": "GCP_BILLING_API_KEY is not set"}


def test_the_price_job_loads_aws_then_gcp(store, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("PRICE_PROVIDERS", "aws,gcp")
    monkeypatch.setenv("GCP_BILLING_API_KEY", "test-key")
    monkeypatch.setenv("PRICE_FILES_DIR", str(tmp_path / "aws-price-files"))
    monkeypatch.setenv("GCP_PRICE_FILES_DIR", str(tmp_path / "gcp-price-files"))
    monkeypatch.setattr(worker, "download_prices", lambda dest: shutil.copytree(AWS, dest) and 0)
    monkeypatch.setattr(worker, "download_gcp_prices", lambda dest: shutil.copytree(GCP, dest) and 0)
    job, _ = jobs.enqueue(store, "prices", "maintenance", name="prices", dedupe_key="prices:test")
    assert _drain(store) == ["done"]
    result = jobs.get(store, job["id"])["result"]
    assert result["status"] == "current" and result["providers"]["gcp"]["rates"] == 13
    assert PriceCatalog(store.engine).services("gcp") == set(fx.SERVICES)


# ---------------------------------------------------------------- currency
def _rebilled(tmp_path: Path, currency: str, rate: float | None) -> Path:
    """gcp_export_full.json billed at exactly the fixture's list price, in currency at rate (units per USD; None: the
    file prints no rate)."""
    price = {N2: 0.031611, E2: 0.021811}
    recs = json.loads((ROOT / "samples" / "files" / "gcp_export_full.json").read_text())
    for r in recs:
        r["cost"] = round(r["usage"]["amount_in_pricing_units"] * price[r["sku"]["id"]] * (rate or 1.0), 6)
        r["currency"] = currency
        if rate is not None:
            r["currency_conversion_rate"] = rate
    path = tmp_path / f"gcp_{currency}.json"
    path.write_text(json.dumps(recs))
    return path


@pytest.mark.parametrize("currency, rate", [("USD", 1.0), ("EUR", 0.92), ("JPY", 152.0)])
def test_a_bill_at_list_price_has_ratio_one_in_any_currency(tmp_path, currency, rate):
    pipe = _pipe(tmp_path)
    load_snapshot(pipe.store.engine, GCP)
    r = pipe.parse_file(_rebilled(tmp_path, currency, rate))
    rows = pipe.store.get_rows(r["sha256"])
    assert set(rows["price_match_level"]) == {"exact"} and set(rows["currency"]) == {currency}
    assert pd.to_numeric(rows["price_ratio"]).tolist() == pytest.approx([1.0] * 6, rel=1e-4)
    n2 = rows[rows["sku_id"].eq(N2)].iloc[0]
    assert float(n2["list_cost"]) == pytest.approx(24 * 0.031611 * rate)          # in the line's currency
    assert float(n2["catalog_unit_price"]) == pytest.approx(0.031611 * rate)
    note = n2["price_assumptions"] if isinstance(n2["price_assumptions"], str) else ""
    assert (f"currency={currency} at {rate:g} per USD (printed rate)" in note) == (currency != "USD")
    checks = {c["name"]: c["status"] for c in r["checks"]}
    assert checks["implied_unit_price"] == "pass" and checks["catalog_grounding"] == "pass"
    assert r["confidence"] >= 0.9


def test_a_line_in_another_currency_without_a_rate_is_not_priced(tmp_path):
    pipe = _pipe(tmp_path)
    load_snapshot(pipe.store.engine, GCP)
    r = pipe.parse_file(_rebilled(tmp_path, "EUR", None))
    rows = pipe.store.get_rows(r["sha256"])
    assert set(rows["price_match_level"]) == {"unmatched"} and set(rows["price_missing"]) == {"currency"}
    assert pd.to_numeric(rows["list_cost"]).isna().all() and rows["catalog_unit_price"].isna().all()
    checks = {c["name"]: c["status"] for c in r["checks"]}
    assert checks["implied_unit_price"] == "skip" and checks["catalog_grounding"] != "fail"
    assert [(m["field"], m["lines"]) for m in r["pricing"]["missing"]] == [("currency", 6)]


def test_aws_lines_follow_the_same_currency_rule(catalog):
    from test_pricing import LINUX
    rows = pd.DataFrame({"charge_type": "Usage", "provider": "aws", "charge_start": "2026-10-27",
                         "region": "us-east-1", "service_code": "AmazonEC2", "quantity": 10.0, "unit": "Hrs",
                         "billed_cost": 1.92, "sku_id": LINUX, "currency": ["USD", "EUR", "EUR", None],
                         "currency_conversion_rate": [np.nan, np.nan, 0.9, np.nan]})
    match_prices(rows, catalog)
    assert rows["price_match_level"].tolist() == ["exact", "unmatched", "exact", "exact"]
    assert rows.loc[1, "price_missing"] == "currency"
    lst = pd.to_numeric(rows["list_cost"])
    assert lst[0] == pytest.approx(1.92) and np.isnan(lst[1]) and lst[2] == pytest.approx(1.92 * 0.9)
    assert lst[3] == pytest.approx(1.92)                       # no currency printed: the price's


# ---------------------------------------------------------------- a SKU the catalog does not have
def test_a_missing_sku_lowers_confidence_and_keeps_the_builtin_reading(tmp_path):
    """A SKU retired before the first price download (an old bill) fails catalog_grounding: the built-in spec's
    reading stays (route, rows, tax line) with less confidence; a stored spec that reads the same file without the SKU
    column (so without the failure) does not replace it."""
    pipe = _pipe(tmp_path)
    load_snapshot(pipe.store.engine, GCP)
    recs = json.loads((ROOT / "samples" / "files" / "gcp_commitments.json").read_text())
    for r in recs:
        if r["sku"]["id"]:
            r["sku"]["id"] = "0000-1111-2222"                 # the format of a GCP SKU ID, in no catalog
    path = tmp_path / "gcp_commitments_old.json"
    path.write_text(json.dumps(recs))
    builtin = pipe.store.get_spec("gcp-billing-export")
    learned = {k: v for k, v in copy.deepcopy(builtin).items() if k not in ("version", "created_at")}
    learned.update(id="gcp-without-sku", trust="candidate")
    del learned["columns"]["sku_id"]
    pipe.store.save_spec(learned)
    r = pipe.parse_file(path)
    assert (r["spec"]["route"], r["spec"]["id"]) == ("builtin", "gcp-billing-export")
    rows = pipe.store.get_rows(r["sha256"])
    assert len(rows) == 10 and rows["charge_type"].eq("Tax").sum() == 1 and r["analysis"]["total_billed"] == 213.0
    assert set(rows.loc[rows["charge_type"].eq("Usage"), "price_missing"]) == {"no_such_product"}
    checks = {c["name"]: c for c in r["checks"]}
    assert checks["catalog_grounding"]["status"] == "fail" and r["confidence"] < 0.75
