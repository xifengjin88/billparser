"""End-to-end tests. A FakeLLM stands in for the API so LLM routes are tested offline."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "samples"))

import billparse.analyze as analyze_mod  # noqa: E402
from billparse import Pipeline, Settings  # noqa: E402
from billparse.learn import regression_gate, run_nightly  # noqa: E402
from billparse.llm import redact  # noqa: E402
from billparse.spec import SpecError, check_spec  # noqa: E402
from billparse.transforms import parse_number  # noqa: E402
from make_samples import german_xlsx  # noqa: E402

FILES = ROOT / "samples" / "files"

GERMAN_SPEC = {"source": "grid", "detect": {"required": ["Leistung", "Betrag"]},
               "meta": {"provider": "azure", "kind": "cost_lines", "cost_basis": "billed"},
               "table": {"end": "Gesamtbetrag"},
               "columns": {"service_raw": {"from": ["Leistung"]}, "quantity": {"from": ["Menge"]},
                           "unit_price": {"from": ["Einzelpreis"]}, "billed_cost": {"from": ["Betrag"]}},
               "filters": [{"drop_if_blank": "billed_cost"}]}


class FakeLLM:
    """Returns queued responses; records every prompt."""

    def __init__(self, spec_responses=None, extract_response=None):
        self.spec_responses = list(spec_responses or [])
        self.extract_response = extract_response
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        self.calls.append((system, user))
        if system.startswith("Extract every billable"):
            return json.dumps(self.extract_response or {"rows": []})
        resp = self.spec_responses.pop(0) if self.spec_responses else {"bad": True}
        return resp if isinstance(resp, str) else json.dumps(resp)


@pytest.fixture
def make_pipe(tmp_path):
    (tmp_path / "data" / "raw").mkdir(parents=True)
    for f in (ROOT / "data" / "raw").glob("*.json"):
        shutil.copy(f, tmp_path / "data" / "raw" / f.name)
    analyze_mod._RESOLVER = None

    def make(llm=None):
        return Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data",
                                 use_llm=llm is not None), llm=llm)
    yield make
    analyze_mod._RESOLVER = None


# ---------------------------------------------------------------- known and messy samples, no LLM
EXPECTED = {
    "aws_cur2.csv.gz": ("builtin", 4052.23), "azure_ea_usage.csv": ("builtin", None),
    "pc_invoice_recon.csv": ("builtin", 2742.51), "focus_gcp.parquet": ("builtin", None),
    "gcp_export.json": ("builtin", 1320.00), "aws_cost_explorer.csv": ("builtin", None),
    "messy_report.csv": ("mapper", 6438.05), "custom_workbook.xlsx": ("mapper", 1653.32),
    "spend_optimizer_pivot.xlsx": ("mapper", 54094.50), "reseller_invoice.pdf": ("mapper", 3414.88),
    "summary_invoice.pdf": ("mapper", 12513.18), "custom_api.json": ("mapper", 677.76),
    # task 01: commitments, negations and the cost types each export prints
    "aws_cur2_commitments.csv.gz": ("builtin", 34.76), "aws_cur2_no_amortized.csv.gz": ("builtin", 34.76),
    "azure_amortized.csv": ("builtin", 32.84), "azure_actual.csv": ("builtin", 177.16),
    "focus_commitments.parquet": ("builtin", 22.85), "gcp_commitments.json": ("builtin", 213.00),
    # task 02: CUR columns by their exact names, legacy and 2.0 (its "product" map)
    "aws_cur_legacy.csv.gz": ("builtin", 26.24), "aws_cur2_product_map.csv.gz": ("builtin", 26.24),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_samples_parse_without_llm(make_pipe, name):
    r = make_pipe().parse_file(FILES / name)
    route, total = EXPECTED[name]
    assert r["status"] in ("reconciled", "unreconciled"), r["checks"]
    assert r["spec"]["route"] == route
    assert r["llm_calls"] == 0
    if total is not None:
        assert r["analysis"]["total_billed"] == pytest.approx(total, abs=0.01)


def test_reseller_pdf_layout_details(make_pipe):
    pipe = make_pipe()
    r = pipe.parse_file(FILES / "reseller_invoice.pdf")
    rows = pipe.store.get_rows(r["sha256"])
    assert r["status"] == "reconciled" and len(rows) == 38           # spans two pages
    wrapped = rows[rows["description"].str.contains("pay-as-you-go", na=False)]
    assert len(wrapped) == 1                                          # continuation line merged
    assert set(rows["provider"].dropna()) == {"aws", "azure"}         # group headers became provider
    assert r["header_fields"] == {} or isinstance(r["header_fields"], dict)


def test_cached_second_upload(make_pipe):
    pipe = make_pipe()
    pipe.parse_file(FILES / "messy_report.csv")
    assert pipe.parse_file(FILES / "messy_report.csv").get("cached") is True


def test_cost_explorer_unpivot(make_pipe):
    pipe = make_pipe()
    r = pipe.parse_file(FILES / "aws_cost_explorer.csv")
    rows = pipe.store.get_rows(r["sha256"])
    assert len(rows) == 24 and r["status"] == "reconciled"
    assert "Total costs" not in " ".join(rows["service_raw"].astype(str))


# ---------------------------------------------------------------- LLM routes
def test_llm_spec_with_repair_loop(make_pipe, tmp_path):
    bad_transform = {**GERMAN_SPEC, "columns": {**GERMAN_SPEC["columns"],
                                                 "billed_cost": {"from": ["Betrag"], "t": ["parse_euro"]}}}
    no_label = {**GERMAN_SPEC, "columns": {"billed_cost": {"from": ["Betrag"]}}}
    llm = FakeLLM([bad_transform, no_label, GERMAN_SPEC])
    pipe = make_pipe(llm)
    f = tmp_path / "de.xlsx"
    german_xlsx(f)
    r = pipe.parse_file(f)
    assert r["status"] == "reconciled", r["checks"]
    assert r["spec"]["route"] == "llm_spec" and r["llm_calls"] == 3
    assert r["analysis"]["total_billed"] == pytest.approx(276.48 + 363.60 + 540.68 + 44.54, abs=0.01)
    # repair prompts carried the validator feedback
    assert "Unknown transform" in llm.calls[1][1] and "labels" in llm.calls[2][1]


def test_learned_spec_reused_without_llm(make_pipe, tmp_path):
    llm = FakeLLM([GERMAN_SPEC])
    pipe = make_pipe(llm)
    a, b = tmp_path / "sep.xlsx", tmp_path / "oct.xlsx"
    german_xlsx(a)
    german_xlsx(b, month="Oktober 2026", scale=1.1)
    pipe.parse_file(a)
    r = pipe.parse_file(b)
    assert r["spec"]["route"] == "learned" and r["llm_calls"] == 0
    assert r["status"] == "reconciled"


def test_extract_then_derive(make_pipe, tmp_path):
    f = tmp_path / "de.xlsx"
    german_xlsx(f)
    rows = [{"service_raw": "Virtual Machines D4s v5", "billed_cost": 276.48},
            {"service_raw": "Azure SQL Database", "billed_cost": 363.60},
            {"service_raw": "Storage Premium SSD", "billed_cost": 540.68},
            {"service_raw": "Bandwidth", "billed_cost": 44.54}]
    llm = FakeLLM(["not json", "still not json", "{}"], extract_response={"rows": rows, "total": 1225.30})
    pipe = make_pipe(llm)
    r = pipe.parse_file(f)
    assert r["spec"]["route"] == "llm_extract_derived", r["attempts"]
    assert r["analysis"]["total_billed"] == pytest.approx(1225.30, abs=0.01)
    # the derived spec is deterministic: the next file in this layout needs no LLM
    g = tmp_path / "de2.xlsx"
    german_xlsx(g, scale=1.2)
    r2 = pipe.parse_file(g)
    assert r2["spec"]["route"] == "learned" and r2["llm_calls"] == 0


# ---------------------------------------------------------------- nightly learning loop
def _seed_layout(make_pipe, tmp_path):
    pipe = make_pipe(FakeLLM([GERMAN_SPEC]))
    for i, scale in enumerate((1.0, 1.1)):
        f = tmp_path / f"m{i}.xlsx"
        german_xlsx(f, scale=scale)
        pipe.parse_file(f)
    spec_id = pipe.store.learned_specs()[0]["id"]
    # the reseller renames a column; the learned spec now misses it and no LLM is available by day
    changed = tmp_path / "changed.xlsx"
    german_xlsx(changed, amount_header="Betrag (EUR)", scale=1.3)
    day = make_pipe(None)
    r = day.parse_file(changed)
    assert r["status"] == "needs_review"
    return spec_id, changed


def test_nightly_repairs_spec_and_passes_gate(make_pipe, tmp_path):
    spec_id, changed = _seed_layout(make_pipe, tmp_path)
    fixed = {**GERMAN_SPEC, "detect": {"required": ["Leistung"]},
             "columns": {**GERMAN_SPEC["columns"], "billed_cost": {"from": ["Betrag (EUR)", "Betrag"]}}}
    night = make_pipe(FakeLLM([fixed]))
    report = run_nightly(night)
    repair = report["repairs"][0]
    assert repair["result"].startswith("promoted v2"), repair
    assert repair["gate"]["checked"] == 2 and repair["gate"]["passed"]
    assert report["review"][0]["now"] == "reconciled"
    assert not [r for r in night.store.read("review") if r.get("open")]


def test_regression_gate_rejects_breaking_fix(make_pipe, tmp_path):
    spec_id, _ = _seed_layout(make_pipe, tmp_path)
    breaking = {**GERMAN_SPEC, "detect": {"required": ["Leistung"]},
                "columns": {**GERMAN_SPEC["columns"], "billed_cost": {"from": ["Betrag (EUR)"]}}}
    night = make_pipe(FakeLLM([breaking]))
    report = run_nightly(night)
    assert report["repairs"][0]["result"] == "rejected by regression gate"
    assert night.store.versions(spec_id) == [1]


def test_candidate_promoted_after_clean_files(make_pipe, tmp_path):
    pipe = make_pipe(FakeLLM([GERMAN_SPEC]))
    for i in range(3):
        f = tmp_path / f"p{i}.xlsx"
        german_xlsx(f, scale=1 + i / 10)
        pipe.parse_file(f)
    report = run_nightly(pipe)
    assert report["promotions"] and report["promotions"][0]["promoted_to"] == "verified"
    spec = pipe.store.learned_specs()[0]
    assert spec["trust"] == "verified"
    assert regression_gate(pipe, spec, spec["id"])["passed"]


# ---------------------------------------------------------------- safety and units
def test_spec_safety():
    with pytest.raises(SpecError):
        check_spec({"columns": {"billed_cost": {"from": ["x"]}}, "exec": "rm -rf /"})
    with pytest.raises(Exception):
        check_spec({"columns": {"billed_cost": {"from": ["x"]}}, "derive": {"list_cost": "__import__('os')"}})
    with pytest.raises(SpecError):
        check_spec({"columns": {"made_up_field": {"from": ["x"]}}})


@pytest.mark.parametrize("raw,val", [("$1,234.50", 1234.5), ("(12.00)", -12.0), ("12.00-", -12.0),
                                     ("1.234,56 €", 1234.56), ("276,48 €", 276.48), ("1,234", 1234.0),
                                     ("", None), ("n/a", None), (7, 7.0)])
def test_parse_number(raw, val):
    assert parse_number(raw) == val


def test_redaction():
    text = redact("acct 123456789012 sub /subscriptions/aaaa-bbbb/rg ops@contoso.com")
    assert "123456789012" not in text and "contoso.com" not in text and "/subscriptions/" not in text


def test_api(make_pipe, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import billparse.api as api
    pipe = make_pipe()
    api.app.dependency_overrides[api.get_store] = lambda: pipe.store
    api.app.dependency_overrides[api.get_pipeline] = lambda: pipe
    try:
        client = TestClient(api.app)
        with open(FILES / "summary_invoice.pdf", "rb") as fh:
            r = client.post("/parse", files={"file": ("summary_invoice.pdf", fh, "application/pdf")})
        body = r.json()
        assert r.status_code == 200 and body["status"] == "auto_accepted" and body["completeness"] == "reconciled"
        assert body["document"]["charges"] and body["lane"] == "rules"
        assert client.get(f"/documents/{body['sha256']}").json()["sha256"] == body["sha256"]
        assert client.get("/specs").status_code == 200
        assert client.get("/health").json()["database"] == "sqlite"
    finally:
        api.app.dependency_overrides.clear()


def test_a_spec_keeps_the_columns_it_does_not_map():
    """A spec maps what it knows; the other columns are not lost: known headers fill empty fields (never a cost),
    the rest goes in the line's extra text."""
    from billparse.docmodel import load_bytes
    from billparse.spec import run
    csv = ("ServiceName,CostUSD,ChargeType,Quantity,UnitOfMeasure,Meter,ResourceGroup,TaxUSD\n"
           "Virtual Machines,10.5,Usage,24,1 Hour,D4s v5,rg-web,1.0\n"
           "SaaS,4.0,Purchase,,,,rg-mail,0\n")
    spec = {"source": "frame", "columns": {"service_raw": {"from": ["ServiceName"]}, "billed_cost": {"from": ["CostUSD"]}}}
    rows = run(spec, load_bytes(csv.encode(), "export.csv")).rows
    assert list(rows["billed_cost"]) == [10.5, 4.0] and "tax" not in rows
    assert list(rows["charge_type"]) == ["Usage", "Purchase"]
    assert rows["quantity"][0] == 24 and rows["unit"][0] == "1 Hour" and rows["description"][0] == "D4s v5"
    assert "ResourceGroup: rg-web" in rows["_extra"][0] and "TaxUSD: 1.0" in rows["_extra"][0]


