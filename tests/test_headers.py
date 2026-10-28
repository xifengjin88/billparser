"""Task 10: header rules for every field, the seed and learned header dictionary, value detectors, learning hooks,
and the LLM that maps only the columns the mapper did not settle. SQLite, fakes, the fixture price catalog."""

from __future__ import annotations

import shutil
import sys
import threading
from pathlib import Path

import pandas as pd
import pytest

import billparse.analyze as analyze_mod
from billparse import detect, headers, mapper
from billparse.mapper import MapContext, mapping
from billparse.pipeline import Pipeline, Settings
from billparse.review import approve
from billparse.webapi import ApproveRequest, _edited
from prices.load import load_snapshot
from prices.store import PriceCatalog

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "samples" / "files"
PRICES = ROOT / "tests" / "fixtures" / "prices" / "download"
sys.path.insert(0, str(ROOT / "tests"))
from test_pipeline import GERMAN_SPEC, FakeLLM  # noqa: E402

sys.path.insert(0, str(ROOT / "samples"))
from make_samples import german_xlsx  # noqa: E402

LINUX = "5G4TA8Z4MUKE6MJB"                  # the fixture catalog's m5.xlarge Linux Shared us-east-1 ($0.192 per hour)
SKUS = [LINUX, "6U6GZ2DN4RFCJ7D9", "7EQYX3EPSQG98BNK", "8AW2DYRV3M9N5RRZ"]


@pytest.fixture
def make(tmp_path):
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    for f in (ROOT / "data" / "raw").glob("*.json"):
        shutil.copy(f, raw / f.name)
    analyze_mod._RESOLVER = None

    def build(llm=None, prices: bool = False, store: str = "store") -> Pipeline:
        pipe = Pipeline(Settings(store_dir=tmp_path / store, catalog_dir=tmp_path / "data", use_llm=llm is not None,
                                 use_agent=False, spec_candidates=1), llm=llm)
        if prices and not PriceCatalog(pipe.store.engine).version:
            load_snapshot(pipe.store.engine, PRICES)
        return pipe
    yield build
    analyze_mod._RESOLVER = None


def _csv(tmp_path: Path, name: str, df: pd.DataFrame) -> Path:
    path = tmp_path / name
    df.to_csv(path, index=False)
    return path


def _spec_columns(pipe: Pipeline, r: dict) -> dict[str, str]:
    spec = pipe.store.get_spec(r["spec"]["id"], r["spec"]["version"])
    return headers.spec_headers(spec)


# ---------------------------------------------------------------- German, with the seed and no LLM
def test_the_german_invoice_needs_no_llm(make):
    pipe = make()
    r = pipe.parse_file(FILES / "german_invoice.xlsx")
    assert r["spec"]["route"] == "mapper" and r["llm_calls"] == 0 and r["status"] in ("reconciled", "unreconciled")
    assert _spec_columns(pipe, r) == {"service_raw": "Leistung", "quantity": "Menge", "unit_price": "Einzelpreis",
                                      "billed_cost": "Betrag"}


def test_the_seed_is_one_field_per_header_and_normalized():
    seed = headers.load_seed()
    assert headers.normalize("UnitPrice") == "unit price" and headers.normalize("Quantité") == "quantite"
    assert headers.normalize("単価") == "単価" and headers.normalize("Amount (USD)") == "amount usd"
    clash = {h: f for h, f in seed.items() if len(f) > 1}
    assert not clash, clash                                  # a header two fields claim would never be a hit


