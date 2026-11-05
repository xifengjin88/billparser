"""Infrastructure as code as an inventory: defaults, conditions, count, for_each, Auto Scaling groups, comments."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from billparse import iac  # noqa: E402
from billparse.docmodel import load_bytes  # noqa: E402

CFN = """AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  ApiType:
    Type: String
    Default: t3.xlarge
  Legacy:
    Type: String
    Default: 'false'
Conditions:
  UseLegacy: !Equals [!Ref Legacy, 'true']
Resources:
  Api:
    Type: AWS::EC2::Instance
    Properties:
      InstanceType: !Ref ApiType
  Old:
    Type: AWS::EC2::Instance
    Condition: UseLegacy
    Properties:
      InstanceType: c5.9xlarge
  Tpl:
    Type: AWS::EC2::LaunchTemplate
    Properties:
      LaunchTemplateData:
        InstanceType: m6i.large
  Group:
    Type: AWS::AutoScaling::AutoScalingGroup
    Properties:
      MinSize: '2'
      MaxSize: '12'
      DesiredCapacity: '5'
      LaunchTemplate:
        LaunchTemplateId: !Ref Tpl
  Db:
    Type: AWS::RDS::DBInstance
    Properties:
      Engine: postgres
      DBInstanceClass: db.r6g.xlarge
      AllocatedStorage: '500'
      MultiAZ: true
  Nested:
    Type: AWS::CloudFormation::Stack
    Properties:
      TemplateURL: https://example.com/x.yaml
