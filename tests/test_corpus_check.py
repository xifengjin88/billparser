"""The corpus baseline check: a right total with lines under the wrong account is a change, a better proof is better."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from corpus import compare, sums_of  # noqa: E402


def result(**kw) -> dict:
    base = {"status": "reconciled", "completeness": "reconciled", "rows": 4, "total": 10.0, "lines_key": "a",
            "coverage": {"region": 1.0}, "sums": {"account": {"111": 6.0, "222": 4.0}}}
    return {**base, **kw}


def test_the_same_total_under_other_accounts_is_a_change():
    out = compare(result(), result(sums={"account": {"111": 8.0, "222": 2.0}}))
    assert [k for k, _ in out] == ["changed"] and "by account: 2 key(s) moved" in out[0][1]


def test_a_stronger_proof_is_better_and_a_weaker_one_worse():
    assert compare(result(completeness="contradicted", status="needs_review"), result()) == \
        [("better", "completeness contradicted -> reconciled")]
    assert [k for k, _ in compare(result(), result(completeness="contradicted"))] == ["worse"]
    assert compare(result(), result()) == []
    assert [k for k, _ in compare(result(), result(lines_key="b"))] == ["changed"]      # other printed rows
    assert ("worse", "region filled 100% -> 50%") in compare(result(), result(coverage={"region": 0.5}))


def test_sums_by_kind_and_service():
    rows = pd.DataFrame({"service": ["EC2", "EC2", "RDS"], "charge_type": [None, "Tax", "Credit"],
                         "billed_cost": [10.0, 1.3, -2.0]})
    s = sums_of(rows)
    assert s["service"] == {"EC2": 11.3, "RDS": -2.0} and s["kind"] == {"usage": 10.0, "tax": 1.3, "credit": -2.0}
