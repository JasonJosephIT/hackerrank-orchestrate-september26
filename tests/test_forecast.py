from datetime import date

import pandas as pd

from buyorwait.forecast import amount_safe_today, earliest_full_payment_date, is_safe, projection
from buyorwait.intake import FinancialState, Recurrence


def _state(balance=1000.0, minimum=200.0, recurring=None, fixed=None):
    return FinancialState(user_id="u", request_date=date(2026, 1, 1), currency="EUR", balance=balance, minimum=minimum,
                          recurring=recurring or [], fixed_flows=fixed or [], notes=[], facts=[], events=pd.DataFrame())


def _rec(amount, cadence, next_date, direction="debit", eid="event_1"):
    return Recurrence(key=f"{eid}/{direction}", category="c", event_type="expense", direction=direction, amount=amount, cadence_days=cadence,
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


def test_periodic_debit_is_charged_before_same_day_credit_but_monthly_is_netted(monkeypatch):
    import buyorwait.forecast as F
    monkeypatch.setattr(F, "INTRADAY_CHECK", "periodic")
    payday = date(2026, 1, 15)
    salary = _rec(1000, 0, payday, "credit", "event_2")
    # a weekly item landing on the payday: 300 - 150 = 150 < 200 before the salary arrives -> unsafe
    weekly = _state(balance=300, minimum=200, recurring=[_rec(150, 7, payday), salary])
    assert not is_safe(weekly, [])
    assert projection(weekly)[(payday - date(2026, 1, 1)).days] == (payday, 1150, 150)
    # the same amount as a calendar-monthly item is netted against the salary -> safe
    monthly = _state(balance=300, minimum=200, recurring=[_rec(150, 0, payday), salary])
    assert is_safe(monthly, [])
    assert projection(monthly)[(payday - date(2026, 1, 1)).days] == (payday, 1150, 1150)
    # the plan payment on a payday is still charged last, after the salary
    assert is_safe(monthly, [(payday, 900)])
    assert not is_safe(weekly, [(payday, 900)])


def test_conservative_reserve_raises_the_floor():
    # D13: the reserve is a cushion above the user's minimum; safe amount and earliest date honour it
    st = _state(balance=1000, minimum=200)
    assert amount_safe_today(st, 5000) == 800
    st.reserve = 150
    assert st.floor == 350
    assert amount_safe_today(st, 5000) == 650
    assert is_safe(st, [(date(2026, 1, 1), 650)])
    assert not is_safe(st, [(date(2026, 1, 1), 651)])
    assert earliest_full_payment_date(st, 700) is None
    st.reserve = 0
    assert earliest_full_payment_date(st, 700) == date(2026, 1, 1)
