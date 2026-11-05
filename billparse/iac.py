"""Infrastructure as code (CloudFormation YAML or JSON, Terraform) as an inventory: what the files launch, how many,
of what size. No prices: the rows are resources with quantities, for an estimate to price.

  CloudFormation  Parameters at their defaults; Conditions (Equals, Not, And, Or) decide which resources exist;
                  an Auto Scaling group runs DesiredCapacity instances of its launch template's type; a nested stack
                  (AWS::CloudFormation::Stack) is a gap: its template is not in the file.
  Terraform       terraform.py: variables, tfvars, locals, functions, count, for_each, modules, volumes.

Resources that cost nothing by themselves (VPC, security group, IAM, a launch template) are listed as excluded, with
the reason, not as rows. Each row cites its file and line. A value the files do not give is never guessed: the row
keeps it empty, and a gap (name, field, message, src) says what is missing; so does the usage of a resource priced by
usage (USAGE).
"""

from __future__ import annotations

import functools
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from . import expr
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
    "aws_cloudwatch_log_group": ("Amazon CloudWatch", "log groups"),
    "AWS::Logs::LogGroup": ("Amazon CloudWatch", "log groups"),
    "aws_rds_cluster_instance": ("Amazon Relational Database Service", "instances"),
    "AWS::RDS::DBClusterInstance": ("Amazon Relational Database Service", "instances"),
    "aws_eks_node_group": ("Amazon Elastic Compute Cloud", "instances"),
    "aws_ecs_service": ("AWS Fargate", "tasks"),
    "aws_efs_file_system": ("Amazon Elastic File System", "file systems"),
    "aws_opensearch_domain": ("Amazon OpenSearch Service", "instances"),
    "aws_elasticsearch_domain": ("Amazon OpenSearch Service", "instances"),
    "aws_redshift_cluster": ("Amazon Redshift", "nodes"),
    "aws_msk_cluster": ("Amazon Managed Streaming for Apache Kafka", "brokers"),
    "aws_kinesis_stream": ("Amazon Kinesis", "shards"),
    "aws_eip": ("Amazon Virtual Private Cloud", "public IPv4 addresses"),
    "aws_vpc_endpoint": ("Amazon Virtual Private Cloud", "VPC endpoints"),
    "aws_sqs_queue": ("Amazon Simple Queue Service", "queues"),
    "aws_sns_topic": ("Amazon Simple Notification Service", "topics"),
    "aws_cloudfront_distribution": ("Amazon CloudFront", "distributions"),
    "aws_ecr_repository": ("Amazon Elastic Container Registry", "repositories"),
    "aws_secretsmanager_secret": ("AWS Secrets Manager", "secrets"),
    "aws_kms_key": ("AWS Key Management Service", "keys"),
    "aws_route53_zone": ("Amazon Route 53", "hosted zones"),
    "aws_cloudwatch_metric_alarm": ("Amazon CloudWatch", "alarms"),
    "aws_api_gateway_rest_api": ("Amazon API Gateway", "APIs"),
    "aws_apigatewayv2_api": ("Amazon API Gateway", "APIs"),
    "aws_sfn_state_machine": ("AWS Step Functions", "state machines"),
    "aws_wafv2_web_acl": ("AWS WAF", "web ACLs"),
    "aws_elb": ("Elastic Load Balancing", "load balancers"),
    "aws_alb": ("Elastic Load Balancing", "load balancers"),
    "AWS::ElasticLoadBalancing::LoadBalancer": ("Elastic Load Balancing", "load balancers"),
    "AWS::EC2::EIP": ("Amazon Virtual Private Cloud", "public IPv4 addresses"),
    "AWS::KMS::Key": ("AWS Key Management Service", "keys"),
    "AWS::SecretsManager::Secret": ("AWS Secrets Manager", "secrets"),
    "AWS::CloudWatch::Alarm": ("Amazon CloudWatch", "alarms"),
    "opensearch_volume": ("Amazon OpenSearch Service", "volumes"),             # the volume of a domain's node
    "msk_volume": ("Amazon Managed Streaming for Apache Kafka", "volumes"),    # the volume of a broker
}
EBS_TYPES = {"aws_ebs_volume", "AWS::EC2::Volume"}     # EC2 EBS volumes: storage, IOPS and throughput rows
EBS_FREE_IOPS = {"gp3": 3000}                          # what a volume type includes; above it is billed
EBS_FREE_MIBPS = {"gp3": 125}
EBS_IOPS_TYPES = {"gp3", "io1", "io2"}                 # types whose provisioned IOPS are billed
# the usage a resource is priced by, which an inventory does not give (a gap, never a guess)
USAGE = {
    "aws_s3_bucket": "GB stored and requests", "AWS::S3::Bucket": "GB stored and requests",
    "aws_nat_gateway": "GB of data processed", "AWS::EC2::NatGateway": "GB of data processed",
    "aws_alb": "load balancer capacity units (LCU)", "aws_elb": "GB of data processed",
    "AWS::ElasticLoadBalancing::LoadBalancer": "GB of data processed",
    "aws_lb": "load balancer capacity units (LCU)", "AWS::ElasticLoadBalancingV2::LoadBalancer":
        "load balancer capacity units (LCU)",
    "aws_cloudwatch_log_group": "GB ingested and stored", "AWS::Logs::LogGroup": "GB ingested and stored",
    "aws_lambda_function": "requests and GB-seconds", "AWS::Lambda::Function": "requests and GB-seconds",
    "aws_dynamodb_table": "read and write requests, and GB stored",
    "AWS::DynamoDB::Table": "read and write requests, and GB stored",
    "aws_rds_cluster": "GB stored and I/O requests (Aurora)", "AWS::RDS::DBCluster": "GB stored and I/O requests (Aurora)",
    "aws_efs_file_system": "GB stored", "aws_ecr_repository": "GB stored",
    "aws_vpc_endpoint": "GB of data processed", "aws_sqs_queue": "requests", "aws_sns_topic": "requests and deliveries",
    "aws_cloudfront_distribution": "GB transferred out and requests", "aws_route53_zone": "DNS queries",
    "aws_api_gateway_rest_api": "requests", "aws_apigatewayv2_api": "requests",
    "aws_sfn_state_machine": "state transitions", "aws_wafv2_web_acl": "rules and requests",
    "aws_kinesis_stream": "PUT payload units", "aws_secretsmanager_secret": "API calls",
    "aws_redshift_cluster": "managed storage GB (RA3 nodes)",
}
# no charge by themselves (a launch template is counted through the group that runs it)
FREE = re.compile(r"^(aws_(vpc|subnet|security_group\w*|iam_\w+|route_table\w*|internet_gateway|launch_template|"
                  r"network_acl\w*|key_pair|route|vpc_security_group_\w+|default_\w+|eip_association|"
                  r"lb_listener\w*|lb_target_group\w*|alb_listener\w*|alb_target_group\w*|ecs_cluster\w*|"
                  r"ecs_task_definition|db_subnet_group|db_parameter_group|db_option_group|rds_cluster_parameter_group|"
                  r"elasticache_subnet_group|elasticache_parameter_group|s3_bucket_\w+|autoscaling_policy|"
                  r"autoscaling_attachment|appautoscaling_\w+|lambda_permission|lambda_event_source_mapping|"
                  r"cloudwatch_log_stream|cloudwatch_event_\w+|sns_topic_subscription|sqs_queue_policy|"
                  r"kms_alias|route53_record|acm_certificate\w*|eks_addon|ecr_lifecycle_policy|"
                  r"secretsmanager_secret_version|ssm_parameter|vpc_endpoint_route_table_association)|AWS::(EC2::(VPC|Subnet|SecurityGroup\w*|RouteTable|InternetGateway|"
                  r"LaunchTemplate|NetworkAcl\w*|KeyPair)|IAM::\w+))$")
