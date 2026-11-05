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

from make_samples import german_xlsx  # noqa: E402

import billparse.analyze as analyze_mod  # noqa: E402
import billparse.headers as headers_mod  # noqa: E402
from billparse import Pipeline, Settings  # noqa: E402
from billparse.expr import ExprError  # noqa: E402
from billparse.learn import regression_gate, run_nightly  # noqa: E402
from billparse.llm import redact  # noqa: E402
from billparse.spec import SpecError, check_spec, hoist_flags  # noqa: E402
from billparse.transforms import parse_number  # noqa: E402

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
    # task 03: every attribute, ID and commitment column filled
    "aws_cur_legacy_full.csv.gz": ("builtin", 26.24), "aws_cur2_flat.csv.gz": ("builtin", 26.24),
    "aws_cur2_map.csv.gz": ("builtin", 26.24), "azure_usage_full.csv": ("builtin", 55.98),
    "focus_full.parquet": ("builtin", 18.14), "gcp_export_full.json": ("builtin", 186.00),
    # task 04: commitment term and payment option
    "aws_cur_legacy_terms.csv.gz": ("builtin", 26.24),
    # task 10: German headers, read by the seed dictionary with no LLM (GERMAN_SPEC's LLM test turns the seed off)
    "german_invoice.xlsx": ("mapper", 1225.30),
    # an Archera Azure Cost Intelligence Report: its Service Breakdown sheet, found by the report's title
    "cost_report_workbook.xlsx": ("builtin", 9014.00),
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
@pytest.fixture
def no_seed(monkeypatch):
    """No seed dictionary: the German headers are a layout the mapper cannot read (the tests of the LLM routes and of
    learning; with the seed, the mapper reads the German sample: EXPECTED)."""
    monkeypatch.setattr(headers_mod, "load_seed", lambda path=None: {})


def test_llm_spec_with_repair_loop(make_pipe, tmp_path, monkeypatch, no_seed):
    monkeypatch.setenv("BILLPARSE_SPEC_CANDIDATES", "1")         # one candidate per round: the repair loop itself
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


def test_llm_spec_with_an_inline_flag_inside_a_regex(make_pipe, tmp_path, monkeypatch, no_seed):
    """An LLM writes "(?i)" inside a pattern, which Python refuses: the flag is moved to the start, not a failed
    attempt."""
    monkeypatch.setenv("BILLPARSE_SPEC_CANDIDATES", "1")
    flagged = {**GERMAN_SPEC, "table": {"end": "Gesamt(?i)betrag"},
               "filters": [{"drop_if_blank": "billed_cost"}, {"drop_if_match": {"field": "service_raw",
                                                                                "regex": "^(?i)summe|(?i)total"}}]}
    pipe = make_pipe(FakeLLM([flagged]))
    f = tmp_path / "de.xlsx"
    german_xlsx(f)
    r = pipe.parse_file(f)
    assert r["status"] == "reconciled" and r["llm_calls"] == 1, r["attempts"]
    assert not any(a["error"] for a in r["attempts"])


def test_learned_spec_reused_without_llm(make_pipe, tmp_path, no_seed):
    llm = FakeLLM([GERMAN_SPEC])
    pipe = make_pipe(llm)
    a, b = tmp_path / "sep.xlsx", tmp_path / "oct.xlsx"
    german_xlsx(a)
    german_xlsx(b, month="Oktober 2026", scale=1.1)
    pipe.parse_file(a)
    r = pipe.parse_file(b)
    assert r["spec"]["route"] == "learned" and r["llm_calls"] == 0
    assert r["status"] == "reconciled"


def test_extract_then_derive(make_pipe, tmp_path, no_seed):
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


def test_nightly_repairs_spec_and_passes_gate(make_pipe, tmp_path, no_seed):
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


