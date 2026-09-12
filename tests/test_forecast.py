from datetime import date

import pandas as pd

from buyorwait.forecast import amount_safe_today, earliest_full_payment_date, is_safe, projection
from buyorwait.intake import FinancialState, Recurrence


def _state(balance=1000.0, minimum=200.0, recurring=None, fixed=None):
    return FinancialState(user_id="u", request_date=date(2026, 1, 1), currency="EUR", balance=balance, minimum=minimum,
                          recurring=recurring or [], fixed_flows=fixed or [], notes=[], facts=[], events=pd.DataFrame())


def _rec(amount, cadence, next_date, direction="debit", eid="event_1"):
    return Recurrence(key="k", category="c", event_type="expense", direction=direction, amount=amount, cadence_days=cadence,
                      next_date=next_date, last_event_id=eid, flexibility="fixed", minimum_allowed_amount=None, occurrences=3)


def test_projection_applies_flows_in_order():
    st = _state(recurring=[_rec(100, 0, date(2026, 1, 10))], fixed=[(date(2026, 1, 5), -50, "pending")])
    proj = {d: b for d, b, _ in projection(st)}
    assert proj[date(2026, 1, 4)] == 1000
    assert proj[date(2026, 1, 5)] == 950
    assert proj[date(2026, 1, 10)] == 850
    assert proj[date(2026, 2, 10)] == 750


def test_amount_safe_is_trough_minus_minimum_capped():
    st = _state(recurring=[_rec(100, 0, date(2026, 1, 10))])
    # trough over 84 days: 1000 - 3*100 = 700 -> 500 headroom
    assert amount_safe_today(st, 10_000) == 500
    assert amount_safe_today(st, 300) == 300


def test_same_day_ordering_is_configurable(monkeypatch):
    import buyorwait.forecast as F
    st = _state(balance=300, minimum=200, recurring=[_rec(150, 0, date(2026, 1, 15)), _rec(1000, 0, date(2026, 1, 15), "credit", "event_2")])
    # on Jan 15 the debit lands first: 300-150 = 150 < 200 for a moment; end-of-day balance is 1150
    monkeypatch.setattr(F, "INTRADAY_CHECK", True)
    assert not is_safe(st, [])
    monkeypatch.setattr(F, "INTRADAY_CHECK", False)
    assert is_safe(st, [])


def test_earliest_date_scans_forward():
    st = _state(balance=300, minimum=200, recurring=[_rec(1000, 0, date(2026, 1, 15), "credit", "event_2")])
    assert earliest_full_payment_date(st, 500) == date(2026, 1, 15)
    assert earliest_full_payment_date(st, 50) == date(2026, 1, 1)
    assert earliest_full_payment_date(st, 10_000) is None
