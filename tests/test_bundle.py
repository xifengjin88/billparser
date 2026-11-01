"""A zip of separate files is one upload per file (bundle.py); a zip that is one document stays one upload."""

from __future__ import annotations

import gzip
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "samples" / "files"
sys.path.insert(0, str(ROOT / "tests"))

from test_jobs import client, store  # noqa: E402,F401 - the fixtures

from billparse import bundle  # noqa: E402
from billparse.pipeline import Pipeline, Settings  # noqa: E402

LIMIT = 50 * 1024 * 1024


def _zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def _temp(tmp_path: Path):
    n = iter(range(1000))
    return lambda suffix: tmp_path / f"part{next(n)}{suffix}"


def test_a_zip_of_bills_is_one_upload_per_file(tmp_path):
    pdfs = {"bills/a.pdf": (FILES / "summary_invoice.pdf").read_bytes(),
            "b.pdf": (FILES / "reseller_invoice.pdf").read_bytes(), "__MACOSX/._b.pdf": b"junk"}
    parts = bundle.separate_files(_zip(tmp_path / "bills.zip", pdfs), "bills.zip", _temp(tmp_path), LIMIT)
    assert [p.name for p in parts] == ["a.pdf", "b.pdf"]
    assert parts[1].path.read_bytes() == pdfs["b.pdf"]


@pytest.mark.parametrize("files", [
    {"part_0.csv.gz": gzip.compress(b"a,b\n1,2\n"), "part_1.csv.gz": gzip.compress(b"a,b\n3,4\n"),
     "export-Manifest.json": b"{}"},                               # the parts of one export, with its manifest
    {"tf/main.tf": b'resource "aws_instance" "x" {}', "tf/vars.tf": b'variable "y" {}'},   # a Terraform project
    {"only.pdf": b"%PDF-1.4"},                                      # one file
])
def test_a_zip_that_is_one_document_stays_one_upload(tmp_path, files):
    assert bundle.separate_files(_zip(tmp_path / "one.zip", files), "one.zip", _temp(tmp_path), LIMIT) is None


def test_reports_with_different_columns_are_separate_files(tmp_path):
    files = {"cost.csv": b"service,cost\nEC2,1.00\n", "usage.csv": b"date,hours\n2026-01-01,24\n"}
    parts = bundle.separate_files(_zip(tmp_path / "r.zip", files), "r.zip", _temp(tmp_path), LIMIT)
    assert [p.name for p in parts] == ["cost.csv", "usage.csv"]


def test_a_workbook_is_never_a_bundle(tmp_path):
    assert bundle.separate_files(FILES / "custom_workbook.xlsx", "custom_workbook.xlsx", _temp(tmp_path), LIMIT) is None


def test_an_upload_of_a_zip_of_bills_starts_a_parse_of_each_file(client, store, tmp_path):  # noqa: F811
    z = _zip(tmp_path / "bills.zip", {"a.pdf": (FILES / "summary_invoice.pdf").read_bytes(),
                                      "b.pdf": (FILES / "reseller_invoice.pdf").read_bytes()})
    task = client.post("/parse/start", files={"file": ("bills.zip", z.read_bytes())}).json()
    assert [t["name"] for t in task["split"]] == ["a.pdf", "b.pdf"] and task["name"] == "a.pdf"
    assert {t["name"] for t in client.get("/parse/tasks").json()} == {"a.pdf", "b.pdf"}
    job = client.post("/parse/jobs", files={"file": ("bills.zip", z.read_bytes())})
    assert job.status_code == 200 and len(job.json()["split"]) == 2                 # the same files: their jobs
    r = client.post("/parse", files={"file": ("bills.zip", io.BytesIO(z.read_bytes()))})
    assert r.status_code == 422 and "2 separate files" in r.json()["detail"]


def test_a_builtin_specs_notes_are_a_reconciliation_note_not_a_doubt(tmp_path):
    """The notes of a builtin spec say how it reads its layout: shown as a note, never as an uncertain decision."""
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", use_llm=False, use_agent=False))
    files = {"cost.csv": b"ResourceGroup,Resource,ResourceId,ResourceType,ResourceLocation,Cost,CostUSD,Currency\n"
                         b"rg,vm1,/subscriptions/s/vm1,Virtual machine,US East,10.00,10.00,USD\n"}
    path = tmp_path / "cost.csv"
    path.write_bytes(files["cost.csv"])
    r = pipe.parse_file(path)
    assert r["spec"]["id"] == "azure-cost-by-resource"
    meta = json.loads(pipe.store.part(r["sha256"], "meta"))
    note = "the service is the resource type"
    assert any(note in n["message"] for n in meta["notes"])
    assert not any(note in f["message"] for f in meta["flags"])