# attribute names (Terraform, CloudFormation) -> canonical field
SIZE = {"instance_type": "instance_type", "InstanceType": "instance_type", "instance_class": "instance_type",
        "DBInstanceClass": "instance_type", "node_type": "instance_type", "CacheNodeType": "instance_type",
        "engine": "database_engine", "Engine": "database_engine",
        "allocated_storage": "storage_gb", "AllocatedStorage": "storage_gb", "size": "storage_gb", "Size": "storage_gb",
        "multi_az": "multi_az", "MultiAZ": "multi_az",
        "storage_type": "storage_type", "StorageType": "storage_type", "VolumeType": "storage_type",
        "load_balancer_type": "load_balancer_type", "billing_mode": "billing_mode", "BillingMode": "billing_mode"}
# a volume's provisioned performance (CloudFormation; Terraform reads its volume blocks itself)
VOLUME_SIZE = {"Iops": "iops", "Throughput": "throughput"}
# attribute names whose meaning depends on the resource type
RTYPE_SIZE = {"aws_ebs_volume": {"type": "storage_type", "iops": "iops", "throughput": "throughput"},
              "AWS::EC2::Volume": VOLUME_SIZE,
              "AWS::ElasticLoadBalancingV2::LoadBalancer": {"Type": "load_balancer_type"}}
