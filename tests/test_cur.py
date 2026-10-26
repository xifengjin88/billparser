"""AWS CUR, legacy (lineItem/... names) and 2.0 (snake_case, with the "product" map): every field the specs fill
comes from the column of its exact name, never from a guess; the fee lines' unused commitment is effective cost."""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

import billparse.analyze as analyze_mod
from billparse import Pipeline, Settings
from billparse.schema import Document

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "samples" / "files"
CUR = {"aws_cur_legacy.csv.gz": "aws-cur-legacy", "aws_cur2_product_map.csv.gz": "aws-cur2"}


@pytest.fixture
def pipe(tmp_path):
    (tmp_path / "data" / "raw").mkdir(parents=True)
    for f in (ROOT / "data" / "raw").glob("*.json"):
        shutil.copy(f, tmp_path / "data" / "raw" / f.name)
    analyze_mod._RESOLVER = None
    yield Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=False))
    analyze_mod._RESOLVER = None


def parse(pipe: Pipeline, name: str) -> tuple[dict, Document]:
    r = pipe.parse_file(FILES / name)
    assert r["spec"]["id"] == CUR[name] and r["spec"]["route"] == "builtin", r["attempts"]
    return r, Document.model_validate_json(pipe.store.part(r["sha256"], "doc"))


def v(cv):
    return None if cv is None else cv.value


def by_type(doc: Document, typ: str) -> list:
    return [c for c in doc.charges if v(c.line_type_raw) == typ]


@pytest.mark.parametrize("name", sorted(CUR))
def test_columns_by_their_exact_names(pipe, name):
    r, doc = parse(pipe, name)
    assert r["analysis"]["total_billed"] == pytest.approx(26.24, abs=0.01)
    ec2 = next(c for c in by_type(doc, "Usage") if "BoxUsage" in str(v(c.usage_type)))
    assert v(ec2.instance_type) == "m5.xlarge" and v(ec2.operating_system) == "Linux"
    assert v(ec2.sku_id) == "RVS26QBX9XP34QN6"
    assert v(ec2.sku_price_id) == "RVS26QBX9XP34QN6.JRTCKXETXF.6YS6EN2CT7"
    assert v(ec2.pricing_term) == "OnDemand" and ec2.pricing_model is None and v(ec2.context.region) == "us-east-1"
    ri = by_type(doc, "DiscountedUsage")[0]
    assert (v(ri.instance_type), v(ri.database_engine), v(ri.deployment_option)) == ("db.r6g.large", "PostgreSQL",
                                                                                     "Multi-AZ")
    assert v(ri.commitment_id).endswith("reserved-instances/1d3fbc13-f181-4c40-9dd6-12b345678de9")
    sp = by_type(doc, "SavingsPlanCoveredUsage")[0]
    assert "savingsplan/" in v(sp.commitment_id)
    # the region code is empty on data transfer: the region column gives it
    out = next(c for c in by_type(doc, "Usage") if "Out-Bytes" in str(v(c.usage_type)))
    assert v(out.context.region) == "us-west-2"


@pytest.mark.parametrize("name", sorted(CUR))
def test_the_sku_is_never_an_instance_type(pipe, name):
    _, doc = parse(pipe, name)
    fee = by_type(doc, "RIFee")[0]
    assert v(fee.sku_id) == "RVS26QBX9XP34QN6"
    assert v(fee.instance_type) != "RVS26QBX9XP34QN6"
    assert all(v(c.instance_type) in (None, "m5.xlarge", "db.r6g.large", "c5.large") for c in doc.charges)


@pytest.mark.parametrize("name", sorted(CUR))
def test_unused_commitment_is_effective_cost(pipe, name):
    _, doc = parse(pipe, name)
    assert by_type(doc, "RIFee")[0].effective_cost.value == Decimal("3.6")                # 1.00 + 2.60 unused
    assert by_type(doc, "SavingsPlanRecurringFee")[0].effective_cost.value == Decimal("1.44")   # 4.32 - 2.88
    assert all(c.effective_cost.value == 0 for c in by_type(doc, "SavingsPlanNegation"))
    # amortized total = covered usage at its effective cost + on demand + unused commitment + tax
    total = sum((c.effective_cost.value for c in doc.charges if c.effective_cost), Decimal(0))
    assert total == pytest.approx(Decimal("2") * (Decimal("4.608") + Decimal("0.2") + Decimal("3.6") + Decimal("1.44"))
                                  + Decimal("3.6") + Decimal("1.44") + Decimal("1.5"))


def test_columns_the_spec_does_not_name_are_not_guessed(pipe):
    r, doc = parse(pipe, "aws_cur_legacy.csv.gz")
    rows = pipe.store.get_rows(r["sha256"])
    assert "_extra" not in rows                              # an export's other columns are not read (no extra text)
    guessed = {"account_name", "commitment_type", "credits"} & set(rows.columns)   # names no column of this file
    assert not guessed, guessed
