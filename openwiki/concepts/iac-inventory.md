---
type: concept
title: Infrastructure-as-code inventory
description: How billparse reads CloudFormation and Terraform files as an inventory of AWS resources with quantities and sizes (no prices), through the built-in iac spec and billparse/iac.py.
tags: [iac, terraform, cloudformation, inventory]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-f3cc0082d47cbd899db3ec14
    resource: repo://billparse/iac.py
  - id: openwiki-source-fa3b516cbac4d1d454de7725
    resource: repo://billparse/spec.py
  - id: openwiki-source-c52509a8c944c0ef21634ae6
    resource: repo://specs/builtin/iac.json
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Infrastructure-as-code inventory

billparse also reads infrastructure as code: what the files would launch, how many, and of what size. The result is
an **inventory**, not cost lines: every row has an empty `billed_cost`. Pricing the resources is the consumer's job
(for example a cost estimate).

## How a file gets here

1. `docmodel` reads YAML, `.tf` and `.hcl` files as text pages (JSON as a frame plus a key/value region); see
   [Reading files into the document model](../architecture/document-model.md).
2. The built-in spec `specs/builtin/iac.json` has `"source": "iac"` and a `detect.text` regex that matches
   `AWSTemplateFormatVersion`, a Terraform `resource "aws_…" "…"` block, or `"Type": "AWS::`. Its `meta.kind` is
   `inventory`, so the attempt is `auxiliary` and no cost checks run.
3. `spec.run` calls `iac.read(doc)` and `iac.rows(result)`.

`iac.read` takes every text file of the document (`files_of`, indent restored from the x position), skips files that
are not IaC, and reads each as CloudFormation or Terraform. A zip of a Terraform module is read as one module:
variables are shared by all its files.

## CloudFormation

- The template is read as JSON when it starts with `{`, else as YAML.
- Parameters take their defaults; `Conditions` (`Equals`, `Not`, `And`, `Or`, `If`, `Condition`) decide which
  resources exist. A resource whose condition is false is excluded with the reason; one whose condition cannot be
  decided from the file (for example it uses `GetAtt` or `ImportValue`) is kept and also reported as a gap.
- Short-form tags (`!Ref`, `!GetAtt`, `!If`, …) are loaded by a custom YAML loader (`_CfnLoader`).
- An Auto Scaling group runs `DesiredCapacity` instances of its launch template's type.
- A nested stack (`AWS::CloudFormation::Stack`) is a **gap**: its template is not in the file.

## Terraform

- Variables take their defaults; the `aws` provider's `region` gives the region.
- `count` (including `cond ? a : b`) and `for_each` (a list or set) give the number of instances; `count = 0` with the
  defaults excludes the resource with that reason.
- Commented-out blocks are not read.
- An `aws_autoscaling_group` runs `desired_capacity` (else `min_size`) instances of its `aws_launch_template`'s type.
- ElastiCache node counts come from `num_cache_clusters` / `num_cache_nodes`.

## Rows

`iac.rows` produces one row per resource:

| Column | Value |
|---|---|
| `description` | `type.name` (Terraform) or the logical id |
| `service_raw` | the AWS service for known types (`SERVICES`: EC2, Auto Scaling, RDS, ElastiCache, S3, ELB, NAT gateway, EBS, Lambda, DynamoDB, EKS) |
| `quantity`, `unit` | the count and what one unit is (instances, nodes, buckets, …) |
| `instance_type`, `database_engine`, `deployment_option`, `storage_gb` | from size attributes (`instance_type`, `DBInstanceClass`, `engine`, `allocated_storage`, `multi_az`, …) |
| `region`, `provider` | the provider region (Terraform), `aws` |
| `_src` | file and line, so each row cites where it is declared |
| `_extra` | how the count was found (`count = …`, `desired 3 (min 2, max 6)`, the launch template) |

Resources that cost nothing by themselves (VPC, subnet, security group, IAM, route table, internet gateway, launch
template, network ACL, key pair) are listed in `excluded` with the reason, not as rows. Gaps (such as nested stacks)
become warnings on the extraction.

## Tests

`tests/test_iac.py`: CloudFormation resources at their parameter defaults; Terraform `count`, `for_each`, comments and
Auto Scaling groups; and text that is not infrastructure is not read as IaC.