NODES = {"num_cache_clusters": "nodes", "NumCacheClusters": "nodes", "num_cache_nodes": "nodes", "NumCacheNodes": "nodes"}
DETECT = re.compile(r"AWSTemplateFormatVersion|^\s*resource\s+\"aws_\w+\"\s+\"[\w-]+\"|\"Type\"\s*:\s*\"AWS::", re.M)


@dataclass
class Item:
    file: str
    line: int
    name: str
    rtype: str
    count: int | None                                 # None: the files do not say (a gap names why)
    attrs: dict = field(default_factory=dict)
    note: str = ""
    # what the count is the product of: a resource's own count (a volume: its resource's), each module's count
    # (name -> count, None: a gap of that name). Empty: the item's own count.
    factors: dict = field(default_factory=dict)


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
                _gap(res, name, f"{path}:{line}", "condition",
                     f"condition {r['Condition']} cannot be decided from the file")
        if rtype == "AWS::CloudFormation::Stack":
            _gap(res, name, f"{path}:{line}", "module",
                 f"nested stack: its template ({value(props.get('TemplateURL'))}) is not in the file")
            continue
        if FREE.match(rtype):
            res.excluded.append({"name": name, "type": rtype, "src": f"{path}:{line}", "reason": "no charge by itself"})
            continue
        attrs = {(SIZE.get(k) or RTYPE_SIZE.get(rtype, {})[k]): value(v) for k, v in props.items()
                 if k in SIZE or k in RTYPE_SIZE.get(rtype, {})}
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
        if count is None:
            _gap(res, name, f"{path}:{line}", "quantity", "the count cannot be read from the file (a parameter "
                 "with no default, or a value from another stack)")
        res.items.append(Item(path, line, name, rtype, count, attrs, note))
    return True


# ---------------------------------------------------------------- prices (the catalog's list prices, on demand)
RULES_FILE = Path(__file__).resolve().parents[1] / "specs" / "iac-prices" / "aws.json"
HOURLY_UNITS = {"instances", "nodes", "brokers"}      # a row of these runs every hour of the month
RDS_INSTANCE_TYPES = {"aws_db_instance", "AWS::RDS::DBInstance"}   # its storage is a row of its own
RULE_KEYS = {"name", "match", "requires", "defaults", "vars", "set", "quantity", "unit", "note", "stop"}
PREFIX_SQL = ("SELECT region, split_part(usage_type, '-EBS:', 1) FROM price_product WHERE provider = 'aws' AND "
              "service_code = 'AmazonEC2' AND usage_type LIKE '%-EBS:VolumeUsage.gp2' GROUP BY 1, 2")
ESTIMATES = "_estimates"                              # an Item's attrs and a row's column: the LLM's values (JSON)
PRICE_OUT = ["list_unit_price", "list_cost", "price_match_level", "price_missing", "price_assumptions",
             "catalog_sku_id", "catalog_unit_price", "_list_unit_price_inferred", "_list_cost_inferred"]


@functools.cache
def price_rules(path: Path = RULES_FILE) -> dict:
    """The price rules (specs/iac-prices/aws.json), checked once: a rule with an unknown key, a bad regex or a bad
    quantity expression fails here, not on the row it would price."""
    rules = json.loads(path.read_text())
    for i, rule in enumerate(rules["rules"]):
        where = f"{path.name} rule {i} ({rule.get('name', '?')})"
        if (extra := set(rule) - RULE_KEYS):
            raise ValueError(f"{where}: unknown keys {sorted(extra)}")
        if "quantity" not in rule:
            raise ValueError(f"{where}: no quantity")
        expr.validate(rule["quantity"])
        for cond in (rule.get("match") or {}).values():
            if isinstance(cond, dict) and "regex" in cond:
                re.compile(cond["regex"])
        for v in (rule.get("vars") or {}).values():
            if "sub" in v:
                re.compile(v["sub"][0])
    return rules


