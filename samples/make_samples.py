"""Generate test files: known provider exports plus messy custom layouts.

    python samples/make_samples.py        -> samples/files/*

Column names for provider exports follow public documentation; all values are synthetic.
"""

from __future__ import annotations

import gzip
import io
import json
import random
import shutil
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

OUT = Path(__file__).parent / "files"
rnd = random.Random(42)
DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(30)]


def aws_cur2() -> None:
    items = [("AmazonEC2", "Amazon Elastic Compute Cloud", "USE1-BoxUsage:m5.xlarge", "Hrs", 0.192),
             ("AmazonEC2", "Amazon Elastic Compute Cloud", "USE1-EBS:VolumeUsage.gp3", "GB-Mo", 0.08 / 30),
             ("AmazonRDS", "Amazon Relational Database Service", "USE1-InstanceUsage:db.r6g.large", "Hrs", 0.25),
             ("AmazonS3", "Amazon Simple Storage Service", "USE1-TimedStorage-ByteHrs", "GB-Mo", 0.023 / 30),
             ("AWSLambda", "AWS Lambda", "USE1-Request", "Requests", 0.0000002)]
    rows = []
    for d in DAYS:
        for code, name, ut, unit, rate in items:
            for _ in range(rnd.randint(8, 14)):
                q = {"Hrs": 24, "GB-Mo": rnd.uniform(50, 500), "Requests": rnd.randint(10**5, 10**6)}[unit]
                rows.append(_cur_row(d, code, name, ut, unit, rate, q, "Usage"))
    rows.append(_cur_row(DAYS[-1], "AmazonEC2", "Amazon Elastic Compute Cloud", "Tax", "", 0, 0, "Tax", cost=412.55))
    rows.append(_cur_row(DAYS[-1], "AmazonEC2", "Amazon Elastic Compute Cloud", "Credit", "", 0, 0, "Credit", cost=-150.0))
    buf = io.StringIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    (OUT / "aws_cur2.csv.gz").write_bytes(gzip.compress(buf.getvalue().encode()))


def _cur_row(d, code, name, ut, unit, rate, q, typ, cost=None):
    return {"bill_invoice_id": "EUINUS26-123456", "bill_billing_period_start_date": "2026-09-01T00:00:00Z",
            "line_item_usage_account_id": "111122223333", "line_item_line_item_type": typ,
            "line_item_usage_start_date": f"{d}T00:00:00Z", "line_item_usage_end_date": f"{d + timedelta(days=1)}T00:00:00Z",
            "line_item_product_code": code, "line_item_usage_type": ut, "line_item_usage_amount": round(q, 6),
            "line_item_unblended_rate": rate, "line_item_unblended_cost": round(cost if cost is not None else q * rate, 8),
            "line_item_currency_code": "USD", "line_item_line_item_description": f"{ut} usage",
            "product_product_name": name, "product_region_code": "us-east-1", "pricing_unit": unit,
            "pricing_public_on_demand_cost": round(cost if cost is not None else q * rate, 8)}


def azure_ea() -> None:
    meters = [("Virtual Machines", "D4s v5", "DZH318Z0BQ4L", "1 Hour", 0.192),
              ("Storage", "P10 Disks", "DZH318Z0BQ8K", "1/Month", 19.71 / 30),
              ("SQL Database", "vCore", "DZH318Z0BQ1C", "1 Hour", 0.505),
              ("Azure Kubernetes Service", "Standard Uptime SLA", "DZH318Z0BQ3V", "1 Hour", 0.10),
              ("Bandwidth", "Standard Data Transfer Out", "DZH318Z0BRS8", "1 GB", 0.087)]
    rows = []
    for d in DAYS:
        for cat, meter, pid, uom, price in meters:
            for _ in range(rnd.randint(5, 10)):
                q = 24 if "Hour" in uom else 1 if "Month" in uom else rnd.uniform(1, 80)
                rows.append({"BillingAccountId": "12345678", "BillingAccountName": "Contoso", "BillingPeriodStartDate": "09/01/2026",
                             "BillingPeriodEndDate": "09/30/2026", "SubscriptionId": "aaaaaaaa-1111-2222-3333-444444444444",
                             "SubscriptionName": "Prod", "Date": d.strftime("%m/%d/%Y"), "ProductId": pid,
                             "MeterId": "bbbbbbbb-1111-2222-3333-444444444444", "MeterName": meter, "MeterCategory": cat,
                             "MeterRegion": "US East", "Quantity": round(q, 4), "EffectivePrice": price,
                             "CostInBillingCurrency": round(q * price, 6), "UnitOfMeasure": uom, "ChargeType": "Usage",
                             "ResourceLocation": "eastus", "BillingCurrencyCode": "USD"})
    pd.DataFrame(rows).to_csv(OUT / "azure_ea_usage.csv", index=False)