def test_regression_gate_rejects_breaking_fix(make_pipe, tmp_path, no_seed):
    spec_id, _ = _seed_layout(make_pipe, tmp_path)
    breaking = {**GERMAN_SPEC, "detect": {"required": ["Leistung"]},
                "columns": {**GERMAN_SPEC["columns"], "billed_cost": {"from": ["Betrag (EUR)"]}}}
    night = make_pipe(FakeLLM([breaking]))
    report = run_nightly(night)
    assert report["repairs"][0]["result"] == "rejected by regression gate"
    assert night.store.versions(spec_id) == [1]


def test_candidate_promoted_after_clean_files(make_pipe, tmp_path, no_seed):
    pipe = make_pipe(FakeLLM([GERMAN_SPEC]))
    for i in range(3):
        f = tmp_path / f"p{i}.xlsx"
        german_xlsx(f, scale=1 + i / 10)
        r = pipe.parse_file(f)
        # promoted at parse time, by the third good file (not by a later nightly run)
        assert pipe.store.learned_specs()[0]["trust"] == ("verified" if i == 2 else "candidate")
    assert r["spec"]["trust"] == "verified"
    assert run_nightly(pipe)["promotions"] == []                      # nothing left for the nightly loop
    spec = pipe.store.learned_specs()[0]
    assert spec["trust"] == "verified"
    assert regression_gate(pipe, spec, spec["id"])["passed"]


# ---------------------------------------------------------------- safety and units
def test_spec_safety():
    with pytest.raises(SpecError):
        check_spec({"columns": {"billed_cost": {"from": ["x"]}}, "exec": "rm -rf /"})
    with pytest.raises(ExprError):                          # a call is not an expression a spec may use
        check_spec({"columns": {"billed_cost": {"from": ["x"]}}, "derive": {"list_cost": "__import__('os')"}})
    with pytest.raises(SpecError):
        check_spec({"columns": {"made_up_field": {"from": ["x"]}}})


def test_spec_regexes_are_checked_before_the_spec_runs():
    """Every regex of a spec is compiled by check_spec: a bad one is feedback for the LLM's next round, with where it
    is, not an error found when the spec runs."""
    base = {"source": "grid", "columns": {"billed_cost": {"from": ["x"]}}}
    for bad in ({"table": {"start": "Service("}}, {"filters": [{"drop_if_match": {"field": "f", "regex": "a(?i)"}}]},
                {"fields": {"due": {"anchor": "[", "take": "right_of"}}},
                {"columns": {"billed_cost": {"from": ["x"], "t": [{"regex": "("}]}}}):
        with pytest.raises(SpecError, match="bad regex"):
            check_spec({**base, **bad})
    check_spec({**base, "table": {"start": "(?i)^Service Monthly"}})
    assert hoist_flags("^Total(?i)") == "(?i)^Total"
    assert hoist_flags("(?i)a|(?i)b") == "(?i)a|b"
    assert hoist_flags("(?i)ok") == "(?i)ok" and hoist_flags("plain") == "plain"


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
    import pandas as pd

    from billparse.analyze import resolve_services
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
    # a block in letters: "10M", "MM", "100K" (upper case: "MB" and "Month" are units, not blocks)
    assert from_text("$0.35 per 10M metric samples 29,788,727,526 Metric")["unit_price"] == pytest.approx(0.35 / 1e7)
    assert from_text("$0.20 per MM objects 5 Objects")["unit_price"] == pytest.approx(0.20 / 1e6)
    assert from_text("$0.01 per 100K Objects 3 Objects")["unit_price"] == pytest.approx(0.01 / 1e5)
    assert from_text("$0.023 per GB-Month of storage 12 GB-Mo")["unit_price"] == 0.023


