"""Task 08: schema-shaped LLM answers and parallel spec candidates, with fakes only (no network). The candidates are
generated in threads; each is run and checked in the parse's thread, so its verdict has the price checks."""

from __future__ import annotations

import glob
import json
import shutil
import sys
import threading
from pathlib import Path

import jsonschema
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_price_checks import _spec  # noqa: E402
from test_pricing import FIXTURE, LINUX, TINY  # noqa: E402

import billparse.analyze as analyze_mod  # noqa: E402
import billparse.llm as llm_mod  # noqa: E402
from billparse import Pipeline, Settings  # noqa: E402
from billparse.pipeline import CANDIDATE_NOTE  # noqa: E402
from billparse.spec_schema import rows_json_schema, spec_json_schema  # noqa: E402
from prices.load import load_snapshot  # noqa: E402

GOOD, SWAPPED = _spec("c5", "c6"), _spec("c6", "c5")       # quantity and unit price, then swapped


class JsonLLM:
    """An LLM with complete_json: answers by what the prompt asks (the parallel candidates come in any order)."""

    def __init__(self, first, second=None, repair=None):
        self.answers = {"first": first, "second": second if second is not None else first, "repair": repair}
        self.calls: list[str] = []
        self.lock = threading.Lock()
        self.threads: set[str] = set()

    def _pick(self, user: str) -> dict:
        with self.lock:
            self.calls.append(user)
            self.threads.add(threading.current_thread().name)
        key = "repair" if "Validation problems to fix" in user else "second" if CANDIDATE_NOTE in user else "first"
        answer = self.answers[key]
        if answer is None:
            raise ValueError("no answer queued")
        return json.loads(json.dumps(answer))

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int = 4000) -> dict:
        if system.startswith("Extract every billable"):
            return {"rows": []}
        return self._pick(user)

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        raise AssertionError("complete_json must be used")


class TextLLM(JsonLLM):
    """Text only (as tests' FakeLLM): answers through complete, read with _json_from."""

    complete_json = None                                     # noqa: PIE794  hasattr is True: remove it below

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        if system.startswith("Extract every billable"):
            return json.dumps({"rows": []})
        return json.dumps(self._pick(user)) if not isinstance(self.answers["first"], str) else self.answers["first"]


del TextLLM.complete_json


def _opaque_csv(tmp_path: Path, n: int = 12) -> Path:
    rows = [{"c1": "AmazonEC2", "c2": LINUX, "c3": "BoxUsage:m5.xlarge", "c4": "us-east-1", "c5": 24 + i % 3,
             "c6": 0.192, "c7": round((24 + i % 3) * 0.192, 4), "c8": f"2026-10-{i % 28 + 1:02d}", "c9": "Hrs"}
            for i in range(n)]
    path = tmp_path / "opaque.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _pipe(tmp_path: Path, llm) -> Pipeline:
    raw = tmp_path / "data" / "raw"
    if not raw.exists():
        raw.mkdir(parents=True)
        for f in (ROOT / "data" / "raw").glob("*.json"):
            shutil.copy(f, raw / f.name)
    analyze_mod._RESOLVER = None
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=True,
                             use_agent=False), llm=llm)
    load_snapshot(pipe.store.engine, FIXTURE)
    return pipe


def _spec_attempts(r: dict) -> list[dict]:
    return [a for a in r["attempts"] if a["route"] == "llm_spec"]


# ---------------------------------------------------------------- the schema
def test_every_builtin_spec_fits_the_schema():
    v = jsonschema.Draft202012Validator(spec_json_schema())
    jsonschema.Draft202012Validator.check_schema(spec_json_schema())
    files = glob.glob(str(ROOT / "specs" / "builtin" / "*.json"))
    assert files
    for f in files:
        assert not list(v.iter_errors(json.loads(Path(f).read_text()))), f