def pc_invoice_recon() -> None:
    rows = []
    for cat, prod, sku, qty, price in [("Virtual Machines", "Azure plan", "D4s v5", 1440, 0.1632),
                                       ("Storage", "Azure plan", "Premium SSD", 30, 16.75),
                                       ("", "Microsoft 365 E3", "Microsoft 365 E3", 50, 36.0),
                                       ("", "Azure Reserved VM Instance", "D4s v5 1 Year", 2, 102.5)]:
        sub = round(qty * price, 2)
        rows.append({"PartnerId": "p1", "CustomerId": "c1", "CustomerName": "Fabrikam", "InvoiceNumber": "G012345678",
                     "ProductId": "DZH318Z0BQ4L" if cat else "CFQ7TTC0LF8S", "SkuName": sku, "ProductName": prod,
                     "MeterCategory": cat, "SubscriptionId": "s1", "ChargeStartDate": "9/1/2026",
                     "ChargeEndDate": "9/30/2026", "ChargeType": "new" if not cat else "usage",
                     "UnitPrice": price, "EffectiveUnitPrice": price, "Quantity": qty, "BillableQuantity": qty,
                     "Subtotal": sub, "TaxTotal": round(sub * 0.08, 2), "TotalForCustomer": round(sub * 1.08, 2),
                     "Currency": "USD"})
    pd.DataFrame(rows).to_csv(OUT / "pc_invoice_recon.csv", index=False)


def focus_parquet() -> None:
    rows = []
    for d in DAYS:
        for svc, cat, cost in [("Compute Engine", "Compute", 210.0), ("Cloud Storage", "Storage", 35.5),
                               ("BigQuery", "Analytics", 88.25), ("Cloud SQL", "Databases", 61.0)]:
            c = round(cost * rnd.uniform(0.9, 1.1), 4)
            rows.append({"BilledCost": c, "EffectiveCost": round(c * 0.97, 4), "ListCost": round(c * 1.1, 4),
                         "BillingCurrency": "USD", "ChargePeriodStart": f"{d}T00:00:00Z",
                         "ChargePeriodEnd": f"{d + timedelta(days=1)}T00:00:00Z", "ChargeCategory": "Usage",
                         "ProviderName": "Google Cloud", "ServiceName": svc, "ServiceCategory": cat,
                         "SubAccountId": "my-project", "RegionId": "us-central1", "ConsumedQuantity": 1.0,
                         "ConsumedUnit": "unit"})
    pd.DataFrame(rows).to_parquet(OUT / "focus_gcp.parquet", index=False)


def gcp_json() -> None:
    recs = []
    for d in DAYS[:10]:
        for sid, svc, sku, cost in [("6F81-5844-456A", "Compute Engine", "N2 Instance Core running in Americas", 120.0),
                                    ("95FF-2EF5-5EA1", "Cloud Storage", "Standard Storage US Multi-region", 12.0)]:
            recs.append({"billing_account_id": "01ABCD-234567-89EFGH",
                         "service": {"id": sid, "description": svc}, "sku": {"id": "AAAA-BBBB", "description": sku},
                         "usage_start_time": f"{d}T00:00:00Z", "usage_end_time": f"{d}T23:59:59Z",
                         "project": {"id": "shop-prod", "name": "shop-prod"}, "location": {"region": "us-east1"},
                         "cost": cost, "currency": "USD", "usage": {"amount_in_pricing_units": 24, "pricing_unit": "hour"},
                         "credits": [{"name": "Sustained use discount", "amount": -round(cost * 0.2, 2), "type": "SUSTAINED_USAGE_DISCOUNT"}],
                         "cost_type": "regular"})
    (OUT / "gcp_export.json").write_text(json.dumps(recs))