def test_a_price_from_the_words_that_does_not_give_the_cost_is_not_kept():
    """The words' price is kept only when quantity x price is the cost before tax: another unit (GiBps-mo against a
    price per MiBps-month) or a commitment that covered the usage leaves the unit price empty, and the words
    "On Demand" then do not make the line on demand."""
    import pandas as pd

    from billparse import coverage
    from billparse.attributes import extract
    rows = pd.DataFrame({
        "description": ["$0.0456 per GP3 MiBps-month 0.366 GiBps-mo",                  # another unit
                        "$0.085 per On Demand Linux c5.large Instance Hour 744 Hrs",    # a Savings Plan paid it
                        "$0.0188 per On Demand Linux t3a.small Instance Hour 100 Hrs",  # the price, with tax
                        "$0.002 per 1K tokens"],                                        # a quantity in blocks
        "billed_cost": [17.10, 0.0, 2.07, 0.03], "tax": [0.0, 0.0, 0.19, 0.0],
        "quantity": [0.366, None, None, 13.052], "unit": [None, None, None, "1K tokens"]})   # a PDF's columns
    extract(rows)
    assert rows["unit_price"].isna().tolist() == [True, True, False, False]
    assert rows.loc[3, "unit_price"] == pytest.approx(0.002)
    coverage.apply(rows)
    assert rows.loc[1, "coverage"] != "on_demand" and rows.loc[2, "coverage"] == "on_demand"


def test_an_unknown_printed_charge_type_is_usage_for_coverage():
    import pandas as pd

    from billparse import coverage
    rows = pd.DataFrame({"description": ["$1.352 per On Demand Linux m5.4xlarge Instance Hour", "Spot m5.large"],
                         "charge_type": ["LineItem", "LineItem"]})
    coverage.apply(rows)
    assert rows["coverage"].tolist() == ["on_demand", "spot"]


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


def _public(pipe, r):
    from billparse.export import to_schema
    from billparse.schema import Document
    return to_schema(Document.model_validate_json(pipe.store.part(r["sha256"], "doc")))


def test_a_file_with_no_cost_lines_tells_the_consumer_why(make_pipe):
    """Best effort: a part that fails says why in the public contract's confidence reasons, in plain words."""
    pipe = make_pipe()
    memo = b"Dear team,\nwe plan to move the reporting stack next quarter.\nRegards\n"
    out = _public(pipe, pipe.parse_bytes(memo, "memo.txt"))
    assert not out.charges and out.confidence.reasons[0].startswith("No cost lines were found in the file.")
    cut = b"PK\x03\x04" + b"\0" * 64                               # a download cut off: no zip directory
    out = _public(pipe, pipe.parse_bytes(cut, "cut.zip"))
    assert out.confidence.reasons[0].startswith("The file could not be opened: the archive is damaged")


def test_a_file_without_a_currency_is_usd_and_says_so(make_pipe):
    pipe = make_pipe()
    out = _public(pipe, pipe.parse_bytes(b"Service,Cost\nAmazon EC2,10.50\nAmazon S3,2.25\n", "costs.csv"))
    assert len(out.charges) == 2 and (out.currency.value, out.currency.origin) == ("USD", "inferred")
    assert "The file does not state its currency: USD is assumed." in out.confidence.reasons


def test_the_currency_in_a_cost_header_or_a_json_key_is_the_files_currency(make_pipe):
    pipe = make_pipe()
    out = _public(pipe, pipe.parse_bytes(b"ServiceName,CostUSD\nVirtual Machines,10.50\nStorage,2.25\n",
                                         "by-service.csv"))
    assert out.currency.value == "USD" and not any("currency" in r for r in out.confidence.reasons)
    data = (b'{"currency": "EUR", "items": [{"service": "Amazon EC2", "cost": 10.5}, '
            b'{"service": "Amazon S3", "cost": 2.25}]}')
    out = _public(pipe, pipe.parse_bytes(data, "cost.json"))
    assert len(out.charges) == 2 and out.currency.value == "EUR"


def test_a_line_in_several_regions_or_none_has_no_region_code():
    import pandas as pd

    from billparse.attributes import normalize_regions
    rows = pd.DataFrame({"region": ["US East", "global, US East", "Unassigned", "US Central"]})
    normalize_regions(rows)
    assert [None if pd.isna(v) else v for v in rows["region"]] == ["eastus", None, None, "centralus"]
    assert rows["region_name"].tolist() == ["US East", "global, US East", "Unassigned", "US Central"]


