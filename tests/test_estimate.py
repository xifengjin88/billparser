"""The LLM's estimates of the values an inventory's files do not give (estimate.py)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from billparse import estimate, iac  # noqa: E402
from billparse.docmodel import load  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "iac" / "04-aws-missing-usage.tf"   # no region, count or instance type


class FakeLLM:
    """Answers the estimate call with the given estimates; counts the calls."""

    def __init__(self, estimates: list[dict] | Exception):
        self.estimates, self.calls = estimates, []

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        self.calls.append(user)
        if isinstance(self.estimates, Exception):
            raise self.estimates
        return json.dumps({"estimates": self.estimates})


GOOD = [{"gap": 1, "value": "us-east-1", "reason": "the team's other stacks run there", "confidence": "medium"},
        {"gap": 2, "value": 2, "reason": "an app tier: two for availability", "confidence": "low"},
        {"gap": 3, "value": "m5.xlarge", "reason": "a common size for an app server", "confidence": "low"}]


@pytest.fixture
def pipe_with(tmp_path, monkeypatch):
    """A pipeline with the test price catalog and the given fake LLM (as in the app: Linux assumed)."""
    import billparse.analyze as analyze_mod
    from billparse import Pipeline, Settings
    from prices.load import load_snapshot
    monkeypatch.setenv("PRICE_AUTO_DEFAULTS", "1")
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    for f in (ROOT / "data" / "raw").glob("*.json"):
        shutil.copy(f, raw / f.name)
    analyze_mod._RESOLVER = None
    made = []

    def make(llm, **kw):
        p = Pipeline(Settings(store_dir=tmp_path / f"store{len(made)}", catalog_dir=tmp_path / "data",
                              use_agent=False, **kw), llm=llm)
        load_snapshot(p.store.engine, ROOT / "tests" / "fixtures" / "prices" / "download")
        made.append(p)
        return p
    yield make
    analyze_mod._RESOLVER = None


def parsed(pipe, path: Path = FIXTURE):
    from billparse.schema import Document
    r = pipe.parse_file(path)
    return r, Document.model_validate_json(pipe.store.part(r["sha256"], "doc"))


def test_the_llm_fills_the_gaps_and_its_values_are_marked_and_priced_apart(pipe_with):
    llm = FakeLLM(GOOD)
    r, doc = parsed(pipe_with(llm))
    assert len(llm.calls) == 1 and r["llm_calls"] == 1
    assert "GAPS:\n1. provider.aws" in llm.calls[0] and "var.replicas" in llm.calls[0]
    app = next(c for c in doc.charges if c.description.value == "aws_instance.application")
    assert app.quantity.value == 2 and (app.quantity.origin, app.quantity.source) == ("inferred", "claude")
    assert app.instance_type.value == "m5.xlarge" and app.instance_type.confidence == estimate.CONFIDENCE["low"]
    assert app.context.region.value == "us-east-1" and app.context.region.source == "claude"
    assert app.attributes["estimated quantity"].startswith("2 (an LLM estimate, low: an app tier")
    assert float(app.list_cost.value) == pytest.approx(0.192 * 2 * 730)
    assert app.price_assumptions.value.startswith("instance_type=m5.xlarge (an LLM estimate, low); quantity=2 (an "
                                                  "LLM estimate, low)")
    # the gaps stay open, each with the LLM's value as its candidate; the file still needs review
    gaps = {(g.ref, g.field): g for g in doc.gaps if g.kind == "missing_input"}
    assert gaps[(app.id, "instance_type")].candidates == ["m5.xlarge (an LLM estimate, low: a common size for an "
                                                          "app server)"]
    assert r["status"] == "needs_review" and "3 of them are LLM estimates" in doc.confidence.reasons[1]
    # two totals: the files give no priced row; with the estimates, the instances
    assert doc.pricing.list_total_from_files == 0.0
    assert doc.pricing.list_total == pytest.approx(round(0.192 * 2 * 730, 2))


def test_a_value_that_fails_its_check_is_not_used_and_its_gap_stays_without_a_candidate(pipe_with):
    bad = [{"gap": 1, "value": "mars-north-1", "reason": "x", "confidence": "high"},
           {"gap": 2, "value": 1000, "reason": "x", "confidence": "high"},
           {"gap": 3, "value": "db.m5.large", "reason": "x", "confidence": "high"},
           {"gap": 9, "value": "us-east-1", "reason": "no such gap", "confidence": "high"}]
    r, doc = parsed(pipe_with(FakeLLM(bad)))
    why = {e["field"]: e.get("why") for e in r["evidence"]["estimates"]}
    assert why == {"region": "not an AWS region the catalog knows", "quantity": "out of range (1 to 100, a whole number)",
                   "instance_type": "not an instance type for Amazon Elastic Compute Cloud"}
    assert not any(e["used"] for e in r["evidence"]["estimates"])
    app = next(c for c in doc.charges if c.description.value == "aws_instance.application")
    assert app.quantity is None and app.instance_type is None and app.list_cost is None
    assert not any(g.candidates for g in doc.gaps)
    assert doc.pricing.list_total is None


def test_no_call_without_a_gap_an_llm_or_with_the_setting_off(pipe_with):
    llm = FakeLLM(GOOD)
    r, _ = parsed(pipe_with(llm), ROOT / "tests" / "fixtures" / "iac" / "01-aws-explicit.tf")   # every value given
    assert not llm.calls and r["llm_calls"] == 0 and "estimates" not in r["evidence"]
    r, _ = parsed(pipe_with(llm, use_estimates=False))
    assert not llm.calls and "estimates" not in r["evidence"]


def test_a_failed_call_leaves_the_rows_as_the_files_give_them(pipe_with):
    r, doc = parsed(pipe_with(FakeLLM(ValueError("No JSON object in LLM response"))))
    assert r["status"] == "needs_review" and "estimates" not in r["evidence"]
    assert any("The LLM estimate of the missing values failed" in w for w in r["warnings"])
    assert not any(g.candidates for g in doc.gaps)


def test_a_reprice_reuses_the_stored_estimates_and_makes_no_call(pipe_with):
    llm = FakeLLM(GOOD)
    pipe = pipe_with(llm)
    r, _ = parsed(pipe)
    again = pipe.reprice(r["sha256"])
    assert len(llm.calls) == 1
    assert again["pricing"]["list_total"] == pytest.approx(round(0.192 * 2 * 730, 2))


def test_the_region_of_a_cloudformation_template_is_a_gap_the_llm_can_fill():
    doc = load(ROOT / "tests" / "fixtures" / "iac" / "01-aws-explicit.tf")
    assert not [g for g in iac.read(doc).gaps if g["field"] == "region"]       # Terraform gives it
    from billparse.docmodel import load_bytes
    tpl = b"""AWSTemplateFormatVersion: '2010-09-09'
