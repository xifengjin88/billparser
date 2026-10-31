"""Task 07: price-based validation checks. The catalog's list prices (the task 05 fixture) catch parses that add up but
are wrong: swapped columns, wrong units, products that do not exist. Every check reads RowStats, so a frame and the
same frame in chunks give the same check; the CUR cases also run on the large path."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_pricing import FILES, FIXTURE, LINUX, TINY, _doc, _lines, _pipe  # noqa: E402
from test_pricing import _cur as _cur_september  # noqa: E402

import billparse.analyze as analyze_mod  # noqa: E402
from billparse import Pipeline, Settings  # noqa: E402
from billparse.db import engine_for  # noqa: E402
from billparse.export import to_schema  # noqa: E402
from billparse.learn import regression_gate  # noqa: E402
from billparse.pricing import match_prices  # noqa: E402
from billparse.spec import Extraction  # noqa: E402
from billparse.validate import PRICE_CHECKS, RowStats, price_checks, validate  # noqa: E402
from prices.load import load_snapshot  # noqa: E402
from prices.store import PriceCatalog  # noqa: E402


def _cur(tmp_path: Path, list_cost: bool = True) -> Path:
    """The CUR of test_pricing two months later (November 2026): inside the fixture catalog's history, so the
    printed list prices are compared (a date before the catalog's oldest price is not)."""
    src = _cur_september(tmp_path, list_cost)
    df = pd.read_csv(src, dtype=str, keep_default_na=False)
    c5 = df["lineItem/UsageType"].str.contains("c5.large")                 # its own real SKU, $0.085
    df.loc[c5, "product/sku"] = C5
    df.loc[c5 & df["pricing/RateCode"].ne(""), "pricing/RateCode"] = f"{C5}.JRTCKXETXF.6YS6EN2CT7"
    for c in ("lineItem/UsageStartDate", "lineItem/UsageEndDate"):
        d = pd.to_datetime(df[c], errors="coerce", utc=True) + pd.Timedelta(days=61)
        df[c] = d.dt.strftime("%Y-%m-%dT%H:%M:%SZ").where(d.notna(), "")
    path = tmp_path / f"cur_nov{'' if list_cost else '_nolist'}.csv.gz"
    df.to_csv(path, index=False, compression="gzip")
    return path


C5 = "6U6GZ2DN4RFCJ7D9"                          # c5.large Linux Shared us-east-1: $0.085 per hour
STATUS_RANK = {"reconciled": 0, "unreconciled": 1, "auxiliary": 1, "needs_review": 2, "unsupported": 3}


@pytest.fixture
def catalog(tmp_path) -> PriceCatalog:
    engine = engine_for(f"sqlite:///{tmp_path / 'prices.db'}")
    load_snapshot(engine, FIXTURE)
    return PriceCatalog(engine)


def _checks(verdict: dict) -> dict:
    return {c["name"]: c for c in verdict["checks"] if c["name"] in PRICE_CHECKS}


def _both(rows: pd.DataFrame, text: str | None = None) -> dict:
    """The price checks of priced rows on the normal path (one frame) and the large path (RowStats of two chunks,
    merged): they must be the same."""
    normal = _checks(validate(Extraction(rows=rows), text=text))
    half = len(rows) // 2
    stats = RowStats.of(rows.iloc[:half])
    stats.merge(RowStats.of(rows.iloc[half:]))
    large = {c.name: c for c in price_checks(stats, rows, text)}
    for name, c in normal.items():
        assert (c["status"], c["weight"]) == (large[name].status, large[name].weight), name
    return normal


def _priced(catalog, **cols) -> pd.DataFrame:
    rows = _lines(**cols)
    rows["_src"] = [f"t:{i + 2}" for i in range(len(rows))]
    match_prices(rows, catalog)
    return rows


def _write(tmp_path: Path, name: str, change) -> Path:
    df = pd.read_csv(_cur(tmp_path), dtype=str, keep_default_na=False)
    df = change(df)
    path = tmp_path / name
    df.to_csv(path, index=False, compression="gzip")
    return path


def _parse(tmp_path: Path, monkeypatch, path: Path, large: bool, store: str = "store") -> dict:
    pipe = _pipe(tmp_path, store + ("_large" if large else ""))
    with monkeypatch.context() as m:
        if large:
            m.setenv("LARGE_FILE_MB", TINY)
            m.setenv("CHUNK_ROWS", "4")
        r = pipe.parse_file(path)
    assert bool(r.get("large")) == large
    return r


# ---------------------------------------------------------------- a correct CUR, and column mix-ups
@pytest.mark.parametrize("large", [False, True])
def test_a_correct_cur_passes_or_skips_and_confidence_does_not_fall(tmp_path, monkeypatch, large):
    path = _cur(tmp_path)
    priced = _parse(tmp_path, monkeypatch, path, large)
    plain = _pipe(tmp_path, "plain", prices=False)
    with monkeypatch.context() as m:
        if large:
            m.setenv("LARGE_FILE_MB", TINY)
            m.setenv("CHUNK_ROWS", "4")
        before = plain.parse_file(path)
    checks = _checks(priced)
    assert all(c["status"] in ("pass", "skip") for c in checks.values()), checks
    assert checks["catalog_grounding"]["status"] == "pass" and checks["printed_list_price"]["status"] == "pass"
    assert checks["implied_unit_price"]["status"] == "pass"
    assert priced["confidence"] >= before["confidence"]
    assert STATUS_RANK[priced["status"]] <= STATUS_RANK[before["status"]]


@pytest.mark.parametrize("large", [False, True])
def test_quantity_and_unit_price_swapped(tmp_path, monkeypatch, large):
    def swap(df):
        df[["lineItem/UsageAmount", "lineItem/UnblendedRate"]] = df[["lineItem/UnblendedRate", "lineItem/UsageAmount"]]
        return df
    r = _parse(tmp_path, monkeypatch, _write(tmp_path, "swapped.csv.gz", swap), large)
    c = _checks(r)["implied_unit_price"]
    assert c["status"] == "fail" and "mix-up" in c["detail"] and "rows:" in c["detail"]
    assert any(f.startswith("implied_unit_price") for f in r["feedback"]) if "feedback" in r else True


@pytest.mark.parametrize("large", [False, True])
def test_seconds_labeled_as_hours(tmp_path, monkeypatch, large):
    def seconds(df):
        hourly = df["pricing/unit"].eq("Hrs")
        q = pd.to_numeric(df.loc[hourly, "lineItem/UsageAmount"])
        df.loc[hourly, "lineItem/UsageAmount"] = (q * 3600).astype(str)
        rate = pd.to_numeric(df.loc[hourly, "lineItem/UnblendedRate"])
        df.loc[hourly, "lineItem/UnblendedRate"] = (rate / 3600).astype(str)
        return df
    r = _parse(tmp_path, monkeypatch, _write(tmp_path, "seconds.csv.gz", seconds), large)
    c = _checks(r)["implied_unit_price"]
    assert c["status"] == "fail" and "3600" in c["detail"], c


@pytest.mark.parametrize("large", [False, True])
def test_a_monthly_list_price_on_hourly_lines(tmp_path, monkeypatch, large):
    def monthly(df):
        hourly = df["pricing/unit"].eq("Hrs") & df["pricing/publicOnDemandRate"].ne("")
        rate = pd.to_numeric(df.loc[hourly, "pricing/publicOnDemandRate"])
        df.loc[hourly, "pricing/publicOnDemandRate"] = (rate * 730).astype(str)
        return df
    r = _parse(tmp_path, monkeypatch, _write(tmp_path, "monthly.csv.gz", monthly), large)
    c = _checks(r)["printed_list_price"]
    assert c["status"] == "fail" and "×730" in c["detail"], c


# ---------------------------------------------------------------- impossible values
def test_products_that_do_not_exist(catalog):
    rows = _priced(catalog, sku_id=[LINUX] * 2 + [None] * 3, instance_type=[None, None, "Linux/UNIX", "m5.xlarge", "xx9.huge"],
                   region=["us-east-1"] * 3 + ["me-south-9", "us-east-1"], operating_system=[None, None, "Linux", "Linux", "Linux"])
    assert rows["price_missing"].tolist()[2:] == ["no_such_product"] * 3
    c = _both(rows)["catalog_grounding"]
    assert c["status"] == "fail" and "m5.xlarge in me-south-9" in c["detail"] and "t:4" in c["detail"]


def test_a_service_the_catalog_does_not_load(catalog):
    rows = _priced(catalog, sku_id=["SOMESKU"] * 3, service_code=["AmazonDynamoDB"] * 3, usage_type=["ReadUnits"] * 3)
    assert set(rows["price_missing"]) == {"service_not_loaded"}
    assert _both(rows)["catalog_grounding"]["status"] == "skip"


def test_usage_hours(catalog):
    rows = _priced(catalog, sku_id=[LINUX, LINUX], resource_id=["i-1", "i-2"], quantity=[25.0, 24.0],
                   charge_start=["2026-10-01T00:00:00Z"] * 2, charge_end=["2026-10-02T00:00:00Z"] * 2)
    c = _both(rows)["usage_hours"]
    assert c["status"] == "fail" and "i-1" in c["detail"] and "i-2" not in c["detail"]


def test_usage_hours_skips_lines_counted_per_core_or_gpu(catalog):
    """One resource's per-core or per-GPU hours (a GCP N2 core SKU, an Azure vCore meter, a Fargate vCPU) are
    hours times its cores: they may exceed the window."""
    window = {"charge_start": ["2026-10-01T00:00:00Z"] * 4, "charge_end": ["2026-10-02T00:00:00Z"] * 4}
    rows = _priced(catalog, sku_id=[LINUX] * 4, resource_id=["i-1", "vm-2", "vm-3", "task-4"],
                   quantity=[25.0, 96.0, 48.0, 96.0], **window,
                   description=["c5.large", "N2 Instance Core running in Americas", "Nvidia T4 GPU running in Americas",
                                "Fargate"], usage_type=[None, None, None, "USE1-Fargate-vCPU-Hours:perCPU"])
    c = _both(rows)["usage_hours"]
    assert c["status"] == "fail" and c["detail"].startswith("1 of 1 ") and "i-1" in c["detail"]


def test_cost_order(catalog):
    rows = _priced(catalog, sku_id=[LINUX] * 10, list_cost=[1.0] * 10, effective_cost=[2.0] * 10)
    c = _both(rows)["cost_order"]
    assert c["status"] == "warn" and "swapped" in c["detail"]
    rows = _priced(catalog, sku_id=[LINUX] * 10, list_cost=[2.0] * 10, effective_cost=[1.0] * 10)
    assert _both(rows)["cost_order"]["status"] == "pass"


def test_grounding_values(catalog):
    rows = _priced(catalog, instance_type=["m5.xlarge", "r9.mega"], operating_system=["Linux"] * 2)
    text = "Amazon EC2 m5.xlarge Linux us-east-1 ..."
    c = _both(rows, text)["grounding_values"]
    assert c["status"] == "fail" and "r9.mega" in c["detail"] and "m5.xlarge" not in c["detail"]
    assert _both(rows)["grounding_values"]["status"] == "skip"           # rows read by a spec


def test_price_units(catalog):
    rows = _priced(catalog, sku_id=[LINUX] * 4, unit=["Requests"] * 3 + ["Hrs"])
    assert _both(rows)["price_units"]["status"] == "warn"


# ---------------------------------------------------------------- skips: today's verdict
def test_no_catalog_every_new_check_skips_and_the_verdict_is_todays(tmp_path):
    rows = _lines(sku_id=[LINUX] * 3)
    match_prices(rows, None)
    v = validate(Extraction(rows=rows))
    assert all(c["status"] == "skip" and c["weight"] == 0 for c in _checks(v).values())
    assert len(_checks(v)) == len(PRICE_CHECKS)


@pytest.mark.parametrize("name", ["azure_actual.csv", "focus_gcp.parquet"])
def test_azure_and_gcp_files_get_todays_verdict(tmp_path, name):
    priced = _pipe(tmp_path, "priced").parse_file(FILES / name)
    plain = _pipe(tmp_path, "plain", prices=False).parse_file(FILES / name)
    assert all(c["status"] == "skip" for c in _checks(priced).values())
    assert (priced["status"], priced["confidence"], priced["completeness"]) == \
        (plain["status"], plain["confidence"], plain["completeness"])


# ---------------------------------------------------------------- route choice, LLM repair, regression gate
class FakeLLM:
    def __init__(self, specs):
        self.specs, self.calls = list(specs), []

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        self.calls.append(user)
        if system.startswith("Extract every billable"):
            return json.dumps({"rows": []})
        return json.dumps(self.specs.pop(0) if self.specs else {"bad": True})


def _spec(qty: str, price: str) -> dict:
    return {"source": "grid", "detect": {"required": ["c1", "c7"]},
            "meta": {"provider": "aws", "kind": "cost_lines", "cost_basis": "billed"},
            "columns": {"product_code": {"from": ["c1"]}, "service_raw": {"from": ["c1"]}, "sku_id": {"from": ["c2"]}, "usage_type": {"from": ["c3"]},
                        "region": {"from": ["c4"]}, "quantity": {"from": [qty]}, "unit_price": {"from": [price]},
                        "billed_cost": {"from": ["c7"]}, "charge_start": {"from": ["c8"]}, "unit": {"from": ["c9"]}},
            "filters": [{"drop_if_blank": "billed_cost"}]}


def test_the_price_checks_choose_the_route_and_steer_the_repair(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLPARSE_SPEC_CANDIDATES", "1")         # one candidate per round: the repair is told
    rows = [{"c1": "AmazonEC2", "c2": LINUX, "c3": "BoxUsage:m5.xlarge", "c4": "us-east-1", "c5": 24 + i % 3,
             "c6": 0.192, "c7": round((24 + i % 3) * 0.192, 4), "c8": f"2026-10-{i + 1:02d}", "c9": "Hrs"}
            for i in range(12)]
    path = tmp_path / "opaque.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    for f in (ROOT / "data" / "raw").glob("*.json"):
        shutil.copy(f, raw / f.name)
    analyze_mod._RESOLVER = None
    llm = FakeLLM([_spec("c6", "c5"), _spec("c5", "c6")])      # quantity and unit price swapped, then right
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=True,
                             use_agent=False), llm=llm)
    load_snapshot(pipe.store.engine, FIXTURE)
    r = pipe.parse_file(path)
    spec_attempts = [a for a in r["attempts"] if a["route"] == "llm_spec"]
    assert len(spec_attempts) == 2 and spec_attempts[0]["status"] != "reconciled"
    assert any("implied_unit_price" in f for f in spec_attempts[0]["feedback"])
    assert any("implied_unit_price" in u for u in llm.calls[1:])                # the repair was told
    assert r["spec"]["route"] == "llm_spec"
    rows_out = pipe.store.get_rows(r["sha256"])
    assert pd.to_numeric(rows_out["quantity"]).min() >= 24                     # the right spec won
    assert _checks(r)["implied_unit_price"]["status"] == "pass"


