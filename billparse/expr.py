"""Tiny safe expression language for spec "derive" entries.

Allowed: field names, numbers, + - * /, unary minus, and the functions below.
Parsed with ast and walked by hand. Nothing is ever passed to eval().

    "billed_cost + credits"
    "coalesce(billed_cost, quantity * unit_price)"
    "round(quantity * unit_price - billed_cost, 2)"
"""

from __future__ import annotations

import ast

import numpy as np
import pandas as pd


def _coalesce(*args):
    out = args[0]
    for a in args[1:]:
        out = out.where(out.notna(), a) if isinstance(out, pd.Series) else (a if out is None else out)
    return out


FUNCS = {
    "coalesce": _coalesce,
    "abs": lambda x: x.abs() if isinstance(x, pd.Series) else abs(x),
    "round": lambda x, n=2: x.round(int(n)) if isinstance(x, pd.Series) else round(x, int(n)),
    "fillna": lambda x, v: x.fillna(v) if isinstance(x, pd.Series) else (v if x is None else x),
    "neg": lambda x: -x,
}
_BINOPS = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide}


class ExprError(ValueError):
    pass


def evaluate(expr: str, df: pd.DataFrame) -> pd.Series:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"Bad expression {expr!r}: {e}") from e
    out = _eval(tree.body, df)
    if not isinstance(out, pd.Series):
        out = pd.Series([out] * len(df), index=df.index)
    return out


def _eval(node: ast.AST, df: pd.DataFrame):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in df.columns:
            return pd.Series([np.nan] * len(df), index=df.index, dtype=float)
        col = df[node.id]
        return pd.to_numeric(col, errors="coerce") if col.dtype == object else col
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        a, b = _eval(node.left, df), _eval(node.right, df)
        with np.errstate(divide="ignore", invalid="ignore"):
            return _BINOPS[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand, df)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS:
        if node.keywords:
            raise ExprError("Keyword arguments are not allowed")
        return FUNCS[node.func.id](*[_eval(a, df) for a in node.args])
    raise ExprError(f"Not allowed in expressions: {ast.dump(node)[:80]}")


def validate(expr: str) -> None:
    """Raise if the expression uses anything outside the whitelist (used on LLM specs)."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        ok = isinstance(node, (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp,
                               ast.UnaryOp, ast.USub, ast.Call, *_BINOPS))
        if not ok:
            raise ExprError(f"Not allowed: {type(node).__name__}")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in FUNCS):
            raise ExprError("Unknown function")
