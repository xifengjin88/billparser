"""Cost-type steps a spec names in meta.post, run after its columns and derives (spec.run).

azure_amortization: Azure cost details come as an actual-cost or an amortized-cost export with the same columns. Each
row's PricingModel says how it was priced, so its effective (amortized) cost is known row by row:

  OnDemand, Spot usage                        effective = cost, in either export (no commitment to spread)
  Reservation, SavingsPlan usage, Purchase,   effective = cost in an amortized export; empty in an actual export
  UnusedReservation, UnusedSavingsPlan        or when the export cannot be told

The export is told apart by its commitment rows (Microsoft Learn, "Pricing behavior in cost details"): in an actual
export, reservation and savings plan usage costs 0 and purchases appear as Purchase rows; in an amortized export,
that usage carries the prorated commitment cost and there are no purchase rows. A file with no PricingModel column
gets no effective cost.
"""

from __future__ import annotations

import pandas as pd

from .spec import HOW, Extraction

COMMITTED_MODELS = {"reservation", "reservations", "savingsplan"}
PLAIN_MODELS = {"ondemand", "spot"}


def _key(s: pd.Series) -> pd.Series:
    return s.map(lambda v: "".join(str(v).lower().split()) if isinstance(v, str) else "")


def azure_basis(rows: pd.DataFrame) -> str:
    """actual | amortized | unknown, from the commitment rows."""
    if "pricing_model" not in rows or "billed_cost" not in rows:
        return "unknown"
    model, kind = _key(rows["pricing_model"]), _key(rows.get("line_type_raw", pd.Series("", index=rows.index)))
    cost = pd.to_numeric(rows["billed_cost"], errors="coerce").fillna(0.0)
    qty = pd.to_numeric(rows.get("quantity", pd.Series(1.0, index=rows.index)), errors="coerce").fillna(0.0)
    committed_usage = model.isin(COMMITTED_MODELS) & kind.eq("usage") & qty.ne(0)
    actual = (committed_usage & cost.eq(0)).any() or (model.isin(COMMITTED_MODELS) & kind.eq("purchase")).any()
    amortized = (committed_usage & cost.ne(0)).any()
    if actual and not amortized:
        return "actual"
    if amortized and not actual:
        return "amortized"
    return "unknown"


def azure_amortization(ext: Extraction, known: str | None = None) -> None:
    """known: the export's basis told from other rows of the same file (the first rows of a large file), used when
    these rows cannot tell it (a chunk of a large file with no commitment usage)."""
    rows = ext.rows
    if rows.empty or "pricing_model" not in rows:
        return
    basis = azure_basis(rows)
    if basis == "unknown" and known in ("actual", "amortized"):
        basis = known
    model = _key(rows["pricing_model"])
    keep = model.isin(PLAIN_MODELS) | (model.isin(COMMITTED_MODELS) if basis == "amortized" else False)
    cost = pd.to_numeric(rows["billed_cost"], errors="coerce")
    rows["effective_cost"] = cost.where(keep)
    rows[HOW + "effective_cost"] = pd.Series("printed", index=rows.index).where(rows["effective_cost"].notna(), None)
    ext.fields["cost_basis"] = {"value": basis, "src": None}


POST = {"azure_amortization": azure_amortization}
