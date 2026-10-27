"""The output contract: exactly the schema agreed on, validated strictly (extra fields forbidden).

The pipeline works on richer internal models (confidence, decision source, reconciliation
detail, service candidates). `to_schema()` projects them onto this contract; everything
else goes into the review sidecar returned by `sidecar()`.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from . import schema as internal

T = TypeVar("T")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Cite(_Strict):
    page: Optional[int] = None      # PDF / text / slide: page + line
    line: Optional[int] = None
    table: Optional[str] = None     # exports: table + row + column
    row: Optional[int] = None
    column: Optional[str] = None


class CitedValue(_Strict, Generic[T]):
    value: T
    cite: Cite
    origin: Literal["printed", "inferred"]


class Section(_Strict):
    id: str
    name: Optional[CitedValue[str]] = None
    parent_id: Optional[str] = None


class StatedTotal(_Strict):
    id: str
    label: CitedValue[str]
    amount: CitedValue[Decimal]
    level: Literal["grand", "section", "subtotal"]
    section_id: Optional[str] = None


class Context(_Strict):
    service: Optional[CitedValue[str]] = None
    region: Optional[CitedValue[str]] = None
    account_id: Optional[CitedValue[str]] = None


class Charge(_Strict):
    id: str
    section_id: Optional[str] = None
    cite: Cite
    description: CitedValue[str]
    cost: Optional[CitedValue[Decimal]] = None
    basis: Optional[Literal["billed", "estimated", "priced"]] = None
    currency: Optional[CitedValue[str]] = None
    counted: bool
    kind: Literal["usage", "commitment_fee", "discount_or_credit", "tax", "support_fee", "marketplace_usage",
                  "marketplace_fee", "commitment_negation", "other"]
    list_cost: Optional[CitedValue[Decimal]] = None
    contracted_cost: Optional[CitedValue[Decimal]] = None
    effective_cost: Optional[CitedValue[Decimal]] = None
    coverage: Optional[CitedValue[Literal["on_demand", "reserved", "savings_plan", "committed_use", "spot",
                                          "unknown"]]] = None
    line_type_raw: Optional[CitedValue[str]] = None
    context: Context
    service_code: Optional[CitedValue[str]] = None
    pricing_model: Optional[CitedValue[str]] = None
    commitment_id: Optional[CitedValue[str]] = None
    commitment_type: Optional[CitedValue[str]] = None
    commitment_status: Optional[CitedValue[str]] = None
    marketplace_seller: Optional[CitedValue[str]] = None
    period_start: Optional[CitedValue[str]] = None
    period_end: Optional[CitedValue[str]] = None
    instance_type: Optional[CitedValue[str]] = None
    operating_system: Optional[CitedValue[str]] = None
    database_engine: Optional[CitedValue[str]] = None
    deployment_option: Optional[CitedValue[str]] = None
    usage_type: Optional[CitedValue[str]] = None
    operation: Optional[CitedValue[str]] = None
    quantity: Optional[CitedValue[Decimal]] = None
    unit: Optional[CitedValue[str]] = None
    unit_price: Optional[CitedValue[Decimal]] = None
    sku_id: Optional[CitedValue[str]] = None
    catalog_sku_id: Optional[CitedValue[str]] = None
    sku_price_id: Optional[CitedValue[str]] = None
    meter_id: Optional[CitedValue[str]] = None
    tenancy: Optional[CitedValue[str]] = None
    license_model: Optional[CitedValue[str]] = None
    resource_id: Optional[CitedValue[str]] = None                # FOCUS ResourceId
    pricing_term: Optional[CitedValue[str]] = None
    purchase_option: Optional[CitedValue[str]] = None
    list_unit_price: Optional[CitedValue[Decimal]] = None        # FOCUS ListUnitPrice
    contracted_unit_price: Optional[CitedValue[Decimal]] = None  # FOCUS ContractedUnitPrice
    commitment_term: Optional[CitedValue[str]] = None
    pre_installed_sw: Optional[CitedValue[str]] = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Commitment(_Strict):
    id: str
    commitment_type: CitedValue[str]
    commitment_id: Optional[CitedValue[str]] = None
    status: Optional[CitedValue[str]] = None
    term: Optional[CitedValue[str]] = None
    payment_option: Optional[CitedValue[str]] = None
    amount: Optional[CitedValue[Decimal]] = None
    unused: Optional[CitedValue[Decimal]] = None
    charge_ids: list[str] = Field(default_factory=list)
    cite: Cite


class StatedFact(_Strict):
    label: str
    amount: CitedValue[Decimal]
    currency: Optional[str] = None
    period: Optional[str] = None
    cite: Cite


class Gap(_Strict):
    kind: Literal["unclaimed_amount", "unreconciled_total", "missing_match_field", "failed_window",
                  "rejected_claim", "no_bill_table"]
    message: str
    cite: Optional[Cite] = None
    amount: Optional[Decimal] = None
    field: Optional[str] = None
    currency: Optional[str] = None


class Confidence(_Strict):
    level: Literal["high", "medium", "low"]    # high: totals prove it; medium: no total, every amount read; low: see reasons
    reasons: list[str] = Field(default_factory=list)


class Document(_Strict):
    schema_version: str
    source_name: str
    sha256: str
    page_count: int
    doc_type: Optional[CitedValue[Literal["invoice", "statement", "cost_report", "estimate", "inventory", "memo", "other"]]] = None
    provider: Optional[CitedValue[str]] = None
    currency: Optional[CitedValue[str]] = None
    exchange_rate: Optional[CitedValue[Decimal]] = None
    billing_period_start: Optional[CitedValue[str]] = None
    billing_period_end: Optional[CitedValue[str]] = None
    stated_totals: list[StatedTotal] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    charges: list[Charge] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    stated_facts: list[StatedFact] = Field(default_factory=list)
    completeness: Literal["reconciled", "consistent", "unchecked", "contradicted"]
    gaps: list[Gap] = Field(default_factory=list)
    confidence: Optional[Confidence] = None
    cost_types: list[Literal["billed", "list", "contracted", "effective"]] = Field(default_factory=list)
    cost_basis: Optional[Literal["actual", "amortized", "unknown"]] = None


# ---------------------------------------------------------------- projection

def _cite(c: Optional[internal.Cite]) -> Optional[dict]:
    if c is None:
        return None
    d = {"page": c.page, "line": c.line, "table": c.table, "row": c.row, "column": c.column}
    if c.path and not (c.table or c.line):
        d["table"] = c.path            # JSON / Terraform address when nothing else locates it
    return d


def _cv(v: Optional[internal.CitedValue]) -> Optional[dict]:
    if v is None:
        return None
    return {"value": v.value, "cite": _cite(v.cite) or {}, "origin": v.origin}


def _jsonable(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def to_schema(doc: internal.Document) -> Document:
    charges = []
    for c in doc.charges:
        charges.append(dict(
            id=c.id, section_id=c.section_id, cite=_cite(c.cite), description=_cv(c.description), cost=_cv(c.cost),
            basis=c.basis, currency=_cv(c.currency), counted=c.counted, kind=c.kind.value,
            context=dict(service=_cv(c.context.service), region=_cv(c.context.region), account_id=_cv(c.context.account_id)),
            **{k: _cv(getattr(c, k)) for k in (
                "service_code", "pricing_model", "commitment_id", "commitment_type", "commitment_status",
                "marketplace_seller", "period_start", "period_end", "instance_type", "operating_system", "database_engine", "deployment_option", "usage_type",
                "operation", "quantity", "unit", "unit_price", "sku_id", "catalog_sku_id", "sku_price_id", "meter_id",
                "list_cost", "contracted_cost", "effective_cost", "coverage", "line_type_raw", "tenancy", "license_model",
                "resource_id", "pricing_term", "purchase_option", "list_unit_price", "contracted_unit_price",
                "commitment_term", "pre_installed_sw")},
            attributes=_jsonable(c.attributes),
        ))
    out = dict(
        schema_version=doc.schema_version, source_name=doc.source_name, sha256=doc.sha256, page_count=doc.page_count,
        doc_type=_cv(doc.doc_type), provider=_cv(doc.provider), currency=_cv(doc.currency),
        exchange_rate=_cv(doc.exchange_rate), billing_period_start=_cv(doc.billing_period_start),
        billing_period_end=_cv(doc.billing_period_end),
        stated_totals=[dict(id=t.id, label=_cv(t.label), amount=_cv(t.amount), level=t.level, section_id=t.section_id)
                       for t in doc.stated_totals],
        sections=[dict(id=s.id, name=_cv(s.name), parent_id=s.parent_id) for s in doc.sections],
        charges=charges,
        commitments=[dict(id=k.id, commitment_type=_cv(k.commitment_type), commitment_id=_cv(k.commitment_id),
                          status=_cv(k.status), term=_cv(k.term), payment_option=_cv(k.payment_option),
                          amount=_cv(k.amount), unused=_cv(k.unused), charge_ids=k.charge_ids, cite=_cite(k.cite))
                     for k in doc.commitments],
        stated_facts=[dict(label=f.label, amount=_cv(f.amount), currency=f.currency, period=f.period, cite=_cite(f.cite))
                      for f in doc.stated_facts],
        completeness=doc.completeness,
        gaps=[dict(kind=g.kind, message=g.message, cite=_cite(g.cite), amount=g.amount, field=g.field, currency=g.currency)
              for g in doc.gaps],
        confidence=doc.confidence.model_dump() if doc.confidence else None,
        cost_types=doc.cost_types, cost_basis=doc.cost_basis,
    )
    return Document.model_validate(out)


def sidecar(doc: internal.Document) -> dict:
    """What review needs beyond the contract: confidence, sources, reconciliation, candidates."""
    def conf(v):
        return None if v is None else {"confidence": v.confidence, "source": v.source}
    return {
        "doc_type": conf(doc.doc_type),
        "charges": {c.id: {"kind": conf(c.kind), "service_code": conf(c.service_code),
                           "cite_text": c.cite.text} for c in doc.charges},
        "totals": {t.id: {"status": t.status, "computed": str(t.computed) if t.computed is not None else None,
                          "covers": t.covers} for t in doc.stated_totals},
        "gaps": [{"ref": g.ref, "candidates": g.candidates} for g in doc.gaps],
    }


def json_schema() -> dict:
    return Document.model_json_schema()
