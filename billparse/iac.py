"""Infrastructure as code (CloudFormation YAML or JSON, Terraform) as an inventory: what the files launch, how many,
of what size. No prices: the rows are resources with quantities, for an estimate to price.

  CloudFormation  Parameters at their defaults; Conditions (Equals, Not, And, Or) decide which resources exist;
                  an Auto Scaling group runs DesiredCapacity instances of its launch template's type; a nested stack
                  (AWS::CloudFormation::Stack) is a gap: its template is not in the file.
  Terraform       variables at their defaults; `count` (with `cond ? a : b`) and `for_each` (a list or a set) give
                  the number of instances; commented-out blocks are not read; an aws_autoscaling_group runs
                  desired_capacity instances of its aws_launch_template's type; the provider gives the region.

Resources that cost nothing by themselves (VPC, security group, IAM, a launch template) are listed as excluded, with
the reason, not as rows. Each row cites its file and line.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pandas as pd
import yaml

from .docmodel import Document

# resource type (Terraform or CloudFormation) -> (service, what one unit is, attribute of the size)
SERVICES = {
    "aws_instance": ("Amazon Elastic Compute Cloud", "instances"),
    "AWS::EC2::Instance": ("Amazon Elastic Compute Cloud", "instances"),
    "aws_autoscaling_group": ("Amazon Elastic Compute Cloud", "instances"),
    "AWS::AutoScaling::AutoScalingGroup": ("Amazon Elastic Compute Cloud", "instances"),
    "aws_db_instance": ("Amazon Relational Database Service", "instances"),
    "AWS::RDS::DBInstance": ("Amazon Relational Database Service", "instances"),
    "aws_rds_cluster": ("Amazon Relational Database Service", "clusters"),
    "AWS::RDS::DBCluster": ("Amazon Relational Database Service", "clusters"),
    "aws_elasticache_replication_group": ("Amazon ElastiCache", "nodes"),
    "aws_elasticache_cluster": ("Amazon ElastiCache", "nodes"),
    "AWS::ElastiCache::ReplicationGroup": ("Amazon ElastiCache", "nodes"),
    "AWS::ElastiCache::CacheCluster": ("Amazon ElastiCache", "nodes"),
    "aws_s3_bucket": ("Amazon Simple Storage Service", "buckets"),
    "AWS::S3::Bucket": ("Amazon Simple Storage Service", "buckets"),
    "aws_lb": ("Elastic Load Balancing", "load balancers"),
    "AWS::ElasticLoadBalancingV2::LoadBalancer": ("Elastic Load Balancing", "load balancers"),
    "aws_nat_gateway": ("Amazon Virtual Private Cloud", "NAT gateways"),
    "AWS::EC2::NatGateway": ("Amazon Virtual Private Cloud", "NAT gateways"),
    "aws_ebs_volume": ("Amazon Elastic Compute Cloud", "volumes"),
    "AWS::EC2::Volume": ("Amazon Elastic Compute Cloud", "volumes"),
    "aws_lambda_function": ("AWS Lambda", "functions"),
    "AWS::Lambda::Function": ("AWS Lambda", "functions"),
    "aws_dynamodb_table": ("Amazon DynamoDB", "tables"),
    "AWS::DynamoDB::Table": ("Amazon DynamoDB", "tables"),
    "aws_eks_cluster": ("Amazon Elastic Container Service for Kubernetes", "clusters"),
    "AWS::EKS::Cluster": ("Amazon Elastic Container Service for Kubernetes", "clusters"),
}
# no charge by themselves (a launch template is counted through the group that runs it)
FREE = re.compile(r"^(aws_(vpc|subnet|security_group\w*|iam_\w+|route_table\w*|internet_gateway|launch_template|"
                  r"network_acl\w*|key_pair)|AWS::(EC2::(VPC|Subnet|SecurityGroup\w*|RouteTable|InternetGateway|"
                  r"LaunchTemplate|NetworkAcl\w*|KeyPair)|IAM::\w+))$")
# attribute names (Terraform, CloudFormation) -> canonical field
SIZE = {"instance_type": "instance_type", "InstanceType": "instance_type", "instance_class": "instance_type",
        "DBInstanceClass": "instance_type", "node_type": "instance_type", "CacheNodeType": "instance_type",
        "engine": "database_engine", "Engine": "database_engine",
        "allocated_storage": "storage_gb", "AllocatedStorage": "storage_gb", "size": "storage_gb", "Size": "storage_gb",
        "multi_az": "multi_az", "MultiAZ": "multi_az"}
NODES = {"num_cache_clusters": "nodes", "NumCacheClusters": "nodes", "num_cache_nodes": "nodes", "NumCacheNodes": "nodes"}
DETECT = re.compile(r"AWSTemplateFormatVersion|^\s*resource\s+\"aws_\w+\"\s+\"[\w-]+\"|\"Type\"\s*:\s*\"AWS::", re.M)


@dataclass
class Item:
    file: str
    line: int
    name: str
    rtype: str
    count: int
    attrs: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class Result:
    items: list[Item] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    region: str | None = None


# ---------------------------------------------------------------- files out of the document
def files_of(doc: Document) -> dict[str, list[tuple[int, str]]]:
    """Each text file of the document as (line number, line) pairs, the indent kept (a text region keeps it as x)."""
    out: dict[str, list[tuple[int, str]]] = {}
    for reg in doc.regions:
        if reg.is_frame or not reg.rows:
            continue
        name = reg.name[:-len("page1")].rstrip(":") if reg.name.endswith("page1") else reg.name
        lines = [(row.index, " " * int(round(row.cells[0].x0 / 6.0)) + row.text) for row in reg.rows if row.cells]
        out[name or doc.name] = lines
    return out


def is_iac(text: str) -> bool:
    return bool(DETECT.search(text))


# ---------------------------------------------------------------- CloudFormation
class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        if suffix == "GetAtt":
            value = value.split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {name: value}


_CfnLoader.add_multi_constructor("!", _tag)


def cloudformation(path: str, lines: list[tuple[int, str]], res: Result) -> bool:
    text = "\n".join(line for _, line in lines)
    try:
        tpl = json.loads(text) if text.lstrip().startswith("{") else yaml.load(text, Loader=_CfnLoader)  # noqa: S506
    except (yaml.YAMLError, json.JSONDecodeError):
        return False
    if not isinstance(tpl, dict) or not isinstance(tpl.get("Resources"), dict):
        return False
    params = {k: (v or {}).get("Default") for k, v in (tpl.get("Parameters") or {}).items()}
    conds: dict[str, bool | None] = {}

    def value(v):
        if isinstance(v, dict) and len(v) == 1:
            (k, a), = v.items()
            if k == "Ref":
                return params.get(a, a)
            if k == "Fn::Equals":
                x, y = (value(z) for z in a)
                return None if x is None or y is None else str(x) == str(y)
            if k == "Fn::Not":
                x = value(a[0])
                return None if x is None else not x
            if k in ("Fn::And", "Fn::Or"):
                xs = [value(z) for z in a]
                if None in xs:
                    return None
                return all(xs) if k == "Fn::And" else any(xs)
            if k == "Condition":
                return condition(a)
            if k == "Fn::If":
                c = condition(a[0])
                return None if c is None else value(a[1] if c else a[2])
            return None                                 # GetAtt, Sub, ImportValue, ...: not known from the file
        return v

    def condition(name):
        if name not in conds:
            conds[name] = None
            conds[name] = value((tpl.get("Conditions") or {}).get(name))
        return conds[name]

    where = _line_finder(lines)
    resources = tpl["Resources"]
    templates = {k: r for k, r in resources.items() if (r or {}).get("Type") == "AWS::EC2::LaunchTemplate"}
    for name, r in resources.items():
        rtype, props = (r or {}).get("Type", ""), (r or {}).get("Properties") or {}
        line = where(f"{name}:") or where(f'"{name}"')
        if r.get("Condition"):
            c = condition(r["Condition"])
            if c is False:
                res.excluded.append({"name": name, "type": rtype, "src": f"{path}:{line}",
                                     "reason": f"condition {r['Condition']} is false with the parameter defaults"})
                continue
            if c is None:
                res.gaps.append({"name": name, "src": f"{path}:{line}",
                                 "message": f"condition {r['Condition']} cannot be decided from the file"})
        if rtype == "AWS::CloudFormation::Stack":
            res.gaps.append({"name": name, "src": f"{path}:{line}", "message":
                             f"nested stack: its template ({value(props.get('TemplateURL'))}) is not in the file"})
            continue
        if FREE.match(rtype):
            res.excluded.append({"name": name, "type": rtype, "src": f"{path}:{line}", "reason": "no charge by itself"})
            continue
        attrs = {SIZE[k]: value(v) for k, v in props.items() if k in SIZE}
        count, note = 1, ""
        if rtype == "AWS::AutoScaling::AutoScalingGroup":
            count = _int(value(props.get("DesiredCapacity")) or value(props.get("MinSize")) or 0)
            note = f"desired {count} (min {value(props.get('MinSize'))}, max {value(props.get('MaxSize'))})"
            ref = value((props.get("LaunchTemplate") or {}).get("LaunchTemplateId")) or \
                (props.get("LaunchTemplate") or {}).get("LaunchTemplateId", {}).get("Ref")
            data = ((templates.get(ref) or {}).get("Properties") or {}).get("LaunchTemplateData") or {}
            if data.get("InstanceType"):
                attrs["instance_type"] = value(data["InstanceType"])
                note += f"; launch template {ref}"
        for k, v in props.items():
            if k in NODES:
                count = _int(value(v))
        res.items.append(Item(path, line, name, rtype, count, attrs, note))
    return True


# ---------------------------------------------------------------- Terraform
BLOCK = re.compile(r"^\s*(resource|variable|provider|data)\s+\"([\w-]+)\"(?:\s+\"([\w-]+)\")?\s*\{", re.M)


def _strip_comments(text: str) -> str:
    """Drop # and // comments and /* */ blocks, keeping the line breaks (so line numbers stay right)."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    out = []
    for line in text.split("\n"):
        cut, quote = len(line), False
        for i, ch in enumerate(line):
            if ch == '"' and (i == 0 or line[i - 1] != "\\"):
                quote = not quote
            elif not quote and (ch == "#" or line.startswith("//", i)):
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def _body(text: str, start: int) -> str:
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    return text[start + 1:]


