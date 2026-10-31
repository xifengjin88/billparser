"""The document's provider from the file's own evidence: its name, its headers, its values and the lines whose service
resolved; lines that do not say get it."""

from __future__ import annotations

import pandas as pd

from billparse.provider import detect_provider, fill_provider


def _azure_reseller() -> pd.DataFrame:
    return pd.DataFrame({"description": ["Alerts Metric Monitored", "D4s v5", "P10 LRS Disk"],
                         "resource_id": ["0c873ca2-4113-4997-8cf6-ad5df5775599"] * 3,
                         "provider": [None, "azure", None]})


AZURE_HEADERS = ["Customer#", "Tenant Id", "Subscription Id", "Subscription Name", "Resource Group", "Meter Name",
                 "Resource Type", "Consumed Qty", "Unit", "MSRP", "Extended MSRP"]


def test_an_azure_reseller_export_is_azure_from_its_name_headers_and_lines():
    got = detect_provider(_azure_reseller(), AZURE_HEADERS, "caretria-azure-export.zip")
    assert got is not None and got["value"] == "azure"
    why = " ".join(got["from"])
    assert "file name" in why and "Meter Name" in why and "Subscription Id" in why


def test_headers_alone_tell_aws_azure_and_gcp():
    empty = pd.DataFrame({"billed_cost": [1.0]})
    assert detect_provider(empty, ["lineItem/UsageType", "lineItem/UnblendedCost", "product/region"], "x.csv")["value"] == "aws"
    assert detect_provider(empty, ["MeterCategory", "MeterName", "SubscriptionId", "ResourceGroup"], "x.csv")["value"] == "azure"
    assert detect_provider(empty, ["project.id", "sku.id", "billing_account_id", "service.description"], "x.csv")["value"] == "gcp"


def test_values_tell_the_provider():
    arm = pd.DataFrame({"resource_id": ["/subscriptions/a/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/v"] * 3,
                        "region": ["eastus"] * 3})
    assert detect_provider(arm, ["Cost"], "bill.csv")["value"] == "azure"
    arn = pd.DataFrame({"resource_id": ["arn:aws:ec2:us-east-1:123456789012:instance/i-1"] * 3,
                        "region": ["us-east-1"] * 3})
    assert detect_provider(arn, ["Cost"], "bill.csv")["value"] == "aws"


def test_too_little_or_mixed_evidence_says_nothing():
    plain = pd.DataFrame({"description": ["Consulting", "Support"], "billed_cost": [1.0, 2.0]})
    assert detect_provider(plain, ["Description", "Amount"], "invoice.pdf") is None
    mixed = pd.DataFrame({"provider": ["aws"] * 10 + ["azure"] * 10})       # a multi-cloud reseller invoice
    assert detect_provider(mixed, ["Service", "Amount"], "invoice.pdf") is None


def test_fill_gives_lines_without_a_provider_the_documents_and_keeps_the_others():
    rows = pd.DataFrame({"provider": [None, "aws", None]})
    n = fill_provider(rows, {"value": "azure", "from": ["file name"]})
    assert n == 2 and rows["provider"].tolist() == ["azure", "aws", "azure"]