HOURS_A_MONTH = price_rules()["hours_a_month"]        # AWS's month of hours (365 x 24 / 12)


def region_prefixes(catalog) -> dict[str, str]:
    """Each region's usage type prefix (us-west-2: USW2, eu-west-1: EU; us-east-1 has none), read once from the catalog:
    the gp2 volume of each region carries it. Kept on the catalog object."""
    if catalog is None:
        return {}
    found = getattr(catalog, "_region_prefixes", None)
    if found is None:
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError
        try:
            with catalog.engine.connect() as conn:
                found = {r: p for r, p in conn.execute(text(PREFIX_SQL)).all() if r and p}
        except SQLAlchemyError:                      # SQLite has no split_part: the Python fallback
            try:
                with catalog.engine.connect() as conn:
                    sql = ("SELECT region, usage_type FROM price_product WHERE provider = 'aws' AND service_code = "
                           "'AmazonEC2' AND usage_type LIKE '%-EBS:VolumeUsage.gp2'")
                    found = {r: u.split("-EBS:")[0] for r, u in conn.execute(text(sql)).all() if r and u}
            except SQLAlchemyError:
                found = {}
        catalog._region_prefixes = found
    return found


def _usage_type(prefixes: dict[str, str], region, name: str) -> str:
    """The usage type of a region: its prefix and the name. us-east-1 gets USE1-: some of its usage types have it
    (EKS, Secrets Manager, public IPv4) and the catalog finds the others without it (prices.store usage_alt)."""
    p = "USE1" if region == "us-east-1" else prefixes.get(region) if isinstance(region, str) else None
    return f"{p}-{name}" if p else name


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA or (isinstance(v, str) and not v.strip())


def _lower(v) -> str | None:
    return None if _blank(v) else str(v).strip().lower()


def _fill(template, ctx: dict):
    """A template ("{prefix}NodeUsage:{instance_type}") with the row's values; None stays None."""
    return template.format_map(ctx) if isinstance(template, str) else template


def _apply(rule: dict, r: dict, prefixes: dict[str, str], hours: int) -> tuple[dict, list[str]] | None:
    """The rule on one row: (the row's values with the rule's defaults and vars, the notes of the defaults it used),
    else None. Every rule needs a quantity above 0."""
    q = r.get("quantity")
    if _blank(q) or not q > 0:
        return None
    r = dict(r)
    notes: list[str] = []
    for fld, d in (rule.get("defaults") or {}).items():
        if _blank(r.get(fld)):
            r[fld] = d["value"]
            notes.append(d.get("note") or f"{fld}={d['value']} (the default, the files do not say)")
    for fld in rule.get("requires") or []:
        v = r.get(fld)
        if _blank(v) or (isinstance(v, (int, float)) and not v > 0):
            return None
    region = r.get("region")
    ctx = {**{k: (None if _blank(v) else v) for k, v in r.items()}, "hours": hours,
           "prefix": f"{p}-" if (p := "USE1" if region == "us-east-1" else
                                 prefixes.get(region) if isinstance(region, str) else None) else ""}
    for name, v in (rule.get("vars") or {}).items():
        raw = r.get(v["from"])
        if "map" in v:
            hit = {k.lower(): x for k, x in v["map"].items()}.get(_lower(raw))
            if hit is None and "else" not in v:
                return None                           # a value the rule has no key for: not this rule's row
            val = _fill(v["else"], ctx) if hit is None else hit
        elif "format" in v:
            val = v["else"] if _blank(raw) else v["format"].format(value=raw)
        else:
            val = "" if _blank(raw) else str(raw)
        if "sub" in v:
            val = re.sub(v["sub"][0], v["sub"][1], val)
        ctx[name] = val.upper() if v.get("upper") else val
    return ctx, notes


def _matches(rule: dict, r: dict) -> bool:
    for fld, cond in (rule.get("match") or {}).items():
        v = r.get(fld)
        if isinstance(cond, dict):
            if "regex" in cond and not (isinstance(v, str) and re.search(cond["regex"], v, re.I)):
                return False
        elif _lower(v) not in {str(c).lower() for c in cond}:
            return False
    return True


