"""Formats with a built-in reading found in real files: synthetic copies of their layout."""

from __future__ import annotations

import csv
import io

import pytest

from billparse import Pipeline, Settings

ECSV_HEAD = ["InvoiceID", "PayerAccountId", "LinkedAccountId", "RecordType", "RecordID", "BillingPeriodStartDate",
             "BillingPeriodEndDate", "InvoiceDate", "PayerAccountName", "LinkedAccountName", "TaxationAddress",
             "PayerPONumber", "ProductCode", "ProductName", "SellerOfRecord", "UsageType", "Operation", "RateId",
             "ItemDescription", "UsageStartDate", "UsageEndDate", "UsageQuantity", "BlendedRate", "CurrencyCode",
             "CostBeforeTax", "Credits", "TaxAmount", "TaxType", "TotalCost"]
LINES = [("111111111111", "AmazonEC2", "Amazon Elastic Compute Cloud", "BoxUsage:m5.large", 720, 0.096, 69.12),
         ("111111111111", "AmazonS3", "Amazon Simple Storage Service", "TimedStorage-ByteHrs", 100, 0.023, 2.30),
         ("222222222222", "AmazonRDS", "Amazon Relational Database Service", "InstanceUsage:db.t4g.medium", 720,
          0.065, 46.80)]


def _ecsv(linked: bool) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, ECSV_HEAD)
    w.writeheader()

    def row(rt: str, **kw) -> None:
        w.writerow({**{h: "" for h in ECSV_HEAD}, "InvoiceID": "Estimated", "PayerAccountId": "999999999999",
                    "RecordType": rt, "BillingPeriodStartDate": "2026/04/01 00:00:00",
                    "BillingPeriodEndDate": "2026/04/30 23:59:59", "CurrencyCode": "USD", **kw})
    total = round(sum(x[-1] for x in LINES), 2)
    for _acct, code, name, usage, qty, rate, cost in LINES:    # the payer's sum of the linked lines (same money)
        row("PayerLineItem", ProductCode=code, ProductName=name, UsageType=usage, ItemDescription=f"{name} {usage}",
            UsageQuantity=qty, BlendedRate=rate, CostBeforeTax=cost, TotalCost=cost)
    if linked:
        for acct, code, name, usage, qty, rate, cost in LINES:
            row("LinkedLineItem", LinkedAccountId=acct, ProductCode=code, ProductName=name, UsageType=usage,
                ItemDescription=f"{name} {usage}", UsageQuantity=qty, BlendedRate=rate, CostBeforeTax=cost,
                TotalCost=cost)
        for acct in ("111111111111", "222222222222"):
            row("AccountTotal", LinkedAccountId=acct, ItemDescription=f"Total for linked account# {acct}",
                TotalCost=round(sum(x[-1] for x in LINES if x[0] == acct), 2))
    row("InvoiceTotal", ItemDescription="Total amount for invoice Estimated", TotalCost=total)
    row("StatementTotal", ItemDescription="Total statement amount for period 2026/04/01 - 2026/04/30",
        TotalCost=total)
    return out.getvalue()


@pytest.mark.parametrize("linked", [True, False])
def test_an_aws_ecsv_reads_each_line_once_and_ties_to_its_statement_total(tmp_path, linked):
    """The eCSV prints the same money per linked account, per payer, per account and as the statement's total: the
    lines are the linked lines (a payer-only account: the payer lines), once, proved by the StatementTotal."""
    path = tmp_path / "ecsv_4_2026.csv"
    path.write_text(_ecsv(linked))
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", use_llm=False, use_agent=False))
    r = pipe.parse_file(path)
    rows = pipe.store.get_rows(r["sha256"])
    assert (r["spec"] or {}).get("id") == "aws-ecsv" and r["status"] == "reconciled"
    assert len(rows) == 3 and set(rows["line_type_raw"]) == {"LinkedLineItem" if linked else "PayerLineItem"}
    assert round(float(rows["billed_cost"].sum()), 2) == 118.22
    if linked:
        assert set(rows["account_id"]) == {"111111111111", "222222222222"}


def test_a_month_table_of_counts_is_not_money(tmp_path):
    """Instances and instance hours by coverage type and month (a coverage report): usage figures, never cost lines."""
    months = ["2026-01", "2026-02", "2026-03", "2026-04"]
    text = "Sheet: # Instances\nCoverage Type," + ",".join(months) + ",Total\n"
    for label, n in (("Compute Savings Plan", 12), ("On Demand", 30), ("Spot", 4), ("Standard RI", 9)):
        text += f"{label}," + ",".join(str(n + k) for k in range(4)) + f",{4 * n + 6}\n"
    text += "Total," + ",".join("61" for _ in months) + ",244\n\nSheet: # Instance Hours\nCoverage Type," \
        + ",".join(months) + ",Total\nOn Demand,21600,20100,22300,21000,85000\n"
    path = tmp_path / "instance-usage.csv"
    path.write_text(text)
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", use_llm=False, use_agent=False))
    r = pipe.parse_file(path)
    assert r["status"] == "auxiliary" and r["kind"] == "inventory"
    rows = pipe.store.get_rows(r["sha256"])
    assert "billed_cost" not in rows or rows["billed_cost"].isna().all()
    assert {"On Demand", "Spot"} <= set(rows["description"])


