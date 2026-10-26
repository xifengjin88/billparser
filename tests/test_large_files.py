"""Large files (bigfile.py): the large path gives the small path's results, chunk by chunk, within its memory, and
the API stays responsive. The large path is forced on small files by setting LARGE_FILE_MB very low; no generated
large file is committed (scripts/bench_large.py writes them to a temp directory)."""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import billparse.analyze as analyze_mod
import billparse.api as api
from billparse import bigfile, coverage, webapi
from billparse.pipeline import Pipeline, Settings
from billparse.transforms import map_unique, t_charge_type, t_strip
from billparse.validate import _provider_of

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "samples" / "files"
sys.path.insert(0, str(ROOT / "scripts"))
import bench_large  # noqa: E402

TINY = "0.000001"                                       # LARGE_FILE_MB: every file is large


def _pipe(tmp_path: Path, name: str = "store") -> Pipeline:
    raw = tmp_path / "data" / "raw"
    if not raw.exists():
        raw.mkdir(parents=True)
        for f in (ROOT / "tests" / "fixtures" / "raw").glob("*.json"):
            shutil.copy(f, raw / f.name)
    analyze_mod._RESOLVER = None
    return Pipeline(Settings(store_dir=tmp_path / name, catalog_dir=tmp_path / "data", use_llm=False,
                             use_agent=False))


def _facts(pipe: Pipeline, r: dict) -> dict:
    """What both paths must agree on: rows, verdict, billed totals by charge type and service, the coverage split."""
    rows = pipe.store.get_rows(r["sha256"])
    out = {"rows": r["rows"], "status": r["status"], "completeness": r["completeness"],
           "confidence": r["confidence"], "checks": [(c["name"], c["status"]) for c in r["checks"]],
           "cost_basis": (r.get("header_fields") or {}).get("cost_basis")}
    for k in ("charge_type", "service", "coverage"):
        sums = rows.groupby(rows[k].astype("string").fillna("-"))["billed_cost"].sum()
        out[k] = {str(a): round(float(b), 2) for a, b in sums.items()}
    a = r["analysis"]
    out["analysis"] = {k: a[k] for k in ("total_billed", "usage_spend", "by_category", "by_provider", "period",
                                         "monthly", "by_service_and_type")}
    return out


