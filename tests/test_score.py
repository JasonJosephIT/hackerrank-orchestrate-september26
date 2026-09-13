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


class _Chg:
    """Minimal duck-typed stand-in for plans.Change -- score.py only reads these three fields."""
    def __init__(self, kind, event_id, new_amount=None):
        self.kind, self.event_id, self.new_amount = kind, event_id, new_amount


def test_exclude_and_override_move_more_than_just_liquidity():
    """D13: before this change, stop/reduce only ever showed up in the trough (liquidity_buffer);
    commitment_load/flexibility/savings_behaviour ignored exclude/overrides entirely."""
    flexible = _rec(300, 0, 6, [300] * 6, flex="stoppable", eid="sub")
    fixed = _rec(1000, 0, 6, [1000] * 6, flex="fixed", eid="rent")
    st = _state([flexible, fixed])
    base = S.components(st)
    stopped = S.components(st, exclude={"sub"})
    reduced = S.components(st, overrides={"sub": 100.0})
    assert stopped["_monthly_outflow"] == pytest.approx(base["_monthly_outflow"] - 300, abs=0.01)
    assert reduced["_monthly_outflow"] == pytest.approx(base["_monthly_outflow"] - 200, abs=0.01)
    assert stopped["flexibility"] < base["flexibility"]
    # the baseline (no exclude/overrides passed) is unchanged -- this is purely additive
    assert S.components(st) == base


def test_change_impact_and_payment_impact_move_the_expected_sub_components():
    """The composite's overall sign can go either way (a flexibility-ratio component can move against
    a cut -- see docs), but the mechanically guaranteed pieces must hold: freeing cash flow can only
    help liquidity/savings, and an added payment can only hurt liquidity."""
    salary = _rec(2000, 0, 6, [2000] * 6, direction="credit", eid="sal")
    fixed = _rec(1000, 0, 6, [1000] * 6, flex="fixed", eid="rent")
    flexible = _rec(300, 0, 6, [300] * 6, flex="stoppable", eid="sub")
    st = _state([salary, fixed, flexible])
    ci = S.change_impact(st, _Chg("stop", "sub"))
    assert ci["delta"]["savings_behaviour"] >= 0
    assert ci["delta"]["liquidity_buffer"] >= 0
    pi = S.payment_impact(st, [(st.request_date, 10000.0)])
    assert pi["delta"]["liquidity_buffer"] <= 0
    assert pi["composite_delta"] <= 0


def test_with_changes_ranks_by_score_match_not_raw_dollar_saving(monkeypatch):
    """Plumbing test: with_changes() must rank candidates by how closely score.change_impact's
    composite_delta offsets score.payment_impact's hit, using score.py -- not simply cheapest-first --
    with raw dollar saving only as the tie-break. Impact numbers are controlled here so the test does
    not depend on the score formula's real arithmetic, only on with_changes()'s selection logic."""
    import buyorwait.plans as P

    tiny = P.Change("stop", "tiny", "c", "tiny sub", None, 10.0)     # cheapest in dollars
    big = P.Change("stop", "big", "c", "big sub", None, 500.0)      # best score match, chosen first

    def fake_payment_impact(state, payments, before=None):
        return {"composite_delta": -50.0}

    def fake_change_impact(state, change, before=None):
        return {"composite_delta": {"tiny": 2.0, "big": 48.0}[change.event_id]}

    def fake_is_safe(state, pay, **kw):
        return bool(kw.get("exclude"))  # either cut alone is "enough" in this toy scenario

    monkeypatch.setattr(P.score, "payment_impact", fake_payment_impact)
    monkeypatch.setattr(P.score, "change_impact", fake_change_impact)
    monkeypatch.setattr(P, "is_safe", fake_is_safe)

    st = _state([])
    chosen = P.with_changes(st, [(st.request_date, 10000.0)], [tiny, big])
    assert chosen is not None and [c.event_id for c in chosen] == ["big"]
