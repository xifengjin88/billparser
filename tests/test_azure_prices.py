"""Task 11: Azure public prices. The Azure Retail Prices API download (scripts/azure/fetch_azure_retail_prices.py) is
loaded into the same price tables as AWS's, with the same snapshot, history and sanity rules, and Azure usage lines
are matched like AWS lines: exact by meter ID, else by meter name in their region (and the VM meter's OS).

The fixture is a small real download of eastus (tests/fixtures/prices/azure/make_fixture.py): the VM sizes the samples
use (Linux and Windows, Spot, Low Priority, DevTest, 1 and 3 year reservations), P10 disks, the Hot LRS block blob
meters (tiers, 10K operations), Bandwidth (tiers) and the AKS uptime SLA. No test uses the network.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_jobs import _drain  # noqa: E402
from test_jobs import store as store  # noqa: E402,F401  (the fixture)
from test_pricing import FILES, TINY, _lines  # noqa: E402
from test_pricing import FIXTURE as AWS_FIXTURE  # noqa: E402
from test_pricing import _pipe as _aws_pipe  # noqa: E402

from billparse import jobs, worker  # noqa: E402
from billparse.db import engine_for  # noqa: E402
from billparse.pricing import PriceDefaults, flagged, match_prices  # noqa: E402
from billparse.spec import Extraction  # noqa: E402
from billparse.validate import PRICE_CHECKS, validate  # noqa: E402
from prices.load import Refused, load_snapshot  # noqa: E402
from prices.store import PriceCatalog  # noqa: E402
from prices.units import azure_unit  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "prices" / "azure" / "download"
D4S_LINUX = "db8c0962-f3f8-5b44-99aa-f3c7c1e668c8"        # D4s v5, eastus: $0.192 per hour
D4S_WINDOWS = "57bde05c-423b-50c9-8e57-d668c0481cce"      # $0.376
D4S_SPOT = "ea1d84e1-3804-5a61-9c27-4a3eaede5031"         # Linux Spot: $0.040531
BLOB_STORED = "272492b3-1c92-4a5f-bee9-52a8e10ca514"      # Hot LRS Data Stored: $0.0208 per GB/Month, 3 tiers
BLOB_WRITES = "14854f12-8d37-4299-842a-b0f2df82a305"      # Hot LRS Write Operations: $0.05 per 10K
P10 = "93a6a529-4f49-47cb-9b1e-db9e5f23263f"              # P10 LRS Disk (managed disks): $19.71 per 1/Month


@pytest.fixture
def engine(tmp_path):
    return engine_for(f"sqlite:///{tmp_path / 'prices.db'}")


@pytest.fixture
def catalog(engine) -> PriceCatalog:
    load_snapshot(engine, AWS_FIXTURE)
    load_snapshot(engine, FIXTURE)
    return PriceCatalog(engine)


def _items(download: Path, service: str) -> tuple[Path, list[dict]]:
    path = download / json.loads((download / "manifest.json").read_text())["files"][service]["eastus"]["path"]
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return path, [json.loads(line) for line in fh]


def _edit(download: Path, service: str, change) -> None:
    """Rewrite one service's file in a copy of the fixture: change(item) -> item, or None to drop it."""
    path, items = _items(download, service)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for i in items:
            if (i := change(dict(i))) is not None:
                fh.write(json.dumps(i) + "\n")


def _copy(tmp_path: Path, fetched_at: str = "2026-11-03T00:00:00+00:00") -> Path:
    download = tmp_path / "azure-download"
    shutil.copytree(FIXTURE, download)
    manifest = json.loads((download / "manifest.json").read_text())
    manifest["fetched_at"] = fetched_at
    (download / "manifest.json").write_text(json.dumps(manifest))
    return download


def _keys(**cols) -> pd.DataFrame:
    k = pd.DataFrame(cols)
    k["key_id"] = range(len(k))
    k["provider"] = "azure"
    return k


