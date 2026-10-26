"""Task 01: the cost types a file prints (billed, list, contracted, effective) as typed fields, and commitment
coverage on every line. Every value comes from the file; nothing is priced or looked up."""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

import billparse.analyze as analyze_mod
import billparse.contract as contract_mod
from billparse import Pipeline, Settings
from billparse.expr import evaluate
from billparse.schema import SCHEMA_VERSION, Document
from billparse.spec import SpecError, check_spec

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "samples" / "files"
COVERED = {"DiscountedUsage", "SavingsPlanCoveredUsage"}
COMMITTED = {"reserved", "savings_plan", "committed_use"}


@pytest.fixture
def pipe(tmp_path):
    (tmp_path / "data" / "raw").mkdir(parents=True)
    for f in (ROOT / "data" / "raw").glob("*.json"):
        shutil.copy(f, tmp_path / "data" / "raw" / f.name)
    analyze_mod._RESOLVER = None
    yield Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=False))
    analyze_mod._RESOLVER = None


def parse(pipe: Pipeline, path: Path) -> tuple[dict, Document]:
    r = pipe.parse_file(path)
    return r, Document.model_validate_json(pipe.store.part(r["sha256"], "doc"))


def value(cv):
    return None if cv is None else cv.value


def line_type(c) -> str | None:
    return value(c.line_type_raw)


# ---------------------------------------------------------------- AWS CUR
def test_covered_usage_is_never_on_demand(pipe):
    r, doc = parse(pipe, FILES / "aws_cur2_commitments.csv.gz")
    assert r["spec"]["route"] == "builtin" and r["llm_calls"] == 0
    covered = [c for c in doc.charges if line_type(c) in COVERED]
    assert covered and all(value(c.coverage) in COMMITTED for c in covered)
    assert {value(c.coverage) for c in covered if line_type(c) == "DiscountedUsage"} == {"reserved"}
    assert {value(c.coverage) for c in covered if line_type(c) == "SavingsPlanCoveredUsage"} == {"savings_plan"}
    assert all(value(c.commitment_status) == "used" for c in covered)


def test_spot_usage_is_read_from_the_usage_type(pipe):
    _, doc = parse(pipe, FILES / "aws_cur2_commitments.csv.gz")
    usage = [c for c in doc.charges if line_type(c) == "Usage"]
    assert {value(c.coverage) for c in usage if "SpotUsage" in str(value(c.usage_type))} == {"spot"}
    assert {value(c.coverage) for c in usage if "BoxUsage" in str(value(c.usage_type))} == {"on_demand"}


def test_negation_lines_are_kept_as_their_own_kind(pipe):
    r, doc = parse(pipe, FILES / "aws_cur2_commitments.csv.gz")
    neg = [c for c in doc.charges if line_type(c) == "SavingsPlanNegation"]
    assert len(neg) == 3 and {c.kind.value for c in neg} == {"commitment_negation"}
    assert {value(c.coverage) for c in neg} == {"savings_plan"}
    assert r["analysis"]["total_billed"] == pytest.approx(34.76, abs=0.01)   # the file still adds up


def test_negations_are_not_usage_and_not_credits(pipe):
    r, doc = parse(pipe, FILES / "aws_cur2_commitments.csv.gz")
    a = r["analysis"]
    usage = sum(float(c.cost.value) for c in doc.charges if c.kind.value == "usage")
    assert a["usage_spend"] == pytest.approx(usage, abs=0.01)
    assert a["non_usage"]["Negation"] == pytest.approx(-6.12, abs=0.01)
    assert a["non_usage"]["Credit"] == pytest.approx(-1.0, abs=0.01)                # credits do not include negations
    assert not any(c.kind.value == "discount_or_credit" for c in doc.charges if line_type(c) == "SavingsPlanNegation")