def test_lines_carry_the_service_code_and_name(tmp_path):
    """Every matched line has the catalog's service code and name; a label no service matches has neither."""
    from billparse.analyze import resolve_services
    import pandas as pd
    rows = pd.DataFrame({"service_raw": ["Amazon Elastic Compute Cloud", "AmazonS3", "Team lunch"],
                         "billed_cost": [10.0, 5.0, 1.0]})
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    for f in (ROOT / "tests" / "fixtures" / "raw").glob("*.json"):
        shutil.copy(f, raw / f.name)
    analyze_mod._RESOLVER = None
    resolve_services(rows, tmp_path / "data")
    analyze_mod._RESOLVER = None
    assert list(rows["service_code"][:2]) == ["AmazonEC2", "AmazonS3"]
    assert list(rows["service_name"][:2]) == ["Amazon Elastic Compute Cloud", "Amazon Simple Storage Service"]
    assert pd.isna(rows["service_code"][2]) and pd.isna(rows["service_name"][2])
    assert rows["service"][2] == "Team lunch"                 # summaries still show the printed label


def test_quantity_and_unit_are_read_from_a_line_under_headings():
    """A bill line under its headings ("$0.0188 per ... 4,216.07 Hrs", path "EC2 > US East") gets every detail."""
    import pandas as pd

    from billparse.attributes import extract
    rows = pd.DataFrame({"description": ["$0.0188 per On Demand Linux t3a.small Instance Hour 4,216.07 Hrs"],
                         "billed_cost": [79.26], "_path": ["Elastic Compute Cloud > US East (N. Virginia)"]})
    extract(rows)
    r = rows.iloc[0]
    assert (r["quantity"], r["unit"], r["unit_price"]) == (4216.07, "Hrs", 0.0188)
    assert (r["instance_type"], r["operating_system"], r["pricing_model"]) == ("t3a.small", "Linux", "On Demand")