def _azure(**cols) -> pd.DataFrame:
    """Azure usage lines as a parse leaves them."""
    base = {"provider": "azure", "service_raw": "Virtual Machines", "region": "eastus", "unit": "1 Hour",
            "quantity": 10.0, "billed_cost": 1.0, "charge_start": "2026-10-27"}
    return _lines(**{**base, **cols}).drop(columns=["service_code"])


# ---------------------------------------------------------------- the load
def test_load_rows_and_one_snapshot_with_aws(engine):
    aws = load_snapshot(engine, AWS_FIXTURE)
    res = load_snapshot(engine, FIXTURE)                    # a full Azure load on the AWS snapshot's day: added to it
    assert res.status == "current" and res.snapshot_id == aws.snapshot_id == "2026-10-27"
    assert res.counts == {"azure:Virtual Machines": 36, "azure:Storage": 12, "azure:Bandwidth": 23,
                          "azure:Azure Kubernetes Service": 1}
    assert (res.products, res.rates) == (51, 72)
    with engine.connect() as conn:
        snaps = conn.exec_driver_sql("SELECT id, status, counts FROM price_snapshot").all()
        assert len(snaps) == 1 and {"AmazonEC2", "azure:Storage"} <= set(json.loads(snaps[0][2]))
        prod = dict(conn.exec_driver_sql(
            "SELECT usage_type, instance_type || '|' || operating_system || '|' || meter_name || '|' || product_name "
            "FROM price_product WHERE provider = 'azure' AND usage_type IN (?, ?)", (D4S_LINUX, D4S_WINDOWS)).all())
        rates = conn.exec_driver_sql(
            "SELECT term_type, lease_length, unit, unit_raw, unit_price, price_raw FROM price_rate "
            "WHERE provider = 'azure' AND rate_code LIKE ? ORDER BY term_type, lease_length", (D4S_LINUX + "%",)).all()
    assert prod[D4S_LINUX] == "Standard_D4s_v5|Linux|D4s v5|Virtual Machines Dsv5 Series"
    assert prod[D4S_WINDOWS] == "Standard_D4s_v5|Windows|D4s v5|Virtual Machines Dsv5 Series Windows"
    # a reservation's price is the whole term's: stored per hour, the term's price kept as printed
    (od, *reserved) = rates
    assert od[:4] == ("on_demand", None, "hour", "1 Hour") and float(od[4]) == pytest.approx(0.192)
    assert [(r[1], r[3], float(r[5])) for r in reserved] == [("1yr", "1 Year", 1038.0), ("3yr", "3 Years", 1993.0)]
    assert float(reserved[0][4]) == pytest.approx(1038 / 8760) and float(reserved[1][4]) == pytest.approx(1993 / 26280)


def test_units_tiers_and_dev_test(engine):
    load_snapshot(engine, FIXTURE)
    with engine.connect() as conn:
        def rows(meter):
            return conn.exec_driver_sql(
                "SELECT term_type, unit, unit_raw, unit_price, begin_range, end_range, effective_from FROM price_rate "
                "WHERE rate_code LIKE ? ORDER BY term_type, begin_range", (meter + "%",)).all()
        stored, writes, disk, windows = rows(BLOB_STORED), rows(BLOB_WRITES), rows(P10), rows(D4S_WINDOWS)
    assert [(r[1], float(r[4]), r[5] and float(r[5])) for r in stored] == \
        [("gb_month", 0, 51200.0), ("gb_month", 51200, 512000.0), ("gb_month", 512000, None)]
    assert writes[0][1:3] == ("request", "10K") and float(writes[0][3]) == pytest.approx(0.05 / 10_000)
    assert disk[0][1:3] == ("count_month", "1/Month") and float(disk[0][3]) == pytest.approx(19.71)
    assert [r[0] for r in windows] == ["dev_test", "on_demand"]
    assert windows[1][6] == "2021-11-01"                     # effectiveStartDate


