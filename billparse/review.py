"""Human review on the contract document (as invoice-parser's review.py): apply a person's corrections, check the
totals again, and teach the system.

Approving a document
  * stores the corrected document as gold (store part "gold") and its charges as the approved lines (the nightly
    loop learns from them),
  * saves corrected service codes as synonyms, so the same label resolves next time,
  * verifies the candidate spec that read it when no charge was changed.
"""

from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation

import pandas as pd

from . import contract
from .analyze import RESOLVER_LOCK, get_resolver
from .schema import Charge, Cite, Context, Document, cv
from .settings import catalog_dir
from .store import Store
from .transforms import parse_number

CHECKLIST = [
    ("totals", "Totals tie: every printed total matches the lines it covers (or the gap explains why not)."),
    ("unclaimed", "Every unclaimed amount is resolved: it is a charge, a total, or not money for this bill."),
    ("counted", "Summary or subtotal rows that repeat other lines are marked not counted."),
    ("header", "Document type, provider, currency and billing period are right."),
    ("spot", "Spot-checked three lines against the source: description, amount and service code match."),
]
NUMBER = re.compile(r"[-(]?[$€£¥]?\s?\d[\d,.' ]*\)?")
CHARGE_TYPE_OF_KIND = {"usage": "Usage", "commitment_fee": "Purchase", "discount_or_credit": "Credit", "tax": "Tax",
                       "support_fee": "Support", "marketplace_usage": "Usage", "marketplace_fee": "Purchase",
                       "commitment_negation": "Negation", "other": "Fee"}


def charge_rows(doc: Document, charges=None) -> list[dict]:
    """Editable rows for the review table; `charges` limits them to some of the document's charges."""
    rows = []
    for c in (doc.charges if charges is None else charges):
        rows.append({
            "id": c.id,
            "delete": False,
            "description": str(c.description.value),
            "cost": float(c.cost.value) if c.cost else None,
            "kind": c.kind.value,
            "counted": c.counted,
            "service": str(c.context.service.value) if c.context.service else "",
            "service_code": str(c.service_code.value) if c.service_code else "",
            "region": str(c.context.region.value) if c.context.region else "",
            "quantity": float(c.quantity.value) if c.quantity else None,
            "section": c.section_id or "",
            "source": c.cite.label(),
        })
    return rows


def _dec(v) -> Decimal | None:
    if v is None or v == "" or (isinstance(v, float) and v != v):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        n = parse_number(str(v))
        return None if n is None else Decimal(str(n))


def _origin_for(value, cite: Cite) -> str:
    """A corrected value stays 'printed' only if it is literally in the cited text."""
    txt = cite.text or ""
    if isinstance(value, Decimal):
        found = {Decimal(str(n)) for m in NUMBER.findall(txt) if (n := parse_number(m.strip())) is not None}
        return "printed" if value in found or -value in found else "inferred"
    return "printed" if str(value).strip() and str(value).strip().lower() in txt.lower() else "inferred"


def apply_edits(doc: Document, rows: list[dict]) -> tuple[Document, dict]:
    """Apply the edited charge table. Returns the new document and what changed (for learning)."""
    by_id = {c.id: c for c in doc.charges}
    changes: dict = {"deleted": [], "kind": {}, "service_code": {}, "counted": {}, "cost": {}, "added": []}
    kept: list[Charge] = []
    n_new = 0
    for r in rows:
        cid = r.get("id")
        if r.get("delete"):
            if cid in by_id:
                changes["deleted"].append(cid)
            continue
        c = by_id.get(cid) if cid is not None else None
        if c is None:
            n_new += 1
            cite = Cite(text="added in review")
            c = Charge(id=f"r{n_new}", cite=cite, description=cv(str(r.get("description") or ""), cite, "inferred", 1.0, "human"),
                       kind=cv(r.get("kind") or "usage", cite, "inferred", 1.0, "human"), context=Context(),
                       basis="billed")
            changes["added"].append(c.id)
        desc = str(r.get("description") or "")
        if desc != str(c.description.value):
            c.description = cv(desc, c.description.cite, _origin_for(desc, c.description.cite), 1.0, "human")
        cost = _dec(r.get("cost"))
        old = c.cost.value if c.cost else None
        if cost != old:
            if cost is None:
                c.cost, c.basis = None, None
            else:
                cite = c.cost.cite if c.cost else c.cite
                c.cost = cv(cost, cite, _origin_for(cost, cite), 1.0, "human")
                c.basis = c.basis or "billed"
            changes["cost"][c.id] = str(cost)
        kind = r.get("kind") or "usage"
        if kind != c.kind.value:
            c.kind = cv(kind, c.kind.cite, "inferred", 1.0, "human")
            changes["kind"][c.id] = kind
        counted = bool(r.get("counted", True))
        if counted != c.counted:
            c.counted = counted
            changes["counted"][c.id] = counted
        svc = str(r.get("service") or "").strip()
        if svc != (str(c.context.service.value) if c.context.service else ""):
            c.context.service = cv(svc, c.cite, _origin_for(svc, c.cite), 1.0, "human") if svc else None
        code = str(r.get("service_code") or "").strip()
        if code != (str(c.service_code.value) if c.service_code else ""):
            cite = c.context.service.cite if c.context.service else c.cite
            c.service_code = cv(code, cite, "inferred", 1.0, "human") if code else None
            if code:
                changes["service_code"][c.id] = code
        kept.append(c)
    doc.charges = kept
    # gaps that a person has now resolved
    resolved = set(changes["service_code"]) | set(changes["deleted"])
    doc.gaps = [g for g in doc.gaps if not (g.kind == "missing_match_field" and g.field == "service_code" and g.ref in resolved)
                and not (g.kind == "rejected_claim" and g.ref in resolved)]
    contract.rereconcile(doc, doc.completeness)
    return doc, changes


