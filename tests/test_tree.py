"""The tree of sums on a synthetic bill page laid out like an AWS bill PDF: the rounding proof, headings that a total
completed within rounding, $0.00 groups, cells (quantity, unit, other amounts), tax lines, header fields, regions."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from billparse import tree  # noqa: E402
from billparse.context import lines_frame  # noqa: E402
from billparse.docmodel import Cell, Region, Row  # noqa: E402
from billparse.validate import _tree_total  # noqa: E402
from svcres.regions import lookup  # noqa: E402

# x positions of the columns: description (by level), quantity, amount
X = {"total": 30.0, "service": 36.0, "region": 36.0, "usage": 40.5, "line": 51.0}


def row(i: int, level: str, text: str, amount: str | None = None, qty: str | None = None, bold: bool = False) -> Row:
    x = X[level]
    cells = [Cell(text, x, x + 200, bold)]
    if qty:
        cells.append(Cell(qty, 300, 360, bold))
    if amount:
        cells.append(Cell(amount, 400, 460, bold))
    return Row(cells, i, page=1)


def header(i: int, *names: str) -> Row:
    xs = [(30, 230), (300, 360), (400, 460), (480, 540)]
    return Row([Cell(n, *xs[k]) for k, n in enumerate(names)], i, page=1)


def page() -> Region:
    """Total pre-tax 10.05; its services add up to 10.04 (0.01 rounding). Service B (0.04) comes when the total is
    already complete within rounding (10.04 of 10.05): its regions after it add up to it exactly, so it heads them.
    Service C has $0.00 regions."""
    rows = [
        header(0, "Billing period", "Account ID"),
        Row([Cell("Apr 1 - Apr 30, 2025", 30, 230), Cell("111122223333", 300, 360)], 1, page=1),
        header(2, "Description", "Usage Quantity", "Amount in USD"),
        row(3, "total", "Acme Cloud (3) Total pre-tax", "USD 10.05"),
        row(4, "service", "Elastic Compute Cloud", "USD 10.00", bold=True),
        row(5, "region", "Canada (Central)", "USD 10.00", bold=True),
        row(6, "usage", "Amazon Elastic Compute Cloud running Linux/UNIX", "USD 10.00"),
        row(7, "line", "$0.0232 per On Demand Linux t3.small Instance Hour", "USD 6.67", "287.5 Hrs"),
        row(8, "line", "$0.0464 per On Demand Linux t3.medium Instance Hour", "USD 3.33", "71.77 Hrs"),
        row(9, "service", "Kinesis Firehose", "USD 0.04", bold=True),
        row(10, "region", "US West (Oregon)", "USD 0.03", bold=True),
        row(11, "line", "Tier 1 $0.029 per GB of data ingested", "USD 0.03", "1.1 GB"),
        row(12, "region", "EU (Ireland)", "USD 0.01", bold=True),
        row(13, "line", "Tier 1 $0.031 per GB of data ingested", "USD 0.01", "0.3 GB"),
        row(14, "service", "CloudFront", "USD 0.00", bold=True),
        row(15, "region", "Asia Pacific (Tokyo)", "USD 0.00", bold=True),
        row(16, "usage", "Amazon CloudFront JP-Requests-Tier1", "USD 0.00"),
        row(17, "line", "$0.000 per request under the global monthly free tier", "USD 0.00", "45 Requests"),
    ]
    return Region("page", "page1", rows)


def test_the_lines_are_proved_by_the_rounding_at_each_printed_total():
    t = tree.read([page()], {})
    rec = t.evidence["reconcile"]
    assert rec["proved"] and rec["amount"] == 10.05 and rec["lines"] == 10.04 and rec["rounding"] == 0.01
    assert [x["label"] for x in rec["rounding_at"]] == ["Acme Cloud (3) Total pre-tax"]
    # the heading that came when the total was complete within rounding keeps its lines (none is counted twice)
    labels = [line["label"] for line in t.lines if line["amount"]]
    assert "Kinesis Firehose" not in labels and len(labels) == 4
    assert t.evidence["unexplained_count"] == 0


def test_a_difference_that_is_not_rounding_fails():
    rec = {"label": "Total", "amount": 10.05, "src": "page1:3", "rounding": 0.01, "rounding_at": [], "over_bound": []}
    assert _tree_total(rec, 10.04)[1]
    check, ok = _tree_total(rec, 10.25)                 # 0.21 more than the lines and rounding allow
    assert not ok and check.status == "fail" and "-0.21 is not explained" in check.detail
    over = dict(rec, over_bound=[{"label": "EC2", "amount": 5.0, "src": "p", "parts": 2, "children": 4.9,
                                  "rounding": 0.1}])
    assert not _tree_total(over, 10.04)[1]              # 0.10 over 2 parts: not rounding
    assert tree.bound(2) < 0.016 < tree.bound(3)


def test_lines_get_region_quantity_and_description_from_their_cells():
    t = tree.read([page()], {})
    rows = lines_frame(t.lines, None, t.currency, {})
    first = rows.iloc[0]
    assert first["description"] == "$0.0232 per On Demand Linux t3.small Instance Hour"
    assert (first["quantity"], first["unit"]) == (287.5, "Hrs")
    assert (first["region"], first["region_name"]) == ("ca-central-1", "Canada (Central)")
    ireland = rows[rows["description"].str.contains("0.031")].iloc[0]
    assert (ireland["region"], ireland["region_name"]) == ("eu-west-1", "EU (Ireland)")
    # a $0.00 group still gives its lines their region
    free = rows[rows["description"].str.contains("free tier")].iloc[0]
    assert free["region"] == "ap-northeast-1" and free["billed_cost"] == 0
    assert rows["charge_type"].isna().all()             # "Total pre-tax" above the lines is not a tax


def test_header_fields_are_read_from_a_row_of_names_over_a_row_of_values():
    facts = tree.facts_from([page()])
    assert facts["Billing period"]["value"] == "Apr 1 - Apr 30, 2025"
    assert facts["Account ID"] == {"value": "111122223333", "src": "page1:1"}


def test_tax_lines_keep_the_other_amounts_by_column_name():
    line = {"label": "Relational Database Service USD 4,571.96 USD 4,045.98", "amount": 525.98, "src": "page14:64",
            "path": ["Total tax"], "cells": ["Relational Database Service", "USD 4,571.96", "USD 4,045.98",
                                             "USD 525.98"],
            "columns": {"Service name": "Relational Database Service", "Post-tax charges": "USD 4,571.96",
                        "Pre-tax charges": "USD 4,045.98", "Taxes": "USD 525.98"}}
    usage = dict(line, label="Relational Database Service", amount=4045.98, path=["Acme (3) Total pre-tax"],
                 cells=[], columns={})
    rows = lines_frame([line, usage], None, "USD", {})
    tax = rows.iloc[0]
    assert tax["charge_type"] == "Tax" and tax["description"] == "Relational Database Service"
    assert tax["_extra"] == "Post-tax charges: 4571.96 | Pre-tax charges: 4045.98"
    assert pd.isna(rows.iloc[1]["charge_type"])


def test_regions_are_whole_labels_only():
    assert lookup("US East (N.Virginia)") == ("aws", "us-east-1")
    assert lookup("Europe (Frankfurt)") == ("aws", "eu-central-1")
    assert lookup("East US 2") == ("azure", "eastus2") and lookup("us-central1") == ("gcp", "us-central1")
    assert lookup("Any") == ("aws", "global")
    assert lookup("$0.05 per GB - US West (Oregon)") is None and lookup("London") is None


def test_each_charge_type_is_its_own_line_and_keeps_its_service():
    """Usage, tax, credit and commitment fee stay apart; "CPU Credits" and "covered by Savings Plan" are usage."""
    from billparse.context import Context
    c = Context()
    cases = [("Relational Database Service", ["Total tax"], 5.0, "Tax"),
             ("USD 0.075 per vCPU-Hour of T4g CPU Credits", ["Acme (3) Total pre-tax"], 7.12, None),
             ("AWS Credits applied", ["Elastic Compute Cloud"], -10.0, "Credit"),
             ("Savings Plan recurring fee", ["Savings Plans for AWS Compute usage"], 100.0, "Purchase"),
             ("$0.034 per hour covered by Savings Plan", ["Elastic Compute Cloud"], 0.0, None),
             ("GST", [], 1.0, "Tax"), ("Total pre-tax", [], 1.0, None)]
    for label, path, amount, want in cases:
        assert c.charge_type_of(label, path, None, amount) == want, label


def test_a_region_column_of_names_becomes_codes():
    from billparse.attributes import normalize_regions
    rows = pd.DataFrame({"region": ["US East (N. Virginia)", "East US 2", "us-west-2", "Somewhere", None]})
    assert normalize_regions(rows) == 2
    assert list(rows["region"][:4]) == ["us-east-1", "eastus2", "us-west-2", "Somewhere"]
    assert list(rows["region_name"][:2]) == ["US East (N. Virginia)", "East US 2"] and pd.isna(rows["region_name"][2])


def consolidated() -> list[Region]:
    """A bilingual consolidated invoice: a summary and a detail for each linked account, two accounts paying the same
    0.42 for the same service, amounts in USD with the total due in CAD."""
    def r(i, x, text, amount=None, bold=False, other=None):
        cells = [Cell(text, x, x + 200, bold)]
        if amount:
            cells.append(Cell(amount + (f" {other}" if other else ""), 400, 520, bold))
        return Row(cells, i, page=1)
    head = [Row([Cell("Invoice Number: / Numéro de facture :", 30, 230), Cell("INV-1", 300, 360)], 0, page=1),
            Row([Cell("Account number / Numéro de compte :", 30, 230)], 1, page=1),
            Row([Cell("111122223333", 30, 120)], 2, page=1),
            r(3, 30, "This Invoice is for the billing period 2025/10/01 - 2025/10/31"),
            r(4, 30, "AWS Service Charges / Frais de service AWS (1 USD = 1.5 CAD)", "USD 10.84", other="CAD 16.26")]
    page1 = Region("page", "page1", head)
    rows, i = [], 0

    def add(*a, **k):
        nonlocal i
        rows.append(r(i, *a, **k))
        i += 1
    add(36, "Summary for Linked Account / Résumé pour le compte lié", bold=True)
    add(36, "Alpha (111111111111)", "USD 10.42", bold=True)
    add(46, "Charges / Frais", "USD 10.00")
    add(46, "GST / TPS", "USD 0.42")
    add(36, "Detail for Linked Account / Détails pour le compte lié", bold=True)
    for svc, ch, tax in (("Amazon Elastic Compute Cloud / Amazon Elastic Compute Cloud", "9.60", "0.40"),
                         ("AWS Cost Explorer / AWS Cost Explorer", "0.40", "0.02")):
        add(36, svc, f"USD {float(ch) + float(tax):.2f}", bold=True)
        add(46, "Charges / Frais", f"USD {ch}")
        add(46, "GST / TPS", f"USD {tax}")
    add(36, "Summary for Linked Account / Résumé pour le compte lié", bold=True)
    add(36, "Beta (222222222222)", "USD 0.42", bold=True)
    add(46, "Charges / Frais", "USD 0.40")
    add(46, "GST / TPS", "USD 0.02")
    add(36, "Detail for Linked Account / Détails pour le compte lié", bold=True)
    add(36, "AWS Cost Explorer / AWS Cost Explorer", "USD 0.42", bold=True)
    add(46, "Charges / Frais", "USD 0.40")
    add(46, "GST / TPS", "USD 0.02")
    add(30, "Total allocated for this invoice / Total alloué pour cette facture", "USD 10.84")
    return [page1, Region("page", "page2", rows)]


def test_two_accounts_with_the_same_service_amount_keep_their_own_lines():
    t = tree.read(consolidated(), {})
    srcs = [line["src"] for line in t.lines]
    assert len(srcs) == len(set(srcs)) and not t.evidence["duplicate_lines"]
    rows = lines_frame(t.lines, None, t.currency, {})
    by = rows.groupby("account_id")["billed_cost"].sum().round(2).to_dict()
    assert by == {"111111111111": 10.42, "222222222222": 0.42}
    beta = rows[rows["account_id"].eq("222222222222")]
    assert set(beta["description"]) == {"Charges", "GST"} and set(beta["charge_type"].fillna("")) == {"", "Tax"}
    assert t.evidence["reconcile"]["proved"]


def test_label_value_fields_exchange_rate_and_converted_amounts():
    from billparse.spec import Extraction
    from billparse.validate import _converted
    t = tree.read(consolidated(), {})
    facts = {k: v["value"] for k, v in t.facts.items()}
    assert facts["Invoice Number"] == "INV-1" and facts["Account number"] == "111122223333"
    assert facts["Billing period"] == "2025/10/01 - 2025/10/31" and facts["Exchange rate"] == "1 USD = 1.5 CAD"
    conv = t.evidence["converted"]
    assert conv and conv[0]["amount"] == 10.84 and conv[0]["other"] == 16.26 and "16.26" not in conv[0]["label"]
    ext = Extraction(rows=pd.DataFrame(), fields=t.facts, evidence=t.evidence)
    assert _converted(ext).status == "pass"
    ext.evidence = dict(t.evidence, converted=[dict(conv[0], other=16.30)])
    assert _converted(ext).status == "fail"


def microsoft_page() -> list[Region]:
    """A Microsoft invoice by product: part number, a bold name wrapped over the numbers, Net Charge + Tax = Total,
    the period that ends the charge; a part number printed with the numbers of another charge."""
    def r(i, *cells, bold=False):
        xs = [36, 217, 290, 362, 446, 518]
        return Row([Cell(t, xs[k], xs[k] + 50, bold and k == 0) for k, t in enumerate(cells) if t], i, page=1)
    rows = [r(0, "Billing Details By Product", "(Amount in USD)"),
            r(1, "Usage Charges", "Unit Price", "Qty", "Net Charge", "Tax Amount", "Total", bold=True),
            r(2, "AAA-16180"),
            r(3, "Az Logic Apps Int Account-Std Unit-", bold=True),
            r(4, "", "32.25", "62.000", "1,999.50", "203.95", "2,203.45"),
            r(5, "1/Day", bold=True),
            r(6, "01-Jul-2025 - 31-Jul-2025"),
            r(7, "AAA-21874"),
            r(8, "Az DDOS Prot-NW Prot Plan-1 Hr", "4.03", "744.000", "2,998.32", "305.83", "3,304.15", bold=True),
            r(9, "01-Jul-2025 - 31-Jul-2025"),
            r(10, "N9H-00813", "3.60", "0.003", "0.01", "0.00", "0.01"),
            r(11, "Az Std Page Blob-100M Disk Write Ops", bold=True),
            r(12, "01-Jul-2025 - 31-Jul-2025"),
            r(13, "Total Amount Due", "", "", "", "", "USD 5,507.61")]
    return [Region("page", "page1", rows)]


def test_a_microsoft_charge_is_its_rows_up_to_its_period():
    t = tree.read(microsoft_page(), {"blocks": True})
    rows = lines_frame(t.lines, None, t.currency, {})
    usage = rows[rows["charge_type"].isna()].set_index("product_code")
    assert usage.loc["AAA-16180", "description"] == "Az Logic Apps Int Account-Std Unit- 1/Day"
    assert usage.loc["N9H-00813", "description"] == "Az Std Page Blob-100M Disk Write Ops"
    assert (usage.loc["AAA-21874", "billed_cost"], usage.loc["AAA-21874", "unit_price"]) == (2998.32, 4.03)
    assert usage.loc["AAA-16180", "charge_start"] == "2025-07-01" and usage.loc["AAA-16180", "charge_end"] == "2025-07-31"
    tax = rows[rows["charge_type"].eq("Tax")]                     # the tax of each charge is a line of its own
    assert sorted(tax["billed_cost"]) == [203.95, 305.83] and set(tax["product_code"]) == {"AAA-16180", "AAA-21874"}
    assert round(rows["billed_cost"].sum(), 2) == 5507.61 and t.evidence["reconcile"]["proved"]


def test_a_header_printed_on_several_rows_names_each_column():
    from billparse.docmodel import Cell as C
    xs = {"a": 62, "price": 217, "qty": 270, "chg": 308, "rate": 396, "tax": 460, "total": 549}

    def row(i, **cells):
        return Row([C(t, xs[k], xs[k] + 40) for k, t in cells.items()], i, page=1)
    rows = [row(0, total="Total"), row(1, price="Unit", chg="Charges/", total="(including"),
            row(2, a="Purchases", price="Price", chg="Credits", tax="Tax Amount", total="Tax)"),
            row(3, a="Charge Start Date - Charge End Date", price="(USD)", qty="Qty", chg="(USD)", rate="Sales Tax",
                tax="(USD)", total="(USD)"),
            row(4, a="09/21/2025-10/20/2025", price="4.58", qty="1", chg="4.58", rate="10.20%", tax="0.47", total="5.05")]
    header = tree._headers([(None, r, r.text) for r in rows])[id(rows[4])]
    assert [c.text for c in header.cells] == ["Purchases Charge Start Date - Charge End Date", "Unit Price (USD)", "Qty",
                                              "Charges/ Credits (USD)", "Sales Tax", "Tax Amount (USD)",
                                              "Total (including Tax) (USD)"]
    cols = tree._columns(rows[4], header)
    assert cols["Tax Amount (USD)"] == "0.47" and "10.20%" not in cols.values()


def test_microsoft_short_product_names_find_their_service():
    from svcres.normalize import azure_head
    assert azure_head("Az DDOS Prot-NW Prot Plan-1 Hr") == "azure ddos protection"
    assert azure_head("Az SQL DB Sgl/El Pool Gen Pur-Comp") == "azure sql database single elastic pool general purpose"
    assert azure_head("Az P/L-Std") == "azure private link" and azure_head("VM Dadsv5 Series-D4ads") == "virtual machines dadsv5 series"


def test_a_service_whose_lines_net_to_zero_keeps_its_name():
    """An AWS invoice prints a service total of $0.00 when an EDP discount or credits cancel its charges
    ("Amazon Bedrock USD 0.00" over "Charges 0.01", "Discount -0.01", "Tax 0.00"): its lines are still its lines."""
    def r(i: int, x: float, text: str, amount: str) -> Row:
        return Row([Cell(text, x, x + 200), Cell(amount, 400, 460)], i, page=1)
    rows = [r(0, 36.0, "Amazon Web Services, Inc. charges", "USD 300.23"),
            r(1, 49.5, "Amazon Route 53", "USD 300.23"),
            r(2, 59.5, "Charges", "USD 315.40"),
            r(3, 59.5, "Discount (Enterprise Discount Program)", "-USD 58.60"),
            r(4, 59.5, "Tax", "USD 43.43"),
            r(5, 49.5, "Amazon Bedrock", "USD 0.00"),
            r(6, 59.5, "Charges", "USD 0.01"),
            r(7, 59.5, "Discount (Enterprise Discount Program)", "-USD 0.01"),
            r(8, 59.5, "Tax", "USD 0.00"),
            r(9, 49.5, "Amazon GuardDuty", "USD 0.00"),
            r(10, 59.5, "Charges", "USD 0.53"),
            r(11, 59.5, "Discount (Enterprise Discount Program)", "-USD 0.13"),
            r(12, 59.5, "Credits", "-USD 0.40")]
    t = tree.read([Region("page", "page1", rows)], {})
    under = {(line["label"], line["amount"]): line["path"] for line in t.lines}
    assert under[("Charges", 0.01)][-1] == "Amazon Bedrock"
    assert under[("Discount (Enterprise Discount Program)", -0.01)][-1] == "Amazon Bedrock"
    assert under[("Charges", 0.53)][-1] == "Amazon GuardDuty"
    assert under[("Credits", -0.4)][-1] == "Amazon GuardDuty"
    assert under[("Charges", 315.4)][-1] == "Amazon Route 53"
    assert not any(line["label"].startswith("Amazon") for line in t.lines)   # a service total is not a line
    assert t.evidence["reconcile"]["proved"] and t.evidence["unexplained_count"] == 0


# ---------------------------------------------------------------- a zip of several bills: one tree per file
def bill_pdf(client: str, lines: list[tuple[str, float]], region: str) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    total = round(sum(a for _, a in lines), 2)
    top = max(lines, key=lambda x: x[1])
    y = 740
    def row(label, amount=None, bold=False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        c.drawString(50, y, label)
        if amount is not None:
            c.drawRightString(540, y, f"USD {amount:,.2f}")
        y -= 16
    row(f"Billing summary for {client}", bold=True)
    row("Amazon Web Services, Inc. (2) Total pre-tax", total)
    row("Highest service spend", bold=True)
    row(f"Highest service spend {top[0]}", top[1])
    row("Highest AWS Region spend", bold=True)
    row(region, total)
    y -= 10
    row("Charges by service", bold=True)
    for label, amount in lines:
        row(label, amount)
    row("Total", total, bold=True)
    c.save()
    return buf.getvalue()


def bills_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ClientA_April.pdf", bill_pdf("Client A", [("Amazon Elastic Compute Cloud", 812.40), ("Amazon Simple Storage Service", 120.15)], "US East (N. Virginia)"))
        z.writestr("ClientB_April.pdf", bill_pdf("Client B", [("Amazon Relational Database Service", 455.00), ("AWS Lambda", 12.31), ("Amazon CloudFront", 40.02)], "EU (Ireland)"))
    return buf.getvalue()


def test_a_zip_of_bills_reads_each_bill_with_its_own_tree(tmp_path):
    """Ten monthly bills in one zip each print their own summary ("Total pre-tax", "Highest service spend", the
    region) and total. One tree over every page matched one bill's total and lost the others (or read the summary
    rows as lines). Each file now gets its own tree: every service line, no summary row, and the files' totals
    together prove the lines."""
    from billparse import Pipeline, Settings
    path = tmp_path / "bills.zip"
    path.write_bytes(bills_zip())
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", use_llm=False, use_agent=False))
    r = pipe.parse_file(path)
    rows = pipe.store.get_rows(r["sha256"])
    assert sorted(rows["description"]) == sorted(["Amazon Elastic Compute Cloud", "Amazon Simple Storage Service",
                                                  "Amazon Relational Database Service", "AWS Lambda",
                                                  "Amazon CloudFront"])
    assert r["status"] == "reconciled"
    assert round(float(rows["billed_cost"].sum()), 2) == round(812.40 + 120.15 + 455.00 + 12.31 + 40.02, 2)
    checks = {c["name"]: c["status"] for c in r["checks"]}
    assert checks["stated_total"] == "pass" and checks["amounts_explained"] == "pass"
    assert [(f["file"], f["status"], f["rows"]) for f in r["files"]] == [("ClientA_April.pdf", "reconciled", 2),
                                                                         ("ClientB_April.pdf", "reconciled", 3)]


# ---------------------------------------------------------------- amounts in European and other formats
@pytest.mark.parametrize("text, expect", [
    ("1 234,56 €", [(1234.56, "EUR")]), ("1.234,56 €", [(1234.56, "EUR")]), ("234,56 €", [(234.56, "EUR")]),
    ("1 234,56 €", [(1234.56, "EUR")]), ("1 234,56 EUR", [(1234.56, "EUR")]),
    ("Montant total (hors taxes) 1 234,56 €", [(1234.56, "EUR")]), ("TVA 20% 246,91 €", [(246.91, "EUR")]),
    ("-1.234,56 €", [(-1234.56, "EUR")]), ("(1 234,56) €", [(-1234.56, "EUR")]),
    ("CHF 1'234.50", [(1234.5, "CHF")]), ("¥1,234", [(1234.0, "JPY")]), ("合計金額 ¥12,345", [(12345.0, "JPY")]),
    ("$1,234.56", [(1234.56, "USD")]), ("USD 1,234.56", [(1234.56, "USD")]), ("1,234.56", [(1234.56, None)]),
    ("Amazon CloudFront global Data Transfer Out GB 1,234.00 0.0850 104,89 €", [(1234.0, None), (104.89, "EUR")]),
    ("IMPORTE TOTAL A PAGAR1 481,47 €", [(1481.47, "EUR")]),
    ("132,978.088 Lambda-GB- USD 2.22", [(2.22, "USD")]), ("Discount - USD 5.00", [(-5.0, "USD")]),
    ("Ubuntu 22.04 LTS", []), ("20%", []), ("1,234", []), ("2026-10-01", []),
])
def test_amounts_in_european_and_other_formats(text, expect):
    assert [(round(v, 2), c) for v, c, *_ in tree.money_in(text)] == expect


def test_a_space_inside_a_number_must_be_inside_one_cell():
    """"12" and "345,67 €" in two columns are two values, not 12,345.67."""
    assert [v for v, *_ in tree.money_in("12 345,67 €", cells=["12", "345,67 €"])] == [345.67]
    assert [v for v, *_ in tree.money_in("12 345,67 €", cells=["12 345,67 €"])] == [12345.67]


# ---------------------------------------------------------------- the AWS Billing console Bills page
BILLS_PAGE = ROOT / "specs" / "builtin" / "aws-bills-page-pdf.json"


def bills_page(grand: str = "USD 1,000.03") -> Region:
    """A Bills page: the summary by seller (two currencies) and its Grand total, a highest-spend box, then the
    charges. Compute (900.00) has two regions that pass it before its discount; Registry ($0.00) is a credit and the
    charge it cancels at the service's indent; Queue (0.10) comes when the seller total is already complete and its
    regions add up to it; a credit (-300.00) is larger than the Grand total's share of the seller. The Grand total
    (1,000.03) is 3 cents more than pre-tax plus tax: AWS adds up the unrounded lines."""
    def money(v: str) -> str:
        return f"USD {v}"
    rows = [
        header(0, "AWS bill summary"),
        header(1, "Service provider", "Total in invoice currency", "Total in USD"),
        Row([Cell("Example Seller", 36, 230), Cell("GBP 750.00", 300, 360), Cell("USD 1,000.03", 400, 460)], 2, page=1),
        Row([Cell("Grand total:", 36, 230), Cell(grand, 400, 460)], 3, page=1),
        header(4, "Highest cost by service provider"),
        row(5, "service", "Highest service spend", money("900.00")),
        header(6, "Charges by service"),
        Row([Cell("Example Seller (4)", 30, 230), Cell("Total pre-tax USD 900.10", 400, 460)], 7, page=1),
        header(8, "Description", "Usage Quantity", "Amount in USD"),
        row(9, "service", "Compute", money("900.00"), bold=True),
        row(10, "region", "EU (Ireland)", money("700.00"), bold=True),
        row(11, "line", "$0.10 per instance hour", money("700.00"), "7,000 Hrs"),
        row(12, "region", "US East (Ohio)", money("500.00"), bold=True),
        row(13, "line", "$0.10 per instance hour", money("500.00"), "5,000 Hrs"),
        row(14, "region", "No Region", "(USD 300.00)", bold=True),
        row(15, "line", "Promo credit, credit from account: 111122223333", "(USD 300.00)"),
        row(16, "service", "Registry", money("0.00"), bold=True),
        row(17, "region", "No Region", "(USD 4.00)", bold=True),
        row(18, "line", "Promo credit, credit from account: 111122223333", "(USD 4.00)"),
        row(19, "region", "EU (Frankfurt)", money("4.00"), bold=True),
        row(20, "line", "$0.10 per GB-month of data storage", money("4.00"), "40 GB-Mo"),
        row(21, "service", "Queue", money("0.10"), bold=True),
        row(22, "region", "EU (London)", money("0.00"), bold=True),
        row(23, "line", "First 1,000,000 requests per month are free", money("0.00"), "120 Requests"),
        row(24, "region", "EU (Ireland)", money("0.10"), bold=True),
        row(25, "line", "$0.40 per million requests", money("0.10"), "250,000 Requests"),
        row(26, "total", "Total tax", money("99.90")),
        Row([Cell("Savings (1)", 30, 230), Cell("Total savings (USD 304.00)", 400, 460)], 27, page=1),
        row(28, "service", "Promo credit", "(USD 304.00)"),
    ]
    return Region(name="page1", kind="page", rows=rows)


def bills_options() -> dict:
    import json
    return json.loads(BILLS_PAGE.read_text())["tree"]


def test_a_bills_page_is_proved_against_its_grand_total():
    t = tree.read([bills_page()], bills_options())
    rec = t.evidence["reconcile"]
    assert rec["label"].startswith("Grand total") and rec["proved"], rec
    assert not t.evidence["unexplained"]
    labels = [line["label"] for line in t.lines]
    # the lines and the seller's tax; no summary row, highest-spend box, service, region or savings row
    assert sorted(round(line["amount"], 2) for line in t.lines) == \
        sorted([700.0, 500.0, -300.0, -4.0, 4.0, 0.0, 0.1, 99.9])
    assert not any(x.startswith(("Example Seller", "Highest", "Compute", "Savings")) for x in labels)
    assert round(sum(line["amount"] for line in t.lines), 2) == 1000.0


def test_regions_past_their_service_until_its_discount_are_its_parts():
    t = tree.read([bills_page()], bills_options())
    compute = next(r for r in t.records if r.label == "Compute")
    assert [round(c.value, 2) for c in compute.children] == [700.0, 500.0, -300.0]
    registry = next(r for r in t.records if r.label == "Registry")
    assert [round(c.value, 2) for c in registry.children] == [-4.0, 4.0]
    queue = next(r for r in t.records if r.label == "Queue")
    assert [round(c.value, 2) for c in queue.children] == [0.0, 0.1]


def test_line_rounding_is_needed_for_a_grand_total_of_unrounded_lines():
    opts = bills_options()

    def proved_by(page: Region, o: dict) -> str | None:
        rec = tree.read([page], o).evidence["reconcile"]
        return rec["label"] if rec["proved"] else None
    assert proved_by(bills_page(), opts) == "Grand total"
    assert proved_by(bills_page(), {**opts, "line_rounding": False}) != "Grand total"
    # more than the rounding of the lines is not rounding
    assert proved_by(bills_page("USD 1,001.00"), opts) != "Grand total"


def test_line_tol_is_never_more_than_half_a_cent_a_line():
    assert tree.line_tol(1) == pytest.approx(tree.bound(1))
    assert tree.line_tol(100) == pytest.approx(0.011 + 0.116)
    assert tree.line_tol(10_000) < 1.2
