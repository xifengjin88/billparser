"""The output contract: exactly the schema agreed on, validated strictly (extra fields forbidden).

The pipeline works on richer internal models (confidence, decision source, reconciliation
detail, service candidates). `to_schema()` projects them onto this contract; everything
else goes into the review sidecar returned by `sidecar()`.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from . import schema as internal

T = TypeVar("T")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Cite(_Strict):
    page: int | None = None      # PDF / text / slide: page + line
    line: int | None = None
    table: str | None = None     # exports: table + row + column
    row: int | None = None
    column: str | None = None


class CitedValue(_Strict, Generic[T]):  # noqa: UP046  pydantic builds generic models from Generic[T]
    value: T
    cite: Cite
    origin: Literal["printed", "inferred"]


class Section(_Strict):
    id: str
    name: CitedValue[str] | None = None
    parent_id: str | None = None


class StatedTotal(_Strict):
    id: str
    label: CitedValue[str]
    amount: CitedValue[Decimal]
    level: Literal["grand", "section", "subtotal"]
    section_id: str | None = None


class Context(_Strict):
    service: CitedValue[str] | None = None
    region: CitedValue[str] | None = None
    account_id: CitedValue[str] | None = None


class Charge(_Strict):
    id: str
    section_id: str | None = None
    cite: Cite
    description: CitedValue[str]
    cost: CitedValue[Decimal] | None = None
    basis: Literal["billed", "estimated", "priced"] | None = None
    currency: CitedValue[str] | None = None
    counted: bool
    kind: Literal["usage", "commitment_fee", "discount_or_credit", "tax", "support_fee", "marketplace_usage",
                  "marketplace_fee", "commitment_negation", "other"]
    list_cost: CitedValue[Decimal] | None = None
    contracted_cost: CitedValue[Decimal] | None = None
    effective_cost: CitedValue[Decimal] | None = None
    coverage: CitedValue[Literal["on_demand", "reserved", "savings_plan", "committed_use", "spot", "unknown"]] | None = None
    line_type_raw: CitedValue[str] | None = None
    context: Context
    service_code: CitedValue[str] | None = None
    pricing_model: CitedValue[str] | None = None
    commitment_id: CitedValue[str] | None = None
    commitment_type: CitedValue[str] | None = None
    commitment_status: CitedValue[str] | None = None
    marketplace_seller: CitedValue[str] | None = None
    period_start: CitedValue[str] | None = None
    period_end: CitedValue[str] | None = None
    instance_type: CitedValue[str] | None = None
    operating_system: CitedValue[str] | None = None
    database_engine: CitedValue[str] | None = None
    deployment_option: CitedValue[str] | None = None
    usage_type: CitedValue[str] | None = None
    operation: CitedValue[str] | None = None
    quantity: CitedValue[Decimal] | None = None
    unit: CitedValue[str] | None = None
    unit_price: CitedValue[Decimal] | None = None
    sku_id: CitedValue[str] | None = None
    catalog_sku_id: CitedValue[str] | None = None
    sku_price_id: CitedValue[str] | None = None
    meter_id: CitedValue[str] | None = None
    tenancy: CitedValue[str] | None = None
    license_model: CitedValue[str] | None = None
    resource_id: CitedValue[str] | None = None                # FOCUS ResourceId
    pricing_term: CitedValue[str] | None = None
    purchase_option: CitedValue[str] | None = None
    list_unit_price: CitedValue[Decimal] | None = None        # FOCUS ListUnitPrice
    contracted_unit_price: CitedValue[Decimal] | None = None  # FOCUS ContractedUnitPrice
    commitment_term: CitedValue[str] | None = None
    pre_installed_sw: CitedValue[str] | None = None
    price_match_level: CitedValue[str] | None = None
    price_missing: CitedValue[str] | None = None
    price_assumptions: CitedValue[str] | None = None
    implied_unit_price: CitedValue[Decimal] | None = None
    price_ratio: CitedValue[Decimal] | None = None
    catalog_unit_price: CitedValue[Decimal] | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Commitment(_Strict):
    id: str
    commitment_type: CitedValue[str]
    commitment_id: CitedValue[str] | None = None
    status: CitedValue[str] | None = None
    term: CitedValue[str] | None = None
    payment_option: CitedValue[str] | None = None
    amount: CitedValue[Decimal] | None = None
    unused: CitedValue[Decimal] | None = None
    charge_ids: list[str] = Field(default_factory=list)
    cite: Cite


class StatedFact(_Strict):
    label: str
    amount: CitedValue[Decimal]
    currency: str | None = None
    period: str | None = None
    cite: Cite


class Gap(_Strict):
    kind: Literal["unclaimed_amount", "unreconciled_total", "missing_match_field", "failed_window",
                  "rejected_claim", "no_bill_table"]
    message: str
    cite: Cite | None = None
    amount: Decimal | None = None
    field: str | None = None
    currency: str | None = None


class Confidence(_Strict):
    level: Literal["high", "medium", "low"]    # high: totals prove it; medium: no total, every amount read; low: see reasons
    reasons: list[str] = Field(default_factory=list)


class PricingLevel(_Strict):
    lines: int
    cost: float


class PricingMissing(_Strict):
    field: str
    lines: int
    cost: float


class PricingDefault(_Strict):
    provider: str
    field: str
    value: str
    source: str
    lines: int
    cost: float


class InferredDiscount(_Strict):
    value: float
    estimate: bool = True
    note: str = ""


class Pricing(_Strict):
    catalog_version: str | None = None
    defaults: dict[str, str] = Field(default_factory=dict)
    defaults_used: list[PricingDefault] = Field(default_factory=list)
    undated_as_of: str | None = None
    match_rate: dict[str, float | None] = Field(default_factory=dict)
    levels: dict[str, PricingLevel] = Field(default_factory=dict)
    missing: list[PricingMissing] = Field(default_factory=list)
    inferred_discount: InferredDiscount | None = None
    warnings: list[str] = Field(default_factory=list)


class Document(_Strict):
    schema_version: str
    source_name: str
    sha256: str
    page_count: int
    doc_type: CitedValue[Literal["invoice", "statement", "cost_report", "estimate", "inventory", "memo", "other"]] | None = None
    provider: CitedValue[str] | None = None
    currency: CitedValue[str] | None = None
    exchange_rate: CitedValue[Decimal] | None = None
    billing_period_start: CitedValue[str] | None = None
    billing_period_end: CitedValue[str] | None = None
    stated_totals: list[StatedTotal] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    charges: list[Charge] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    stated_facts: list[StatedFact] = Field(default_factory=list)
    completeness: Literal["reconciled", "consistent", "unchecked", "contradicted"]
    gaps: list[Gap] = Field(default_factory=list)
    confidence: Confidence | None = None
    cost_types: list[Literal["billed", "list", "contracted", "effective"]] = Field(default_factory=list)
    cost_basis: Literal["actual", "amortized", "unknown"] | None = None
    pricing: Pricing | None = None


# ---------------------------------------------------------------- projection

def _cite(c: internal.Cite | None) -> dict | None:
    if c is None:
        return None
    d = {"page": c.page, "line": c.line, "table": c.table, "row": c.row, "column": c.column}
    if c.path and not (c.table or c.line):
        d["table"] = c.path            # JSON / Terraform address when nothing else locates it
    return d


def _cv(v: internal.CitedValue | None) -> dict | None:
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
                "commitment_term", "pre_installed_sw", "price_match_level", "price_missing", "price_assumptions",
                "implied_unit_price", "price_ratio", "catalog_unit_price")},
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
        pricing=doc.pricing.model_dump() if doc.pricing else None,
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
