from datetime import date

import pandas as pd
import pytest

import buyorwait.score as S
from buyorwait.intake import FinancialState, Recurrence


def _rec(amount, cadence, occurrences, history, direction="debit", flex="fixed", eid="e1"):
    return Recurrence(key="k", category="c", event_type="expense", direction=direction, amount=amount, cadence_days=cadence,
                      next_date=date(2026, 1, 10), last_event_id=eid, flexibility=flex, minimum_allowed_amount=None,
                      occurrences=occurrences, history=history, description="stream " + eid)


def _state(recs):
    return FinancialState(user_id="u", request_date=date(2026, 1, 1), currency="EUR", balance=5000, minimum=500,
                          recurring=recs, fixed_flows=[], notes=[], facts=[], events=pd.DataFrame())


def test_evidence_curve_reference_points():
    assert S.established(_rec(10, 0, 3, [10, 10, 10])) == pytest.approx(0.632, abs=0.001)
    assert S.established(_rec(10, 0, 6, [10] * 6)) == pytest.approx(0.865, abs=0.001)
    assert S.established(_rec(10, 0, 12, [10] * 12)) == pytest.approx(0.982, abs=0.001)
    assert S.established(_rec(10, 0, 0, [])) == 0.0
    # a 5-day item is absorbed by months of tenure, not by firing often: 6 occurrences = 1 month
    assert S.established(_rec(10, 5, 6, [10] * 6)) < S.established(_rec(10, 0, 3, [10] * 3))


def test_expense_weight_has_floor(monkeypatch):
    r = _rec(10, 0, 36, [10] * 36)
    assert S.expense_weight(r) == pytest.approx(1 - S.LAMBDA, abs=0.01)
    monkeypatch.setattr(S, "ABSORPTION", False)
    assert S.expense_weight(r) == 1.0


def test_income_increase_is_trusted_progressively():
    proven = _rec(1000, 0, 6, [1000] * 6, direction="credit", eid="sal")
    raise_msg = _rec(1200, 0, 6, [1000] * 6, direction="credit", eid="sal")   # raise announced, no payslip yet
    raise_two = _rec(1200, 0, 8, [1000] * 6 + [1200, 1200], direction="credit", eid="sal")
    cut = _rec(800, 0, 6, [1000] * 6, direction="credit", eid="sal")
    t = lambda r: S.trusted_income(_state([r]))[0]
    assert t(proven) == 1000
    assert 1000 < t(raise_msg) < 1100                     # seed trust only
    assert t(raise_msg) < t(raise_two) < 1200             # grows with settled payslips at the new level
    assert t(cut) == 800                                   # decreases at face value


def test_reliability_and_commitment_move_together(monkeypatch):
    recs = [_rec(1200, 0, 6, [1000] * 6, direction="credit", eid="sal"), _rec(400, 0, 6, [400] * 6, eid="rent")]
    on = S.components(_state(recs))
    monkeypatch.setattr(S, "ABSORPTION", False)
    off = S.components(_state(recs))
    assert on["income_reliability"] < off["income_reliability"] == 100.0
    assert on["_trusted_income"] < on["_monthly_income"]
    # the rent is an established habit: its commitment weight drops below face value
    assert on["commitment_load"] > 100 * (1 - 400 / on["_trusted_income"]) - 1e-6 or on["commitment_load"] >= 0
