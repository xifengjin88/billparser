"""Output schema. Every value read from a file keeps its proof (a Cite)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Generic, Literal, Optional, TypeVar, cast

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.9"            # 1.2: database_engine, deployment_option on charges; 1.3: confidence;
                                  # 1.4: typed list/contracted/effective cost, coverage, line_type_raw on charges,
                                  #      kind commitment_negation, cost_types and cost_basis on documents
                                  # 1.5: tenancy, license_model, resource_id, pricing_term, purchase_option,
                                  #      list_unit_price, contracted_unit_price on charges (read from export columns)
                                  # 1.6: the analysis gives commitment_eligible_usage (facts) and no savings
                                  #      estimate; analysis.commitment_opportunity is deprecated, removed in 1.8;
                                  #      commitment_term on charges; term, payment option and unused on commitments
                                  # 1.7: pre_installed_sw on charges (AWS product/preInstalledSw)
                                  # 1.8: price matching: price_match_level, price_missing, price_assumptions,
                                  #      implied_unit_price, price_ratio on charges; pricing on documents;
                                  #      analysis.commitment_opportunity removed
                                  # 1.9: catalog_unit_price on charges; unmatched reasons service_not_loaded and
                                  #      no_such_product in price_missing; price checks (validate.py)

T = TypeVar("T")

DocType = Literal["invoice", "statement", "cost_report", "estimate", "inventory", "memo", "other"]
# marketplace_usage: pay-as-you-go spend on an AWS/Azure/GCP Marketplace product
# marketplace_fee:   a Marketplace subscription, contract, renewal or upfront fee
# commitment_negation: a line that cancels the on-demand cost of usage a commitment covers (AWS SavingsPlanNegation);
#                      not a credit or a discount, and not usage
Kind = Literal["usage", "commitment_fee", "discount_or_credit", "tax", "support_fee", "marketplace_usage",
               "marketplace_fee", "commitment_negation", "other"]
Coverage = Literal["on_demand", "reserved", "savings_plan", "committed_use", "spot", "unknown"]
Basis = Literal["billed", "estimated", "priced"]
Origin = Literal["printed", "inferred"]
Completeness = Literal["reconciled", "consistent", "unchecked", "contradicted"]
GapKind = Literal[
    "unclaimed_amount", "unreconciled_total", "missing_match_field", "failed_window",
    "rejected_claim", "no_bill_table",
]

DOC_TYPES = list(DocType.__args__)
KINDS = list(Kind.__args__)


class Cite(BaseModel):
    """Where a value came from. PDFs/text use page+line; exports use table+row+column."""
    page: Optional[int] = None          # page / slide / sheet index (1-based)
    line: Optional[int] = None          # line number within the page (1-based)
    table: Optional[str] = None         # sheet name or table id
    row: Optional[int] = None           # 1-based row as a person sees it in the file
    column: Optional[str] = None        # column header as printed (or letter)
    path: Optional[str] = None          # JSON path / Terraform address
    evidence_id: Optional[str] = None   # internal id of the evidence unit
    text: Optional[str] = None          # the printed text, for review

    def label(self) -> str:
        parts = []
        if self.table: parts.append(self.table)
        if self.page is not None: parts.append(f"p{self.page}")
        if self.line is not None: parts.append(f"line {self.line}")
        if self.row is not None: parts.append(f"row {self.row}")
        if self.column: parts.append(f"col {self.column}")
        if self.path: parts.append(self.path)
        return " · ".join(parts) or "—"


class CitedValue(BaseModel, Generic[T]):
    value: T
    cite: Cite
    origin: Origin = "printed"
    confidence: Optional[float] = None  # decision probability when a model or rule chose it
    source: Optional[str] = None        # rule | template | jev | claude | human | mapping


class Section(BaseModel):
    id: str
    name: Optional[CitedValue[str]] = None
    parent_id: Optional[str] = None
    counted: bool = True                # False when the whole section repeats money counted elsewhere


class StatedTotal(BaseModel):
    id: str
    label: CitedValue[str]
    amount: CitedValue[Decimal]
    level: Literal["grand", "section", "subtotal"] = "subtotal"
    section_id: Optional[str] = None
    computed: Optional[Decimal] = None  # sum of counted charges it covers
    covers: list[str] = Field(default_factory=list)
    status: Literal["match", "mismatch", "untested"] = "untested"


class Context(BaseModel):
    service: Optional[CitedValue[str]] = None
    region: Optional[CitedValue[str]] = None
    account_id: Optional[CitedValue[str]] = None


class Charge(BaseModel):
    id: str
    section_id: Optional[str] = None
    cite: Cite
    description: CitedValue[str]

    cost: Optional[CitedValue[Decimal]] = None          # the billed cost
    basis: Optional[Basis] = None
    list_cost: Optional[CitedValue[Decimal]] = None     # public on-demand price x quantity, no discounts
    contracted_cost: Optional[CitedValue[Decimal]] = None  # after negotiated discounts, before commitments
    effective_cost: Optional[CitedValue[Decimal]] = None   # after all discounts, commitments amortized
    coverage: Optional[CitedValue[str]] = None          # a Coverage value; None on lines that are not usage
    line_type_raw: Optional[CitedValue[str]] = None     # the provider's own line type
    currency: Optional[CitedValue[str]] = None
    counted: bool = True

    kind: CitedValue[str]
    context: Context = Field(default_factory=Context)
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
    database_engine: Optional[CitedValue[str]] = None      # RDS / ElastiCache: MySQL, PostgreSQL, Redis, ...
    deployment_option: Optional[CitedValue[str]] = None    # RDS: Single-AZ, Multi-AZ
    usage_type: Optional[CitedValue[str]] = None
    operation: Optional[CitedValue[str]] = None
    quantity: Optional[CitedValue[Decimal]] = None
    unit: Optional[CitedValue[str]] = None
    unit_price: Optional[CitedValue[Decimal]] = None
    sku_id: Optional[CitedValue[str]] = None
    catalog_sku_id: Optional[CitedValue[str]] = None
    sku_price_id: Optional[CitedValue[str]] = None
    meter_id: Optional[CitedValue[str]] = None
    tenancy: Optional[CitedValue[str]] = None              # Shared, Dedicated, Host
    license_model: Optional[CitedValue[str]] = None
    pre_installed_sw: Optional[CitedValue[str]] = None    # AWS: NA, SQL Std, SQL Web, SQL Ent
    # price matching (pricing.py): inferred, source price_catalog
    price_match_level: Optional[CitedValue[str]] = None    # exact, attributes, assumed, ambiguous, unmatched, ...
    price_missing: Optional[CitedValue[str]] = None        # the key columns that would have matched it
    price_assumptions: Optional[CitedValue[str]] = None    # "tenancy=Shared (text); operating_system=Linux (request)"
    implied_unit_price: Optional[CitedValue[Decimal]] = None   # effective (else billed) cost / quantity
    price_ratio: Optional[CitedValue[Decimal]] = None          # implied unit price / list unit price
    catalog_unit_price: Optional[CitedValue[Decimal]] = None   # the catalog's list price in the line's unit (1.9)
    resource_id: Optional[CitedValue[str]] = None
    pricing_term: Optional[CitedValue[str]] = None         # OnDemand, Reserved (as printed)
    purchase_option: Optional[CitedValue[str]] = None      # All Upfront, Partial Upfront, No Upfront
    commitment_term: Optional[CitedValue[str]] = None      # 1yr, 3yr, 12 months (as printed)
    list_unit_price: Optional[CitedValue[Decimal]] = None  # one unit at the public list price
    contracted_unit_price: Optional[CitedValue[Decimal]] = None  # one unit after negotiated discounts
    attributes: dict[str, Any] = Field(default_factory=dict)


class Commitment(BaseModel):
    id: str
    commitment_type: CitedValue[str]            # savings_plan | reserved_instance | ...
    commitment_id: Optional[CitedValue[str]] = None
    status: Optional[CitedValue[str]] = None
    term: Optional[CitedValue[str]] = None
    payment_option: Optional[CitedValue[str]] = None
    amount: Optional[CitedValue[Decimal]] = None
    unused: Optional[CitedValue[Decimal]] = None       # the commitment paid for but not used, from its lines
    charge_ids: list[str] = Field(default_factory=list)
    cite: Cite


class StatedFact(BaseModel):
    id: str
    label: str
    amount: CitedValue[Decimal]
    currency: Optional[str] = None
    period: Optional[str] = None
    cite: Cite


class Gap(BaseModel):
    kind: GapKind
    message: str
    cite: Optional[Cite] = None
    amount: Optional[Decimal] = None
    field: Optional[str] = None
    currency: Optional[str] = None
    candidates: list[str] = Field(default_factory=list)
    ref: Optional[str] = None           # id of the charge / total / evidence this is about


class Confidence(BaseModel):
    """How far the parsed data can be relied on, and why. high: the charges add up to the printed totals.
    medium: the file prints no total, and every amount in it was read. low: something was not read or does not tie
    (each reason says what)."""
    level: Literal["high", "medium", "low"]
    reasons: list[str] = Field(default_factory=list)


class PricingLevel(BaseModel):
    lines: int
    cost: float                     # |billed cost|


class PricingMissing(BaseModel):
    field: str
    lines: int
    cost: float


class InferredDiscount(BaseModel):
    value: float                    # 1 - the steady ratio of billed to list price (rule R2)
    estimate: bool = True
    note: str = ""


class Pricing(BaseModel):
    """How the usage lines matched the public list prices (pricing.py)."""
    catalog_version: Optional[str] = None
    defaults: dict[str, str] = Field(default_factory=dict)          # what the request said to assume
    undated_as_of: Optional[str] = None     # the date of lines without one: kept, so a reprice uses it again
    match_rate: dict[str, Optional[float]] = Field(default_factory=dict)   # sure, assumed (by cost); *_lines
    levels: dict[str, PricingLevel] = Field(default_factory=dict)
    missing: list[PricingMissing] = Field(default_factory=list)
    inferred_discount: Optional[InferredDiscount] = None
    warnings: list[str] = Field(default_factory=list)


class Document(BaseModel):
    schema_version: str = SCHEMA_VERSION
    source_name: str
    sha256: str
    page_count: int = 1

    doc_type: Optional[CitedValue[str]] = None
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

    completeness: Completeness = "unchecked"
    gaps: list[Gap] = Field(default_factory=list)
    cost_types: list[str] = Field(default_factory=list)   # billed, list, contracted, effective: what the file gives
    #                                                       for every usage line
    cost_basis: Optional[str] = None                     # actual | amortized | unknown (Azure exports); else None
    confidence: Optional[Confidence] = None
    pricing: Optional[Pricing] = None                    # the list price match of the usage lines (1.8)

    def counted_total(self) -> Decimal:
        return sum((c.cost.value for c in self.charges if c.counted and c.cost), Decimal(0))


def cv(value, cite: Cite, origin="printed", confidence=None, source=None) -> CitedValue:
    """Shorthand constructor."""
    return CitedValue(value=value, cite=cite, origin=cast(Origin, origin), confidence=confidence, source=source)