Resources:
  Web:
    Type: AWS::EC2::Instance
    Properties:
      InstanceType: t3.large
"""
    res = iac.read(load_bytes(tpl, "stack.yaml"))
    assert [(g["name"], g["field"]) for g in res.gaps] == [("AWS::Region", "region")]
    answers = estimate.check([{"gap": 1, "value": "eu-west-1", "reason": "r", "confidence": "high"}],
                             estimate.open_gaps(res.gaps), res, ({"eu-west-1"}, {"t3.large"}))
    estimate.apply(res, answers)
    rows = iac.rows(res)
    assert rows.loc[0, "region"] == "eu-west-1"
    assert json.loads(rows.loc[0, iac.ESTIMATES])["region"]["confidence"] == "high"


def _project(files: dict[str, str]):
    import io
    import zipfile

    from billparse.docmodel import load_bytes
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return load_bytes(buf.getvalue(), "project.zip")


def test_an_estimated_count_reaches_the_volumes_of_its_resource_and_the_resources_of_a_module():
    """A volume of a launch template is as many as its group; a module's resources, as many as the module times
    their own: an estimated count of the group or of the module gives them their count too."""
    doc = _project({"main.tf": """
provider "aws" {
  region = "us-east-1"
}
variable "workers" {}
variable "copies" {}
resource "aws_launch_template" "lt" {
  instance_type = "m5.large"
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 50
      volume_type = "gp3"
    }
  }
}
resource "aws_autoscaling_group" "workers" {
  desired_capacity = var.workers
  launch_template {
    id = aws_launch_template.lt.id
  }
}
module "cache" {
  source = "./cache"
  count  = var.copies
}
""", "cache/main.tf": """
resource "aws_instance" "node" {
  count         = 2
  instance_type = "t3.small"
}
"""})
    res = iac.read(doc)
    by = {it.name: it for it in res.items}
    vol = by['aws_autoscaling_group.workers.block_device_mappings[/dev/xvda]']
    node = by["module.cache.aws_instance.node"]
    assert vol.count is None and node.count is None
    gaps = estimate.open_gaps(res.gaps)
    n = {g["name"]: i for i, g in enumerate(gaps, 1)}
    answers = estimate.check([{"gap": n["aws_autoscaling_group.workers"], "value": 4, "reason": "r1", "confidence": "medium"},
                              {"gap": n["module.cache"], "value": 3, "reason": "r2", "confidence": "low"}],
                             gaps, res, None)
    estimate.apply(res, [e for e in answers if e["used"]])
    assert by["aws_autoscaling_group.workers"].count == 4 and vol.count == 4
    assert node.count == 2 * 3
    said = json.loads(vol.attrs[iac.ESTIMATES])["quantity"]
    assert said == {"value": 4, "reason": "aws_autoscaling_group.workers x 4: r1", "confidence": "medium"}
    assert json.loads(node.attrs[iac.ESTIMATES])["quantity"]["confidence"] == "low"
    rows = iac.rows(res).set_index("description")
    assert rows.loc['aws_autoscaling_group.workers.block_device_mappings[/dev/xvda]', "quantity"] == 4