def cost_explorer() -> None:
    svcs = ["EC2-Instances($)", "Relational Database Service($)", "S3($)", "EC2-Other($)"]
    data = [[round(rnd.uniform(100, 400), 2) for _ in svcs] for _ in range(6)]
    months = ["2026-04-01", "2026-05-01", "2026-06-01", "2026-07-01", "2026-08-01", "2026-09-01"]
    lines = ["Service," + ",".join(svcs) + ",Total costs($)"]
    tot = [round(sum(r[i] for r in data), 2) for i in range(len(svcs))]
    lines.append("Service total," + ",".join(map(str, tot)) + f",{round(sum(tot), 2)}")
    for m, r in zip(months, data):
        lines.append(m + "," + ",".join(map(str, r)) + f",{round(sum(r), 2)}")
    (OUT / "aws_cost_explorer.csv").write_text("\n".join(lines))


def messy_csv() -> None:
    rows = [("Amazon EC2", "us-east-1", 4210.55), ("Amazon RDS", "us-east-1", 1820.10),
            ("Amazon S3", "us-east-1", 312.40), ("Amazon CloudFront", "global", 95.00)]
    total = round(sum(r[2] for r in rows), 2)
    text = "Monthly cloud spend report\nGenerated 2026-10-02 by FinOps team\n\n"
    text += "Service Name,Region,Monthly Cost (USD)\n"
    text += "\n".join(f"{a},{b},\"${c:,.2f}\"" for a, b, c in rows)
    text += f"\nTotal,,\"${total:,.2f}\"\n"
    (OUT / "messy_report.csv").write_text(text)


def custom_workbook() -> None:
    import openpyxl
    from openpyxl.styles import Alignment, Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cloud Charges"
    ws["A1"] = "Northwind Managed Cloud - Monthly Statement"
    ws.merge_cells("A1:E1")
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"], ws["B2"] = "Customer:", "Tailspin Toys"
    ws["A3"], ws["B3"] = "Period:", "September 2026"
    ws.append([])
    ws.append(["Item", "Details", "Qty", "Unit Price", "Amount"])
    for c in ws[5]:
        c.font = Font(bold=True)
    groups = {"Azure Compute": [("Virtual Machines", "D4s v5 - East US", 1440, 0.192),
                                ("Virtual Machines", "E8s v5 - East US", 720, 0.504),
                                ("Azure Kubernetes Service", "Uptime SLA", 720, 0.10)],
              "Azure Storage": [("Storage", "Premium SSD P30", 4, 135.17), ("Storage", "Blob Hot LRS 2 TB", 2048, 0.0184)],
              "Azure Databases": [("SQL Database", "General Purpose 4 vCore", 720, 0.505)]}
    r = 6
    for g, items in groups.items():
        ws.cell(r, 1, g).font = Font(bold=True)
        r += 1
        first = r
        for svc, det, q, p in items:
            ws.cell(r, 1, svc).alignment = Alignment(indent=1)
            ws.cell(r, 2, det)
            ws.cell(r, 3, q)
            ws.cell(r, 4, p)
            ws.cell(r, 5, f"=C{r}*D{r}")
            r += 1
        ws.cell(r, 4, "Subtotal").font = Font(bold=True)
        ws.cell(r, 5, f"=SUM(E{first}:E{r - 1})").font = Font(bold=True)
        r += 2
    notes = wb.create_sheet("Notes")
    notes["A1"] = "Prices exclude tax. Contact billing@northwind.example for questions."
    _save_recalculated(wb, OUT / "custom_workbook.xlsx")


