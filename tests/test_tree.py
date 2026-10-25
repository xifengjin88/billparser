"""The tree of sums on a synthetic bill page laid out like an AWS bill PDF: the rounding proof, headings that a total
completed within rounding, $0.00 groups, cells (quantity, unit, other amounts), tax lines, header fields, regions."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

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