def drop_gaps(doc: Document, indexes: list[int]) -> Document:
    """A person marks unclaimed amounts / notes as resolved ('not money for this bill')."""
    drop = set(indexes)
    doc.gaps = [g for i, g in enumerate(doc.gaps) if i not in drop]
    return contract.rereconcile(doc, doc.completeness)


def rows_of(doc: Document) -> pd.DataFrame:
    """The document's counted charges as canonical lines (the approved lines the nightly loop learns from)."""
    out = []
    for c in doc.charges:
        if not c.counted:
            continue
        out.append({
            "description": str(c.description.value), "billed_cost": float(c.cost.value) if c.cost else None,
            "service_raw": str(c.context.service.value) if c.context.service else None,
            "service_code": str(c.service_code.value) if c.service_code else None,
            "region": str(c.context.region.value) if c.context.region else None,
            "account_id": str(c.context.account_id.value) if c.context.account_id else None,
            "charge_type": CHARGE_TYPE_OF_KIND.get(str(c.kind.value), "Usage"),
            "currency": str(c.currency.value) if c.currency else None,
            **{f: float(getattr(c, f).value) if getattr(c, f) else None
               for f in ("list_cost", "contracted_cost", "effective_cost")},
            "coverage": str(c.coverage.value) if c.coverage else None,
            "_src": c.cite.label(),
        })
    return pd.DataFrame(out)


def approve(store: Store, sha: str, doc: Document, changes: dict, note: str = "", by: str | None = None) -> dict:
    row = store.document(sha)
    if row is None:
        raise ValueError(f"No stored document {sha} to approve.")
    provider = str(doc.provider.value) if doc.provider else None
    # 1. synonyms from corrected service codes
    with RESOLVER_LOCK:
        res = get_resolver(catalog_dir())
        for cid, code in changes.get("service_code", {}).items():
            c = next((x for x in doc.charges if x.id == cid), None)
            key = res.key_for_code(code, provider) if c is not None else None
            if c is None or key is None:
                continue
            label = str(c.context.service.value) if c.context.service else str(c.description.value)
            res.add_synonym(label, key, provider=provider)
    # 2. gold: the corrected document, and its lines as the approved lines
    store.save_part(sha, "gold", doc.model_dump_json(exclude_none=True))
    store.save_approved(sha, rows_of(doc), {"completeness": doc.completeness, "changes": changes}, by, note)
    summary = dict(row.summary or {})
    summary["contract"] = {**(summary.get("contract") or {}), "completeness": doc.completeness,
                           "money_at_risk": float(contract.money_at_risk(doc))}
    store.update_document(sha, summary=summary, reviewed_at=time.time())
    # 3. a candidate spec that read the file with no charge changed is now verified
    promoted = None
    spec = summary.get("spec") or {}
    changed = changes["deleted"] or changes["added"] or changes["cost"] or changes["kind"] or changes["counted"]
    if spec.get("id") and not changed:
        current = store.get_spec(spec["id"])
        if current and current.get("trust") == "candidate":
            promoted = store.save_spec(dict(current, trust="verified"))["version"]
    return {"sha256": sha, "status": "approved", "spec_verified": promoted}
