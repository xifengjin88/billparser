"""The LLM's estimates for the values an inventory's files do not give (a region, a count, an instance type, a size).

One call for each file with such gaps (iac.py), at parse time. The LLM sees the files (redacted) and the numbered
gaps, and answers each gap it can with a value, a one-line reason and a confidence. billparse checks every value
before it uses it (a real region, an instance type the catalog knows for that resource, a count or size in range);
a value that fails stays out and its gap stays open. A used value goes into the resource's rows marked as an
estimate (the `_estimates` column: contract origin inferred, source claude), the gap stays open with the value as
its candidate, and the rows priced with it are in the inventory's second total only (iac.totals). A reprice prices
the stored rows: it reuses the estimates and makes no call.
"""

from __future__ import annotations

import json
import math
import re

from . import iac
from . import llm as llm_mod
from .docmodel import Document

FIELDS = ("region", "quantity", "instance_type", "storage_gb", "vcpu", "memory_gb")   # the gaps an estimate may fill
LIMITS = {"quantity": (1, 100), "storage_gb": (1, 65536), "vcpu": (0.25, 192), "memory_gb": (0.5, 1536)}
CONFIDENCE = {"low": 0.3, "medium": 0.6, "high": 0.85}   # a CitedValue's confidence for each word
MAX_TEXT = 40_000                                         # characters of the files sent
REGION = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]?)?-[a-z]+-\d$")
INSTANCE = re.compile(r"^[a-z][a-z0-9-]*\.[a-z0-9-]+(\.[a-z0-9]+)?$")
# the instance type a resource takes: RDS db.*, ElastiCache cache.*, OpenSearch *.search, the rest none of them
FAMILY = {"Amazon Relational Database Service": lambda t: t.startswith("db."),
          "Amazon ElastiCache": lambda t: t.startswith("cache."),
          "Amazon OpenSearch Service": lambda t: t.endswith(".search"),
          "Amazon Redshift": lambda t: not t.startswith(("db.", "cache.")),
          "Amazon MSK": lambda t: t.startswith("kafka.")}

GUIDE = """You estimate the values that infrastructure-as-code files (Terraform, CloudFormation) do not give, so that
their resources can be priced. You get the files and a numbered list of gaps: each is one value of one resource
(or the region of all of them) that the files leave open, for example a variable with no default.

For each gap you can estimate, give:
- gap: its number
- value: the value. region: an AWS region code (us-east-1). quantity: a whole number of resources. instance_type: an
  AWS instance type of the right family for the resource (db.* for RDS, cache.* for ElastiCache). storage_gb,
  vcpu, memory_gb: a number.
- reason: one short line from the files' evidence (names, tags, comments, other resources, defaults elsewhere).
- confidence: low, medium or high. high only when the files point at the value (a comment, a similar resource, a
  name such as "prod-us-west-2"); a common default with no evidence is low.

Leave out a gap when the files give no basis at all for a value. Never invent a gap."""

SCHEMA = {
    "type": "object",
    "properties": {"estimates": {"type": "array", "items": {
        "type": "object",
        "properties": {"gap": {"type": "integer"}, "value": {"type": ["string", "number"]},
                       "reason": {"type": "string"}, "confidence": {"type": "string", "enum": list(CONFIDENCE)}},
        "required": ["gap", "value", "reason", "confidence"]}}},
    "required": ["estimates"],
}


def open_gaps(gaps: list[dict] | None) -> list[dict]:
    """The gaps an estimate may fill."""
    return [g for g in gaps or [] if g.get("field") in FIELDS]


def ask(llm: llm_mod.LLM, doc: Document, gaps: list[dict]) -> list[dict]:
    """One call: the LLM's answers ({gap, value, reason, confidence}) for the numbered gaps."""
    files = "\n\n".join(f"--- {path}\n" + "\n".join(line for _, line in lines)
                        for path, lines in iac.files_of(doc).items())
    listed = "\n".join(f"{i}. {g['name']} ({g['src']}): {g['field']}: {g['message']}" for i, g in enumerate(gaps, 1))
    user = f"FILES:\n{llm_mod.redact(files)[:MAX_TEXT]}\n\nGAPS:\n{listed}\n\nEstimate the gaps you can."
    out = llm_mod._ask(llm, GUIDE, user, SCHEMA, max_tokens=4000)
    return [a for a in out.get("estimates") or [] if isinstance(a, dict)]


def known_values(catalog) -> tuple[set[str], set[str]] | None:
    """The catalog's AWS regions and instance types, or None (no catalog: the values are checked by their shape)."""
    if catalog is None:
        return None
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    try:
        with catalog.engine.connect() as conn:
            rows = conn.execute(text("SELECT DISTINCT region, instance_type FROM price_product WHERE provider = 'aws'"
                                     )).all()
    except SQLAlchemyError:
        return None
    regions = {r for r, _ in rows if r}
    types = {t for _, t in rows if t}
    return (regions, types) if regions and types else None