def test_the_regression_gate_rejects_a_version_that_swaps_two_columns(tmp_path):
    pipe = _pipe(tmp_path)
    r = pipe.parse_file(_cur(tmp_path))
    assert r["spec"]["id"] == "aws-cur-legacy"
    accepted = pipe.store.spec_docs("aws-cur-legacy")[r["sha256"]]
    assert accepted["checks"]["implied_unit_price"] == "pass"
    spec = pipe.store.get_spec("aws-cur-legacy")
    assert regression_gate(pipe, spec, "aws-cur-legacy")["passed"]
    swapped = json.loads(json.dumps(spec))
    swapped["columns"]["quantity"], swapped["columns"]["unit_price"] = \
        spec["columns"]["unit_price"], spec["columns"]["quantity"]
    gate = regression_gate(pipe, swapped, "aws-cur-legacy")
    assert not gate["passed"] and "implied_unit_price" in json.dumps(gate["failures"])


# ---------------------------------------------------------------- contract, samples
def test_catalog_unit_price_in_the_contract(tmp_path):
    pipe = _pipe(tmp_path)
    r = pipe.parse_file(_cur(tmp_path))
    out = to_schema(_doc(pipe, r["sha256"]))
    priced = [c for c in out.charges if c.catalog_unit_price is not None]
    assert out.schema_version == "1.10" and priced
    assert all(c.catalog_unit_price.origin == "inferred" for c in priced)