"""

TF = """provider "aws" {
  region = "eu-west-1"
}
variable "web_type" {
  default = "m5.large"
}
variable "legacy" {
  default = false
}
resource "aws_instance" "web" {
  count         = 3
  instance_type = var.web_type
}
resource "aws_instance" "api" {
  for_each      = toset(["staging", "prod"])
  instance_type = "r6i.xlarge"   # per environment
}
resource "aws_instance" "batch" {
  count         = var.legacy ? 4 : 0
  instance_type = "c5.4xlarge"
}
# resource "aws_instance" "old" {
#   count = 6
# }
resource "aws_launch_template" "workers" {
  instance_type = "c5.2xlarge"
}
resource "aws_autoscaling_group" "workers" {
  desired_capacity = 4
  launch_template {
    id = aws_launch_template.workers.id
  }
}
resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
}
"""


def inventory(doc):
    res = iac.read(doc)
    rows = iac.rows(res).set_index("description")
    return res, rows


def test_cloudformation_resources_at_their_defaults():
    res, rows = inventory(load_bytes(CFN.encode(), "stack.yaml"))
    assert list(rows.index) == ["Api", "Group", "Db", "Db.storage"]          # RDS storage: a row of its own
    assert (rows.loc["Db.storage", "quantity"], rows.loc["Db.storage", "unit"]) == (500, "GB")
    assert (rows.loc["Api", "instance_type"], rows.loc["Api", "quantity"]) == ("t3.xlarge", 1)
    assert (rows.loc["Group", "instance_type"], rows.loc["Group", "quantity"]) == ("m6i.large", 5)
    db = rows.loc["Db"]
    assert (db["database_engine"], db["storage_gb"], db["deployment_option"]) == ("postgres", 500, "Multi-AZ")
    assert {x["name"] for x in res.excluded} == {"Old", "Tpl"}
    assert [g["name"] for g in res.gaps] == ["Nested", "AWS::Region"]   # a template does not give its region


def test_terraform_count_for_each_comments_and_groups():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("tf/main.tf", TF)
    res, rows = inventory(load_bytes(buf.getvalue(), "tf.zip"))
    assert rows["quantity"].to_dict() == {"aws_instance.web": 3, "aws_instance.api": 2, "aws_autoscaling_group.workers": 4}
    assert rows.loc["aws_instance.web", "instance_type"] == "m5.large"
    assert rows.loc["aws_autoscaling_group.workers", "instance_type"] == "c5.2xlarge"
    assert set(rows["region"]) == {"eu-west-1"}
    assert {x["name"] for x in res.excluded} == {"aws_instance.batch", "aws_launch_template.workers", "aws_vpc.main"}
    assert "aws_instance.old" not in rows.index


def test_not_infrastructure_is_not_read():
    assert not iac.is_iac("Invoice\nTotal USD 10.00\n")


# ---------------------------------------------------------------- the estimator's Terraform files (synthetic)
FIXTURES = ROOT / "tests" / "fixtures" / "iac"


def tf_file(name: str):
    res, rows = inventory(load_bytes((FIXTURES / name).read_bytes(), name))
    return res, rows, {(g["name"], g["field"]) for g in res.gaps}


def value(rows, name, col):
    v = rows.loc[name, col]
    return None if pd.isna(v) else v                    # NaN or NA: no value


def test_root_volumes_and_ebs_volumes_are_storage_rows():
    res, rows, gaps = tf_file("01-aws-explicit.tf")
    assert rows["quantity"].to_dict() == {"aws_instance.web": 3, "aws_instance.web.root_block_device": 3,
                                          "aws_ebs_volume.data": 2}
    root = rows.loc["aws_instance.web.root_block_device"]
    assert (root["unit"], root["storage_gb"], root["storage_type"]) == ("volumes", 30, "gp3")
    assert root["service_raw"] == "Amazon Elastic Compute Cloud" and root["_src"] == "01-aws-explicit.tf:10"
    assert (value(rows, "aws_ebs_volume.data", "storage_gb"), value(rows, "aws_ebs_volume.data", "storage_type")) \
        == (200, "gp3")
    assert set(rows["region"]) == {"us-east-1"} and not gaps


def test_the_attributes_that_change_a_price_are_read():
    res, rows, gaps = tf_file("02-aws-mixed-stack.tf")
    db = rows.loc["aws_db_instance.database"]
    assert (db["instance_type"], db["database_engine"], db["deployment_option"], db["storage_gb"], db["storage_type"]) \
        == ("db.m6i.large", "postgres", "Multi-AZ", 500, "gp3")
    assert value(rows, "aws_lb.public", "load_balancer_type") == "application"
    assert (rows.loc["aws_elasticache_cluster.cache", "quantity"], rows.loc["aws_elasticache_cluster.cache", "unit"]) \
        == (2, "nodes")
    assert rows.loc["aws_instance.api.root_block_device", "quantity"] == 4
    logs = rows.loc["aws_cloudwatch_log_group.application"]
    assert (logs["service_raw"], logs["unit"]) == ("Amazon CloudWatch", "log groups")
    # what the file cannot say: the usage of the resources that are priced by usage
    assert gaps == {(n, "usage") for n in ("aws_lb.public", "aws_nat_gateway.egress", "aws_s3_bucket.assets",
                                           "aws_cloudwatch_log_group.application")}
    assert {x["name"] for x in res.excluded} == {"aws_vpc.network", "aws_security_group.application"}


def test_for_each_over_a_local_map_gives_a_row_for_each_key():
    res, rows, gaps = tf_file("03-aws-derived-quantities.tf")
    assert (rows.loc["aws_instance.worker", "quantity"], rows.loc["aws_instance.worker", "instance_type"]) \
        == (6, "c6i.large")
    assert rows.loc["aws_instance.worker.root_block_device", "quantity"] == 6
    primary, reporting = rows.loc['aws_db_instance.database["primary"]'], rows.loc['aws_db_instance.database["reporting"]']
    assert (primary["quantity"], primary["storage_gb"]) == (1, 100)
    assert (reporting["quantity"], reporting["storage_gb"]) == (1, 250)
    assert {primary["instance_type"], reporting["instance_type"]} == {"db.t3.medium"}
    assert set(rows["region"]) == {"us-west-2"} and not gaps


def test_a_value_the_file_does_not_give_is_a_gap_never_a_guess():
    res, rows, gaps = tf_file("04-aws-missing-usage.tf")
    app = "aws_instance.application"
    assert value(rows, app, "quantity") is None and value(rows, app, "instance_type") is None
    assert rows["region"].isna().all()                  # not the text "var.region"
    assert value(rows, "aws_dynamodb_table.events", "billing_mode") == "PAY_PER_REQUEST"
    assert gaps == {(app, "quantity"), (app, "instance_type"), ("provider.aws", "region"),
                    ("aws_s3_bucket.archive", "usage"), ("aws_dynamodb_table.events", "usage")}
    messages = " ".join(g["message"] for g in res.gaps)
    assert "var.replicas" in messages and "var.region" in messages and "var.instance_type" in messages


def test_a_variable_region_with_a_default_is_resolved():
    tf = 'variable "region" {\n  default = "eu-central-1"\n}\nprovider "aws" {\n  region = var.region\n}\n' \
         'resource "aws_instance" "a" {\n  instance_type = "t3.micro"\n}\n'
    res, rows = inventory(load_bytes(tf.encode(), "main.tf"))
    assert set(rows["region"]) == {"eu-central-1"} and not res.gaps


# ---------------------------------------------------------------- a Terraform project: modules, tfvars, functions
def project(files: dict[str, str]):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    res, rows = inventory(load_bytes(buf.getvalue(), "project.zip"))
    return res, rows, {(g["name"], g["field"]) for g in res.gaps}


PROVIDER = 'provider "aws" {\n  region = "eu-west-1"\n}\n'


def test_variables_in_their_own_file_and_tfvars_override_the_defaults():
    res, rows, gaps = project({
        "app/main.tf": PROVIDER + 'resource "aws_instance" "web" {\n  count = var.web_count\n'
                                  '  instance_type = var.web_type\n}\n',
        "app/variables.tf": 'variable "web_count" {\n  default = 2\n}\nvariable "web_type" {\n  default = "t3.small"\n}\n',
        "app/terraform.tfvars": 'web_count = 5\n',
        "app/prod.auto.tfvars": 'web_type = "m6i.large"\n',
    })
    assert (rows.loc["aws_instance.web", "quantity"], rows.loc["aws_instance.web", "instance_type"]) == (5, "m6i.large")
    assert not gaps


def test_several_var_files_are_a_gap_and_none_is_used():
    res, rows, gaps = project({
        "main.tf": PROVIDER + 'variable "n" {\n  default = 1\n}\nresource "aws_instance" "a" {\n  count = var.n\n'
                              '  instance_type = "t3.micro"\n}\n',
        "dev.tfvars": "n = 2\n", "prod.tfvars": "n = 9\n",
    })
    assert rows.loc["aws_instance.a", "quantity"] == 1 and ("tfvars", "variables") in gaps


def test_a_local_module_is_read_with_its_inputs_and_a_remote_one_is_a_gap():
    res, rows, gaps = project({
        "root/main.tf": PROVIDER + 'module "db" {\n  source = "../modules/db"\n  class = "db.r6g.large"\n'
                                   '  replicas = 2\n}\nmodule "vpc" {\n  source = "terraform-aws-modules/vpc/aws"\n}\n'
                                   'module "cache" {\n  source = "../modules/db"\n  count = 2\n'
                                   '  class = "db.t4g.small"\n}\n',
        "modules/db/main.tf": 'variable "class" {}\nvariable "replicas" {\n  default = 1\n}\n'
                              'resource "aws_rds_cluster_instance" "node" {\n  count = var.replicas\n'
                              '  instance_class = var.class\n  engine = "aurora-postgresql"\n}\n',
    })
    db = rows.loc["module.db.aws_rds_cluster_instance.node"]
    assert (db["quantity"], db["instance_type"], db["service_raw"]) == (2, "db.r6g.large",
                                                                         "Amazon Relational Database Service")
    cache = rows.loc["module.cache.aws_rds_cluster_instance.node"]
    assert (cache["quantity"], cache["instance_type"]) == (2, "db.t4g.small")      # count 2 x 1 replica
    assert ("module.vpc", "module") in gaps and set(rows["region"]) == {"eu-west-1"}


def test_functions_arithmetic_and_values_known_only_at_apply():
    res, rows, gaps = project({"main.tf": PROVIDER + '''
variable "azs" {
  default = ["a", "b", "c"]
}
locals {
  sizes = { small = "t3.small", large = "m6i.xlarge" }
  tier  = "large"
}
resource "aws_instance" "app" {
  count         = length(var.azs) * 2
  instance_type = lookup(local.sizes, local.tier, "t3.micro")
}
resource "aws_instance" "from_ami" {
  instance_type = data.aws_ec2_instance_type.x.id
}
'''})
    assert (rows.loc["aws_instance.app", "quantity"], rows.loc["aws_instance.app", "instance_type"]) == (6, "m6i.xlarge")
    assert ("aws_instance.from_ami", "instance_type") in gaps
    assert "known only when Terraform runs" in next(g["message"] for g in res.gaps if g["name"] == "aws_instance.from_ami")


def test_container_search_and_warehouse_types():
    res, rows, gaps = project({"main.tf": PROVIDER + '''
resource "aws_eks_node_group" "workers" {
  instance_types = ["m6i.large"]
  disk_size      = 50
  scaling_config {
    desired_size = 3
    min_size     = 1
    max_size     = 6
  }
}
resource "aws_ecs_task_definition" "api" {
  cpu    = "512"
  memory = "1024"
}
resource "aws_ecs_service" "api" {
  launch_type     = "FARGATE"
  desired_count   = 4
  task_definition = aws_ecs_task_definition.api.arn
}
resource "aws_opensearch_domain" "logs" {
  cluster_config {
    instance_type  = "r6g.large.search"
    instance_count = 3
  }
  ebs_options {
    ebs_enabled = true
    volume_size = 100
    volume_type = "gp3"
  }
}
resource "aws_redshift_cluster" "dw" {
  node_type       = "ra3.xlplus"
  number_of_nodes = 2
}
resource "aws_vpc_endpoint" "s3" {
  service_name = "com.amazonaws.eu-west-1.s3"
}
resource "aws_glue_job" "etl" {
  name = "etl"
}
'''})
    q = rows["quantity"].to_dict()
    assert (q["aws_eks_node_group.workers"], rows.loc["aws_eks_node_group.workers", "instance_type"]) == (3, "m6i.large")
    assert (q["aws_eks_node_group.workers.disk"], rows.loc["aws_eks_node_group.workers.disk", "storage_gb"]) == (3, 50)
    api = rows.loc["aws_ecs_service.api"]
    assert (api["quantity"], api["vcpu"], api["memory_gb"], api["unit"]) == (4, 0.5, 1, "tasks")
    assert (q["aws_opensearch_domain.logs"], q["aws_opensearch_domain.logs.ebs_options"]) == (3, 3)
    assert rows.loc["aws_opensearch_domain.logs.ebs_options", "storage_gb"] == 100
    assert (q["aws_redshift_cluster.dw"], rows.loc["aws_redshift_cluster.dw", "instance_type"]) == (2, "ra3.xlplus")
    assert "aws_vpc_endpoint.s3" in {x["name"] for x in res.excluded}          # a gateway endpoint: free
    assert ("aws_glue_job.etl", "service") in gaps                            # not known: a row and a gap


def test_the_volumes_of_a_launch_template_are_counted_for_each_instance_of_its_group():
    res, rows, gaps = project({"main.tf": PROVIDER + '''
resource "aws_launch_template" "w" {
  instance_type = "c6i.large"
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 40
      volume_type = "gp3"
    }
  }
}
resource "aws_autoscaling_group" "w" {
  desired_capacity = 5
  launch_template {
    id = aws_launch_template.w.id
  }
}
'''})
    vol = rows.loc["aws_autoscaling_group.w.block_device_mappings[/dev/xvda]"]
    assert (vol["quantity"], vol["storage_gb"], vol["storage_type"]) == (5, 40, "gp3")
    assert rows.loc["aws_autoscaling_group.w", "instance_type"] == "c6i.large" and not gaps


# ---------------------------------------------------------------- the contract: missing_input gaps
def test_the_contract_gives_missing_input_gaps_that_point_at_their_charges(tmp_path):
    from billparse import Pipeline, Settings
    from billparse.schema import Document
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=False))
    r = pipe.parse_file(FIXTURES / "04-aws-missing-usage.tf")
    doc = Document.model_validate_json(pipe.store.part(r["sha256"], "doc"))
    assert doc.doc_type.value == "inventory"
    gaps = {(g.field, g.message.split(":")[0]): g for g in doc.gaps}
    assert {g.kind for g in doc.gaps} == {"missing_input"}
    assert set(gaps) == {("region", "provider.aws"), ("quantity", "aws_instance.application"),
                         ("instance_type", "aws_instance.application"), ("usage", "aws_s3_bucket.archive"),
                         ("usage", "aws_dynamodb_table.events")}
    app = next(c for c in doc.charges if c.description.value == "aws_instance.application")
    assert gaps[("quantity", "aws_instance.application")].ref == app.id and app.quantity is None
    assert gaps[("region", "provider.aws")].ref is None                       # about the file, not one charge
    assert gaps[("quantity", "aws_instance.application")].cite.row == 18
    assert not any("has no default" in w for w in r.get("warnings") or [])     # a gap, not a note as well


def test_an_inventory_that_lacks_an_input_needs_review_and_its_confidence_says_why(tmp_path):
    from billparse import Pipeline, Settings
    from billparse.schema import Document
    from billparse.webapi import charge_row
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=False))

    def parsed(name):
        r = pipe.parse_file(FIXTURES / name)
        return r, Document.model_validate_json(pipe.store.part(r["sha256"], "doc"))

    r, doc = parsed("04-aws-missing-usage.tf")
    assert r["status"] == "needs_review" and doc.confidence.level == "low"
    assert "3 value(s) the files do not give (instance_type, quantity, region)" in doc.confidence.reasons[0]
    r, doc = parsed("02-aws-mixed-stack.tf")                  # usage alone is not a missing input
    assert r["status"] == "auxiliary" and doc.confidence.level == "high"
    assert doc.confidence.reasons[1].startswith("4 resource(s) are priced by usage")
    r, doc = parsed("01-aws-explicit.tf")
    root = next(c for c in doc.charges if c.description.value == "aws_instance.web.root_block_device")
    row = charge_row(root, set())
    assert (row.unit, row.quantity, row.storage_gb, row.storage_type) == ("volumes", 3, 30, "gp3")


# ---------------------------------------------------------------- list prices: count x 730 hours a month
def test_hourly_resources_get_their_list_price_a_month(tmp_path, monkeypatch):
    """As in the app: pricing assumes Linux for an instance whose OS the files do not say (the tests turn it off)."""
    import shutil
    monkeypatch.setenv("PRICE_AUTO_DEFAULTS", "1")

    import billparse.analyze as analyze_mod
    from billparse import Pipeline, Settings
    from billparse.schema import Document
    from prices.load import load_snapshot
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    for f in (ROOT / "data" / "raw").glob("*.json"):
        shutil.copy(f, raw / f.name)
    analyze_mod._RESOLVER = None
    pipe = Pipeline(Settings(store_dir=tmp_path / "store", catalog_dir=tmp_path / "data", use_llm=False,
                             use_agent=False))
    load_snapshot(pipe.store.engine, ROOT / "tests" / "fixtures" / "prices" / "download")
    tf = tmp_path / "main.tf"
    tf.write_text(PROVIDER.replace("eu-west-1", "us-east-1") + '''
variable "n" {}
resource "aws_instance" "app" {
  count         = 2
  instance_type = "m5.xlarge"
}
resource "aws_db_instance" "db" {
  engine         = "mysql"
  instance_class = "db.m5.large"
  multi_az       = false
}
resource "aws_instance" "unknown" {
  count         = var.n
  instance_type = "m5.xlarge"
}
resource "aws_s3_bucket" "b" {
  bucket = "b"
}
resource "aws_ebs_volume" "data" {
  count = 2
  size  = 200
  type  = "gp3"
  iops  = 5000
}
''')
    r = pipe.parse_file(tf)
    doc = Document.model_validate_json(pipe.store.part(r["sha256"], "doc"))
    by = {c.description.value: c for c in doc.charges}
    app, db = by["aws_instance.app"], by["aws_db_instance.db"]
    assert float(app.list_unit_price.value) == pytest.approx(0.192) and app.quantity.value == 2
    assert float(app.list_cost.value) == pytest.approx(0.192 * 2 * 730)            # a month, always running
    assert app.list_cost.source == "price_catalog" and app.list_cost.origin == "inferred"
    assert "hours=730" in app.price_assumptions.value
    assert float(db.list_cost.value) == pytest.approx(0.171 * 730)                 # mysql -> MySQL in the catalog
    assert by["aws_instance.unknown"].list_cost is None                            # no count: no price (a gap)
    assert by["aws_s3_bucket.b"].list_cost is None and by["aws_s3_bucket.b"].price_match_level.value == "not_priced"
    assert by["aws_s3_bucket.b"].price_missing.value.startswith("its price depends on usage")
    assert doc.cost_types == []                     # a partial list cost is not a total a consumer can sum
    vol, iops = by["aws_ebs_volume.data"], by["aws_ebs_volume.data.iops"]
    assert float(vol.list_unit_price.value) == pytest.approx(0.08)                  # gp3, a GB-month
    assert float(vol.list_cost.value) == pytest.approx(0.08 * 2 * 200) and vol.quantity.value == 2
    assert "GB-months=2 volumes x 200 GB" in vol.price_assumptions.value
    assert iops.quantity.value == 4000 and iops.unit.value == "IOPS"               # (5000 - 3000 included) x 2
    assert iops.list_cost is None                   # the fixture has no IOPS price: unmatched, never guessed
    # Price again: the stored rows priced again, the same prices, the contract saved again
    again = pipe.reprice(r["sha256"])
    assert again.get("repriced") and again["status"] == r["status"]
    doc = Document.model_validate_json(pipe.store.part(r["sha256"], "doc"))
    by = {c.description.value: c for c in doc.charges}
    assert float(by["aws_instance.app"].list_cost.value) == pytest.approx(0.192 * 2 * 730)
    assert float(by["aws_ebs_volume.data"].list_cost.value) == pytest.approx(0.08 * 2 * 200)
    assert by["aws_s3_bucket.b"].price_match_level.value == "not_priced"


def test_provisioned_iops_and_throughput_are_rows_as_aws_bills_them():
    res, rows, gaps = project({"main.tf": PROVIDER + '''
resource "aws_ebs_volume" "fast" {
  count      = 2
  size       = 500
  type       = "gp3"
  iops       = 6000
  throughput = 250
}
resource "aws_ebs_volume" "io" {
  size = 100
  type = "io2"
  iops = 4000
}
resource "aws_ebs_volume" "base" {
  size = 100
  type = "gp3"
  iops = 3000
}
resource "aws_ebs_volume" "old" {
  size = 100
  type = "gp2"
}
'''})
    q = rows["quantity"].to_dict()
    assert q["aws_ebs_volume.fast.iops"] == 6000 and rows.loc["aws_ebs_volume.fast.iops", "unit"] == "IOPS"
    assert q["aws_ebs_volume.fast.throughput"] == 250 and rows.loc["aws_ebs_volume.fast.throughput", "unit"] == "MiB/s"
    assert q["aws_ebs_volume.io.iops"] == 4000                    # io2: every IOPS is billed
    assert "aws_ebs_volume.base.iops" not in q and "aws_ebs_volume.old.iops" not in q   # within what gp3 includes


def test_elasticache_nodes_are_priced_by_node_usage_type_and_engine_operation(monkeypatch):
    """The catalog keys a node's price on its usage type (with the region's prefix) and the engine's operation."""
    import billparse.pricing as pricing_mod
    from billparse.pricing import PriceStats
    seen = []

    def fake_match(sub, catalog, defaults=None, cache=None, period_start=None):
        seen.append(sub.copy())
        return PriceStats()
    monkeypatch.setattr(pricing_mod, "match_prices", fake_match)

    class Catalog:
        _region_prefixes = {"us-west-2": "USW2", "eu-west-1": "EU"}
    res, rows, gaps = project({"main.tf": PROVIDER.replace("eu-west-1", "us-west-2") + '''
resource "aws_elasticache_replication_group" "sessions" {
  node_type               = "cache.r6g.large"
  num_node_groups         = 2
  replicas_per_node_group = 1
}
resource "aws_elasticache_cluster" "mc" {
  engine          = "memcached"
  node_type       = "cache.m6g.large"
  num_cache_nodes = 3
}
'''})
    rows = rows.reset_index()
    iac.price(rows, Catalog())
    sub = next(s for s in seen if "operation" in s).set_index("description")
    redis, mc = sub.loc["aws_elasticache_replication_group.sessions"], sub.loc["aws_elasticache_cluster.mc"]
    assert (redis["usage_type"], redis["operation"], redis["quantity"]) == \
        ("USW2-NodeUsage:cache.r6g.large", "CreateCacheCluster:0002", 4 * 730)     # 2 shards x (1 + 1 replica)
    assert (mc["usage_type"], mc["operation"], mc["quantity"]) == \
        ("USW2-NodeUsage:cache.m6g.large", "CreateCacheCluster:0001", 3 * 730)
    notes = rows.set_index("description")["price_assumptions"]
    assert notes["aws_elasticache_replication_group.sessions"].startswith("engine=redis (the default")
    assert iac._usage_type({}, "us-east-1", "NodeUsage:x") == "USE1-NodeUsage:x"    # found with or without it
    assert iac._usage_type({}, "mars-1", "NodeUsage:x") == "NodeUsage:x"             # a region with no prefix


