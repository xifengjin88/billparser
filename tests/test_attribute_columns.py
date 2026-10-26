"""Task 03: attribute, ID and commitment fields read from the export's own columns (instance type, OS, usage type,
operation, SKU, meter, resource, commitment, unit prices), on the small and the large path."""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import billparse.analyze as analyze_mod
import billparse.api as api
from billparse import Pipeline, Settings, webapi
from billparse.schema import SCHEMA_VERSION, Document
from billparse.transforms import t_json_get

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "samples" / "files"
AWS = ["aws_cur_legacy_full.csv.gz", "aws_cur2_flat.csv.gz", "aws_cur2_map.csv.gz"]
SAMPLES = {**{n: "aws" for n in AWS}, "azure_usage_full.csv": "azure", "focus_full.parquet": "focus",
           "gcp_export_full.json": "gcp"}


def _pipe(tmp_path: Path, name: str = "store") -> Pipeline:
    raw = tmp_path / "data" / "raw"
    if not raw.exists():
        raw.mkdir(parents=True)
        for f in (ROOT / "data" / "raw").glob("*.json"):
            shutil.copy(f, raw / f.name)
    analyze_mod._RESOLVER = None
    return Pipeline(Settings(store_dir=tmp_path / name, catalog_dir=tmp_path / "data", use_llm=False,
                             use_agent=False))


@pytest.fixture
def pipe(tmp_path):
    yield _pipe(tmp_path)
    analyze_mod._RESOLVER = None


def parse(pipe: Pipeline, name: str) -> tuple[dict, Document]:
    r = pipe.parse_file(FILES / name)
    assert r["spec"]["route"] == "builtin" and r["status"] in ("reconciled", "unreconciled"), r["checks"]
    return r, Document.model_validate_json(pipe.store.part(r["sha256"], "doc"))


def v(cv):
    return None if cv is None else cv.value


def compute(doc: Document) -> list:
    """Usage lines of an instance (compute or database): not data transfer, storage, fees or tax."""
    out = []
    for c in doc.charges:
        text = " ".join(str(x) for x in (v(c.usage_type), v(c.description), c.attributes.get("service_raw")))
        if c.kind.value == "usage" and any(w in text for w in ("BoxUsage", "InstanceUsage", "Virtual Machines",
                                                              "Instance Core", "Instance Hour")):
            out.append(c)
    return out


def test_schema_version():
    assert SCHEMA_VERSION == "1.5"


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_instance_lines_have_their_attributes(pipe, name):
    _, doc = parse(pipe, name)
    lines = compute(doc)
    assert lines and all(c.instance_type is not None for c in lines), [v(c.description) for c in lines]
    if SAMPLES[name] == "aws":
        ec2 = [c for c in lines if "BoxUsage" in str(v(c.usage_type))]
        assert ec2 and all(c.operating_system and c.usage_type and c.operation for c in ec2)
        assert all(v(c.operation) == "RunInstances" and v(c.tenancy) == "Shared" for c in ec2)
        db = [c for c in lines if "InstanceUsage" in str(v(c.usage_type))]
        assert db and all(v(c.database_engine) == "PostgreSQL" and v(c.operation).startswith("CreateDBInstance")
                          for c in db)
        assert all(c.sku_id and c.sku_price_id and c.resource_id and c.list_unit_price for c in lines)
    if SAMPLES[name] == "azure":
        assert all(c.meter_id is not None and c.resource_id is not None for c in doc.charges)
        assert all(c.list_unit_price is not None and c.contracted_unit_price is not None for c in doc.charges)
    if SAMPLES[name] in ("focus", "gcp"):
        assert all(c.sku_id is not None and c.resource_id is not None for c in doc.charges)
    covered = [c for c in doc.charges if v(c.coverage) in ("reserved", "savings_plan", "committed_use")]
    assert all(c.commitment_id is not None for c in covered), [v(c.description) for c in covered]


def test_aws_fields_by_their_columns(pipe):
    for name in AWS:
        _, doc = parse(pipe, name)
        ec2 = next(c for c in compute(doc) if "m5.xlarge" in str(v(c.usage_type)))
        assert (v(ec2.instance_type), v(ec2.pricing_term), v(ec2.license_model)) == ("m5.xlarge", "OnDemand",
                                                                                     "No License required")
        assert v(ec2.resource_id) == "i-0abc123def4567890" and v(ec2.list_unit_price) == Decimal("0.192")
        fee = next(c for c in doc.charges if v(c.line_type_raw) == "RIFee")
        assert v(fee.purchase_option) == "No Upfront" and v(fee.instance_type) == "db.r6g.large"


def test_azure_meter_names_are_sizes(pipe):
    _, doc = parse(pipe, "azure_usage_full.csv")
    by_meter = {}
    for c in doc.charges:
        by_meter.setdefault(str(v(c.description)), c)
    assert v(by_meter["D4s v5"].instance_type) == "Standard_D4s_v5"            # from AdditionalInfo ServiceType
    spot = by_meter["D4s v5 Spot"]                                             # from the meter name
    assert v(spot.instance_type) == "Standard_D4s_v5" and spot.attributes["meter_name"] == "D4s v5 Spot"
    assert v(spot.pricing_model) == "Spot"
    assert v(by_meter["B2ms"].instance_type) == "Standard_B2ms"
    assert by_meter["P10 Disks"].instance_type is None                         # a disk is not a VM size
    assert v(by_meter["B2ms"].commitment_id).endswith("savingsPlans/def")      # BenefitId
    assert v(by_meter["E8ds v5"].commitment_id) == "11111111-2222-3333-4444-555555555555"   # ReservationId