def test_cur_effective_cost_only_from_the_amortized_columns(pipe):
    _, doc = parse(pipe, FILES / "aws_cur2_commitments.csv.gz")
    assert "effective" in doc.cost_types
    usage = [c for c in doc.charges if c.kind.value == "usage"]
    assert all(c.effective_cost is not None and c.effective_cost.origin == "printed" for c in usage)
    ri = next(c for c in usage if line_type(c) == "DiscountedUsage")
    sp = next(c for c in usage if line_type(c) == "SavingsPlanCoveredUsage")
    assert ri.effective_cost.value == Decimal("3.6") and sp.effective_cost.value == Decimal("1.44")
    for c in doc.charges:                       # spread into the usage they cover: 0, computed
        if line_type(c) == "SavingsPlanNegation":
            assert c.effective_cost.value == 0 and c.effective_cost.origin == "inferred"
            assert c.effective_cost.source == "derived"
        if line_type(c) in ("SavingsPlanRecurringFee", "RIFee"):   # their unused part is not in this file: unknown
            assert c.effective_cost is None

    _, bare = parse(pipe, FILES / "aws_cur2_no_amortized.csv.gz")
    assert "effective" not in bare.cost_types and bare.cost_types == ["billed", "list"]
    assert all(c.effective_cost is None for c in bare.charges)


def test_commitment_fees_keep_their_kind_and_get_coverage(pipe):
    _, doc = parse(pipe, FILES / "aws_cur2_commitments.csv.gz")
    fees = {line_type(c): c for c in doc.charges if c.kind.value == "commitment_fee"}
    assert value(fees["RIFee"].coverage) == "reserved"
    assert value(fees["SavingsPlanRecurringFee"].coverage) == "savings_plan"


def test_lines_that_are_not_usage_have_no_coverage(pipe):
    _, doc = parse(pipe, FILES / "aws_cur2_commitments.csv.gz")
    for c in doc.charges:
        if c.kind.value in ("tax", "discount_or_credit"):
            assert c.coverage is None


def test_one_source_column_feeds_several_fields(pipe):
    _, doc = parse(pipe, FILES / "aws_cur2_commitments.csv.gz")
    c = next(c for c in doc.charges if line_type(c) == "SavingsPlanCoveredUsage")
    assert c.kind.value == "usage" and line_type(c) == "SavingsPlanCoveredUsage" and value(c.coverage) == "savings_plan"
    assert c.attributes.get("charge_type") == "Usage"


# ---------------------------------------------------------------- Azure
def test_azure_amortized_and_actual_exports(pipe):
    _, am = parse(pipe, FILES / "azure_amortized.csv")
    _, ac = parse(pipe, FILES / "azure_actual.csv")
    assert am.cost_basis == "amortized" and ac.cost_basis == "actual"

    def by_model(doc, model):
        return [c for c in doc.charges if value(c.pricing_model) == model and line_type(c) == "Usage"]
    for doc in (am, ac):                        # on demand and Spot: effective = cost in either export
        for c in by_model(doc, "OnDemand") + by_model(doc, "Spot"):
            assert c.effective_cost.value == c.cost.value and c.effective_cost.origin == "printed"
    assert all(c.effective_cost.value == c.cost.value != 0 for c in by_model(am, "Reservation"))
    assert all(c.effective_cost is None for c in by_model(ac, "Reservation"))      # the export shows 0, not amortized
    assert {value(c.coverage) for c in by_model(am, "Reservation")} == {"reserved"}
    assert {value(c.coverage) for c in by_model(am, "Spot")} == {"spot"}
    unused = next(c for c in ac.charges if line_type(c) == "UnusedReservation")
    assert value(unused.commitment_status) == "unused" and value(unused.coverage) == "reserved"


def test_azure_list_and_contracted_cost_are_computed_from_printed_prices(pipe):
    _, doc = parse(pipe, FILES / "azure_amortized.csv")
    c = next(c for c in doc.charges if value(c.pricing_model) == "OnDemand")
    assert c.list_cost.value == Decimal("4.8") and c.list_cost.origin == "inferred" and c.list_cost.source == "derived"
    assert c.contracted_cost.value == Decimal("4.32") and c.contracted_cost.origin == "inferred"
    assert doc.cost_types == ["billed", "list", "contracted", "effective"]


def test_azure_without_pricing_model_has_no_effective_cost(pipe):
    _, doc = parse(pipe, FILES / "azure_ea_usage.csv")
    assert doc.cost_basis is None and "effective" not in doc.cost_types
    assert all(c.effective_cost is None for c in doc.charges)


