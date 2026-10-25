"""Infrastructure as code as an inventory: defaults, conditions, count, for_each, Auto Scaling groups, comments."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
    assert list(rows.index) == ["Api", "Group", "Db"]
    assert (rows.loc["Api", "instance_type"], rows.loc["Api", "quantity"]) == ("t3.xlarge", 1)
    assert (rows.loc["Group", "instance_type"], rows.loc["Group", "quantity"]) == ("m6i.large", 5)
    db = rows.loc["Db"]
    assert (db["database_engine"], db["storage_gb"], db["deployment_option"]) == ("postgres", 500, "Multi-AZ")
    assert {x["name"] for x in res.excluded} == {"Old", "Tpl"}
    assert [g["name"] for g in res.gaps] == ["Nested"]


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