# samples whose confidence the price checks lower, and why: a real inconsistency in the sample, not a false alarm
KNOWN = {"aws_cur2.csv.gz": ("implied_unit_price", "×1/30.42")}   # daily EBS lines: GB printed as GB-Mo
#                                                                  (make_samples: $0.08/30 per "GB-Mo")


@pytest.mark.parametrize("name", sorted(p.name for p in FILES.iterdir() if p.is_file()))
def test_no_sample_gets_worse(tmp_path, name):
    priced = _pipe(tmp_path, "priced").parse_file(FILES / name)
    plain = _pipe(tmp_path, "plain", prices=False).parse_file(FILES / name)
    assert STATUS_RANK[priced["status"]] <= STATUS_RANK[plain["status"]], (priced["checks"], plain["status"])
    assert (priced["spec"] or {}).get("route") == (plain["spec"] or {}).get("route")
    if name in KNOWN:
        check, factor = KNOWN[name]
        c = _checks(priced)[check]
        assert c["status"] == "fail" and factor in c["detail"], c
    else:
        assert priced["confidence"] >= plain["confidence"] - 1e-9, [c for c in priced["checks"] if c["status"] != "skip"]


# ---------------------------------------------------------------- review fixes
def test_a_guessed_usage_type_is_no_false_alarm(tmp_path):
    """The reseller invoice's "Amazon S3 Standard Storage" gives usage type "S3" from the words: catalog_row, not
    no_such_product; and one tested line is too few to judge."""
    pipe = _pipe(tmp_path)
    r = pipe.parse_file(FILES / "reseller_invoice.pdf")
    rows = pipe.store.get_rows(r["sha256"])
    s3 = rows[rows["usage_type"].eq("S3")]
    assert len(s3) and set(s3["price_missing"]) == {"catalog_row"}
    assert "no_such_product" not in set(rows["price_missing"].dropna())
    assert _checks(r)["catalog_grounding"]["status"] == "skip"