# ---------------------------------------------------------------- FOCUS
def test_focus_cost_types_coverage_and_status(pipe):
    r, doc = parse(pipe, FILES / "focus_commitments.parquet")
    assert r["spec"]["route"] == "builtin"
    assert doc.cost_types == ["billed", "list", "contracted", "effective"]
    usage = [c for c in doc.charges if c.kind.value == "usage"]
    for c in usage:
        for f in ("list_cost", "contracted_cost", "effective_cost"):
            assert getattr(c, f).origin == "printed"
    got = {(value(c.coverage), value(c.commitment_status)) for c in usage}
    assert got == {("on_demand", None), ("savings_plan", "used"), ("reserved", "used"), ("spot", None),
                   ("savings_plan", "unused")}
    purchase = next(c for c in doc.charges if c.kind.value == "commitment_fee")
    assert value(purchase.coverage) == "savings_plan"
    assert next(c for c in doc.charges if c.kind.value == "tax").coverage is None


# ---------------------------------------------------------------- GCP
def test_gcp_committed_use_discount_credits(pipe):
    _, doc = parse(pipe, FILES / "gcp_commitments.json")
    cov = {str(c.description.value).split(" running")[0]: c.coverage for c in doc.charges if c.kind.value == "usage"}
    assert cov["N2 Instance Core"].value == "committed_use" and cov["N2 Instance Core"].origin == "printed"
    assert cov["Spot Preemptible N2 Instance Core"].value == "spot"
    assert cov["Spot Preemptible N2 Instance Core"].origin == "inferred"            # from the SKU's words
    assert cov["E2 Instance Core"].value == "unknown"                                 # a sustained use credit is no commitment
    assert "list" in doc.cost_types and all(c.list_cost.origin == "printed" for c in doc.charges
                                            if c.kind.value == "usage")


# ---------------------------------------------------------------- files with no coverage column
def test_coverage_from_the_words_of_a_line(pipe, tmp_path):
    f = tmp_path / "custom.csv"
    pd.DataFrame([
        {"Service": "EC2", "Description": "Savings Plan covered m5.xlarge hours", "Amount": 10.0},
        {"Service": "EC2", "Description": "On Demand Linux m5.xlarge Instance Hour", "Amount": 20.0},
        {"Service": "EC2", "Description": "Spot m5.xlarge Instance Hour", "Amount": 5.0},
        {"Service": "S3", "Description": "Standard storage", "Amount": 7.0},
        {"Service": "Tax", "Description": "Sales tax", "Amount": 3.0},
    ]).to_csv(f, index=False)
    _, doc = parse(pipe, f)
    cov = {str(c.description.value).split(" ")[0]: c.coverage for c in doc.charges}
    assert {k: v.value for k, v in cov.items() if v} == {"Savings": "savings_plan", "On": "on_demand",
                                                          "Spot": "spot", "Standard": "unknown"}
    assert cov["Savings"].origin == "inferred" and cov["Savings"].source == "text"
    assert cov["Sales"] is None
    assert doc.cost_types == ["billed"]


# ---------------------------------------------------------------- roll-up, schema, expressions
def test_rollup_keeps_coverage_apart_and_sums_effective_cost(pipe, monkeypatch):
    monkeypatch.setattr(contract_mod, "MAX_CHARGES", 5)
    _, doc = parse(pipe, FILES / "aws_cur2_commitments.csv.gz")
    assert any("rolled up" in str(c.attributes) or c.attributes.get("lines") for c in doc.charges)
    ri = [c for c in doc.charges if line_type(c) == "DiscountedUsage"]
    assert len(ri) == 1 and ri[0].attributes.get("lines") == 3 and value(ri[0].coverage) == "reserved"
    assert ri[0].effective_cost.value == pytest.approx(Decimal("10.8"))
    od = [c for c in doc.charges if line_type(c) == "Usage" and value(c.coverage) == "on_demand"]
    assert od and all(value(c.coverage) != "reserved" for c in od)
    fees = [c for c in doc.charges if c.kind.value == "tax"]
    assert fees and all(c.effective_cost is not None for c in fees)


def test_schema_version():
    assert SCHEMA_VERSION >= "1.4"                    # 1.4 added the cost types (task 03 moved it to 1.5)