@pytest.mark.parametrize("unit, expect", [("1 Hour", ("hour", 1)), ("10 Hours", ("hour", 10)), ("10K", ("request", 1e4)),
                                          ("1 GB/Month", ("gb_month", 1)), ("1/Month", ("count_month", 1)),
                                          ("10K/Month", ("count_month", 1e4)), ("100/Month", ("count_month", 100)),
                                          ("1/Day", ("count_day", 1)), ("10000", ("count", 1e4)), ("odd", None)])
def test_azure_units(unit, expect):
    assert azure_unit(unit) == expect


def test_a_second_load_writes_nothing_and_a_change_keeps_history(engine, tmp_path):
    load_snapshot(engine, FIXTURE)
    same = load_snapshot(engine, _copy(tmp_path / "same", "2026-11-01T00:00:00+00:00"))
    assert (same.closed, same.inserted) == (0, 0)
    download = _copy(tmp_path)

    def raise_d4s(i: dict) -> dict:
        if i["meterId"] == D4S_LINUX and i["type"] == "Consumption":
            i["retailPrice"] = i["unitPrice"] = 0.2
            i["effectiveStartDate"] = "2026-11-02T00:00:00Z"
        return i
    _edit(download, "Virtual Machines", raise_d4s)
    res = load_snapshot(engine, download)
    assert (res.closed, res.inserted) == (1, 1)
    found = PriceCatalog(engine).lookup(_keys(as_of=["2026-10-31", "2026-11-02"], usage_type=[D4S_LINUX] * 2))
    got = found.set_index("key_id")["unit_price"].to_dict()
    assert got[0] == pytest.approx(0.192) and got[1] == pytest.approx(0.2)


def test_a_big_drop_is_refused_and_the_current_snapshot_stays(engine, tmp_path):
    load_snapshot(engine, FIXTURE)
    download = _copy(tmp_path)
    _edit(download, "Virtual Machines", lambda i: None if i["armSkuName"] == "Standard_D4s_v5" else i)   # 11 of 36
    with pytest.raises(Refused, match="azure:Virtual Machines"):
        load_snapshot(engine, download)
    assert PriceCatalog(engine).version == "2026-10-27"
    with engine.connect() as conn:
        snaps = dict(conn.exec_driver_sql("SELECT id, status FROM price_snapshot").all())
    assert snaps == {"2026-10-27": "current", "2026-11-03": "refused"}