def check(answers: list[dict], gaps: list[dict], res: iac.Result, known: tuple[set[str], set[str]] | None
          ) -> list[dict]:
    """Each answer with its gap, and used (True) or the reason it is not used (why)."""
    services = {it.name: iac.SERVICES.get(it.rtype, (None, ""))[0] for it in res.items}
    out, seen = [], set()
    for a in answers:
        n = a.get("gap")
        if not isinstance(n, int) or not 1 <= n <= len(gaps) or n in seen:
            continue
        seen.add(n)
        g = gaps[n - 1]
        conf = a.get("confidence") if a.get("confidence") in CONFIDENCE else "low"
        e = {"name": g["name"], "field": g["field"], "src": g["src"], "value": a.get("value"),
             "reason": str(a.get("reason") or "")[:300], "confidence": conf}
        why = _why_not(g["field"], a.get("value"), services.get(g["name"]), known)
        if why is None:
            e["value"] = _typed(g["field"], a.get("value"))
        out.append(dict(e, used=why is None, **({"why": why} if why else {})))
    return out


def _typed(fld: str, v):
    if fld == "quantity":
        return int(float(v))
    if fld in LIMITS:
        n = float(v)
        return int(n) if n == int(n) else n
    return str(v).strip()


def _why_not(fld: str, v, service: str | None, known) -> str | None:
    """Why a value cannot be used, or None."""
    if fld in LIMITS:
        try:
            n = float(v)
        except (TypeError, ValueError):
            return "not a number"
        lo, hi = LIMITS[fld]
        if not lo <= n <= hi or (fld == "quantity" and n != int(n)):
            return f"out of range ({lo:g} to {hi:g}{', a whole number' if fld == 'quantity' else ''})"
        return None
    if not isinstance(v, str) or not v.strip():
        return "no value"
    v = v.strip()
    if fld == "region":
        ok = v in known[0] if known else bool(REGION.match(v))
        return None if ok else "not an AWS region the catalog knows"
    if fld == "instance_type":
        if known and v not in known[1] or not known and not INSTANCE.match(v):
            return "not an instance type the catalog knows"
        fits = FAMILY.get(service or "", lambda t: not t.startswith(("db.", "cache.")) and not t.endswith(".search"))
        return None if fits(v) else f"not an instance type for {service or 'this resource'}"
    return "not a field billparse estimates"


def apply(res: iac.Result, used: list[dict]) -> None:
    """Put the used values into the inventory (the region, an item's count or size), each item keeping what the LLM
    gave it (iac.ESTIMATES, JSON). The region goes to every item, as the files' region would."""
    region = next((e for e in used if e["field"] == "region"), None)
    if region and not res.region:
        res.region = region["value"]
    by_name: dict[str, dict] = {}
    for e in used:
        if e["field"] not in ("region", "quantity"):
            by_name.setdefault(e["name"], {})[e["field"]] = e
    counts = {e["name"]: e for e in used if e["field"] == "quantity"}
    for it in res.items:
        mine = {f: {k: e[k] for k in ("value", "reason", "confidence")} for f, e in by_name.get(it.name, {}).items()}
        for fld, e in mine.items():
            it.attrs[fld] = e["value"]
        if (q := _count(it, counts)) is not None:
            mine["quantity"] = q
        if region and res.region == region["value"]:
            mine["region"] = {k: region[k] for k in ("value", "reason", "confidence")}
        if mine:
            it.attrs[iac.ESTIMATES] = json.dumps(mine)


def _count(it: iac.Item, counts: dict[str, dict]) -> dict | None:
    """An item's count with the LLM's counts in it: its own, its resource's (a volume) or a module's. The item's
    count is set when every factor is then known; returns the estimate to record, or None (no estimate used)."""
    factors = dict(it.factors or {it.name: it.count})
    hit = [n for n, v in factors.items() if v is None and n in counts]
    if not hit:
        return None
    for n in hit:
        factors[n] = counts[n]["value"]
    if any(v is None for v in factors.values()):
        return None                                  # another factor is still not known: no count
    it.count = math.prod(factors.values())
    rank = list(CONFIDENCE)
    said = [counts[n]["reason"] if n == it.name else f"{n} x {counts[n]['value']}: {counts[n]['reason']}" for n in hit]
    return {"value": it.count, "reason": "; ".join(said),
            "confidence": min((counts[n]["confidence"] for n in hit), key=rank.index)}


def estimate(llm: llm_mod.LLM, doc: Document, catalog) -> tuple[iac.Result, list[dict]] | None:
    """The inventory read again with the LLM's checked values in it, and every answer (used or not). None when no
    gap can be estimated (no call is made)."""
    res = iac.read(doc)
    gaps = open_gaps(res.gaps)
    if not gaps:
        return None
    answers = check(ask(llm, doc, gaps), gaps, res, known_values(catalog))
    apply(res, [e for e in answers if e["used"]])
    return res, answers