def _cents(x):
    """Money to the cent: the large path adds the same amounts in another order, so a sum can round 1 cent apart."""
    if isinstance(x, float):
        return round(x, 2)
    if isinstance(x, dict):
        return {k: _cents(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_cents(v) for v in x]
    return x


def _same(a: dict, b: dict) -> None:
    assert {k: v for k, v in a.items() if k != "analysis"} == {k: v for k, v in b.items() if k != "analysis"}
    flat_a, flat_b = pd.json_normalize(a["analysis"]), pd.json_normalize(b["analysis"])
    assert list(flat_a.columns) == list(flat_b.columns)
    for c in flat_a.columns:
        x, y = flat_a[c][0], flat_b[c][0]
        if isinstance(x, float) and isinstance(y, float):
            assert abs(x - y) <= 0.0101, (c, x, y)
        elif isinstance(x, list):
            assert len(x) == len(y) and all(
                u.keys() == v.keys() and all(abs(u[k] - v[k]) <= 0.0101 if isinstance(u[k], float) else u[k] == v[k]
                                             for k in u) for u, v in zip(x, y, strict=True)), (c, x, y)
        else:
            assert x == y, (c, x, y)


def _both(tmp_path: Path, monkeypatch, f: Path, chunk: int) -> tuple[dict, dict, dict]:
    small = _pipe(tmp_path, "small")
    a = small.parse_path(f, agent=False)
    monkeypatch.setenv("LARGE_FILE_MB", TINY)
    monkeypatch.setenv("CHUNK_ROWS", str(chunk))
    large = _pipe(tmp_path, "large")
    b = large.parse_path(f, agent=False)
    monkeypatch.delenv("LARGE_FILE_MB")
    return _facts(small, a), _facts(large, b), b


# ---------------------------------------------------------------- same results
def test_same_results_on_a_300k_row_cur(tmp_path, monkeypatch):
    f = tmp_path / "cur.csv.gz"
    expected = bench_large.generate(f, 300_000)
    small, large, r = _both(tmp_path, monkeypatch, f, 100_000)
    assert r["large"]["chunks"] == 4 and r["large"]["source_rows"] == small["rows"]
    _same(small, large)
    assert small["analysis"]["total_billed"] == round(expected, 2)


@pytest.mark.parametrize("name", ["aws_cur2.csv.gz", "aws_cur2_commitments.csv.gz", "azure_actual.csv",
                                  "azure_amortized.csv", "azure_ea_usage.csv", "focus_commitments.parquet",
                                  "focus_gcp.parquet", "pc_invoice_recon.csv"])
def test_same_results_on_the_samples_in_small_chunks(tmp_path, monkeypatch, name):
    small, large, r = _both(tmp_path, monkeypatch, FILES / name, 3)
    assert r["large"]["chunks"] >= 2
    _same(small, large)


def test_zip_of_one_csv_is_read_in_chunks(tmp_path, monkeypatch):
    f = tmp_path / "export.zip"
    with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("usage/azure.csv", (FILES / "azure_ea_usage.csv").read_bytes())
    small, large, r = _both(tmp_path, monkeypatch, f, 100)
    _same(small, large)
    assert r["large"]["chunks"] >= 2


# ---------------------------------------------------------------- chunk boundaries
def _csv(path: Path, rows: list[dict]) -> Path:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


def test_chunk_boundaries(tmp_path, monkeypatch):
    rows = [{"Service": "Amazon EC2", "Description": f"line {i}", "Region": "", "Cost": "10.00"} for i in range(12)]
    rows[0]["Cost"] = "110.00"                           # = the sum of the 11 others: no total inside a chunk
    for r in rows[7:]:
        r["Region"] = "us-east-1"                        # empty in the first chunks, filled later
    f = _csv(tmp_path / "bill.csv", rows)
    monkeypatch.setenv("LARGE_FILE_MB", TINY)
    monkeypatch.setenv("CHUNK_ROWS", "5")
    pipe = _pipe(tmp_path)
    r = pipe.parse_path(f, agent=False)
    assert r["large"]["chunks"] == 3 and r["rows"] == 12
    out = pipe.store.get_rows(r["sha256"])
    assert out["_src"].tolist() == [f"csv:{i + 2}" for i in range(12)]     # the file's row numbers, every chunk
    assert out["region"].isna().tolist() == [True] * 7 + [False] * 5 and set(out["region"].dropna()) == {"us-east-1"}
    assert round(out["billed_cost"].sum(), 2) == 220.0 and not r["stated_totals"]


def test_the_sample_routes_and_the_spec_reads_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("LARGE_FILE_MB", TINY)
    monkeypatch.setenv("SAMPLE_ROWS", "20")
    monkeypatch.setenv("CHUNK_ROWS", "500")
    pipe = _pipe(tmp_path)
    r = pipe.parse_path(FILES / "aws_cur2.csv.gz", agent=False)
    assert r["spec"]["id"] == "aws-cur2" and r["rows"] == 1648 and r["large"]["chunks"] == 4
    bundle = json.loads(pipe.store.part(r["sha256"], "bundle"))
    assert len(bundle["tables"][0]["rows"]) == 21                    # the source viewer: the sample's header + rows


# ---------------------------------------------------------------- memory
@pytest.mark.skipif(not os.environ.get("BILLPARSE_SLOW"), reason="about a minute: set BILLPARSE_SLOW=1")
def test_memory_of_a_2m_row_parse(tmp_path):
    # First run (macOS, 2026-10-26): 2,099,500 rows, peak 962 MB above the baseline with CHUNK_ROWS=100000
    # (the in-memory path needs about 9 GB). The bound leaves room for other machines.
    f = tmp_path / "cur.csv.gz"
    bench_large.generate(f, 2_000_000)
    env = {**os.environ, "CHUNK_ROWS": "100000", "LARGE_FILE_MB": "10"}
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "bench_large.py"), "--child", str(f), "--entry",
                          "path"], capture_output=True, text=True, cwd=ROOT, env=env, check=True)
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["rows"] > 2_000_000
    assert res["above_baseline_mb"] < 1600, res