def test_a_list_of_vms_gets_its_provider_and_compute_service(make_pipe):
    """A VM list names no service and no provider: "Azure" in its headers and Azure sizes say Azure, and a line with
    an instance type is a Virtual Machines line (AWS: EC2, RDS for db.*)."""
    pipe = make_pipe()
    r = pipe.parse_bytes(b"Azure Region,Device Name,Operating System,Optimized Azure Profile\n"
                         b"eastus,app01,Windows,E2as_v7\neastus,app02,Linux,D4s_v5\nwestus2,db01,Linux,E8as_v7\n",
                         "vm-sizes.csv")
    rows = pipe.store.get_rows(r["sha256"])
    assert set(rows["provider"]) == {"azure"} and set(rows["service_name"]) == {"Virtual Machines"}
    from billparse.analyze import _compute_service
    assert [_compute_service("aws", t) for t in ("m5.large", "db.r6g.large", "cache.t3.micro")] == \
        ["Amazon EC2", "Amazon RDS", "Amazon ElastiCache"]


def test_a_number_format_names_the_currency_of_a_workbook():
    from billparse.docmodel import format_currency
    assert [format_currency(f) for f in ('[$$-409]#,##0.00', '"$"#,##0.00', '[$€-407] #,##0', '[$$-1009]#,##0',
                                         '#,##0.00', 'General', '0%')] == ["USD", "USD", "EUR", "CAD", None, None, None]


def test_a_template_with_placeholder_labels_is_not_accepted(make_pipe):
    pipe = make_pipe()
    r = pipe.parse_bytes(b"Provider,Month 1,Month 2\nAWS,1.00,1.00\n[cloud_provider],1.00,1.00\n", "tco.csv")
    assert r["status"] == "needs_review"
    assert any(c["name"] == "placeholder" and c["status"] == "fail" for c in r["checks"])


def test_a_size_or_a_share_is_never_an_amount():
    from billparse.tree import money_in
    assert [money_in(t) for t in ("Storage 0.89 TB", "Data 0.83 TiB", "Usage 45 %")] == [[], [], []]
    assert [v for v, *_ in money_in("EC2 4,216.07 Hrs 79.26")] == [79.26]


def test_a_slide_table_is_also_a_sheet_with_its_cells_apart():
    import io
    import zipfile

    from billparse.docmodel import load_bytes
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    cell = lambda t: f"<a:tc><a:txBody><a:p><a:r><a:t>{t}</a:t></a:r></a:p></a:txBody></a:tc>"   # noqa: E731
    slide = (f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" {a}><a:tbl>'
             f'<a:tr>{cell("Item")}{cell("Cost")}</a:tr><a:tr>{cell("Compute")}{cell("102,901")}</a:tr></a:tbl></p:sld>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ppt/slides/slide1.xml", slide)
    doc = load_bytes(buf.getvalue(), "deck.pptx")
    sheet = next(r for r in doc.regions if r.kind == "sheet")
    assert [[c.text for c in row.cells] for row in sheet.rows] == [["Item", "Cost"], ["Compute", "102,901"]]


def test_a_coverage_word_in_a_count_report_is_not_a_service(make_pipe):
    """An instance report by coverage type ("Compute Savings Plan": 12 instances) has no money: the words say how
    the instances ran, not the plan's fee service. On a bill with costs the same words stay that service."""
    import pandas as pd

    from billparse.analyze import resolve_services
    pipe = make_pipe()
    counts = pd.DataFrame({"description": ["Compute Savings Plan", "On Demand", "Spot"], "quantity": [12, 30, 4]})
    resolve_services(counts, pipe.s.catalog_dir)
    assert counts["service_code"].isna().all()
    bill = pd.DataFrame({"description": ["Compute Savings Plan"], "billed_cost": [100.0], "provider": ["aws"]})
    resolve_services(bill, pipe.s.catalog_dir)
    assert bill["service_code"].notna().all()


def test_the_field_check_finds_values_a_consumer_should_not_get():
    from decimal import Decimal

    from billparse import fieldcheck
    from billparse.export import Document
    cite = {"table": "t", "row": 1}
    v = lambda x: {"value": x, "cite": cite, "origin": "printed"}   # noqa: E731
    charge = {"id": "c1", "cite": cite, "description": v("Total"), "cost": v(Decimal("10")), "counted": True,
              "kind": "usage", "context": {"region": v("US East, global")}, "quantity": v(Decimal("2")),
              "unit_price": v(Decimal("3"))}
    doc = Document.model_validate({"schema_version": "1.11", "source_name": "x.csv", "sha256": "0", "page_count": 1,
                                   "charges": [charge], "completeness": "consistent",
                                   "confidence": {"level": "medium", "reasons": ["x"]}})
    assert set(fieldcheck.issues(doc, "unreconciled")) == {"no_currency", "unit_price", "region", "coverage",
                                                          "service", "total_as_line"}



# ---------------------------------------------------------------- the LLM never runs without a limit
def _limited_pipe(tmp_path, llm, **kw):
    return Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=True,
                             use_agent=False, **kw), llm=llm)