def test_a_month_table_of_costs_stays_cost_lines(tmp_path):
    months = ["2026-01", "2026-02", "2026-03", "2026-04"]
    text = "Cost by purchase option (USD)\nPurchase Option," + ",".join(months) + "\n"
    text += "On Demand,1200.50,1100.00,1300.25,1250.00\nSpot,40.10,38.00,41.00,39.90\n"
    path = tmp_path / "cost.csv"
    path.write_text(text)
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", use_llm=False, use_agent=False))
    r = pipe.parse_file(path)
    assert r["kind"] == "cost_lines" and r["rows"] == 8


def test_only_a_zip_of_pdfs_is_read_file_by_file():
    """Bills in a zip are read one by one; other files in a zip belong together (Terraform: a variable in one file,
    its use in another)."""
    from billparse.docmodel import Cell, Document, Region, Row
    from billparse.pipeline import file_parts

    def page(name: str) -> Region:
        return Region(kind="page", name=name, rows=[Row([Cell("Total", 0, 10), Cell("1.00", 50, 60)], 1)])
    pdfs = Document("bills.zip", "pdf", "x", [page("a.pdf:page1"), page("a.pdf:page2"), page("b.pdf:page1")])
    assert [d.name for d in file_parts(pdfs)] == ["a.pdf", "b.pdf"]
    tf = Document("tf.zip", "txt", "x", [page("tf/data.tf:page1"), page("tf/main.tf:page1")])
    assert file_parts(tf) == []
    one = Document("one.zip", "pdf", "x", [page("a.pdf:page1")])
    assert file_parts(one) == []


def test_a_json_total_amount_due_proves_the_lines(tmp_path):
    """A JSON report prints its total as a key ("total_amount_due"): the lines are proved against it."""
    import json
    data = {"report_type": "monthly_cloud_cost", "provider": "aws", "currency": "USD", "total_amount_due": 130.25,
            "line_items": [{"service": "Amazon EC2", "cost": 100.0}, {"service": "Amazon S3", "cost": 30.25}]}
    path = tmp_path / "monthly.json"
    path.write_text(json.dumps(data))
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", use_llm=False, use_agent=False))
    r = pipe.parse_file(path)
    assert r["status"] == "reconciled" and r["rows"] == 2


@pytest.mark.parametrize("text, expect", [
    ("(1 234,56) €", -1234.56), ("USD (1,234.56)", -1234.56), ("(12.00)", -12.0), ("12.00-", -12.0),
    ("1 234,56 €", 1234.56), ("1 234,56 €", 1234.56), ("CHF 1'234.50", 1234.5), ("-1.234,56 €", -1234.56),
    ("€ (99,00)", -99.0), ("1,234.56", 1234.56),
])
def test_parse_number_signs_and_formats(text, expect):
    from billparse.transforms import parse_number
    assert parse_number(text) == expect


def test_an_azure_cost_by_resource_view_takes_the_service_from_the_resource_type(tmp_path):
    """The portal's cost analysis by resource has no service column; a column a person added (a category, a
    department) is not the service."""
    head = "ResourceGroup,ResourceGroupId,SubscriptionName,SubscriptionId,Resource,ResourceId,ResourceType," \
           "ResourceLocation,Tags,Cost,CostUSD,Currency,category,Department\n"
    rows = [("rg1", "vm1", "Virtual machine", "120.50", "Backups", "IT"),
            ("rg1", "disk1", "Disk", "30.25", "Backups", "IT"),
            ("rg2", "sa1", "Storage account", "12.00", "Data platform", "Apps")]
    text = head + "".join(f"{g},/subscriptions/s/resourceGroups/{g},Dev,s,{n},/subscriptions/s/{n},{t},US East,[],"
                          f"{c},{c},USD,{cat},{dep}\n" for g, n, t, c, cat, dep in rows)
    path = tmp_path / "cost-by-resource.csv"
    path.write_text(text)
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", use_llm=False, use_agent=False))
    r = pipe.parse_file(path)
    assert r["spec"]["id"] == "azure-cost-by-resource"
    out = pipe.store.get_rows(r["sha256"])
    assert list(out["service_raw"]) == ["Virtual machine", "Disk", "Storage account"]
    assert list(out["description"]) == ["vm1", "disk1", "sa1"]
    assert round(float(out["billed_cost"].sum()), 2) == 162.75