# ---------------------------------------------------------------- guards
def _flags(pipe: Pipeline, r: dict) -> str:
    return " ".join(m["message"] for m in json.loads(pipe.store.part(r["sha256"], "meta"))["flags"])


@pytest.mark.parametrize("max_mb", [3, 12])
def test_gzip_past_the_uncompressed_limit_is_refused(tmp_path, monkeypatch, max_mb):
    f = tmp_path / "huge.csv.gz"
    bench_large.generate(f, 60_000)                     # about 18 MB of CSV
    monkeypatch.setenv("LARGE_FILE_MB", "1")            # the size check reads the first 8 MB
    # 3 MB: refused by the size check; 12 MB: refused while the file is read
    monkeypatch.setenv("MAX_UNCOMPRESSED_GB", str(max_mb / 1024))
    pipe = _pipe(tmp_path)
    r = pipe.parse_path(f, agent=False)
    assert r["status"] == "unsupported" and r["rows"] == 0
    assert "MAX_UNCOMPRESSED_GB" in _flags(pipe, r) and "Split the export" in _flags(pipe, r)
    assert not list((tmp_path / "store" / "tmp").glob("*.parquet"))            # no rows file left behind


def test_large_excel_gets_the_export_as_csv_message(tmp_path, monkeypatch):
    monkeypatch.setenv("LARGE_FILE_MB", TINY)
    pipe = _pipe(tmp_path)
    r = pipe.parse_path(FILES / "custom_workbook.xlsx", agent=False)
    assert r["status"] == "unsupported" and "Export this report as CSV" in _flags(pipe, r)
    assert pipe.store.file_path(r["sha256"]) is not None                     # kept, to parse again later


def test_a_reshaping_spec_cannot_read_a_large_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LARGE_FILE_MB", TINY)
    pipe = _pipe(tmp_path)
    r = pipe.parse_path(FILES / "aws_cost_explorer.csv", agent=False)
    assert r["status"] == "unsupported"
    assert "reshape" in _flags(pipe, r)


def test_stored_large_file_gives_its_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("LARGE_FILE_MB", TINY)
    monkeypatch.setenv("SAMPLE_ROWS", "10")
    pipe = _pipe(tmp_path)
    r = pipe.parse_path(FILES / "azure_ea_usage.csv", agent=False)
    doc = bigfile.stored_doc(pipe.store, r["sha256"])
    assert len(doc.regions) == 1 and len(doc.regions[0].df) == 10


def test_regression_gate_reads_a_large_file_in_chunks(tmp_path, monkeypatch):
    from billparse.learn import _check
    monkeypatch.setenv("LARGE_FILE_MB", TINY)
    monkeypatch.setenv("CHUNK_ROWS", "100")
    pipe = _pipe(tmp_path)
    r = pipe.parse_path(FILES / "aws_cur2.csv.gz", agent=False)
    n, total, verdict = _check(pipe, pipe.store.get_spec("aws-cur2"), r["sha256"], pipe.store.file_path(r["sha256"]))
    assert n == r["rows"] and round(total, 2) == r["analysis"]["total_billed"] and verdict["status"] == r["status"]


# ---------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path, monkeypatch):
    pipe = _pipe(tmp_path)
    monkeypatch.setenv("CATALOG_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BILLPARSE_USE_LLM", "0")
    monkeypatch.setattr(webapi, "_large_pool", ThreadPoolExecutor(1, thread_name_prefix="parse-large"))
    webapi._pipeline.cache_clear()
    webapi._tasks.clear()
    api.app.dependency_overrides[webapi.get_store] = lambda: pipe.store
    api.app.dependency_overrides[webapi.get_pipeline] = lambda: pipe
    yield TestClient(api.app)
    webapi._large_pool.shutdown(wait=True)
    api.app.dependency_overrides.clear()
    webapi._pipeline.cache_clear()
    analyze_mod._RESOLVER = None


