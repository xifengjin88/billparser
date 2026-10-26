"""Tiny safe expression language for spec "derive" entries.

Allowed: field names, numbers, + - * /, unary minus, and the functions below.
Parsed with ast and walked by hand. Nothing is ever passed to eval().

    "billed_cost + credits"
    "coalesce(billed_cost, quantity * unit_price)"
    "round(quantity * unit_price - billed_cost, 2)"
"""

from __future__ import annotations

import ast
import re

import numpy as np
import pandas as pd


def _coalesce(*args):
    out = args[0]
    for a in args[1:]:
        out = out.where(out.notna(), a) if isinstance(out, pd.Series) else (a if out is None else out)
    return out


def _missing(n: int, index) -> pd.Series:
    out = pd.Series([np.nan] * n, index=index, dtype=float)
    out.attrs["missing"] = True                      # the column is not in the file at all (not just empty)
    return out


# CUR line types whose effective cost is already spread into the usage they cover (rule E1: their effective cost is 0)
_SPREAD = {"savingsplannegation", "savingsplanrecurringfee", "savingsplanupfrontfee", "rifee"}


def _cur_effective(line_type: pd.Series, billed: pd.Series, ri: pd.Series, sp: pd.Series,
                   ri_unused_upfront: pd.Series | None = None, ri_unused_recurring: pd.Series | None = None,
                   sp_total: pd.Series | None = None, sp_used: pd.Series | None = None) -> pd.Series:
    """AWS CUR effective (amortized) cost by line type: DiscountedUsage -> the reservation effective cost,
    SavingsPlanCoveredUsage -> the Savings Plan effective cost, negation and upfront fee lines -> 0 (their cost is
    spread into the usage they cover), every other line -> its unblended cost. A file with neither effective-cost
    column gives no effective cost at all: without them the amortized cost cannot be known.

    The recurring fee lines carry the commitment that was not used (AWS, "Understanding your amortized costs"):
    RIFee -> unused amortized upfront fee + unused recurring fee; SavingsPlanRecurringFee -> total commitment to date
    - used commitment. When the file has no such columns, those lines have no effective cost: how much of the
    commitment was unused cannot be told (0 would read as "nothing wasted")."""
    if ri.attrs.get("missing") and sp.attrs.get("missing"):
        return _missing(len(billed), billed.index)
    kind = line_type.map(lambda v: re.sub(r"[\s_-]+", "", str(v).lower()) if isinstance(v, str) else "")
    ri, sp = ri.reindex(billed.index), sp.reindex(billed.index)
    out = billed.astype(float).copy()
    out[kind.eq("discountedusage")] = ri[kind.eq("discountedusage")]
    out[kind.eq("savingsplancoveredusage")] = sp[kind.eq("savingsplancoveredusage")]
    spread = kind.isin(_SPREAD)
    out[spread] = 0.0

    def given(*cols) -> bool:
        return all(isinstance(c, pd.Series) and not c.attrs.get("missing") for c in cols)
    fee = kind.eq("rifee")
    if given(ri_unused_upfront, ri_unused_recurring):
        unused = ri_unused_upfront.reindex(billed.index).fillna(0) + ri_unused_recurring.reindex(billed.index).fillna(0)
        out[fee] = unused[fee]
    else:
        out[fee] = np.nan
    fee = kind.eq("savingsplanrecurringfee")
    if given(sp_total, sp_used):
        unused = sp_total.reindex(billed.index) - sp_used.reindex(billed.index).fillna(0)
        out[fee] = unused[fee]
    else:
        out[fee] = np.nan
    out.attrs["how"] = pd.Series(np.where(spread, "derived", "printed"), index=billed.index)
    return out


TEXT_FIRST = {"cur_effective"}                      # functions whose first argument is a text column, read as is

FUNCS = {
    "cur_effective": _cur_effective,
    "coalesce": _coalesce,
    "abs": lambda x: x.abs() if isinstance(x, pd.Series) else abs(x),
    "round": lambda x, n=2: x.round(int(n)) if isinstance(x, pd.Series) else round(x, int(n)),
    "fillna": lambda x, v: x.fillna(v) if isinstance(x, pd.Series) else (v if x is None else x),
    "neg": lambda x: -x,
}
_BINOPS = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide}


class ExprError(ValueError):
    pass


def evaluate(expr: str, df: pd.DataFrame, source: pd.DataFrame | None = None) -> pd.Series:
    """source: the file's own columns, for col("header") (rows in the same order as df)."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"Bad expression {expr!r}: {e}") from e
    out = _eval(tree.body, df, source)
    if not isinstance(out, pd.Series):
        out = pd.Series([out] * len(df), index=df.index)
    return out


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(h).lower())


def _source_col(name: str, df: pd.DataFrame, source: pd.DataFrame | None) -> pd.Series:
    if source is not None:
        hit = next((c for c in source.columns if c == name), None) or \
            next((c for c in source.columns if _norm(c) == _norm(name)), None)
        if hit is not None:
            return pd.to_numeric(source[hit], errors="coerce").reindex(df.index)
    return _missing(len(df), df.index)


def _eval(node: ast.AST, df: pd.DataFrame, source: pd.DataFrame | None = None):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in df.columns:
            return _missing(len(df), df.index)
        col = df[node.id]
        return pd.to_numeric(col, errors="coerce") if col.dtype == object else col
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "col":
        if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
            raise ExprError('col() takes one column name in quotes: col("PayGPrice")')
        return _source_col(node.args[0].value, df, source)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        a, b = _eval(node.left, df, source), _eval(node.right, df, source)
        with np.errstate(divide="ignore", invalid="ignore"):
            return _BINOPS[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand, df, source)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS:
        if node.keywords:
            raise ExprError("Keyword arguments are not allowed")
        args = [_eval(a, df, source) for a in node.args]
        if node.func.id in TEXT_FIRST and node.args and isinstance(node.args[0], ast.Name):
            first = node.args[0].id
            args[0] = df[first] if first in df.columns else pd.Series([None] * len(df), index=df.index, dtype=object)
        return FUNCS[node.func.id](*args)
    raise ExprError(f"Not allowed in expressions: {ast.dump(node)[:80]}")


def validate(expr: str) -> None:
    """Raise if the expression uses anything outside the whitelist (used on LLM specs)."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        ok = isinstance(node, (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp,
                               ast.UnaryOp, ast.USub, ast.Call, *_BINOPS))
        if not ok:
            raise ExprError(f"Not allowed: {type(node).__name__}")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in (*FUNCS, "col")):
            raise ExprError("Unknown function")
