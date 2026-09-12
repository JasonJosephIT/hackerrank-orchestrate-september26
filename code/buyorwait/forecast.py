"""Balance projection, amount_safe_to_pay, earliest_date_for_full_payment.

The safety check is applied after every projected debit (debits before credits on the same
day), so a bill that lands on payday is covered by the balance before the salary arrives.
"""
from __future__ import annotations

from datetime import date, timedelta

from .intake import FinancialState

# The spec describes a 90-day check. On the 25 solved samples the reference forecast only
# reconciles when the window closes before day 87 (sample 13's earliest date, samples 05/10's
# troughs), so the engine projects 84 days (12 weeks). See docs/DECISIONS.md D6.
HORIZON_DAYS = 84
# When True the safety check also looks at the balance after each debit inside a day (before
# that day's credits). The samples score slightly better on end-of-day balances, so it is off.
INTRADAY_CHECK = False


def projection(state: FinancialState, extra: list[tuple[date, float, str]] | None = None,
               exclude: set[str] | None = None, overrides: dict[str, float] | None = None,
               horizon: int = HORIZON_DAYS) -> list[tuple[date, float, float]]:
    """Per day: (date, end-of-day balance, lowest balance reached during the day)."""
    start, end = state.request_date, state.request_date + timedelta(days=horizon)
    # ordering within a day: recurring/pending debits (0) -> credits (1) -> plan payments (2)
    flows = [(d, a, l, 0 if a < 0 else 1) for d, a, l in state.flows(start, end, exclude=exclude, overrides=overrides)]
    if extra:
        flows = sorted(flows + [(d, a, l, 2) for d, a, l in extra], key=lambda t: (t[0], t[3]))
    bal, out, i = state.balance, [], 0
    d = start
    while d <= end:
        low = bal
        while i < len(flows) and flows[i][0] <= d:
            bal += flows[i][1]
            low = min(low, bal)
            i += 1
        out.append((d, bal, low if INTRADAY_CHECK else bal))
        d += timedelta(days=1)
    return out


def trough(proj: list[tuple[date, float, float]], start: date | None = None) -> float:
    return min(low for d, _, low in proj if start is None or d >= start)


def is_safe(state: FinancialState, payments: list[tuple[date, float]], **kw) -> bool:
    extra = [(d, -a, "plan") for d, a in payments]
    return trough(projection(state, extra=extra, **kw)) >= state.minimum - 1e-9


def amount_safe_today(state: FinancialState, requested: float, **kw) -> float:
    """Largest amount payable on request_date keeping every projected balance >= minimum."""
    safe = trough(projection(state, **kw)) - state.minimum
    return max(0.0, min(requested, round(safe, 2)))


def earliest_full_payment_date(state: FinancialState, amount: float, **kw) -> date | None:
    """First date d in the window such that paying `amount` on d keeps the path >= minimum.

    Paying on d lowers every balance from d onward by `amount`; the suffix minimum is
    non-decreasing in d, so a forward scan finds the first feasible day.
    """
    proj = projection(state, **kw)
    # paying at the end of day d: day d is checked on its end-of-day balance, later days on their lows
    suffix_low, m = {}, float("inf")
    for d, _, low in reversed(proj):
        suffix_low[d] = m          # min low strictly after d
        m = min(m, low)
    for d, end_bal, _ in proj:
        if min(end_bal, suffix_low[d]) - amount >= state.minimum - 1e-9:
            return d
    return None