# ---------------------------------------------------------------- rules for the new fields
def _new_fields(tmp_path: Path) -> Path:
    n = 6
    return _csv(tmp_path, "export.csv", pd.DataFrame({
        "Service": ["Amazon Elastic Compute Cloud"] * n, "Usage Quantity": [10, 20, 30, 40, 50, 60],
        "Unit Price": [0.15] * n, "List Price": [0.192] * n, "List Cost": [1.92 * k for k in range(1, n + 1)],
        "Amortized Cost": [1.40 * k for k in range(1, n + 1)], "Cost": [1.50 * k for k in range(1, n + 1)],
        "Resource ID": [f"i-0123456789abcdef{k}" for k in range(n)], "SKU": [LINUX] * n,
        "Region": ["us-east-1"] * n, "Instance Type": ["m5.xlarge"] * n,
        "Date": [f"2026-10-0{k + 1}" for k in range(n)]}))


def test_each_new_column_maps_to_its_own_field(make, tmp_path):
    pipe = make(prices=True)
    r = pipe.parse_file(_new_fields(tmp_path))
    assert r["spec"]["route"] == "mapper", r["attempts"]
    assert _spec_columns(pipe, r) == {
        "service_raw": "Service", "quantity": "Usage Quantity", "unit_price": "Unit Price",
        "list_unit_price": "List Price", "list_cost": "List Cost", "effective_cost": "Amortized Cost",
        "billed_cost": "Cost", "resource_id": "Resource ID", "sku_id": "SKU", "region": "Region",
        "instance_type": "Instance Type", "charge_start": "Date"}
    checks = {c["name"]: c["status"] for c in r["checks"]}
    assert checks["cost_order"] != "skip" and checks["printed_list_price"] != "skip", checks


def test_a_header_never_scores_for_two_fields_at_3():
    for h in ("Purchase Option", "Commitment Type", "Pricing Model", "List Cost", "Unit Price", "List Price",
              "Effective Price", "Resource ID", "SKU", "Line Item Type", "Charge Category", "Pricing Term"):
        strong = [f for f in mapper.HEADER_RULES if mapper.header_score(f, h) >= 3.0]
        assert len(strong) <= 1, (h, strong)
    assert mapper.header_score("billed_cost", "List Cost") < 1 and mapper.header_score("list_unit_price", "Unit Price") == 0
    assert mapper.header_score("list_cost", "Cost ID") < 3


# ---------------------------------------------------------------- two unit prices, by line math
@pytest.mark.parametrize("fits", ["EffectivePrice", "UnitPrice"])
def test_two_unit_prices_are_told_apart_by_line_math(fits):
    qty = [10, 4, 7, 2]
    paid, listed = [0.15, 0.30, 0.45, 0.60], [0.20, 0.40, 0.60, 0.80]
    eff, unit = (paid, listed) if fits == "EffectivePrice" else (listed, paid)
    cols = {"MeterCategory": ["Virtual Machines"] * 4, "Quantity": qty, "UnitPrice": unit, "EffectivePrice": eff,
            "Cost": [round(q * p, 4) for q, p in zip(qty, paid)]}
    m = mapping(cols)
    other = "UnitPrice" if fits == "EffectivePrice" else "EffectivePrice"
    assert m.chosen["unit_price"] == ("EffectivePrice" if fits == "EffectivePrice" else "UnitPrice")
    assert m.chosen["contracted_unit_price"] == other and not m.warnings


def test_two_unit_prices_none_fits_keeps_the_header_choice():
    cols = {"Service": ["a", "b"], "Quantity": [1, 2], "Unit Price": [5.0, 6.0], "Effective Price": [7.0, 8.0],
            "Cost": [100.0, 200.0]}
    m = mapping(cols)
    assert m.chosen["unit_price"] in ("Unit Price", "Effective Price") and m.warnings


