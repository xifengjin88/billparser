"""Task 04: the analysis gives facts about commitment-eligible usage (covered, on demand, unused) and no savings
estimate, the same on the normal path, with roll-ups, and on the large path."""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
import pytest

import billparse.analyze as analyze_mod
import billparse.contract as contract_mod
from billparse import Pipeline, Settings
from billparse.analyze import COVERAGES, analyze, instance_family
from billparse.schema import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "samples" / "files"
UNUSED = {"aws_cur2_commitments.csv.gz": None, "aws_cur_legacy_full.csv.gz": 5.04, "azure_amortized.csv": 0.44,
          "focus_commitments.parquet": 0.30, "azure_actual.csv": None, "aws_cur2_no_amortized.csv.gz": None}


def _pipe(tmp_path: Path, name: str = "store") -> Pipeline:
    raw = tmp_path / "data" / "raw"
    if not raw.exists():
        raw.mkdir(parents=True)
        for f in (ROOT / "data" / "raw").glob("*.json"):
            shutil.copy(f, raw / f.name)
    analyze_mod._RESOLVER = None
    return Pipeline(Settings(store_dir=tmp_path / name, catalog_dir=tmp_path / "data", use_llm=False,
                             use_agent=False))


def eligible(tmp_path: Path, name: str, store: str = "store") -> dict:
    return _pipe(tmp_path, store).parse_file(FILES / name)["analysis"]["commitment_eligible_usage"]


def test_schema_version():
    assert SCHEMA_VERSION == "1.6"


def test_covered_usage_is_under_its_commitment(tmp_path):
    e = eligible(tmp_path, "aws_cur2_commitments.csv.gz")
    assert e["cost_type"] == "effective"
    assert e["by_category"]["Databases"] == {"reserved": 10.8, "total": 10.8,      # the RDS line an RI covers
                                             "list_cost": {"reserved": 18.0}, "quantity": {"reserved": {"Hrs": 72.0}}}
    compute = e["by_category"]["Compute"]
    assert compute["savings_plan"] == 4.32 and compute["spot"] == 4.32 and compute["on_demand"] == 13.82
    fams = {(g["instance_family"], tuple(k for k in g if k in COVERAGES)) for g in e["by_group"]}
    assert ("db.r6g", ("reserved",)) in fams and ("c5", ("savings_plan",)) in fams


@pytest.mark.parametrize("name", sorted(UNUSED))
def test_unused_commitment(tmp_path, name):
    u = eligible(tmp_path, name)["unused_commitment_spend"]
    if UNUSED[name] is None:
        assert u["total"] is None and u["lines"] >= 1 and u["note"]          # unknown is null, never 0
    else:
        assert u["total"] == pytest.approx(UNUSED[name], abs=0.005)
        assert sum(u["by_type"].values()) == pytest.approx(u["total"], abs=0.01)


def test_unused_spend_is_not_counted_as_covered_usage(tmp_path):
    e = eligible(tmp_path, "focus_commitments.parquet")                    # the unused line is Usage, savings_plan
    assert e["by_category"]["Compute"]["savings_plan"] == 4.32              # 3 x 1.44, without the 0.30 unused


@pytest.mark.parametrize("name", ["aws_cur2_commitments.csv.gz", "azure_actual.csv", "azure_amortized.csv",
                                  "focus_commitments.parquet", "aws_cur_legacy_full.csv.gz"])
def test_splits_add_up(tmp_path, name):
    e = eligible(tmp_path, name)
    for split in [*e["by_category"].values(), *e["by_group"]]:
        assert sum(v for k, v in split.items() if k in COVERAGES) == pytest.approx(split["total"], abs=0.02)
    assert sum(s["total"] for s in e["by_category"].values()) == \
        pytest.approx(sum(g["total"] for g in e["by_group"]), abs=0.02)