def pivot_workbook() -> None:
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Spend by month"
    ws["A1"] = "Azure spend optimizer - 6 month view"
    ws["A1"].font = Font(bold=True)
    months = ["Apr 2026", "May 2026", "Jun 2026", "Jul 2026", "Aug 2026", "Sep 2026"]
    ws.append([])
    ws.append(["Service", *months, "Total"])
    svcs = {"Virtual Machines": 5200, "Storage": 900, "SQL Database": 1500, "Azure Kubernetes Service": 700, "Bandwidth": 210}
    r = 4
    for s, base in svcs.items():
        ws.cell(r, 1, s)
        for j in range(6):
            ws.cell(r, 2 + j, round(base * (1 + 0.02 * j) * rnd.uniform(0.97, 1.03), 2))
        ws.cell(r, 8, f"=SUM(B{r}:G{r})")
        r += 1
    ws.cell(r, 1, "Total").font = Font(bold=True)
    for j in range(7):
        col = "BCDEFGH"[j]
        ws.cell(r, 2 + j, f"=SUM({col}4:{col}{r - 1})")
    _save_recalculated(wb, OUT / "spend_optimizer_pivot.xlsx")


def _save_recalculated(wb, path: Path) -> None:
    """Real Excel files carry cached formula values; openpyxl doesn't write them. LibreOffice recalculates."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src" / path.name
        src.parent.mkdir()
        wb.save(src)
        try:
            subprocess.run(["soffice", "--headless", "--calc", "--convert-to", "xlsx", "--outdir", d, str(src)],
                           check=True, capture_output=True, timeout=120)
            shutil.copy(Path(d) / path.name, path)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            wb.save(path)                                  # formulas without cached values


def reseller_pdf() -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(OUT / "reseller_invoice.pdf"), pagesize=letter)
    W, H = letter
    cols = [50, 330, 380, 440, 520]       # Description, Qty, Unit, Unit Price, Amount(right edge)

    def header(y):
        c.setFont("Helvetica-Bold", 9)
        for x, t in zip(cols, ["Description", "Qty", "Unit", "Unit Price", "Amount"]):
            c.drawString(x, y, t) if t != "Amount" else c.drawRightString(x + 40, y, t)
        return y - 16

    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, H - 50, "Contoso Cloud Partners LLC")
    c.setFont("Helvetica", 9)
    c.drawString(50, H - 66, "100 Main Street, Springfield")
    c.drawString(380, H - 50, "Invoice Number: INV-2026-0912")
    c.drawString(380, H - 64, "Billing Period: 09/01/2026 - 09/30/2026")
    c.drawString(380, H - 78, "Customer: Wide World Importers")
    y = header(H - 120)
    groups = {"Microsoft Azure": [("Virtual Machines D4s v5 East US", 1440, "Hours", 0.1920),
                                  ("Virtual Machines E8s v5 East US (reserved capacity not applied, billed at pay-as-you-go rates)", 720, "Hours", 0.5040),
                                  ("Premium SSD Managed Disks P30", 4, "Disks", 135.1700),
                                  ("Azure SQL Database General Purpose", 720, "Hours", 0.5050)],
              "Amazon Web Services": [("Amazon EC2 m5.xlarge", 2160, "Hours", 0.1920),
                                      ("Amazon RDS db.r6g.large", 720, "Hours", 0.2500),
                                      ("Amazon S3 Standard Storage", 5120, "GB-Mo", 0.0230)]}
    groups["Amazon Web Services"] += [(f"Amazon EC2 t3.medium instance {i}", 720, "Hours", 0.0416) for i in range(1, 31)]
    subtotal = 0.0
    for g, items in groups.items():
        c.setFont("Helvetica-Bold", 9)
        c.drawString(50, y, g)
        y -= 14
        for desc, q, unit, p in items:
            if y < 80:
                c.showPage()
                y = header(H - 60)
            amt = round(q * p, 2)
            subtotal += amt
            c.setFont("Helvetica", 9)
            first, rest = (desc[:55], desc[55:]) if len(desc) > 55 else (desc, "")
            c.drawString(60, y, first)
            c.drawRightString(cols[1] + 30, y, f"{q:,}")
            c.drawString(cols[2], y, unit)
            c.drawRightString(cols[3] + 50, y, f"${p:,.4f}")
            c.drawRightString(cols[4] + 40, y, f"${amt:,.2f}")
            y -= 12
            if rest:
                c.drawString(60, y, rest.strip())
                y -= 12
        y -= 6
    tax = round(subtotal * 0.0825, 2)
    c.setFont("Helvetica", 9)
    for label, val in [("Subtotal", subtotal), ("Sales Tax (8.25%)", tax)]:
        c.drawString(cols[3] - 60, y, label)
        c.drawRightString(cols[4] + 40, y, f"${val:,.2f}")
        y -= 14
    c.setFont("Helvetica-Bold", 10)
    c.drawString(cols[3] - 60, y, "Total Due")
    c.drawRightString(cols[4] + 40, y, f"${subtotal + tax:,.2f}")
    c.save()


def list_pdf() -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(OUT / "summary_invoice.pdf"), pagesize=letter)
    W, H = letter
    c.setFont("Helvetica-Bold", 13)
    c.drawString(50, H - 50, "Blue Yonder IT Services - Cloud Usage Summary")
    c.setFont("Helvetica", 10)
    c.drawString(50, H - 70, "Statement date: October 3, 2026")
    items = [("Amazon EC2", 8423.17), ("Amazon RDS", 2210.40), ("Amazon S3", 512.88), ("AWS Lambda", 41.07),
             ("Amazon CloudFront", 188.10), ("AWS Support (Business)", 1137.56)]
    y = H - 110
    for name, amt in items:
        dots = "." * max(3, 60 - len(name))
        c.drawString(50, y, f"{name} {dots} ${amt:,.2f}")
        y -= 16
    y -= 10
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y, f"Total Due: ${sum(a for _, a in items):,.2f}")
    c.save()


def custom_json() -> None:
    lines = [{"svc": "Virtual Machines", "detail": "D4s v5", "qty": "1440", "amt": "$276.48"},
             {"svc": "Storage", "detail": "Blob Hot", "qty": "2048", "amt": "$37.68"},
             {"svc": "SQL Database", "detail": "GP 4 vCore", "qty": "720", "amt": "$363.60"}]
    doc = {"customer": {"name": "Litware"}, "invoice": {"number": "LW-77", "lines": lines, "grand": "$677.76"}}
    (OUT / "custom_api.json").write_text(json.dumps(doc, indent=1))


def german_xlsx(path: Path, month: str = "September 2026", amount_header: str = "Betrag", scale: float = 1.0) -> None:
    """EU reseller invoice: German headers, decimal commas, total row. The rule mapper can't read it."""
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rechnung"
    ws["A1"] = "Fabrikam Cloud GmbH - Rechnung"
    ws["A1"].font = Font(bold=True)
    ws["A2"] = f"Leistungszeitraum: {month}"
    ws.append([])
    ws.append(["Leistung", "Menge", "Einzelpreis", amount_header])
    items = [("Virtual Machines D4s v5", 1440, 0.1920), ("Azure SQL Database", 720, 0.5050),
             ("Storage Premium SSD", 4, 135.1700), ("Bandwidth", 512, 0.0870)]
    total = 0.0
    for name, q, p in items:
        amt = round(q * p * scale, 2)
        total += amt
        ws.append([name, q, f"{p * scale:.4f}".replace(".", ","), f"{amt:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")])
    ws.append(["Gesamtbetrag", None, None, f"{total:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")])
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    wb.save(path)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for fn in (aws_cur2, azure_ea, pc_invoice_recon, focus_parquet, gcp_json, cost_explorer, messy_csv,
               custom_workbook, pivot_workbook, reseller_pdf, list_pdf, custom_json):
        fn()
        print("ok", fn.__name__)
    german_xlsx(OUT / "german_invoice.xlsx")
    print("ok german_xlsx")