def test_learned_specs_fit_the_schema(tmp_path):
    """The specs the tests' LLM routes write and save."""
    from test_pipeline import GERMAN_SPEC
    v = jsonschema.Draft202012Validator(spec_json_schema())
    for spec in (GERMAN_SPEC, GOOD, SWAPPED):
        assert not list(v.iter_errors(spec))
    pipe = _pipe(tmp_path, JsonLLM(GOOD))
    pipe.parse_file(_opaque_csv(tmp_path))
    saved = pipe.store.learned_specs()
    assert saved and all(not list(v.iter_errors({k: x for k, x in s.items()})) for s in saved)


@pytest.mark.parametrize("bad", [{"columns": {"billed_cost": {"from": ["x"], "t": ["no_such_transform"]}}},
                                 {"columns": {"no_such_field": {"from": ["x"]}}},
                                 {"derive": {"no_such_field": "a + b"}}, {"bogus_key": 1},
                                 {"columns": {"billed_cost": {"from": ["x"], "t": [{"date": "%Y", "number": 1}]}}}])
def test_invalid_specs_fail_the_schema(bad):
    assert list(jsonschema.Draft202012Validator(spec_json_schema()).iter_errors(bad))


def test_the_rows_schema():
    v = jsonschema.Draft202012Validator(rows_json_schema())
    assert not list(v.iter_errors({"rows": [{"service_raw": "EC2", "billed_cost": 1.5}], "total": 1.5}))
    assert list(v.iter_errors({"rows": [{"service_raw": "EC2"}]}))


# ---------------------------------------------------------------- structured and text paths
def test_the_structured_path_is_used(tmp_path, monkeypatch):
    def never(text):
        raise AssertionError("_json_from must not be called")
    monkeypatch.setattr(llm_mod, "_json_from", never)
    llm = JsonLLM(GOOD)
    r = _pipe(tmp_path, llm).parse_file(_opaque_csv(tmp_path))
    assert r["spec"]["route"] == "llm_spec" and llm.calls


