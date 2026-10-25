"""The agent loop (agent.py) with a fake model: tools, verification by the code, saving, limits, the regression gate."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import billparse.analyze as analyze_mod
from billparse import agent
from billparse.pipeline import Pipeline, Settings

ROOT = Path(__file__).resolve().parents[1]

BILL = """Acme Cloud Invoice
Billing period: 2026-09-01 - 2026-09-30

Charges by service
  Compute: $100.00
  Storage: $50.00
  Networking: $25.50
Total due: $175.50

Highest service spend: $70.00
"""

GOOD = {"source": "tree", "detect": {"text": "Acme Cloud Invoice"},
        "meta": {"provider": "other", "kind": "cost_lines", "cost_basis": "billed"},
        "tree": {"informational": "^Highest"}}
BAD = {"source": "tree", "detect": {"text": "Acme Cloud Invoice"}, "meta": {"kind": "cost_lines"}}


def tool_use(i: int, name: str, args: dict) -> dict:
    return {"content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": args}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 1000, "output_tokens": 200}}


class FakeLLM:
    """Plays scripted turns; records what it was sent."""

    def __init__(self, turns: list[dict]):
        self.turns = list(turns)
        self.sent: list[list[dict]] = []

    def create(self, system, messages, tools, limits):
        self.sent.append([dict(m) for m in messages])
        if not self.turns:
            return {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn", "usage": {}}
        return self.turns.pop(0)


@pytest.fixture
def pipe(tmp_path):
    (tmp_path / "data" / "raw").mkdir(parents=True)
    for f in (ROOT / "tests" / "fixtures" / "raw").glob("*.json"):
        shutil.copy(f, tmp_path / "data" / "raw" / f.name)
    analyze_mod._RESOLVER = None
    p = Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=False))
    yield p
    analyze_mod._RESOLVER = None


def parse(pipe, tmp_path, text: str, name: str = "bill.txt") -> dict:
    f = tmp_path / name
    f.write_text(text)
    return pipe.parse_file(f)


def test_without_a_spec_the_bill_is_not_proved(pipe, tmp_path):
    r = parse(pipe, tmp_path, BILL)
    assert r["completeness"] != "reconciled"
    assert agent.needs_agent(r)


def test_agent_writes_a_spec_that_proves_the_bill(pipe, tmp_path):
    r = parse(pipe, tmp_path, BILL)
    job_id = agent.enqueue_for(pipe, r)
    assert job_id is not None and agent.enqueue_for(pipe, r) == job_id        # one job per layout (queued: the same)
    llm = FakeLLM([tool_use(1, "row_shapes", {}), tool_use(2, "check_spec", {"spec": GOOD}),
                   tool_use(3, "submit_spec", {"spec": GOOD, "note": "none"})])
    job = agent.run_job(pipe, job_id, llm=llm)
    assert job["status"] == "proved", job
    assert agent.enqueue_for(pipe, r) is None                                   # the layout had its job
    assert job["spec_id"] and job["rounds"] == 1 and job["tool_calls"] == 3 and job["cost_usd"] > 0
    # the tools ran on the real file: row_shapes and check_spec answered the model
    results = [b for m in llm.sent[-1] if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"]
    assert any("reconciled" in b["content"] for b in results)
    # the file was parsed again with the saved spec, and is proved
    again = pipe.store.get_result(r["sha256"])
    assert again["completeness"] == "reconciled" and again["spec"]["id"] == job["spec_id"]
    # the trace has every step
    events = [e["event"] for e in agent.read_trace(pipe.store, job_id)]
    assert events.count("tool") == 3 and "verify" in events and events[-1] == "decision"
    # the next bill of this layout uses the spec with no agent
    nxt = parse(pipe, tmp_path, BILL.replace("100.00", "110.00").replace("175.50", "185.50"), "october.txt")
    assert nxt["spec"]["route"] == "learned" and nxt["completeness"] == "reconciled"
    assert not agent.needs_agent(nxt)


def test_a_spec_the_checks_do_not_prove_is_not_saved(pipe, tmp_path):
    r = parse(pipe, tmp_path, BILL)
    job_id = agent.enqueue_for(pipe, r)
    turns = [tool_use(i, "submit_spec", {"spec": BAD, "note": "unsure"}) for i in range(3)]
    job = agent.run_job(pipe, job_id, llm=FakeLLM(turns), limits=agent.Limits(rounds=3))
    assert job["status"] == "unproved" and job["rounds"] == 3
    assert job["report"]["completeness"] != "reconciled"
    assert not [s for s in pipe.store.learned_specs() if s["id"].startswith("agent-")]


def test_a_bad_option_is_an_error_the_model_sees(pipe, tmp_path):
    r = parse(pipe, tmp_path, BILL)
    job_id = agent.enqueue_for(pipe, r)
    wrong = dict(GOOD, tree={"no_such_option": 1})
    llm = FakeLLM([tool_use(1, "check_spec", {"spec": wrong}),
                   tool_use(2, "submit_spec", {"spec": GOOD, "note": "fixed the option"})])
    job = agent.run_job(pipe, job_id, llm=llm)
    assert job["status"] == "proved"
    first_result = llm.sent[1][-1]["content"][0]
    assert first_result["is_error"] and "Unknown tree option" in first_result["content"]


def test_the_cost_limit_stops_the_job(pipe, tmp_path):
    r = parse(pipe, tmp_path, BILL)
    job_id = agent.enqueue_for(pipe, r)
    expensive = {"content": [{"type": "tool_use", "id": "x", "name": "row_shapes", "input": {}}],
                 "stop_reason": "tool_use", "usage": {"input_tokens": 2_000_000, "output_tokens": 0}}
    job = agent.run_job(pipe, job_id, llm=FakeLLM([expensive] * 5), limits=agent.Limits(max_cost_usd=1.0))
    assert job["status"] == "limit" and job["cost_usd"] >= 1.0


def test_a_repair_must_pass_the_regression_gate(pipe, tmp_path):
    r = parse(pipe, tmp_path, BILL)
    job = agent.run_job(pipe, agent.enqueue_for(pipe, r), llm=FakeLLM(
        [tool_use(1, "submit_spec", {"spec": GOOD, "note": "none"})]))
    spec_id = job["spec_id"]
    # a repair that proves a new file but breaks the file the spec read before is rejected
    other = BILL.replace("Highest service spend", "Largest service spend")
    r2 = parse(pipe, tmp_path, other, "other.txt")
    job2_id = pipe.store.enqueue_agent_job("repair:test", r2["sha256"], "other.txt", "repair", spec_id=spec_id)
    breaking = dict(GOOD, tree={"informational": "^Largest"}, filters=[{"drop_if_match": {"field": "description",
                                                                                          "regex": "Compute"}}])
    job2 = agent.run_job(pipe, job2_id, llm=FakeLLM([tool_use(1, "submit_spec", {"spec": breaking, "note": ""})]))
    assert job2["status"] in ("rejected", "unproved")
    assert pipe.store.versions(spec_id) == [1]


def test_a_parse_runs_the_agent_step_by_itself(tmp_path):
    """With the agent on, one parse call returns the result after the agent: the caller sees no agent."""
    (tmp_path / "data" / "raw").mkdir(parents=True)
    for f in (ROOT / "tests" / "fixtures" / "raw").glob("*.json"):
        shutil.copy(f, tmp_path / "data" / "raw" / f.name)
    analyze_mod._RESOLVER = None
    llm = FakeLLM([tool_use(1, "check_spec", {"spec": GOOD}), tool_use(2, "submit_spec", {"spec": GOOD, "note": "none"})])
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=False, use_agent=True),
                    agent_llm=llm)
    steps: list[str] = []
    f = tmp_path / "bill.txt"
    f.write_text(BILL)
    r = pipe.parse_bytes(f.read_bytes(), f.name, on_step=steps.append)
    assert steps == ["learning"]
    assert r["completeness"] == "reconciled" and r["spec"]["route"] == "learned"
    # the next bill of this layout needs no agent step
    steps.clear()
    nxt = pipe.parse_bytes(BILL.replace("100.00", "110.00").replace("175.50", "185.50").encode(), "october.txt",
                           on_step=steps.append)
    assert steps == [] and nxt["completeness"] == "reconciled"
    analyze_mod._RESOLVER = None


def test_a_layout_the_agent_did_not_prove_is_not_tried_again(tmp_path):
    (tmp_path / "data" / "raw").mkdir(parents=True)
    for f in (ROOT / "tests" / "fixtures" / "raw").glob("*.json"):
        shutil.copy(f, tmp_path / "data" / "raw" / f.name)
    analyze_mod._RESOLVER = None
    llm = FakeLLM([tool_use(1, "submit_spec", {"spec": BAD, "note": "none"})] * 3)
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=False, use_agent=True),
                    agent_llm=llm)
    first = pipe.parse_bytes(BILL.encode(), "bill.txt")
    assert first["completeness"] != "reconciled" and len(pipe.store.agent_jobs()) == 1
    calls = len(llm.sent)
    pipe.parse_bytes(BILL.replace("100.00", "110.00").replace("175.50", "185.50").encode(), "october.txt")
    assert len(llm.sent) == calls                     # no new model call: the result of the first pass stands
    analyze_mod._RESOLVER = None


def test_only_contradicted_or_unchecked_results_go_to_the_agent():
    base = {"kind": "cost_lines", "status": "unreconciled"}
    assert not agent.needs_agent({**base, "completeness": "consistent"})       # no printed total: nothing to prove
    assert not agent.needs_agent({**base, "completeness": "reconciled"})
    assert agent.needs_agent({**base, "completeness": "contradicted"})
    assert agent.needs_agent({**base, "completeness": "unchecked"})
    assert agent.needs_agent({**base, "status": "needs_review", "completeness": None})   # nothing read
    assert agent.needs_agent({**base, "completeness": "consistent", "contract": {"completeness": "contradicted"}})
    assert not agent.needs_agent({**base, "completeness": "contradicted", "cached": True})


def test_an_llm_reading_is_tested_against_the_printed_total(pipe, tmp_path):
    """An LLM spec that reads two credit lines of a bill whose printed total the tree found is contradicted, not a
    'consistent' result with nothing to test; the LLM's own total proves nothing."""
    import pandas as pd

    from billparse.spec import Extraction
    from billparse.validate import validate
    rows = pd.DataFrame({"description": ["Credit A", "Credit B"], "billed_cost": [-10.0, -10.39]})
    ext = Extraction(rows=rows, stated_totals=[{"label": "Total due", "amount": 175.5, "kind": "document_total"},
                                               {"label": "llm total", "amount": -20.39, "kind": "llm_claim"}])
    assert validate(ext)["completeness"] == "contradicted"
    ext.stated_totals = [{"label": "llm total", "amount": -20.39, "kind": "llm_claim"}]
    assert validate(ext)["completeness"] != "reconciled"