def test_fixed_price_resources_get_their_catalog_keys(monkeypatch):
    """NAT gateways bill under EC2; a load balancer's type is its operation (application by default); us-east-1's
    usage types get USE1- (the catalog also finds them without it); KMS keys carry the region's name; secrets, keys
    and alarms are priced a month, the others by the hour."""
    import billparse.pricing as pricing_mod
    from billparse.pricing import PriceStats
    seen = []
    monkeypatch.setattr(pricing_mod, "match_prices", lambda sub, *a, **k: seen.append(sub.copy()) or PriceStats())

    class Catalog:
        _region_prefixes = {}
    res, rows, gaps = project({"main.tf": PROVIDER.replace("eu-west-1", "us-east-1") + '''
resource "aws_nat_gateway" "a" {
  count = 2
}
resource "aws_lb" "app" {
  name = "app"
}
resource "aws_lb" "net" {
  load_balancer_type = "network"
}
resource "aws_kms_key" "k" {
  description = "k"
}
resource "aws_secretsmanager_secret" "s" {
  count = 4
}
'''})
    rows = rows.reset_index()
    iac.price(rows, Catalog())
    sub = pd.concat(seen).set_index("description")
    keys = {n: tuple(None if pd.isna(r[c]) else r[c] for c in ("service_code", "usage_type", "operation", "quantity",
                                                             "unit")) for n, r in sub.iterrows()}
    assert keys["aws_nat_gateway.a"] == ("AmazonEC2", "USE1-NatGateway-Hours", "NatGateway", 2 * 730, "Hrs")
    assert keys["aws_lb.app"][1:4] == ("USE1-LoadBalancerUsage", "LoadBalancing:Application", 730)
    assert keys["aws_lb.net"][2] == "LoadBalancing:Network"
    assert keys["aws_kms_key.k"] == ("awskms", "us-east-1-KMS-Keys", None, 1, "keys")
    assert keys["aws_secretsmanager_secret.s"][1:] == ("USE1-AWSSecretsManager-Secrets", None, 4, "secrets")
    notes = rows.set_index("description")["price_assumptions"]
    assert notes["aws_lb.app"].startswith("load_balancer_type=application (the default")
    assert ("aws_lb.app", "usage") in gaps                      # its LCUs: still not in the files