def _close(a, b, path=""):
    if isinstance(a, float) and isinstance(b, float):
        assert abs(a - b) <= 0.011, path
    elif isinstance(a, dict):
        assert a.keys() == b.keys(), path
        for k in a:
            _close(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        key = lambda g: str(sorted((k, v) for k, v in g.items() if not isinstance(v, float)))  # noqa: E731
        a, b = sorted(a, key=key), sorted(b, key=key)
        assert len(a) == len(b), path
        for x, y in zip(a, b, strict=True):
            _close(x, y, path + "[]")
    else:
        assert a == b, (path, a, b)


@pytest.mark.parametrize("name", ["aws_cur2_commitments.csv.gz", "azure_actual.csv", "azure_amortized.csv",
                                  "focus_commitments.parquet", "aws_cur_legacy_full.csv.gz"])
def test_the_same_on_the_large_path_and_with_roll_ups(tmp_path, monkeypatch, name):
    small = eligible(tmp_path, name, "small")
    with monkeypatch.context() as m:
        m.setattr(contract_mod, "MAX_CHARGES", 2)                          # the contract rolls up too
        _close(small, eligible(tmp_path, name, "rolled"))
    monkeypatch.setenv("LARGE_FILE_MB", "0.000001")
    monkeypatch.setenv("CHUNK_ROWS", "3")
    _close(small, eligible(tmp_path, name, "large"))


def _keys(x, path=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield f"{path}.{k}", k, v
            yield from _keys(v, f"{path}.{k}")
    elif isinstance(x, list):
        for v in x:
            yield from _keys(v, path + "[]")


@pytest.mark.parametrize("name", ["aws_cur2_commitments.csv.gz", "focus_commitments.parquet", "azure_amortized.csv"])
def test_no_savings_or_discount_estimate(tmp_path, name):
    a = _pipe(tmp_path).parse_file(FILES / name)["analysis"]
    for path, k, _v in _keys(a):
        if k in COVERAGES:                       # "savings_plan" is a commitment kind the file prints, not a saving
            continue
        assert "discount" not in k.lower() and "saving" not in k.lower(), path
        assert "assume" not in k.lower() and "indicative" not in k.lower(), path
    old = a["commitment_opportunity"]
    assert old["deprecated"] is True and old["replaced_by"] == "commitment_eligible_usage"


def test_the_forecast_excludes_negations():
    rows = pd.DataFrame({"billed_cost": [10.0, 5.0, -5.0], "charge_type": ["Usage", "Usage", "Negation"],
                         "service": ["EC2"] * 3, "charge_start": ["2026-09-01"] * 3})
    with_neg = analyze(rows, 0.9, "unreconciled", {})
    without = analyze(rows[rows["charge_type"] != "Negation"], 0.9, "unreconciled", {})
    assert with_neg["usage_spend"] == 15.0 and with_neg["forecast"] == without["forecast"]


def test_instance_family():
    assert [instance_family(v) for v in ("m5.xlarge", "db.r6g.large", "c7g.8xlarge.search", "Standard_D4s_v5",
                                         "Standard_E8ds_v5", "n2-standard-4", None)] == \
        ["m5", "db.r6g", "c7g", "Standard_Dsv5", "Standard_Edsv5", "n2", None]


# ---------------------------------------------------------------- the commitments table
def _doc(tmp_path, name: str, store: str = "store"):
    from billparse.schema import Document
    p = _pipe(tmp_path, store)
    r = p.parse_file(FILES / name)
    return Document.model_validate_json(p.store.part(r["sha256"], "doc"))


@pytest.mark.parametrize("large", [False, True])
def test_commitments_have_term_payment_and_unused(tmp_path, monkeypatch, large):
    if large:
        monkeypatch.setenv("LARGE_FILE_MB", "0.000001")
        monkeypatch.setenv("CHUNK_ROWS", "3")
    doc = _doc(tmp_path, "aws_cur_legacy_terms.csv.gz", "large" if large else "store")
    by = {str(k.commitment_type.value): k for k in doc.commitments}
    ri, sp = by["reserved_instance"], by["savings_plan"]
    assert (ri.term.value, ri.payment_option.value) == ("1yr", "No Upfront")
    assert (sp.term.value, sp.payment_option.value) == ("3yr", "Partial Upfront")
    assert float(ri.unused.value) == pytest.approx(3.6) and float(sp.unused.value) == pytest.approx(1.44)
    assert float(ri.amount.value) == pytest.approx(10.8) and float(sp.amount.value) == pytest.approx(4.32)


def test_unused_is_null_when_the_file_does_not_print_it(tmp_path):
    doc = _doc(tmp_path, "aws_cur2_no_amortized.csv.gz")
    assert doc.commitments and all(k.unused is None for k in doc.commitments)
    focus = {str(k.commitment_type.value): k for k in _doc(tmp_path, "focus_commitments.parquet", "f").commitments}
    assert float(focus["savings_plan"].unused.value) == pytest.approx(0.30)


def test_a_cur_without_the_unused_columns_never_reports_zero_unused(tmp_path):
    # amortized columns but no reservation_unused_* / savings plan commitment columns: unknown, not "nothing wasted"
    u = eligible(tmp_path, "aws_cur2_commitments.csv.gz")["unused_commitment_spend"]
    assert u["total"] is None and u["note"]
    assert all(k.unused is None for k in _doc(tmp_path, "aws_cur2_commitments.csv.gz", "d").commitments)


@pytest.mark.parametrize("name", ["azure_actual.csv", "aws_cur2_no_amortized.csv.gz"])
def test_covered_usage_billed_at_zero_keeps_its_size(tmp_path, name):
    e = eligible(tmp_path, name)
    assert e["cost_type"] == "billed" and "list_cost and quantity" in e["note"]
    covered = {"azure_actual.csv": ("Compute", "reserved", 28.8, "1 Hour"),
               "aws_cur2_no_amortized.csv.gz": ("Databases", "reserved", 18.0, "Hrs")}[name]
    cat, cov, listed, unit = covered
    split = e["by_category"][cat]
    assert split.get(cov, 0.0) == 0.0                       # billed at 0
    assert split["list_cost"][cov] == pytest.approx(listed) and split["quantity"][cov][unit] == 72.0