def test_api_stays_responsive_and_queues_a_second_large_parse(client, tmp_path, monkeypatch):
    first, second = tmp_path / "a.csv.gz", tmp_path / "b.csv.gz"
    bench_large.generate(first, 200_000)
    bench_large.generate(second, 1_000, seed=8)
    monkeypatch.setenv("LARGE_FILE_MB", TINY)
    monkeypatch.setenv("CHUNK_ROWS", "20000")
    t1 = client.post("/parse/start", files={"file": ("a.csv.gz", first.read_bytes())}).json()
    t2 = client.post("/parse/start", files={"file": ("b.csv.gz", second.read_bytes())}).json()
    assert t2["state"] == "running" and t2["progress"] == "queued"          # one large parse at a time
    slow = []
    while client.get(f"/parse/tasks/{t1['sha256']}").json()["state"] == "running":
        t0 = time.perf_counter()
        assert client.get("/health").status_code == 200
        slow.append(time.perf_counter() - t0)
        time.sleep(0.05)
    assert slow and max(slow) < 0.2, slow
    deadline = time.time() + 60
    while (t := client.get(f"/parse/tasks/{t2['sha256']}").json())["state"] == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert t["state"] == "done" and t["status"] == "auto_accepted"
    view = client.get(f"/documents/{t1['sha256']}/view").json()
    assert view["charges"]
    assert not list((tmp_path / "store" / "tmp").glob("*"))                # uploads moved into the store


def test_upload_goes_to_disk_with_its_hash(tmp_path):
    from fastapi import UploadFile

    from billparse.store import Store
    store = Store(tmp_path / "s")
    data = (FILES / "azure_actual.csv").read_bytes()
    path, sha, size = webapi.save_upload(UploadFile(io.BytesIO(data), filename="x.csv"), store)
    assert path.read_bytes() == data and size == len(data)
    assert sha == __import__("hashlib").sha256(data).hexdigest()


# ---------------------------------------------------------------- hot paths
def test_hot_paths_on_a_million_rows():
    # 1,000,000 rows (macOS, 2026-10-26), main -> this branch: t_strip 0.15 -> 0.13 s, t_charge_type 0.53 -> 0.12 s,
    # _provider_of 1.22 -> 0.12 s, coverage.apply 3.06 -> 1.54 s. The bounds leave room for slower machines.
    n = 1_000_000
    rng = np.random.default_rng(1)
    words = np.array([" Amazon Elastic Compute Cloud ", "AWS Lambda", "Amazon Web Services", "Azure", ""]
                     + [f"svc {i}" for i in range(30)], dtype=object)
    types = np.array(["Usage", "DiscountedUsage", "SavingsPlanCoveredUsage", "SavingsPlanNegation", "Tax", "RIFee"],
                     dtype=object)
    s, ct = pd.Series(rng.choice(words, n)), pd.Series(rng.choice(types, n))
    usage = pd.Series(rng.choice(np.array(["USE1-BoxUsage:m5.large", "USE1-SpotUsage:c5.large"], dtype=object), n))

    def secs(f) -> float:
        t0 = time.perf_counter()
        f()
        return time.perf_counter() - t0
    assert secs(lambda: t_strip(s)) < 1.5
    assert secs(lambda: t_charge_type(ct)) < 1.5
    assert secs(lambda: map_unique(s, _provider_of)) < 1.5
    rows = pd.DataFrame({"charge_type": t_charge_type(ct), "description": s, "service_raw": s, "line_type_raw": ct,
                         "usage_type": usage})
    assert secs(lambda: coverage.apply(rows)) < 6.0


def test_gzip_classify_stops_early(tmp_path, monkeypatch):
    f = tmp_path / "x.csv.gz"
    with gzip.open(f, "wb") as g:
        g.write(b"a,b\n" + b"1,2\n" * 10_000_000)       # 40 MB
    monkeypatch.setenv("LARGE_FILE_MB", "1")
    info = bigfile.classify(f)
    assert info.large and info.fmt == "csv" and info.size <= 8 * 1024 * 1024   # one 8 MB read, not 40 MB