def test_the_rest_of_group_b_gets_its_catalog_keys(monkeypatch):
    """RDS storage is a row of its own (Multi-AZ in its usage type); Aurora's class is written short and the cluster's
    aurora-iopt1 selects I/O-Optimized; Fargate is priced by vCPU and GB hours (ARM apart); Redshift, OpenSearch and
    MSK by their node usage types; an RDS instance with no multi_az is Single-AZ."""
    import billparse.pricing as pricing_mod
    from billparse.pricing import PriceStats
    seen = []
    monkeypatch.setattr(pricing_mod, "match_prices", lambda sub, *a, **k: seen.append(sub.copy()) or PriceStats())

    class Catalog:
        _region_prefixes = {"us-west-2": "USW2"}
    res, rows, gaps = project({"main.tf": PROVIDER.replace("eu-west-1", "us-west-2") + '''
resource "aws_db_instance" "pg" {
  engine            = "postgres"
  instance_class    = "db.m6i.large"
  allocated_storage = 500
  storage_type      = "gp3"
  multi_az          = true
}
resource "aws_db_instance" "my" {
  engine            = "mysql"
  instance_class    = "db.t3.medium"
  allocated_storage = 100
}
resource "aws_rds_cluster" "fast" {
  engine       = "aurora-mysql"
  storage_type = "aurora-iopt1"
}
resource "aws_rds_cluster_instance" "fast" {
  count              = 2
  cluster_identifier = aws_rds_cluster.fast.id
  engine             = "aurora-mysql"
  instance_class     = "db.r6g.2xlarge"
}
resource "aws_ecs_task_definition" "arm" {
  cpu    = "512"
  memory = "1024"
  runtime_platform {
    cpu_architecture = "ARM64"
  }
}
resource "aws_ecs_service" "arm" {
  launch_type     = "FARGATE"
  desired_count   = 4
  task_definition = aws_ecs_task_definition.arm.arn
}
resource "aws_redshift_cluster" "dw" {
  node_type       = "ra3.xlplus"
  number_of_nodes = 2
}
resource "aws_msk_cluster" "events" {
  number_of_broker_nodes = 3
  broker_node_group_info {
    instance_type = "kafka.m5.large"
    storage_info {
      ebs_storage_info {
        volume_size = 500
      }
    }
  }
}
'''})
    rows = rows.reset_index()
    iac.price(rows, Catalog())
    sub = pd.concat(seen).set_index("description")
    key = lambda n: tuple(None if pd.isna(sub.loc[n, c]) else sub.loc[n, c]  # noqa: E731
                          for c in ("service_code", "usage_type", "operation", "quantity", "unit"))
    assert key("aws_db_instance.pg.storage") == ("AmazonRDS", "USW2-RDS:Multi-AZ-GP3-Storage", "CreateDBInstance",
                                                 500, "GB-Mo")
    assert key("aws_db_instance.my.storage")[1] == "USW2-RDS:GP2-Storage"       # gp2: the default
    assert sub.loc["aws_db_instance.my", "deployment_option"] == "Single-AZ"
    assert key("aws_rds_cluster_instance.fast")[1:] == ("USW2-InstanceUsageIOOptimized:db.r6g.2xl", None, 2 * 730,
                                                        "Hrs")
    assert key("aws_ecs_service.arm.vcpu")[1:] == ("USW2-Fargate-ARM-vCPU-Hours:perCPU", None, 4 * 0.5 * 730, "Hrs")
    assert key("aws_ecs_service.arm.memory")[1] == "USW2-Fargate-ARM-GB-Hours"
    assert key("aws_redshift_cluster.dw")[1:3] == ("USW2-Node:ra3.xlplus", "RunComputeNode:0001")
    assert key("aws_msk_cluster.events")[1:3] == ("USW2-Kafka.m5.large", "RunBroker")
    assert key("aws_msk_cluster.events.ebs_storage_info")[1:] == ("USW2-Kafka.Storage.GP2", "RunVolume", 1500, "GB-Mo")
    assert ("aws_redshift_cluster.dw", "usage") in gaps                     # RA3 managed storage