def price(rows: pd.DataFrame, catalog, defaults=None, cache=None, rules: dict | None = None):
    """The on-demand list price of each row the catalog can price from the files alone; list_unit_price is the
    catalog's price (an hour, a GB-month, an IOPS-month) and list_cost the month's (both inferred, from the
    catalog). Each row keeps its own quantity; price_assumptions says how it was priced.

    The rules are data (specs/iac-prices/aws.json, see its README): the first rule whose match fits a row sets its
    catalog keys (service code, usage type with the region's prefix, operation), its quantity in the catalog's unit
    (instances x 730 hours, volumes x GB) and its notes, with a default for each value the files do not give (said in
    the notes). A rule with "stop" keeps the rows it matches from the later rules. Rows priced by usage, and rows
    without a count, type or size, are not priced (their gaps say why). Returns pricing.match_prices' counters."""
    from .pricing import PriceStats, match_prices
    rules = rules or price_rules()
    hours = rules["hours_a_month"]
    engines = {k.lower(): v for k, v in (rules.get("maps") or {}).get("engines", {}).items()}
    for c in PRICE_OUT:
        rows[c] = None
        rows[c] = rows[c].astype(object)
    stats = PriceStats()
    if rows.empty or "quantity" not in rows:
        return stats
    qty = pd.to_numeric(rows["quantity"], errors="coerce").astype(float)
    view = rows.assign(quantity=qty)
    if "storage_gb" in view:
        view["storage_gb"] = pd.to_numeric(view["storage_gb"], errors="coerce")
    prefixes = region_prefixes(catalog)
    picked: dict[int, list[tuple[object, dict, list[str]]]] = {}
    for i, r in zip(rows.index, view.to_dict("records")):
        for n, rule in enumerate(rules["rules"]):
            if not _matches(rule, r):
                continue
            got = _apply(rule, r, prefixes, hours)
            if got is not None:
                picked.setdefault(n, []).append((i, *got))
            if got is not None or rule.get("stop"):
                break
    for n, found in sorted(picked.items()):
        rule = rules["rules"][n]
        idx = [i for i, _, _ in found]
        sub = view.loc[idx].copy()
        sub["hours"] = hours
        q = expr.evaluate(rule["quantity"], sub)
        for fld in rule.get("defaults") or {}:        # the defaults the rule used are the row's values
            sub[fld] = pd.Series([ctx.get(fld) for _, ctx, _ in found], index=idx, dtype=object)
        if "database_engine" in sub:
            sub["database_engine"] = sub["database_engine"].map(lambda v: engines.get(v.lower(), v)
                                                                if isinstance(v, str) else v)
        for fld, template in (rule.get("set") or {}).items():
            sub[fld] = pd.Series([_fill(template, ctx) for _, ctx, _ in found], index=idx, dtype=object)
        sub["quantity"] = q.values
        if rule.get("unit"):
            sub["unit"] = rule["unit"]
        sub = sub.drop(columns="hours")
        stats.merge(match_prices(sub, catalog, defaults, cache=cache))
        for c in PRICE_OUT:
            if c in sub:
                rows.loc[idx, c] = sub[c].astype(object)
        notes = ["; ".join([*used, _fill(rule["note"], ctx)]) for _, ctx, used in found]
        rows.loc[idx, "price_assumptions"] = [f"{a}; {n}" if isinstance(a, str) and a else n
                                              for a, n in zip(rows.loc[idx, "price_assumptions"], notes)]
    _why_not_priced(rows, qty, rules.get("no_rule") or {})
    _say_estimates(rows)
    return stats


def _say_estimates(rows: pd.DataFrame) -> None:
    """A priced row with a value the LLM estimated says so first in its price assumptions."""
    if ESTIMATES not in rows:
        return
    for i, raw in rows[ESTIMATES].items():
        if not isinstance(raw, str) or not raw or pd.isna(rows.at[i, "list_cost"]):
            continue
        said = "; ".join(f"{f}={e['value']} (an LLM estimate, {e['confidence']})" for f, e in json.loads(raw).items())
        a = rows.at[i, "price_assumptions"]
        rows.at[i, "price_assumptions"] = f"{said}; {a}" if isinstance(a, str) and a else said