# ---------------------------------------------------------------- values with no header
def _no_headers(n: int = 8) -> dict[str, list]:
    return {"Col_1": ["123456789012"] * n, "Col_2": ["us-east-1", "us-west-2"] * (n // 2),
            "Col_3": ["m5.xlarge", "c5.large"] * (n // 2), "Col_4": ["BoxUsage:m5.xlarge", "BoxUsage:c5.large"] * (n // 2),
            "Col_5": [f"arn:aws:ec2:us-east-1:123456789012:instance/i-0abc{k}" for k in range(n)],
            "Col_6": [f"2026-10-{k + 1:02d}" for k in range(n)], "Col_7": ["USD"] * n,
            "Col_8": [24 * (k + 1) for k in range(n)], "Col_9": [round(4.608 * (k + 1), 4) + 0.0001 for k in range(n)]}


def test_columns_with_no_header_map_by_their_values():
    m = mapping(_no_headers())
    assert {f: m.chosen.get(f) for f in ("account_id", "region", "instance_type", "usage_type", "resource_id",
                                          "charge_start", "currency")} == {
        "account_id": "Col_1", "region": "Col_2", "instance_type": "Col_3", "usage_type": "Col_4",
        "resource_id": "Col_5", "charge_start": "Col_6", "currency": "Col_7"}
    # by design: a number is a quantity or a cost only by its header (or the dictionary), never by its values
    assert "quantity" not in m.chosen and "billed_cost" not in m.chosen
    assert detect.fields("Col_8", _no_headers()["Col_8"]) == [] and detect.fields("Col_9", _no_headers()["Col_9"]) == []


GUIDS = ["0f8fad5b-d9cb-469f-a165-70867728950e", "7c9e6679-7425-40de-944b-e07fc1f90ae7"] * 4


@pytest.mark.parametrize("header, values", [
    ("Order ID", ["123456789012", "210987654321"] * 4),
    ("Hostname", ["web-01", "web-02", "db-001", "cache-01"] * 2),
    ("Team", ["data-eng", "platform-2", "ml-ops", "web-team"] * 2),
    ("Request ID", GUIDS),
])
def test_an_id_pattern_does_not_make_a_named_column_an_account(header, values):
    # 12 digits, a GUID or a GCP project ID is an account only under a meaningless header or one that says account
    assert mapping({header: values}).chosen.get("account_id") is None


def test_an_invoice_number_of_12_digits_is_the_invoice():
    m = mapping({"Invoice Number": ["123456789012", "210987654321"] * 4})
    assert m.chosen.get("invoice_id") == "Invoice Number" and "account_id" not in m.chosen


@pytest.mark.parametrize("header, values", [
    ("Account", ["123456789012"] * 8), ("Linked Account", ["123456789012"] * 8), ("Subscription", GUIDS),
    ("Tenant", GUIDS), ("Project", ["my-project-1", "data-lake-42"] * 4), ("Column 3", ["123456789012"] * 8),
    ("", GUIDS),
])
def test_an_id_pattern_under_an_account_or_meaningless_header_is_the_account(header, values):
    assert mapping({header: values}).chosen.get("account_id") == header


def test_a_guid_in_an_azure_file_is_the_meter_when_the_account_is_taken():
    m = mapping({"SubscriptionId": GUIDS, "Col_2": GUIDS[::-1], "Cost": [1.0] * 8})
    assert m.chosen["account_id"] == "SubscriptionId" and m.chosen.get("meter_id") == "Col_2"


def test_catalog_backed_detectors(make):
    pipe = make(prices=True)
    ctx = MapContext(catalog=PriceCatalog(pipe.store.engine))
    random_codes = ["Q7WX2KD9ZP4M", "B8NC3LT6YR1V", "H5JF9QS2XE7U", "K3MD8RW4PZ6A"]
    m = mapping({"Ref": SKUS, "Other": random_codes, "Shape": ["m9.xlarge", "r9.large", "m9.2xlarge", "r9.xlarge"],
                 "Real": ["m5.xlarge", "c5.large", "m5.xlarge", "c5.large"]}, ctx)
    assert m.chosen.get("sku_id") == "Ref" and "Other" not in m.chosen.values()
    assert m.chosen.get("instance_type") == "Real" and "Shape" not in m.chosen.values()
    assert ctx.known                                         # the catalog's answers kept for the parse
    no_catalog = mapping({"Ref": SKUS, "Shape": ["m9.xlarge", "r9.large"] * 2})
    assert "sku_id" not in no_catalog.chosen and no_catalog.chosen.get("instance_type") == "Shape"


# ---------------------------------------------------------------- the learned dictionary
UNUSUAL = {"Abrechnungsposten": ["Amazon Elastic Compute Cloud", "Amazon Simple Storage Service", "AWS Lambda",
                                  "Amazon CloudFront"], "Stunden": [720, 10, 5, 1],
           "Nettowert": ["100.00", "50.00", "25.50", "10.00"]}
UNUSUAL_SPEC = {"source": "frame", "detect": {"required": ["Abrechnungsposten", "Nettowert"]},
                "meta": {"kind": "cost_lines", "cost_basis": "billed"},
                "columns": {"service_raw": {"from": ["Abrechnungsposten"]}, "quantity": {"from": ["Stunden"]},
                            "billed_cost": {"from": ["Nettowert"]}},
                "filters": [{"drop_if_blank": "billed_cost"}]}


def test_an_approved_review_teaches_its_headers(make, tmp_path):
    pipe = make(FakeLLM([UNUSUAL_SPEC]))
    first = pipe.parse_file(_csv(tmp_path, "a.csv", pd.DataFrame(UNUSUAL)))
    assert first["spec"]["route"] == "llm_spec" and first["status"] in ("reconciled", "unreconciled")
    d = headers.dictionary(pipe.store.engine)
    assert d.lookup("Abrechnungsposten") is None
    doc, changes, _, _ = _edited(pipe.store, first["sha256"], ApproveRequest())
    out = approve(pipe.store, first["sha256"], doc, changes, "", "reviewer@test")
    assert sorted(out["headers_learned"]) == ["Abrechnungsposten -> service_raw", "Nettowert -> billed_cost",
                                              "Stunden -> quantity"]
    assert d.lookup("abrechnungsposten") == ("service_raw", 3.5)
    # a second file with the same headers in another layout: the mapper reads it through the dictionary, no LLM
    pipe.store.save_spec(dict(pipe.store.get_spec(first["spec"]["id"]), trust="disabled"))
    other = pd.DataFrame({"Kostenstelle": ["A1", "B2", "C3"], **{k: v[:3] for k, v in UNUSUAL.items()}})
    r = make().parse_file(_csv(tmp_path, "b.csv", other))
    assert r["spec"]["route"] == "mapper" and r["llm_calls"] == 0
    cols = _spec_columns(make(), r)
    assert {f: cols.get(f) for f in ("service_raw", "quantity", "billed_cost")} == {
        "service_raw": "Abrechnungsposten", "quantity": "Stunden", "billed_cost": "Nettowert"}


def test_a_spec_confirms_twice_and_two_fields_are_no_hit(make):
    d = headers.dictionary(make().store.engine)
    assert d.learn("Gebuehr", "billed_cost", "spec") and d.lookup("Gebuehr") is None
    d.learn("Gebuehr", "billed_cost", "spec")
    assert d.lookup("Gebuehr") == ("billed_cost", 3.5)
    d.learn("Posten", "service_raw", "admin")
    d.learn("Posten", "description", "review")
    assert d.lookup("Posten") is None                        # two learned fields: ambiguous
    assert not d.learn("ab", "billed_cost", "admin") and not d.learn("2026", "charge_start", "admin")
    assert not d.learn("Cloud", "provider", "admin") and not d.learn("Coverage", "coverage", "admin")


def test_learning_is_one_statement_per_call(make):
    d = headers.dictionary(make().store.engine)

    def learn() -> None:
        for _ in range(50):
            d.learn("Rechnungsposten", "service_raw", "review")
    threads = [threading.Thread(target=learn) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    (row,) = d.entries("service_raw")
    assert row["count"] == 100


# ---------------------------------------------------------------- rejections
def test_a_rejection_stops_rules_seed_and_detectors(make):
    d = headers.dictionary(make().store.engine)
    ctx = MapContext(dictionary=d)
    ruled = {"Service": ["a", "b"], "Cost": ["1.00", "2.00"]}
    seeded = {"Leistung": ["a", "b"], "Betrag": ["3,00 €", "4,00 €"]}
    detected = _no_headers(2)
    assert mapping(ruled, ctx).chosen["billed_cost"] == "Cost"
    assert mapping(seeded, ctx).chosen["billed_cost"] == "Betrag"
    assert mapping(detected, ctx).chosen["region"] == "Col_2"
    d.reject("Cost", "billed_cost")
    d.reject("Betrag", "billed_cost")
    d.reject("Col_2", "region")
    assert "billed_cost" not in mapping(ruled, ctx).chosen                             # the rule's (and the fallback)
    assert "billed_cost" not in mapping(seeded, ctx).chosen                            # the seed's
    assert "region" not in mapping(detected, ctx).chosen                               # the detector's
    assert d.remove("Cost", "billed_cost") and mapping(ruled, ctx).chosen["billed_cost"] == "Cost"


def test_a_repair_that_remaps_a_header_rejects_the_old_field(make):
    engine = make().store.engine
    old = {**GERMAN_SPEC, "id": "x", "columns": {**GERMAN_SPEC["columns"], "description": {"from": ["Leistung"]},
                                                 "service_raw": {"from": ["Menge"]}}}
    new = {**GERMAN_SPEC, "id": "x"}
    assert headers.reject_remapped(engine, old, new) == [("Leistung", "description"), ("Menge", "service_raw")]
    d = headers.dictionary(engine)
    assert d.rejected("Leistung", "description") and not d.rejected("Leistung", "service_raw")
    assert {e["source"] for e in d.entries(rejected=True)} == {"spec"}


# ---------------------------------------------------------------- the LLM maps only what is left
def _contradicted(tmp_path: Path) -> Path:
    """Settled columns (Service, Region, Cost) but a printed total the lines do not reach: not accepted."""
    return _csv(tmp_path, "c.csv", pd.DataFrame({
        "Service": ["Amazon Elastic Compute Cloud", "Amazon Simple Storage Service", "Total"],
        "Region": ["us-east-1", "us-east-1", ""], "Cost": ["10.00", "5.00", "99.00"]}))


def test_the_llm_is_told_the_settled_columns(make, tmp_path):
    spec = {"source": "frame", "meta": {"kind": "cost_lines", "cost_basis": "billed"},
            "columns": {"service_raw": {"from": ["Region"]}, "billed_cost": {"from": ["Cost"]}}}
    llm = FakeLLM([spec])
    pipe = make(llm)
    pipe.s.max_repairs = 1
    r = pipe.parse_file(_contradicted(tmp_path))
    prompt = llm.calls[0][1]
    assert "Settled columns" in prompt and "service_raw <- Service" in prompt and "region <- Region" in prompt
    assert any(a["route"] == "llm_spec" for a in r["attempts"])


def test_a_spec_that_changes_a_settled_column_is_kept_with_a_warning():
    from billparse.pipeline import Attempt, _note_settled
    att = Attempt("llm_spec", {"columns": {"service_raw": {"from": ["Region"]}, "billed_cost": {"from": ["Cost"]}}})
    _note_settled(att, {"service_raw": "Service", "billed_cost": "Cost"})
    assert att.verdict["warnings"] == ["the spec changed a settled column: service_raw"]


def test_with_nothing_settled_the_prompt_is_unchanged(make, tmp_path, monkeypatch):
    monkeypatch.setattr(headers, "load_seed", lambda path=None: {})
    llm = FakeLLM([GERMAN_SPEC])
    pipe = make(llm)
    f = tmp_path / "de.xlsx"
    german_xlsx(f)
    pipe.parse_file(f)
    assert "Settled columns" not in llm.calls[0][1]