def test_a_price_per_block_of_units_is_a_price_per_unit():
    """"$0.0055 per 1,000 requests" is 0.0000055 a request, so quantity x unit price is the cost."""
    from billparse.attributes import from_text
    cases = [("$0.0055 per 1,000 PUT, COPY, POST, or LIST requests 7,275,083 Requests", 40.01),
             ("$0.60 per million requests processed 33,040,241 Request", 19.82),
             ("USD 0.22 per 1 million I/O requests for Aurora MySQL 33,384,496 IOs", 7.34),
             ("$0.03 per 10000 KMS requests in Asia Pacific (Mumbai) 1 Requests", 0.00),
             ("$0.0188 per On Demand Linux t3a.small Instance Hour 4,216.07 Hrs", 79.26)]
    for text, cost in cases:
        d = from_text(text)
        assert round(d["quantity"] * d["unit_price"], 2) == cost, (text, d)
    assert from_text(cases[0][0])["price_per"] == "$0.0055 per 1,000"


def test_a_month_written_as_the_description_becomes_the_period():
    """LLM rows from a table with a column per month: the month is the period, not the description."""
    import pandas as pd

    from billparse.pipeline import _periods_out_of_descriptions
    df = _periods_out_of_descriptions(pd.DataFrame({"service_raw": ["BigQuery", "BigQuery", "BigQuery"],
                                                    "description": ["2026-01", "Feb 2026", "Storage"],
                                                    "billed_cost": [1.0, 2.0, 3.0]}))
    assert list(df["charge_start"][:2]) == ["2026-01-01", "2026-02-01"] and df["charge_end"][1] == "2026-02-28"
    assert df["description"][:2].isna().all() and df["description"][2] == "Storage" and pd.isna(df["charge_start"][2])