def test_repairs_stop_when_a_round_changes_nothing(make_pipe, tmp_path, no_seed):
    """Every draft is rejected for the same reason: the second repair round is not run, and the result says why."""
    f = tmp_path / "de.xlsx"
    german_xlsx(f)
    llm = FakeLLM(["not json"] * 9)
    pipe = make_pipe(llm)
    r = pipe.parse_file(f)
    spec_calls = [c for c in llm.calls if not c[0].startswith("Extract every billable")]
    assert len(spec_calls) == 3                                 # 2 drafts, 1 repair: the next repair is not run
    assert any(a["route"] == "llm_skipped" and "did not change" in (a["error"] or "") for a in r["attempts"])


def test_a_file_stops_calling_the_llm_at_its_call_limit(make_pipe, tmp_path, no_seed):
    from billparse.export import to_schema
    from billparse.schema import Document
    f = tmp_path / "de.xlsx"
    german_xlsx(f)
    make_pipe()                                                 # the service catalog in tmp_path/data
    llm = FakeLLM([{"bad": 1}, {"bad": 2}, {"bad": 3}, {"bad": 4}])
    pipe = _limited_pipe(tmp_path, llm, llm_max_calls=2)
    r = pipe.parse_file(f)
    assert len(llm.calls) == 2                                  # the 2 drafts; no repair, no extraction
    doc = to_schema(Document.model_validate_json(pipe.store.part(r["sha256"], "doc")))
    assert any("this file's LLM calls reached their limit (2 of 2)" in x for x in doc.confidence.reasons)


def test_no_llm_call_once_the_days_budget_is_used(make_pipe, tmp_path, no_seed, monkeypatch):
    f = tmp_path / "de.xlsx"
    german_xlsx(f)
    make_pipe()
    llm = FakeLLM()
    pipe = _limited_pipe(tmp_path, llm, llm_daily_budget=5.0)
    monkeypatch.setattr(pipe.store, "llm_spent_since", lambda ts: 5.0)
    r = pipe.parse_file(f)
    assert not llm.calls
    assert any(a["route"] == "llm_skipped" and "used up" in a["error"] for a in r["attempts"])


def test_no_llm_call_for_a_file_with_nothing_to_read(make_pipe):
    llm = FakeLLM()
    pipe = make_pipe(llm)
    r = pipe.parse_bytes(b"PK\x03\x04" + b"\0" * 64, "cut.zip")         # a damaged zip: nothing to read
    assert not llm.calls and r["status"] == "needs_review"