def test_each_provider_checks_only_its_own_price_lists(engine, tmp_path):
    load_snapshot(engine, FIXTURE)
    load_snapshot(engine, AWS_FIXTURE, snapshot_id="2026-11-01")     # Azure lists missing: not AWS's business
    download = _copy(tmp_path)
    manifest = json.loads((download / "manifest.json").read_text())
    del manifest["files"]["Bandwidth"]
    (download / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(Refused, match="azure:Bandwidth is missing"):
        load_snapshot(engine, download)
    load_snapshot(engine, download, partial=True)
    with engine.connect() as conn:
        counts = json.loads(conn.exec_driver_sql("SELECT counts FROM price_snapshot WHERE status = 'current'")
                            .scalar_one())
    assert {"AmazonEC2", "azure:Bandwidth", "azure:Virtual Machines"} <= set(counts)


# ---------------------------------------------------------------- the lookup
def test_exact_by_meter_id_in_any_region(catalog):
    found = catalog.lookup(_keys(as_of=["2026-10-27"] * 2, usage_type=[D4S_WINDOWS, D4S_LINUX],
                                 region=["westeurope", None]))
    got = found.set_index("key_id")
    assert list(got["matched_by"]) == ["meter_id", "meter_id"]
    assert got.loc[0, "unit_price"] == pytest.approx(0.376) and got.loc[0, "product_region"] == "eastus"
    assert got.loc[1, "unit_price"] == pytest.approx(0.192)


def test_attributes_by_meter_name_region_and_os(catalog):
    found = catalog.lookup(_keys(as_of=["2026-10-27"] * 4, meter_name=["D4s v5", "D4s v5", "D4s v5 Spot", "D4s v5"],
                                 region=["eastus"] * 3 + ["westus"], operating_system=["Linux", None, "Linux", "Linux"],
                                 service_code=["Virtual Machines"] * 4))
    got = found.set_index("key_id")
    assert got.loc[0, "matched_by"] == "attributes" and got.loc[0, "unit_price"] == pytest.approx(0.192)
    assert got.loc[1, "candidates"] == 2 and pd.isna(got.loc[1, "unit_price"]) and "operating_system" in got.loc[1, "differs"]
    assert got.loc[2, "unit_price"] == pytest.approx(0.040531)
    assert 3 not in got.index                                # no such meter in westus


def test_the_reserved_price_by_term(catalog):
    found = catalog.lookup(_keys(as_of=["2026-10-27"], usage_type=[D4S_LINUX], term_type=["reserved"],
                                 lease_length=["3yr"]))
    assert found.iloc[0]["unit_price"] == pytest.approx(1993 / 26280)


# ---------------------------------------------------------------- matching lines
def test_lines_exact_attributes_spot_byol_and_units(catalog):
    rows = _azure(meter_id=[D4S_WINDOWS, None, None, None, None, None],
                  meter_name=["D4s v5", "D4s v5", "D4s v5 Spot", "D4s v5", "D4s v5", None],
                  description=["D4s v5", "D4s v5", "D4s v5 Spot", "D4s v5", "D4s v5", "Hot LRS Write Operations"],
                  service_raw=["Virtual Machines"] * 5 + ["Storage"],
                  operating_system=["Windows", "Linux", "Linux", "Windows", "Windows", None],
                  license_model=[None, None, None, "Bring your own license", "License included", None],
                  unit=["1 Hour", "10 Hours", "1 Hour", "1 Hour", "1 Hour", "10K"],
                  coverage=["on_demand", "reserved", "spot", "on_demand", "on_demand", "on_demand"])
    stats = match_prices(rows, catalog)
    assert list(rows["price_match_level"]) == ["exact", "attributes", "attributes", "attributes", "attributes",
                                               "attributes"]
    price = rows["catalog_unit_price"].astype(float).tolist()
    assert price == pytest.approx([0.376, 1.92, 0.040531, 0.192, 0.376, 0.05])   # "10 Hours": x 10; BYOL: Linux
    assert rows["_list_unit_price_inferred"].all() and rows.loc[0, "list_cost"] == pytest.approx(3.76)
    assert stats.levels["exact"][0] == 1 and "no_catalog" not in stats.levels


def test_lines_ambiguous_unmatched_and_unit_mismatch(catalog):
    rows = _azure(meter_name=["D4s v5", "No Such Meter", "P10 LRS Disk", "vCore"],
                  service_raw=["Virtual Machines", "Virtual Machines", "Storage", "SQL Database"],
                  unit=["1 Hour", "1 Hour", "1 Hour", "1 Hour"])
    match_prices(rows, catalog)
    assert list(rows["price_match_level"]) == ["ambiguous", "unmatched", "unit_mismatch", "unmatched"]
    assert list(rows["price_missing"]) == ["operating_system", "catalog_row", "unit", "service_not_loaded"]
    again = _azure(meter_name=["D4s v5"])
    match_prices(again, catalog, PriceDefaults(operating_system="Linux"))
    assert again.loc[0, "price_match_level"] == "assumed"
    assert again.loc[0, "price_assumptions"] == "operating_system=Linux (request)"


def test_a_meter_id_with_another_region_is_a_conflict(catalog):
    rows = _azure(meter_id=[D4S_LINUX], region=["westeurope"], instance_type=["standard_d4s_v5"])
    match_prices(rows, catalog)
    assert rows.loc[0, "price_match_level"] == "exact"
    assert rows.loc[0, "price_conflict"] == "region westeurope vs eastus"     # the size differs only in case


def test_azure_lines_stay_no_catalog_without_azure_prices(engine):
    load_snapshot(engine, AWS_FIXTURE)
    rows = _azure(meter_id=[D4S_LINUX])
    stats = match_prices(rows, PriceCatalog(engine))
    assert rows.loc[0, "price_match_level"] == "no_catalog" and set(stats.levels) == {"no_catalog"}


def test_azure_lines_feed_the_price_checks(catalog):
    rows = _azure(meter_id=[D4S_LINUX] * 6, billed_cost=[1.8] * 6, coverage="on_demand")   # paid 0.18 of 0.192
    rows["_src"] = [f"t:{i + 2}" for i in range(len(rows))]
    match_prices(rows, catalog)
    checks = {c["name"]: c for c in validate(Extraction(rows=rows))["checks"] if c["name"] in PRICE_CHECKS}
    assert checks["catalog_grounding"]["status"] == "pass" and checks["implied_unit_price"]["status"] == "pass"


def test_a_free_first_tier_is_not_the_price(catalog):
    """Bandwidth's first 100 GB a month are free: a meter's price is its first paid tier, noted tiered. By name, two
    routing products share the meter name with different paid prices: ambiguous (before, both free first tiers read
    as one price of 0)."""
    rows = _azure(meter_id=["9995d93a-7d35-4d3f-9c69-7a7fea447ef4", None], service_raw="Bandwidth", unit="1 GB",
                  description=["Standard Data Transfer Out"] * 2)
    match_prices(rows, catalog)
    by_id, by_name = rows.loc[0], rows.loc[1]
    assert by_id["price_match_level"] == "exact" and float(by_id["catalog_unit_price"]) == pytest.approx(0.087)
    assert "tiered" in by_id["price_assumptions"]
    assert by_name["price_match_level"] == "ambiguous" and by_name["price_missing"] == "product_name"


def test_azure_auto_defaults_region_from_the_file_and_os_from_the_setting(catalog, monkeypatch):
    """A VM line by meter name with no region and no OS: the file's own region and the default OS (Linux), noted and
    counted; a line with a meter ID needs no region (the meter is the price) and assumes none."""
    monkeypatch.setenv("PRICE_AUTO_DEFAULTS", "1")
    rows = _azure(meter_id=[None, None, D4S_LINUX], description=["D4s v5"] * 3, region=[None, "eastus", None])
    stats = match_prices(rows, catalog)
    first = rows.loc[0]
    assert first["price_match_level"] == "assumed" and float(first["catalog_unit_price"]) == pytest.approx(0.192)
    assert "region=eastus (file)" in first["price_assumptions"]
    assert "operating_system=Linux (default)" in first["price_assumptions"]
    assert rows.loc[2, "price_match_level"] == "exact" and not isinstance(rows.loc[2, "price_assumptions"], str)
    used = {(d["provider"], d["field"], d["source"]): d["lines"] for d in stats.summary()["defaults_used"]}
    assert used == {("azure", "region", "file"): 1, ("azure", "operating_system", "default"): 2}


# ---------------------------------------------------------------- whole Azure samples
def _pipe(tmp_path: Path, name: str = "store"):
    pipe = _aws_pipe(tmp_path, name)
    load_snapshot(pipe.store.engine, FIXTURE)
    return pipe


@pytest.mark.parametrize("large", [False, True])
def test_the_azure_cost_details_sample_is_matched(tmp_path, monkeypatch, large):
    pipe = _pipe(tmp_path)
    if large:                                                # the large path: priced chunk by chunk
        monkeypatch.setenv("LARGE_FILE_MB", TINY)
        monkeypatch.setenv("CHUNK_ROWS", "4")
    r = pipe.parse_file(FILES / "azure_usage_full.csv")
    assert bool(r.get("large")) == large
    rows = pipe.store.get_rows(r["sha256"])
    usage = rows[rows["charge_type"].eq("Usage")]
    levels = {d: set(g) for d, g in usage.groupby("description")["price_match_level"]}
    assert levels == {"D4s v5": {"attributes"}, "D4s v5 Spot": {"attributes"}, "E8ds v5": {"attributes"},
                      "B2ms": {"attributes"}, "P10 Disks": {"unmatched"}}          # the sample's disk meter is made up
    matched = usage[usage["price_match_level"].eq("attributes")]
    price = matched.groupby("description")["catalog_unit_price"].first().astype(float).to_dict()
    # Windows license included; Linux Spot; a Linux meter running Windows with Azure Hybrid Benefit; Linux (Canonical)
    assert price == pytest.approx({"D4s v5": 0.376, "D4s v5 Spot": 0.040531, "E8ds v5": 0.576, "B2ms": 0.0832})
    assert r["pricing"]["levels"]["attributes"]["lines"] == 12 and "no_catalog" not in r["pricing"]["levels"]
    # the file printed PayGPrice: the list unit price stays the printed one (R1); the catalog's is beside it
    assert not flagged(usage["_list_unit_price_inferred"]).any()


def test_the_amortized_sample_needs_the_os_and_a_default_gives_it(tmp_path):
    pipe = _pipe(tmp_path)
    r = pipe.parse_file(FILES / "azure_actual.csv")
    rows = pipe.store.get_rows(r["sha256"])
    usage = rows[rows["charge_type"].eq("Usage")]
    assert set(usage["price_match_level"]) == {"ambiguous"} and set(usage["price_missing"]) == {"operating_system"}
    again = pipe.reprice(r["sha256"], PriceDefaults(operating_system="Linux"))
    rows = pipe.store.get_rows(r["sha256"])
    usage = rows[rows["charge_type"].eq("Usage")]
    assert set(usage["price_match_level"]) == {"assumed"} and again["pricing"]["levels"]["assumed"]["lines"] == 9
    price = usage.groupby("description")["catalog_unit_price"].first().astype(float).to_dict()
    assert price == pytest.approx({"D4s v5": 0.192, "D4s v5 Spot": 0.040531, "D8s v5": 0.384})


@pytest.mark.parametrize("name", ["azure_usage_full.csv", "azure_actual.csv", "azure_amortized.csv",
                                  "azure_ea_usage.csv"])
def test_no_azure_sample_gets_worse(tmp_path, name):
    priced = _pipe(tmp_path, "priced").parse_file(FILES / name)
    plain = _aws_pipe(tmp_path, "plain", prices=False).parse_file(FILES / name)
    assert priced["status"] == plain["status"]
    assert (priced["spec"] or {}).get("route") == (plain["spec"] or {}).get("route")
    assert priced["confidence"] >= plain["confidence"] - 1e-9, [c for c in priced["checks"] if c["status"] != "skip"]


# ---------------------------------------------------------------- currency
def _rebilled(tmp_path: Path, currency: str, rate: float | None) -> Path:
    """The EA sample's AKS Uptime SLA lines (the fixture's 0.10 USD per hour) billed at exactly that list price in
    currency at rate (ExchangeRatePricingToBilling: units per USD; None: the file prints no rate)."""
    d = pd.read_csv(FILES / "azure_ea_usage.csv")
    d = d[d["MeterName"].eq("Standard Uptime SLA")].copy()
    d["EffectivePrice"] = 0.1 * (rate or 1.0)
    d["CostInBillingCurrency"] = (d["Quantity"] * 0.1 * (rate or 1.0)).round(6)
    d["BillingCurrencyCode"] = currency
    if rate is not None:
        d["ExchangeRatePricingToBilling"] = rate
    path = tmp_path / f"azure_{currency}.csv"
    d.to_csv(path, index=False)
    return path


@pytest.mark.parametrize("currency, rate", [("USD", 1.0), ("EUR", 0.92), ("JPY", 152.0)])
def test_an_azure_bill_at_list_price_has_ratio_one_in_any_currency(tmp_path, currency, rate):
    pipe = _pipe(tmp_path)
    r = pipe.parse_file(_rebilled(tmp_path, currency, rate))
    rows = pipe.store.get_rows(r["sha256"])
    assert r["spec"]["id"] == "azure-usage-details"
    assert set(rows["price_match_level"]) == {"attributes"} and set(rows["currency"]) == {currency}
    assert pd.to_numeric(rows["price_ratio"]).tolist() == pytest.approx([1.0] * len(rows), rel=1e-4)
    first = rows.iloc[0]
    assert float(first["catalog_unit_price"]) == pytest.approx(0.1 * rate)              # in the line's currency
    assert float(first["list_cost"]) == pytest.approx(float(first["quantity"]) * 0.1 * rate)
    note = first["price_assumptions"] if isinstance(first["price_assumptions"], str) else ""
    assert (f"currency={currency} at {rate:g} per USD (printed rate)" in note) == (currency != "USD")
    checks = {c["name"]: c["status"] for c in r["checks"]}
    assert checks["implied_unit_price"] == "pass" and checks["catalog_grounding"] == "pass"


def test_an_azure_line_in_another_currency_without_a_rate_is_not_priced(tmp_path):
    pipe = _pipe(tmp_path)
    r = pipe.parse_file(_rebilled(tmp_path, "EUR", None))
    rows = pipe.store.get_rows(r["sha256"])
    assert set(rows["price_match_level"]) == {"unmatched"} and set(rows["price_missing"]) == {"currency"}
    assert pd.to_numeric(rows["list_cost"]).isna().all() and rows["catalog_unit_price"].isna().all()
    assert {c["name"]: c["status"] for c in r["checks"]}["implied_unit_price"] == "skip"


# ---------------------------------------------------------------- the fetcher and the weekly refresh
def _fetcher():
    path = ROOT / "scripts" / "azure" / "fetch_azure_retail_prices.py"
    spec = importlib.util.spec_from_file_location("fetch_azure_retail_prices_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_fetcher_follows_next_page_links_and_retries(tmp_path, monkeypatch):
    _, items = _items(FIXTURE, "Virtual Machines")
    urls = []

    def fake(url, timeout=None):
        urls.append(url)
        if len(urls) == 2:                                   # the API's rate limit, once
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {"Retry-After": "0.01"}, None)
        page2 = "skip=20" in url
        body = {"Items": items[20:] if page2 else items[:20], "Count": 16 if page2 else 20,
                "NextPageLink": None if page2 else url + "&$skip=20"}
        return _Response(json.dumps(body).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    f = _fetcher()
    assert f.main(["--out", str(tmp_path / "out"), "--service", "Virtual Machines", "--region", "eastus",
                   "--pause", "0"]) == 0
    assert "serviceName%20eq%20%27Virtual%20Machines%27%20and%20armRegionName%20eq%20%27eastus%27" in urls[0]
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    got = manifest["files"]["Virtual Machines"]["eastus"]
    assert manifest["provider"] == "azure" and (got["rows"], got["requests"]) == (36, 2) and len(urls) == 3
    res = load_snapshot(engine_for(f"sqlite:///{tmp_path / 'p.db'}"), tmp_path / "out")
    assert res.counts == {"azure:Virtual Machines": 36}


def test_a_stopped_download_continues_from_its_last_page(tmp_path, monkeypatch):
    """A run stopped mid-file (Ctrl-C, a closed terminal, a network error) keeps the pages it has: the next run asks
    only for the rest, and the file has every item once."""
    _, items = _items(FIXTURE, "Virtual Machines")
    pages = [items[:12], items[12:24], items[24:]]
    urls, stop = [], {"at": 3}

    def fake(url, timeout=None):
        urls.append(url)
        n = int(url.split("$skip=")[1]) // 12 if "$skip=" in url else 0
        if len(urls) == stop["at"]:
            raise KeyboardInterrupt
        body = {"Items": pages[n], "NextPageLink": url.split("&$skip=")[0] + f"&$skip={(n + 1) * 12}"
                if n + 1 < len(pages) else None}
        return _Response(json.dumps(body).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    args = ["--out", str(tmp_path / "out"), "--service", "Virtual Machines", "--region", "eastus", "--pause", "0"]
    with pytest.raises(KeyboardInterrupt):
        _fetcher().main(args)
    stop["at"] = 0
    urls.clear()
    assert _fetcher().main(args) == 0
    assert len(urls) == 1 and urls[0].endswith("$skip=24")                 # only the page it did not have
    got = json.loads((tmp_path / "out" / "manifest.json").read_text())["files"]["Virtual Machines"]["eastus"]
    assert (got["rows"], got["requests"]) == (len(items), 3)
    res = load_snapshot(engine_for(f"sqlite:///{tmp_path / 'p.db'}"), tmp_path / "out")
    assert res.counts == {"azure:Virtual Machines": len(items)}
    assert not list((tmp_path / "out").rglob("*.part")) and not list((tmp_path / "out").rglob("*.resume"))


def test_a_query_past_the_apis_cap_is_asked_again_per_region(tmp_path, monkeypatch):
    _, items = _items(FIXTURE, "Bandwidth")
    urls = []

    def fake(url, timeout=None):
        urls.append(url)
        capped = "armRegionName" not in url                  # every region at once: more than the API serves
        body = {"Items": items, "NextPageLink": url + "&$skip=1000000" if capped else None}
        return _Response(json.dumps(body).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert _fetcher().main(["--out", str(tmp_path / "out"), "--service", "Bandwidth", "--pause", "0"]) == 0
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert list(manifest["files"]["Bandwidth"]) == ["eastus"] and manifest["totals"]["errors"] == 0
    assert len(urls) == 2 and "armRegionName%20eq%20%27eastus%27" in urls[1]


def test_the_weekly_refresh_loads_aws_then_azure(store, tmp_path, monkeypatch):          # noqa: F811
    monkeypatch.setenv("PRICE_PROVIDERS", "aws,azure")
    monkeypatch.setenv("PRICE_FILES_DIR", str(tmp_path / "aws-price-files"))
    monkeypatch.setenv("AZURE_PRICE_FILES_DIR", str(tmp_path / "azure-price-files"))
    monkeypatch.setattr(worker, "download_prices", lambda dest: shutil.copytree(AWS_FIXTURE, dest) and 0)
    monkeypatch.setattr(worker, "download_azure_prices", lambda dest: shutil.copytree(FIXTURE, dest) and 0)
    job, _ = jobs.enqueue(store, "prices", "maintenance", name="prices", dedupe_key="prices:test")
    assert _drain(store) == ["done"]
    result = jobs.get(store, job["id"])["result"]
    assert result["status"] == "current" and set(result["providers"]) == {"aws", "azure"}
    assert "azure:Virtual Machines" in result["providers"]["azure"]["price_lists"]
    assert PriceCatalog(store.engine).services("azure") == {"Virtual Machines", "Storage", "Bandwidth",
                                                            "Azure Kubernetes Service"}
    assert (tmp_path / "azure-price-files" / "manifest.json").exists()


def test_price_providers_setting(monkeypatch):
    monkeypatch.delenv("PRICE_PROVIDERS", raising=False)
    assert worker.price_providers() == ["aws"]
    monkeypatch.setenv("PRICE_PROVIDERS", " AWS , azure ")
    assert worker.price_providers() == ["aws", "azure"]
    monkeypatch.setenv("PRICE_PROVIDERS", "aws,oracle")
    with pytest.raises(ValueError, match="oracle"):
        worker.price_providers()