@pytest.mark.parametrize("large", [False, True])
def test_a_wrong_region_column_on_exact_matches(tmp_path, monkeypatch, large):
    """Every region changed: the lines still match by SKU and rate code, but they print another region than the
    product."""
    def region(df):
        for c in ("product/region", "product/regionCode"):
            df.loc[df[c].ne(""), c] = "eu-north-9"
        return df
    r = _parse(tmp_path, monkeypatch, _write(tmp_path, "region.csv.gz", region), large)
    c = _checks(r)["catalog_grounding"]
    assert c["status"] == "fail" and "region eu-north-9 vs us-east-1" in c["detail"] and "rows:" in c["detail"], c


@pytest.mark.parametrize("large", [False, True])
def test_a_monthly_list_price_on_an_old_bill_blames_the_printed_column(tmp_path, monkeypatch, large):
    """Lines before the catalog's history are compared too (a price never changes by ×730), and implied_unit_price
    compares against the catalog's price, so only printed_list_price fails."""
    def monthly(df):
        hourly = df["pricing/unit"].eq("Hrs") & df["pricing/publicOnDemandRate"].ne("")
        rate = pd.to_numeric(df.loc[hourly, "pricing/publicOnDemandRate"])
        df.loc[hourly, "pricing/publicOnDemandRate"] = (rate * 730).astype(str)
        return df
    old = _cur_september(tmp_path)                               # September 2026: before the fixture's history
    df = monthly(pd.read_csv(old, dtype=str, keep_default_na=False))
    path = tmp_path / "old_monthly.csv.gz"
    df.to_csv(path, index=False, compression="gzip")
    checks = _checks(_parse(tmp_path, monkeypatch, path, large))
    assert checks["printed_list_price"]["status"] == "fail" and "×730" in checks["printed_list_price"]["detail"]
    assert checks["implied_unit_price"]["status"] in ("pass", "skip"), checks["implied_unit_price"]