def _attrs(body: str) -> dict[str, str]:
    """The block's own `key = value` lines (not those of nested blocks)."""
    out, depth = {}, 0
    for line in body.split("\n"):
        if depth == 0 and (m := re.match(r"\s*([\w-]+)\s*=\s*(.+?)\s*$", line)) and not m.group(2).endswith("{"):
            out[m.group(1)] = m.group(2)
        depth += line.count("{") - line.count("}")
    return out


def terraform(path: str, lines: list[tuple[int, str]], res: Result, variables: dict, blocks: list) -> None:
    """First pass: collect the blocks of one file (variables are shared by all files of a module)."""
    numbers = [n for n, _ in lines]
    text = _strip_comments("\n".join(line for _, line in lines))
    for m in BLOCK.finditer(text):
        kind, a, b = m.group(1), m.group(2), m.group(3)
        body = _body(text, m.end() - 1)
        line = numbers[text.count("\n", 0, m.start())] if numbers else 0
        attrs = _attrs(body)
        if kind == "variable":
            variables[a] = _literal(attrs.get("default"), {})
        elif kind == "provider" and a == "aws" and attrs.get("region"):
            res.region = res.region or _literal(attrs["region"], {})
        elif kind == "resource":
            nested = {k: _attrs(v) for k, v in re.findall(r"([\w-]+)\s*\{([^{}]*)\}", body)}
            blocks.append((path, line, a, b, attrs, nested))