def test_cur_effective_function():
    rows = pd.DataFrame({"line_type_raw": ["Usage", "DiscountedUsage", "SavingsPlanCoveredUsage",
                                           "SavingsPlanNegation", "Tax"],
                         "billed_cost": [1.0, 0.0, 2.0, -2.0, 0.5]})
    raw = pd.DataFrame({"reservation_effective_cost": [0, 3.0, 0, 0, 0],
                        "savings_plan_savings_plan_effective_cost": [0, 0, 1.5, 0, 0]})
    e = 'cur_effective(line_type_raw, billed_cost, col("reservation_effective_cost"), ' \
        'col("savings_plan_savings_plan_effective_cost"))'
    out = evaluate(e, rows, raw)
    assert out.tolist() == [1.0, 3.0, 1.5, 0.0, 0.5]
    assert out.attrs["how"].tolist() == ["printed", "printed", "printed", "derived", "printed"]
    assert evaluate(e, rows, pd.DataFrame(index=rows.index)).isna().all()     # no amortized columns: no value


def test_unknown_post_step_is_rejected():
    spec = json.loads((ROOT / "specs" / "builtin" / "azure-usage-details.json").read_text())
    spec["meta"]["post"] = ["no_such_step"]
    with pytest.raises(SpecError):
        check_spec(spec)


# ---------------------------------------------------------------- more formats
def test_azure_cost_analysis_export_reads_pricing_model_and_charge_type(pipe, tmp_path):
    f = tmp_path / "azure-cost-by-resource.csv"
    pd.DataFrame([
        {"ServiceName": "Virtual Machines", "Meter": "D4s v5", "CostUSD": 50.0, "SubscriptionName": "Prod",
         "PricingModel": "OnDemand", "ChargeType": "Usage", "UnitOfMeasure": "1 Hour", "Quantity": 240},
        {"ServiceName": "Virtual Machines", "Meter": "D8s v5", "CostUSD": 0.0, "SubscriptionName": "Prod",
         "PricingModel": "Reservation", "ChargeType": "Usage", "UnitOfMeasure": "1 Hour", "Quantity": 240},
        {"ServiceName": "Virtual Machines", "Meter": "D2s v5", "CostUSD": 0.0, "SubscriptionName": "Prod",
         "PricingModel": "SavingsPlan", "ChargeType": "Usage", "UnitOfMeasure": "1 Hour", "Quantity": 240},
        {"ServiceName": "Virtual Machines", "Meter": "Reserved VM Instance", "CostUSD": 900.0,
         "SubscriptionName": "Prod", "PricingModel": "Reservation", "ChargeType": "Purchase",
         "UnitOfMeasure": "1 Hour", "Quantity": 1},
    ]).to_csv(f, index=False)
    r, doc = parse(pipe, f)
    assert r["spec"]["id"] == "azure-cost-analysis-export"
    by_meter = {str(c.description.value): c for c in doc.charges}
    cov = {str(c.description.value): value(c.coverage) for c in doc.charges}
    assert {c.kind.value for c in doc.charges if value(c.line_type_raw) == "Purchase"} == {"commitment_fee"}
    assert r["analysis"]["usage_spend"] == pytest.approx(50.0)                 # the purchase is not usage
    assert sorted(v for v in cov.values() if v) == ["on_demand", "reserved", "reserved", "savings_plan"]
    # an actual-cost export (covered usage costs 0, the purchase is a row): effective cost only where it is known
    assert doc.cost_basis == "actual"
    assert by_meter["D4s v5"].effective_cost.value == 50 and by_meter["D8s v5"].effective_cost is None


def test_a_savings_plans_service_is_a_commitment_fee_not_covered_usage(pipe, tmp_path):
    """Cost Explorer prints the Savings Plan fee as a service column ("Savings Plans for  Compute usage")."""
    f = tmp_path / "ce.csv"
    f.write_text('Service,EC2-Instances($),Savings Plans for  Compute usage($),Tax($),Total costs($)\n'
                 'Service total,100.0,40.0,5.0,145.0\n'
                 '2026-07-01,100.0,40.0,5.0,145.0\n')
    r, doc = parse(pipe, f)
    sp = [c for c in doc.charges if "Savings Plans" in str(value(c.context.service) or c.description.value)]
    assert sp and {c.kind.value for c in sp} == {"commitment_fee"}
    assert r["analysis"]["usage_spend"] == pytest.approx(100.0)
    ec2 = [c for c in doc.charges if c.kind.value == "usage"]
    assert ec2 and all(value(c.coverage) == "unknown" for c in ec2)