def test_an_old_bill_with_another_price_only_warns(catalog):
    rows = _priced(catalog, sku_id=[LINUX] * 6, list_unit_price=[0.25] * 6, charge_start=["2020-01-15"] * 6)
    assert rows["price_assumptions"].str.contains("before_history").all()
    assert _both(rows)["printed_list_price"]["status"] == "warn"


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("sample", ["aws_cur_legacy_full.csv.gz", "aws_cur2_flat.csv.gz"])
def test_a_wrong_region_when_the_lines_match_by_usage_type(tmp_path, monkeypatch, large, sample):
    """The samples' rate codes and SKUs are made up: their lines match by usage type, which the region narrows. With a
    region the catalog does not have, they are looked up by the identifier alone and the region conflicts."""
    df = pd.read_csv(FILES / sample, dtype=str, keep_default_na=False)
    for c in [c for c in df if c.lower().replace("/", "_") in ("product_region", "product_regioncode",
                                                                "product_region_code")]:
        df.loc[df[c].ne(""), c] = "eu-north-9"
    path = tmp_path / f"region_{sample}"
    df.to_csv(path, index=False, compression="gzip")
    r = _parse(tmp_path, monkeypatch, path, large)
    c = _checks(r)["catalog_grounding"]
    assert c["status"] == "fail" and "region eu-north-9 vs us-east-1" in c["detail"], c