def totals(rows: pd.DataFrame) -> dict:
    """An inventory's list price a month: of the rows priced from the files alone, and of all the priced rows (with
    the rows that use an LLM estimate). Both None when no row is priced."""
    if rows.empty or "list_cost" not in rows:
        return {"list_total_from_files": None, "list_total": None}
    cost = pd.to_numeric(rows["list_cost"], errors="coerce")
    if cost.notna().sum() == 0:
        return {"list_total_from_files": None, "list_total": None}
    est = rows[ESTIMATES].map(lambda v: isinstance(v, str) and bool(v)) if ESTIMATES in rows else \
        pd.Series(False, index=rows.index)
    return {"list_total_from_files": round(float(cost[~est].sum()), 2), "list_total": round(float(cost.sum()), 2)}


def _why_not_priced(rows: pd.DataFrame, qty: pd.Series, no_rule: dict[str, str]) -> None:
    """Each row the price step did not price says why (price_match_level not_priced, the reason in price_missing):
    the files do not give its region, count or type; it is priced in other rows; its price is usage the files do not
    give; or billparse has no price rule for it yet. A row the catalog could not match keeps the catalog's reason."""
    def text(v) -> str | None:
        return v if isinstance(v, str) and v else None

    for i, r in rows.iterrows():
        if pd.notna(r.get("list_cost")) or text(r.get("price_match_level")):
            continue
        rtype, unit = r.get("resource_type"), r.get("unit")
        lacks = [w for w, missing in (("the region", not text(r.get("region"))), ("how many", pd.isna(qty[i])),
                                      ("the type", unit in HOURLY_UNITS and not text(r.get("instance_type"))))
                 if missing]
        if lacks:
            why = f"the files do not give {' and '.join(lacks) if len(lacks) < 3 else ', '.join(lacks[:-1]) + ' and ' + lacks[-1]} (see Gaps)"
        elif rtype == "aws_ecs_service" and unit == "tasks":
            why = "priced in its .vcpu and .memory rows"
        elif rtype in no_rule:                     # a fixed part billparse does not price yet, before its usage
            why = f"billparse has no price rule for it yet: {no_rule[rtype]}"
        elif rtype in USAGE:
            why = f"its price depends on usage the files do not give: {USAGE[rtype]} (see Gaps)"
        else:
            why = "billparse has no price rule for it yet"
        rows.at[i, "price_match_level"] = "not_priced"
        rows.at[i, "price_missing"] = why


def missing_inputs(gaps: list[dict] | None) -> list[dict]:
    """The gaps of an inventory that leave a resource without its count, size, type or region (or leave resources out:
    a module, a variable file). Usage that is not in the files is not one: no inventory gives it."""
    return [g for g in gaps or [] if g.get("field") != "usage"]


# ---------------------------------------------------------------- helpers (also for terraform.py)
def _gap(res: Result, name: str, src: str, fld: str | None, message: str) -> None:
    """A value the files do not give (fld: the field, "usage", "module", ...): once for each name and field."""
    if not any(g.get("name") == name and g.get("field") == fld for g in res.gaps):
        res.gaps.append({"name": name, "src": src, "field": fld, "message": message})


def _number(v) -> float | int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return int(f) if f == int(f) else f