def test_a_support_charge_has_no_coverage(pipe, tmp_path):
    f = tmp_path / "ce.csv"
    f.write_text('Service,EC2-Instances($),Support (Business)($),Total costs($)\n'
                 'Service total,100.0,10.0,110.0\n'
                 '2026-07-01,100.0,10.0,110.0\n')
    _, doc = parse(pipe, f)
    support = [c for c in doc.charges if c.kind.value == "support_fee"]
    assert support and all(c.coverage is None for c in support)


# ---------------------------------------------------------------- review fixes
NEW_SAMPLES = ["aws_cur2_commitments.csv.gz", "aws_cur2_no_amortized.csv.gz", "azure_amortized.csv",
               "azure_actual.csv", "focus_commitments.parquet", "gcp_commitments.json"]


@pytest.mark.parametrize("name", NEW_SAMPLES)
def test_new_samples_are_not_contradicted(pipe, name):
    """A Savings Plan negation prints quantity x on-demand rate but cancels it: it is no line-math failure."""
    r, doc = parse(pipe, FILES / name)
    assert r["completeness"] != "contradicted" and doc.completeness != "contradicted", r["checks"]
    assert next(c for c in r["checks"] if c["name"] == "line_math")["status"] != "fail"


def test_cost_types_lists_only_what_every_usage_line_has(pipe):
    _, actual = parse(pipe, FILES / "azure_actual.csv")
    assert "effective" not in actual.cost_types            # reservation usage has no effective cost in this export
    assert actual.cost_types == ["billed", "list", "contracted"]
    _, amortized = parse(pipe, FILES / "azure_amortized.csv")
    assert "effective" in amortized.cost_types


def test_text_that_says_on_demand_wins_over_commitment_words(pipe):
    """The reseller PDF: "reserved capacity not applied, billed at pay-as-you-go rates" is on-demand usage."""
    _, doc = parse(pipe, FILES / "reseller_invoice.pdf")
    line = next(c for c in doc.charges if "not applied" in str(c.description.value))
    assert value(line.coverage) == "on_demand" and line.coverage.source == "text"


@pytest.mark.parametrize("name", sorted(p.name for p in FILES.iterdir() if p.is_file() and not p.name.startswith(".")))
def test_no_sample_gets_a_commitment_from_words_that_deny_it(pipe, name):
    """Run the text rules over every sample: a line read as covered by a commitment from its words must not also
    say it was billed at on-demand or pay-as-you-go rates."""
    _, doc = parse(pipe, FILES / name)
    for c in doc.charges:
        if c.coverage is not None and c.coverage.source == "text" and c.coverage.value in COMMITTED:
            text = str(c.description.value).lower()
            assert not any(w in text for w in ("pay-as-you-go", "pay as you go", "on demand", "not applied")), text


@pytest.mark.parametrize("name", sorted(p.name for p in FILES.iterdir() if p.is_file() and not p.name.startswith(".")))
def test_every_sample_fits_the_strict_contract(pipe, name):
    """POST /parse returns export.to_schema(document): a new kind or field must not break it."""
    from billparse.export import to_schema
    _, doc = parse(pipe, FILES / name)
    out = to_schema(doc)
    assert out.schema_version == SCHEMA_VERSION and len(out.charges) == len(doc.charges)


def test_map_unique_is_the_same_as_map():
    """Task 02: map_unique computes f once per distinct value; values and inferred type are those of Series.map."""
    import numpy as np

    from billparse.transforms import map_unique
    s = pd.Series(np.array(["a", "b", None, "a", float("nan"), " c "] * 1000, dtype=object))
    for f in (lambda v: {"a": 1.5}.get(v), lambda v: v.strip() if isinstance(v, str) else None,
              lambda v: v == "a", lambda v: 1 if v == "a" else "x", lambda v: "none" if v is None else str(v)):
        assert map_unique(s, f).equals(s.map(f))