def terraform_items(res: Result, variables: dict, blocks: list) -> None:
    templates = {name: attrs for _, _, rtype, name, attrs, _ in blocks if rtype == "aws_launch_template"}
    for path, line, rtype, name, attrs, nested in blocks:
        src = f"{path}:{line}"
        if FREE.match(rtype):
            res.excluded.append({"name": f"{rtype}.{name}", "type": rtype, "src": src, "reason": "no charge by itself"})
            continue
        count, note = 1, ""
        if "count" in attrs:
            count = _int(_literal(attrs["count"], variables))
            note = f"count = {attrs['count']}"
        elif "for_each" in attrs:
            keys = _literal(attrs["for_each"], variables)
            count = len(keys) if isinstance(keys, (list, dict, set)) else 1
            note = f"for_each {keys}"
        if count == 0:
            res.excluded.append({"name": f"{rtype}.{name}", "type": rtype, "src": src,
                                 "reason": f"{note}: 0 with the variable defaults"})
            continue
        size = {SIZE[k]: _literal(v, variables) for k, v in attrs.items() if k in SIZE}
        for k, v in attrs.items():
            if k in NODES:
                count = _int(_literal(v, variables))
        if rtype == "aws_autoscaling_group":
            desired = attrs.get("desired_capacity") or attrs.get("min_size") or "0"
            count = _int(_literal(desired, variables))
            note = f"desired {count} (min {attrs.get('min_size')}, max {attrs.get('max_size')})"
            ref = re.match(r"aws_launch_template\.([\w-]+)\.", (nested.get("launch_template") or {}).get("id", ""))
            if ref and templates.get(ref.group(1), {}).get("instance_type"):
                size["instance_type"] = _literal(templates[ref.group(1)]["instance_type"], variables)
                note += f"; launch template {ref.group(1)}"
        res.items.append(Item(path, line, f"{rtype}.{name}", rtype, count, size, note))