def _text(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _int(v) -> int | None:
    n = _number(v)
    return None if n is None else int(n)


def _line_finder(lines: list[tuple[int, str]]):
    def find(token: str) -> int:
        return next((n for n, line in lines if line.strip().startswith(token)), 0)
    return find


# ---------------------------------------------------------------- the inventory
def read(doc: Document) -> Result:
    from . import terraform
    res, tf, stacks = Result(), {}, []
    for path, lines in files_of(doc).items():
        text = "\n".join(line for _, line in lines)
        if path.endswith((".tf", ".tfvars")):       # every file of a Terraform module: variables.tf has no resource
            tf[path] = lines
        elif not is_iac(text):
            continue
        elif "AWSTemplateFormatVersion" in text or re.search(r"\"Type\"\s*:\s*\"AWS::", text) or \
                re.search(r"^Resources:\s*$", text, re.M):
            if cloudformation(path, lines, res):
                stacks.append(path)
        else:
            tf[path] = lines
    if tf:
        terraform.read(tf, res)
    if stacks and not res.region:                   # a template is deployed to a region chosen at deploy time
        _gap(res, "AWS::Region", stacks[0], "region",
             "a CloudFormation template does not give its region: it is chosen when the stack is deployed")
    for it in res.items:                            # what an inventory cannot say: the usage of usage-priced resources
        what = USAGE.get(it.rtype)
        if it.rtype in ("aws_dynamodb_table", "AWS::DynamoDB::Table") and \
                str(it.attrs.get("billing_mode", "")).upper() not in ("PAY_PER_REQUEST", ""):
            what = "GB stored"                      # provisioned capacity: only the storage is not in the file
        if what:
            _gap(res, it.name, f"{it.file}:{it.line}", "usage", f"its usage is not in the file: {what}")
    return res


def _performance_rows(vol: dict) -> list[dict]:
    """An EBS volume's provisioned IOPS and throughput above what its type includes (gp3: 3000 IOPS and 125 MiB/s;
    io1 and io2: every IOPS): rows of their own, as AWS bills them. Quantity: the total of all the volumes."""
    t, n = vol.get("storage_type"), vol.get("quantity")
    out = []
    for fld, unit, free, billed in (("iops", "IOPS", EBS_FREE_IOPS, EBS_IOPS_TYPES),
                                    ("throughput", "MiB/s", EBS_FREE_MIBPS, set(EBS_FREE_MIBPS))):
        per = vol.get(fld)
        if t not in billed or per is None or per - free.get(t, 0) <= 0:
            continue
        extra = per - free.get(t, 0)
        out.append(dict(vol, description=f"{vol['description']}.{fld}", unit=unit, storage_gb=None,
                        quantity=None if n is None else n * extra,
                        _extra=f"{fld} {per:g} a volume, {extra:g} above what {t} includes, x {n} volumes"))
    return out


def _billed_apart(row: dict) -> list[dict]:
    """What AWS bills apart from the resource: an RDS instance's storage (GB, all its instances), a Fargate service's
    vCPU and memory (all its tasks). The resource row stays: its count, its size."""
    n, rtype, out = row.get("quantity"), row.get("resource_type"), []
    gb = row.get("storage_gb")
    if rtype in RDS_INSTANCE_TYPES and gb:
        out.append(dict(row, description=f"{row['description']}.storage", unit="GB", instance_type=None,
                        quantity=None if n is None else n * gb, _extra=f"{gb:g} GB a database x {n}"))
    if rtype == "aws_ecs_service":
        for fld, unit, what in (("vcpu", "vCPU", "vCPU"), ("memory_gb", "GB", "GB of memory")):
            per = row.get(fld)
            if per:
                out.append(dict(row, description=f"{row['description']}.{'vcpu' if fld == 'vcpu' else 'memory'}",
                                unit=unit, quantity=None if n is None else n * per,
                                _extra=f"{per:g} {what} a task x {n} tasks"))
    return out


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
            "storage_gb": _number(a.get("storage_gb")), "storage_type": _text(a.get("storage_type")),
            "load_balancer_type": _text(a.get("load_balancer_type")), "billing_mode": _text(a.get("billing_mode")),
            "vcpu": _number(a.get("vcpu")), "memory_gb": _number(a.get("memory_gb")),
            "iops": _number(a.get("iops")), "throughput": _number(a.get("throughput")),
            "architecture": _text(a.get("architecture")),
            "region": res.region, "_src": f"{it.file}:{it.line}", "_extra": it.note or None,
            "_estimates": a.get(ESTIMATES)})          # the LLM's values for this resource (estimate.py), JSON
        if it.rtype in EBS_TYPES:
            out.extend(_performance_rows(out[-1]))
        out.extend(_billed_apart(out[-1]))
    df = pd.DataFrame(out)
    if not df.empty:
        df["billed_cost"] = df["billed_cost"].astype(float)   # an inventory has no cost: empty, as a number
        q = pd.to_numeric(df["quantity"], errors="coerce")
        whole = q.dropna().eq(q.dropna().round()).all()    # counts; a Fargate row can hold 0.25 vCPU
        df["quantity"] = q.astype("Int64" if whole else "Float64")   # also when one is not known (a gap)
    return df