def test_a_weak_match_to_another_provider_is_tried_again_within_the_files_provider():
    import pandas as pd

    from billparse.analyze import _one_provider
    from svcres.resolver import Resolution

    def hit(provider, method, service="S"):
        return Resolution(raw="x", service_key=f"{provider}:{service}", service=service, provider=provider, method=method)

    class Res:                                          # the provider's own catalog has no "Cloud Composer"
        def resolve_many(self, values, provider):
            return {v: Resolution(raw=v, service_key=None) for v in values}
    rows = pd.DataFrame({"service_raw": ["BigQuery", "Cloud SQL", "Cloud Storage", "Cloud Composer"]})
    picked = pd.Series([hit("gcp", "name"), hit("gcp", "name"), hit("gcp", "code"), hit("azure", "cache:prefix")])
    out = _one_provider(rows, picked, Res())
    assert [r.provider if r else None for r in out] == ["gcp", "gcp", "gcp", None]


def test_fees_keep_their_area_and_their_own_kind():
    """Support is a support fee on every bill; a Marketplace line is Marketplace money even when its name matches a
    service; a commitment with no service takes its area from its name; the category x kind table keeps them apart."""
    import pandas as pd

    from billparse.analyze import _by_category_and_type, _fees_and_areas
    rows = pd.DataFrame({
        "service": ["AWS Support (Business)", "Claude (Amazon Bedrock Edition)", None, "Amazon EC2", "Amazon EC2"],
        "description": ["Charges", "AWS Marketplace software usage", "Compute savings plan", "Usage", "Usage"],
        "category": ["Support", "AI + Machine Learning", None, "Compute", "Compute"],
        "charge_type": [None, None, "Purchase", None, "Tax"],
        "billed_cost": [100.0, 0.15, 30.0, 70.0, 7.0]})
    _fees_and_areas(rows)
    assert rows["charge_type"][0] == "Support" and pd.isna(rows["charge_type"][1]) and rows["charge_type"][2] == "Purchase"
    assert list(rows["category"][:3]) == ["Support", "Marketplace", "Compute"]
    table = {r["category"]: r for r in _by_category_and_type(rows)}
    assert table["Compute"] == {"category": "Compute", "commitment_fee": 30.0, "tax": 7.0, "usage": 70.0, "total": 107.0}
    assert table["Support"]["support_fee"] == 100.0 and table["Marketplace"]["marketplace"] == 0.15
