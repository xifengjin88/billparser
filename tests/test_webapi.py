"""The web app's endpoints, with invoice-parser's paths and models: a parse in the background, the document view,
the source viewer, review (preview, approve), the review queue, specs, jobs, costs, the service map and aliases."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import billparse.analyze as analyze_mod
import billparse.api as api
from billparse import webapi
from billparse.pipeline import Pipeline, Settings
from billparse.schema import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "samples" / "files"


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "data" / "raw").mkdir(parents=True)
    for f in (ROOT / "tests" / "fixtures" / "raw").glob("*.json"):
        shutil.copy(f, tmp_path / "data" / "raw" / f.name)
    monkeypatch.setenv("CATALOG_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BILLPARSE_USE_LLM", "0")
    analyze_mod._RESOLVER = None
    webapi._pipeline.cache_clear()
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=False))
    api.app.dependency_overrides[webapi.get_store] = lambda: pipe.store
    api.app.dependency_overrides[webapi.get_pipeline] = lambda: pipe
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()
    webapi._pipeline.cache_clear()
    analyze_mod._RESOLVER = None


def _wait(client: TestClient, task: dict) -> dict:
    deadline = time.time() + 60
    while task["state"] == "running" and time.time() < deadline:
        time.sleep(0.05)
        task = client.get(f"/parse/tasks/{task['sha256']}").json()
    assert task["state"] == "done", task
    return task


def _parse(client: TestClient, name: str, force: bool = False) -> dict:
    with open(FILES / name, "rb") as fh:
        r = client.post(f"/parse/start?force={str(force).lower()}", files={"file": (name, fh)})
    assert r.status_code == 200, r.text
    return _wait(client, r.json())


def test_parse_in_background_then_view(client):
    task = _parse(client, "summary_invoice.pdf")
    assert task["status"] == "auto_accepted" and task["completeness"] == "reconciled" and task["confidence"] == "high"
    assert task["charges"] == 6 and task["gaps"] == 0 and task["cached"] is False and task["lane"] == "rules"
    sha = task["sha256"]
    assert [t["sha256"] for t in client.get("/parse/tasks").json()] == [sha]
    docs = client.get("/documents").json()
    assert [d["sha256"] for d in docs] == [sha] and docs[0]["status"] == "auto_accepted" and docs[0]["fmt"] == "pdf"
    view = client.get(f"/documents/{sha}/view").json()
    assert view["completeness"] == "reconciled" and len(view["charges"]) == 6
    assert view["counted_total"] == "12513.18" and view["money_at_risk"] == "0"
    total = view["totals"][0]
    assert total["level"] == "grand" and total["status"] == "match" and total["lines"] == 6
    first = view["charges"][0]
    assert first["service_code"] == "AmazonEC2" and first["cite"]["page"] == 1 and first["cite"]["line"]
    assert len(view["runs"]) == 1 and view["runs"][0]["status"] == "auto_accepted"
    detail = client.get(f"/documents/{sha}/charges/{first['id']}").json()
    assert {f["field"] for f in detail["fields"]} >= {"cost", "kind", "service", "service_code"}
    # the output contract, as invoice-parser's GET /documents/{sha}
    doc = client.get(f"/documents/{sha}").json()
    assert doc["schema_version"] == SCHEMA_VERSION and len(doc["charges"]) == 6 and doc["completeness"] == "reconciled"
    # the same file again is the stored result
    assert _parse(client, "summary_invoice.pdf")["cached"] is True


def test_source_viewer(client):
    sha = _parse(client, "summary_invoice.pdf")["sha256"]
    info = client.get(f"/documents/{sha}/source").json()
    assert info["fmt"] == "pdf" and info["has_pdf"] and info["lines"] > 0
    lines = client.get(f"/documents/{sha}/source/lines?page=1").json()
    assert {ln["role"] for ln in lines} >= {"charge", "total"}
    png = client.get(f"/documents/{sha}/source/page.png?page=1&focus_line={lines[0]['line']}")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    sha = _parse(client, "custom_workbook.xlsx")["sha256"]
    info = client.get(f"/documents/{sha}/source").json()
    name = info["tables"][0]["name"]
    table = client.get(f"/documents/{sha}/source/table", params={"name": name}).json()
    assert table["col_letters"] and table["columns"][0] == "A"
    assert "charge" in {r["role"] for r in table["rows"]}
    assert client.get("/highlight/legend").json()["order"][0] == "flagged"


def test_review_preview_and_approve(client):
    sha = _parse(client, "summary_invoice.pdf")["sha256"]
    view = client.get(f"/documents/{sha}/view").json()
    first = view["charges"][0]
    # a wrong amount breaks the printed total: the preview says so, nothing is saved
    edits = {"rows": {first["id"]: {"cost": first["cost"] + 100}}}
    preview = client.post(f"/documents/{sha}/review/preview", json=edits).json()
    assert preview["view"]["completeness"] == "contradicted" and preview["changes"]["cost"] == {first["id"]: str(first["cost"] + 100)}
    assert client.get(f"/documents/{sha}/view").json()["completeness"] == "reconciled"
    # approving needs every checklist item
    assert client.post(f"/documents/{sha}/review/approve", json={"checklist": []}).status_code == 400
    keys = [c["key"] for c in client.get("/settings").json()["checklist"]]
    req = {"rows": {first["id"]: {"service_code": "AmazonEC2"}}, "checklist": keys, "note": "checked"}
    r = client.post(f"/documents/{sha}/review/approve", json=req)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    after = client.get(f"/documents/{sha}/view").json()
    assert after["status"] == "approved" and after["showing_reviewed"] and after["reviewer_note"] == "checked"
    # parsing the file again keeps the approved version
    _parse(client, "summary_invoice.pdf", force=True)
    assert client.get(f"/documents/{sha}/view").json()["status"] == "approved"


def test_review_queue_and_delete(client):
    sha = _parse(client, "german_invoice.xlsx")["sha256"]
    queue = client.get("/review/queue").json()
    assert [d["sha256"] for d in queue] == [sha] and queue[0]["status"] == "needs_review"
    assert queue[0]["money_at_risk"] is not None
    assert client.delete(f"/documents/{sha}").status_code == 200
    assert client.get(f"/documents/{sha}/view").status_code == 404
    assert client.get("/review/queue").json() == []


def test_specs_jobs_and_costs(client):
    specs = client.get("/specs").json()
    focus = next(s for s in specs if s["id"] == "focus")
    assert focus["status"] == "trusted" and focus["engine"] == "builtin"
    assert client.patch("/specs/focus/status", json={"status": "disabled"}).json()["status"] == "disabled"
    assert next(s for s in client.get("/specs").json() if s["id"] == "focus")["status"] == "disabled"
    assert client.get("/specs/focus/history").json()[0]["version"] == 1
    spec = {"source": "frame", "detect": {"required": ["Svc", "Amt"]}, "meta": {"kind": "cost_lines"},
            "columns": {"service_raw": {"from": ["Svc"]}, "billed_cost": {"from": ["Amt"]}}}
    saved = client.put("/specs/my-spec", json=spec).json()
    assert saved == {"id": "my-spec", "version": 1, "status": "draft"}
    assert client.get("/specs/my-spec").json()["status"] == "draft"
    with open(FILES / "messy_report.csv", "rb") as fh:
        verify = client.post("/specs/verify", files={"file": ("messy_report.csv", fh)}, data={"spec_id": "my-spec"})
    assert verify.status_code == 200 and "ok" in verify.json()
    assert client.get("/learn/jobs").json() == []
    assert client.post("/specs/learn", files={"file": ("a.csv", b"x,y\n1,2\n")}).status_code == 403
    _parse(client, "summary_invoice.pdf")
    costs = client.get("/costs").json()
    assert len(costs["runs"]) == 1 and costs["totals"]["total"] == 0 and "claude-opus-5-5" in costs["prices"]


def test_mapping_and_aliases(client):
    page = client.get("/mapping", params={"q": "elastic compute"}).json()
    assert any(r["code"] == "AmazonEC2" for r in page["rows"])
    r = client.get("/mapping/resolve", params={"label": "EC2-Other"}).json()
    assert r["code"] == "AmazonEC2" and r["name"] == "Amazon Elastic Compute Cloud"
    assert client.post("/aliases", json={"provider": "aws", "label": "Big VMs", "code": "NoSuchCode"}).status_code == 400
    assert client.post("/aliases", json={"provider": "aws", "label": "Big VMs", "code": "AmazonEC2"}).json() == {"ok": True}
    assert client.get("/mapping/resolve", params={"label": "Big VMs", "provider": "aws"}).json()["code"] == "AmazonEC2"
    alias = next(a for a in client.get("/aliases").json() if a["label_norm"] == "big vms")
    assert alias["code"] == "AmazonEC2"
    client.delete(f"/aliases/aws/{alias['label_norm']}")
    assert not any(a["label_norm"] == "big vms" for a in client.get("/aliases").json())


def test_model_choices_per_upload(client, tmp_path, monkeypatch):
    """The upload page's Backend (spec route) and Fallback (agent) list the model choices of LLM_CHOICES; a choice
    sets that upload's models, 'none' turns the step off, and an unknown choice is refused."""
    from billparse import llm as llm_mod
    monkeypatch.setenv("LLM_CHOICES", "haiku-5-5:low,opus-5-5:high,nonsense:fast")
    s = client.get("/settings").json()
    assert s["backends"] == ["claude", "none", "haiku-5-5:low", "opus-5-5:high"]
    assert llm_mod.parse_choice("sonnet-5-5") == ("claude-sonnet-5-5", None)
    assert llm_mod.parse_choice("claude-opus-5-5:xhigh") == ("claude-opus-5-5", "xhigh")
    assert llm_mod.parse_choice("opus-5-5:fast") is None and llm_mod.parse_choice("claude") is None
    f = tmp_path / "x.csv"
    f.write_text("Service,Cost\nEC2,1.00\n")
    r = client.post("/parse/start?fallback=gpt-9:low", files={"file": ("x.csv", f.read_bytes())})
    assert r.status_code == 400 and "Unknown model choice" in r.json()["detail"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")              # the client is made, no call is sent
    store = api.app.dependency_overrides[webapi.get_store]()     # the test's own store, never store/
    pipe = webapi._pipeline(store, True, "haiku-5-5:low", "opus-5-5:high")
    assert (pipe.llm.model, pipe.llm.effort) == ("claude-haiku-5-5", "low")
    assert (pipe.agent_limits.model, pipe.agent_limits.effort) == ("claude-opus-5-5", "high")
    assert pipe.choice_note == "spec haiku-5-5:low, agent opus-5-5:high"
    assert webapi._pipeline(store, True, None, None).agent_limits is None