def test_the_text_fallback_with_one_invalid_answer_then_a_valid_one(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLPARSE_SPEC_CANDIDATES", "1")
    answers = iter(["not json at all", json.dumps(GOOD)])

    class Text:
        calls = 0

        def complete(self, system, user, max_tokens=4000):
            if system.startswith("Extract every billable"):
                return json.dumps({"rows": []})
            Text.calls += 1
            return next(answers)
    r = _pipe(tmp_path, Text()).parse_file(_opaque_csv(tmp_path))
    spec_atts = _spec_attempts(r)
    assert [a["error"] is not None for a in spec_atts] == [True, False]
    assert r["spec"]["route"] == "llm_spec" and Text.calls == 2


# ---------------------------------------------------------------- two candidates
def test_two_candidates_the_right_one_wins_by_the_price_checks(tmp_path):
    llm = JsonLLM(SWAPPED, GOOD)                               # the first reads quantity and unit price swapped
    pipe = _pipe(tmp_path, llm)
    r = pipe.parse_file(_opaque_csv(tmp_path))
    spec_atts = _spec_attempts(r)
    assert len(spec_atts) == 2 and r["llm_calls"] == 2 and len(llm.calls) == 2
    assert r["spec"]["route"] == "llm_spec"
    rows = pipe.store.get_rows(r["sha256"])
    assert pd.to_numeric(rows["quantity"]).min() >= 24                       # the right reading
    assert {c["name"]: c["status"] for c in r["checks"]}["implied_unit_price"] == "pass"
    assert any("spec-candidate" in t for t in llm.threads)                   # generated in worker threads
    assert any("implied_unit_price" in f for a in spec_atts for f in a["feedback"])   # the loser's checks


def test_the_candidates_verdicts_have_the_price_checks(tmp_path):
    """Run and checked in the parse's own thread: the price context is there (a worker thread would have none)."""
    llm = JsonLLM(SWAPPED, GOOD)
    r = _pipe(tmp_path, llm).parse_file(_opaque_csv(tmp_path))
    swapped = next(a for a in _spec_attempts(r) if a["status"] != r["status"] or a["confidence"] < r["confidence"])
    assert any(f.startswith("implied_unit_price") for f in swapped["feedback"])


def test_two_identical_candidates_agree(tmp_path):
    r = _pipe(tmp_path, JsonLLM(GOOD, GOOD)).parse_file(_opaque_csv(tmp_path))
    checks = {c["name"]: c for c in r["checks"]}
    assert checks["candidates_agree"]["status"] == "pass" and checks["candidates_agree"]["weight"] == 0.05


def test_two_candidates_that_disagree_tell_the_repair(tmp_path):
    other = json.loads(json.dumps(GOOD))
    other["columns"]["region"] = {"from": ["c1"]}               # the region read from the product code column
    other["columns"]["billed_cost"] = {"from": ["c7"]}
    first = json.loads(json.dumps(GOOD))
    first["columns"]["service_raw"] = {"from": ["c3"]}         # a label column the checks do not mind
    weak = json.loads(json.dumps(first))
    weak["columns"].pop("service_raw")                          # no label: labels fails, so no candidate is accepted
    weak2 = json.loads(json.dumps(other))
    weak2["columns"].pop("service_raw")
    llm = JsonLLM(weak, weak2, repair=GOOD)
    r = _pipe(tmp_path, llm).parse_file(_opaque_csv(tmp_path))
    repair_prompts = [u for u in llm.calls if "Validation problems to fix" in u]
    assert repair_prompts and "the candidates disagree on column region" in repair_prompts[0]
    assert len(llm.calls) == 3 and r["spec"]["route"] == "llm_spec"


def test_one_candidate_is_todays_behavior(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLPARSE_SPEC_CANDIDATES", "1")
    llm = JsonLLM(SWAPPED, GOOD, repair=GOOD)
    r = _pipe(tmp_path, llm).parse_file(_opaque_csv(tmp_path))
    assert len(llm.calls) == 2 and r["llm_calls"] == 2                         # one, then one repair
    assert CANDIDATE_NOTE not in "".join(llm.calls)
    assert "candidates_agree" not in {c["name"] for c in r["checks"]}
    assert "Validation problems to fix" in llm.calls[1]


def test_a_failed_candidate_is_an_attempt_and_the_other_is_used(tmp_path):
    llm = JsonLLM({"bogus_key": 1}, GOOD)
    r = _pipe(tmp_path, llm).parse_file(_opaque_csv(tmp_path))
    spec_atts = _spec_attempts(r)
    assert len(spec_atts) == 2 and sum(a["error"] is not None for a in spec_atts) == 1
    assert r["spec"]["route"] == "llm_spec" and r["llm_calls"] == 2


def test_the_cost_of_the_workers_is_charged_to_the_parse(tmp_path):
    class Costly(JsonLLM):
        def __init__(self, *a):
            super().__init__(*a)
            self.local = threading.local()

        def spent(self):
            return getattr(self.local, "cost", 0.0)

        def charge(self, usd):
            self.local.cost = self.spent() + usd

        def complete_json(self, system, user, schema, max_tokens=4000):
            self.charge(0.25)
            return super().complete_json(system, user, schema, max_tokens)
    r = _pipe(tmp_path, Costly(SWAPPED, GOOD)).parse_file(_opaque_csv(tmp_path))
    assert r["claude_cost_usd"] == pytest.approx(0.5)


# ---------------------------------------------------------------- large files
def test_a_large_file_gets_two_candidates_on_its_sample_and_the_chosen_spec_reads_it_all(tmp_path, monkeypatch):
    monkeypatch.setenv("LARGE_FILE_MB", TINY)
    monkeypatch.setenv("CHUNK_ROWS", "7")
    monkeypatch.setenv("SAMPLE_ROWS", "10")
    llm = JsonLLM(dict(SWAPPED, source="frame"), dict(GOOD, source="frame"))      # a large file is read as a table
    pipe = _pipe(tmp_path, llm)
    r = pipe.parse_file(_opaque_csv(tmp_path, n=40))
    assert r.get("large") and len(_spec_attempts(r)) == 2 and len(llm.calls) == 2
    assert r["spec"]["route"] == "llm_spec" and r["rows"] == 40
    assert {c["name"]: c["status"] for c in r["checks"]}["implied_unit_price"] == "pass"