def _literal(expr, variables: dict):
    """A Terraform value: a string, number, bool, list, var.x, `cond ? a : b`, toset([...]). Else the text."""
    if expr is None:
        return None
    s = str(expr).strip()
    if (m := re.match(r"^(.+?)\?(.+?):(.+)$", s)):
        c = _literal(m.group(1), variables)
        if isinstance(c, bool):
            return _literal(m.group(2) if c else m.group(3), variables)
        return s
    if (m := re.match(r"^toset\((.*)\)$", s)):
        return _literal(m.group(1), variables)
    if (m := re.match(r"^var\.([\w-]+)$", s)):
        return variables.get(m.group(1), s)
    if s in ("true", "false"):
        return s == "true"
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return float(s) if "." in s else int(s)
    if s.startswith('"') and s.endswith('"') and '"' not in s[1:-1]:
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        return [_literal(x, variables) for x in re.findall(r'"[^"]*"|[^,\s][^,]*', s[1:-1]) if x.strip()]
    return s


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 1


def _line_finder(lines: list[tuple[int, str]]):
    def find(token: str) -> int:
        return next((n for n, line in lines if line.strip().startswith(token)), 0)
    return find


# ---------------------------------------------------------------- the inventory
def read(doc: Document) -> Result:
    res, variables, blocks = Result(), {}, []
    for path, lines in files_of(doc).items():
        text = "\n".join(line for _, line in lines)
        if not is_iac(text):
            continue
        if "AWSTemplateFormatVersion" in text or re.search(r"\"Type\"\s*:\s*\"AWS::", text) or \
                re.search(r"^Resources:\s*$", text, re.M):
            cloudformation(path, lines, res)
        else:
            terraform(path, lines, res, variables, blocks)
    if blocks:
        terraform_items(res, variables, blocks)
    return res


def rows(res: Result) -> pd.DataFrame:
    out = []
    for it in res.items:
        service, unit = SERVICES.get(it.rtype, (None, "resources"))
        a = it.attrs
        multi = a.get("multi_az")
        out.append({
            "description": it.name, "billed_cost": None, "resource_type": it.rtype, "service_raw": service,
            "provider": "aws",
            "quantity": it.count, "unit": unit, "instance_type": a.get("instance_type"),
            "database_engine": a.get("database_engine"),
            "deployment_option": None if multi is None else ("Multi-AZ" if str(multi).lower() == "true" else "Single-AZ"),
            "storage_gb": _int(a["storage_gb"]) if a.get("storage_gb") is not None else None,
            "region": res.region, "_src": f"{it.file}:{it.line}", "_extra": it.note or None})
    df = pd.DataFrame(out)
    if not df.empty:
        df["billed_cost"] = df["billed_cost"].astype(float)   # an inventory has no cost: empty, as a number
    return df