def test_every_row_without_a_price_says_why(monkeypatch):
    import billparse.pricing as pricing_mod
    from billparse.pricing import PriceStats
    monkeypatch.setattr(pricing_mod, "match_prices", lambda sub, *a, **k: PriceStats())

    class Catalog:
        _region_prefixes = {}
    res, rows, gaps = project({"main.tf": PROVIDER + '''
variable "n" {}
resource "aws_instance" "many" {
  count         = var.n
  instance_type = "m5.large"
}
resource "aws_s3_bucket" "b" {
  bucket = "b"
}
resource "aws_vpc_endpoint" "secrets" {
  vpc_endpoint_type = "Interface"
}
resource "aws_ecs_task_definition" "t" {
  cpu    = "256"
  memory = "512"
}
resource "aws_ecs_service" "s" {
  launch_type     = "FARGATE"
  desired_count   = 1
  task_definition = aws_ecs_task_definition.t.arn
}
'''})
    rows = rows.reset_index()
    iac.price(rows, Catalog())
    why = rows.set_index("description")["price_missing"]
    level = rows.set_index("description")["price_match_level"]
    assert level["aws_instance.many"] == "not_priced" and why["aws_instance.many"].startswith("the files do not give how many")
    assert why["aws_s3_bucket.b"].startswith("its price depends on usage the files do not give: GB stored")
    assert why["aws_vpc_endpoint.secrets"].startswith("billparse has no price rule for it yet: an interface endpoint")
    assert why["aws_ecs_service.s"] == "priced in its .vcpu and .memory rows"
    res, rows, gaps = tf_file("04-aws-missing-usage.tf")
    rows = rows.reset_index()
    iac.price(rows, Catalog())
    why = rows.set_index("description")["price_missing"]
    assert why["aws_instance.application"] == "the files do not give the region, how many and the type (see Gaps)"
    assert why["aws_s3_bucket.archive"] == "the files do not give the region (see Gaps)"


