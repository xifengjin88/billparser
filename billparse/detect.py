"""Value detectors: tests on a column's values that propose a field with no help from its header (mapper.mapping).

A detector that fits at least 80% of the column's non-empty sample values proposes its field with score 3.0 (plus
mapper.value_score), so a column of m5.xlarge under a foreign or empty header is read as instance_type. A header rule
for another field wins when its total is higher; a tie goes to the header. A detector never proposes a number field:
costs and quantities need a header or the dictionary.

Dates propose charge_start (task 10's table has no date detector; its no-header test reads the date column).

Two detectors ask the price catalog, once per column per parse (MapContext.known): instance types (AWS ones must be
products the catalog has, half of the distinct values at least; Azure and GCP sizes by their pattern, the catalog has
none) and SKUs (no proposal without a catalog).
"""

from __future__ import annotations

import re

from svcres import regions

from .attributes import AWS_TYPE, REGION_PREFIX, USAGE_TYPE, instance_type

SCORE = 3.0
FIT = 0.8
DISTINCT_LIMIT = 2000                       # distinct values sent to the catalog at most (the sample has 200 rows)

GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
AWS_ACCOUNT = re.compile(r"^\d{12}$")
GCP_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
RESOURCE = re.compile(r"^(arn:aws[\w-]*:|/subscriptions/[^/]+/resourcegroups/|//[a-z]+\.googleapis\.com/|"
                      r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})$)", re.I)
# AWS operations (lineItem/Operation): the names the price list prints, with their codes ("RunInstances:0002")
OPERATION = re.compile(r"^(?:[A-Z][A-Za-z]+\.)?(?:(?:Run|Create|Consume|Start|Put|Get|Restore|Recognize|Invoke|"
                       r"Describe|Copy|Delete|List|Post|Send|Stopped)[A-Z][A-Za-z]*|LoadBalancing|NatGateway|"
                       r"Hourly|PayPerRequestThroughput)(?::[\w.-]+)*$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$|"
                  r"^\d{1,2}[./]\d{1,2}[./]\d{2,4}$")
REGION_CODE = re.compile(REGION_PREFIX.pattern[2:-1])     # "USE1": the code alone, without its dash
# ISO 4217 codes of the currencies bills are printed in
CURRENCIES = frozenset("""AED ARS AUD BGN BHD BRL CAD CHF CLP CNY COP CZK DKK EGP EUR GBP HKD HUF IDR ILS INR ISK JPY
KES KRW KWD MAD MXN MYR NGN NOK NZD OMR PEN PHP PKR PLN QAR RON RSD RUB SAR SEK SGD THB TRY TWD UAH USD UYU VND ZAR""".split())


def _texts(values: list) -> list[str]:
    out = []
    for v in values:
        if v is None or (isinstance(v, float) and v != v):
            continue
        t = str(v).strip()
        if isinstance(v, float) and v.is_integer():
            t = str(int(v))                     # 123456789012.0 read from a column with a blank
        if t and t.lower() not in ("nan", "none", "null"):
            out.append(t)
    return out


def _fits(vals: list[str], test) -> bool:
    return bool(vals) and sum(1 for v in vals if test(v)) / len(vals) >= FIT


def _whole_instance_type(v: str) -> bool:
    return instance_type(v) == v


def _region(v: str) -> bool:
    return regions.lookup(v) is not None or bool(REGION_CODE.fullmatch(v))


def _usage_type(v: str) -> bool:
    m = USAGE_TYPE.search(v)
    return m is not None and m.group(1) == v


def _known(ctx, column: str, vals: list[str]) -> set[str] | None:
    """The values the catalog has in its column (instance_type, sku), asked once per parse; None without a catalog."""
    cat = getattr(ctx, "catalog", None)
    if cat is None:
        return None
    distinct = sorted(set(vals))[:DISTINCT_LIMIT]
    key = (column, tuple(distinct))
    if key not in ctx.known:
        try:
            ctx.known[key] = cat.known(column, distinct)
        except Exception:                       # noqa: BLE001  a catalog that cannot answer gives no proposal
            ctx.known[key] = None
    return ctx.known[key]


def fields(header: str, values: list, ctx=None, ruled: set[str] | frozenset = frozenset()) -> list[str]:
    """The fields this column's values say it is. ruled: the fields its header's rules give it."""
    vals = _texts(values)
    if not vals:
        return []
    out = []
    if _fits(vals, _whole_instance_type):
        aws = sorted({v for v in vals if AWS_TYPE.fullmatch(v)})
        known = _known(ctx, "instance_type", aws) if aws else set()
        if known is None or not aws or len(known) * 2 >= len(aws):
            out.append("instance_type")
    if _fits(vals, _region):
        out.append("region")
    if _fits(vals, lambda v: bool(AWS_ACCOUNT.match(v) or GUID.match(v))):
        out.append("account_id")
    elif not (ruled - {"account_id", "account_name"}) and _fits(
            vals, lambda v: bool(GCP_PROJECT.match(v)) and bool(re.search(r"[-\d]", v))):
        out.append("account_id")                   # a GCP project ID: only under a header no rule claims
    if _fits(vals, lambda v: v in CURRENCIES):
        out.append("currency")
    if _fits(vals, _usage_type):
        out.append("usage_type")
    if _fits(vals, lambda v: bool(OPERATION.match(v))):
        out.append("operation")
    if _fits(vals, lambda v: bool(RESOURCE.match(v))):
        out.append("resource_id")
    if _fits(vals, lambda v: bool(GUID.match(v))):
        out.append("meter_id")
    if _fits(vals, lambda v: bool(DATE.match(v))):
        out.append("charge_start")                 # a date is the line's start (a header says when it is the end)                     # mapper.mapping proposes it only for an Azure file
    if not any(f in out for f in ("instance_type", "region", "account_id", "currency", "usage_type", "operation",
                                  "resource_id", "charge_start")):
        known = _known(ctx, "sku", vals)
        if known is not None and len(set(vals)) and len(known) * 2 >= len(set(vals)):
            out.append("sku_id")
    return out