def test_gcp_machine_type_from_system_labels(pipe):
    _, doc = parse(pipe, "gcp_export_full.json")
    assert {v(c.instance_type) for c in doc.charges} == {"n2-standard-4", "e2-medium"}
    assert all(str(v(c.resource_id)).startswith("projects/shop-prod/zones/") for c in doc.charges)


def test_json_get():
    s = pd.Series(['{"ServiceType": "Standard_D4s_v5", "VCPUs": 4}', "", None, "not json",
                   [{"key": "compute.googleapis.com/machine_spec", "value": "n2-standard-4"}], {"ServiceType": " B2 "}])
    def got(key: str) -> list:
        return [x if isinstance(x, str) else None for x in t_json_get(s, key).tolist()]
    assert got("ServiceType") == ["Standard_D4s_v5", None, None, None, None, "B2"]
    assert got("compute.googleapis.com/machine_spec")[4] == "n2-standard-4"
    assert got("VCPUs")[0] == "4"


FIELDS = ["instance_type", "operating_system", "usage_type", "operation", "tenancy", "license_model", "sku_id",
          "sku_price_id", "meter_id", "resource_id", "pricing_term", "purchase_option", "commitment_id",
          "list_unit_price", "contracted_unit_price", "database_engine", "meter_name"]


@pytest.mark.parametrize("name", [*AWS, "azure_usage_full.csv", "focus_full.parquet"])
def test_the_large_path_fills_the_same_fields(tmp_path, monkeypatch, name):
    small = _pipe(tmp_path, "small")
    a = small.parse_file(FILES / name)
    monkeypatch.setenv("LARGE_FILE_MB", "0.000001")
    monkeypatch.setenv("CHUNK_ROWS", "4")
    large = _pipe(tmp_path, "large")
    b = large.parse_file(FILES / name)
    assert b["large"]["chunks"] >= 2
    ra, rb = small.store.get_rows(a["sha256"]), large.store.get_rows(b["sha256"])
    for f in FIELDS:
        if f in ra:
            x = ra[f].astype("string").fillna("").tolist()
            y = rb[f].astype("string").fillna("").tolist() if f in rb else [""] * len(rb)
            if f in ("list_unit_price", "contracted_unit_price"):
                x, y = pd.to_numeric(ra[f]).round(8).tolist(), pd.to_numeric(rb[f]).round(8).tolist()
                assert [round(q, 8) for q in x] == [round(q, 8) for q in y], f
            else:
                assert x == y, f


@pytest.fixture
def client(tmp_path, monkeypatch):
    pipe = _pipe(tmp_path)
    monkeypatch.setenv("CATALOG_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BILLPARSE_USE_LLM", "0")
    api.app.dependency_overrides[webapi.get_store] = lambda: pipe.store
    api.app.dependency_overrides[webapi.get_pipeline] = lambda: pipe
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()
    analyze_mod._RESOLVER = None


def test_parse_returns_the_new_fields(client):
    with open(FILES / "aws_cur_legacy_full.csv.gz", "rb") as fh:
        r = client.post("/parse", files={"file": ("aws_cur_legacy_full.csv.gz", fh)})
    assert r.status_code == 200, r.text
    doc = r.json()["document"]
    assert doc["schema_version"] == "1.5"
    ec2 = next(c for c in doc["charges"] if (c.get("usage_type") or {}).get("value") == "USE1-BoxUsage:m5.xlarge")
    for f in ("tenancy", "license_model", "resource_id", "pricing_term", "list_unit_price", "sku_id", "sku_price_id",
              "operation"):
        assert ec2.get(f) and ec2[f]["value"] not in (None, ""), f
    azure = client.post("/parse", files={"file": ("azure_usage_full.csv", (FILES / "azure_usage_full.csv").read_bytes())})
    c = azure.json()["document"]["charges"][0]
    assert c["contracted_unit_price"]["value"] and c["meter_id"]["value"]
    assert json.dumps(c["resource_id"]).count("virtualMachines") == 1


def test_the_web_app_shows_the_new_fields(client):
    with open(FILES / "aws_cur_legacy_full.csv.gz", "rb") as fh:
        r = client.post("/parse", files={"file": ("aws_cur_legacy_full.csv.gz", fh)}).json()
    sha = r["sha256"]
    ec2 = next(c for c in r["document"]["charges"]
               if (c.get("usage_type") or {}).get("value") == "USE1-BoxUsage:m5.xlarge")
    detail = client.get(f"/documents/{sha}/charges/{ec2['id']}").json()
    shown = {f["field"] for f in detail["fields"]}
    assert {"sku_id", "sku_price_id", "resource_id", "tenancy", "license_model", "pricing_term", "operation",
            "list_unit_price"} <= shown, shown