def test_a_price_rule_in_the_rule_file_prices_a_new_resource_type(monkeypatch, tmp_path):
    """The price rules are data (specs/iac-prices/aws.json): a rule added there prices a resource type with no code
    change, its default is said in the notes; the shipped file loads; a rule with a key the engine does not know fails
    when the file is read, not on the row it would price."""
    import json

    import billparse.pricing as pricing_mod
    from billparse.pricing import PriceStats
    seen = []
    monkeypatch.setattr(pricing_mod, "match_prices", lambda sub, *a, **k: seen.append(sub.copy()) or PriceStats())

    class Catalog:
        _region_prefixes = {"eu-west-1": "EU"}
    assert iac.price_rules()["rules"] and iac.HOURS_A_MONTH == 730
    rules = json.loads(iac.RULES_FILE.read_text())
    rules["no_rule"] = {}
    rules["rules"].insert(0, {
        "name": "interface endpoint", "match": {"resource_type": ["aws_vpc_endpoint"]},
        "defaults": {"zones": {"value": 2, "note": "zones=2 (an assumption)"}},
        "set": {"service_code": "AmazonVPC", "usage_type": "{prefix}VpcEndpoint-Hours", "operation": None},
        "quantity": "quantity * hours * 2", "unit": "Hrs", "note": "hours={hours} a month in {zones} zones"})
    res, rows, gaps = project({"main.tf": PROVIDER + '''
resource "aws_vpc_endpoint" "secrets" {
  vpc_endpoint_type = "Interface"
}
'''})
    rows = rows.reset_index()
    iac.price(rows, Catalog(), rules=rules)
    sub = pd.concat(seen).set_index("description").loc["aws_vpc_endpoint.secrets"]
    assert (sub["service_code"], sub["usage_type"], sub["quantity"], sub["unit"]) == \
        ("AmazonVPC", "EU-VpcEndpoint-Hours", 2 * 730, "Hrs")
    note = rows.set_index("description").loc["aws_vpc_endpoint.secrets", "price_assumptions"]
    assert note == "zones=2 (an assumption); hours=730 a month in 2 zones"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**rules, "rules": [{"name": "x", "match": {}, "quantity": "quantity", "colour": 1}]}))
    with pytest.raises(ValueError, match="unknown keys"):
        iac.price_rules(bad)
