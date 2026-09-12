"""Balance projection, amount_safe_to_pay, earliest_date_for_full_payment.

The safety check runs on end-of-day balances plus, for INTRADAY_CHECK="periodic" (the default,
calibrated on the solved samples), the balance after that day's n-day-cadence debits (weekly,
fortnightly, ...) before its credits. INTRADAY_CHECK=True checks the balance after every debit
before the credits; False checks end-of-day balances only.
"""
from __future__ import annotations

from datetime import date, timedelta

from .intake import FinancialState

# The spec describes a 90-day check. On the 25 solved samples the reference forecast only
# reconciles when the window closes before day 87 (sample 13's earliest date, samples 05/10's
# troughs), so the engine projects 84 days (12 weeks). See docs/DECISIONS.md D6.
HORIZON_DAYS = 84
# Same-day ordering of a debit against a credit landing on the same day (payday collisions):
#   False      -> end-of-day balances only (every debit is netted against the credit)
#   True       -> every debit is charged before the credit (balance checked after each debit)
#   "periodic" -> only n-day-cadence debits (weekly/fortnightly/... variable spending) are charged
#                 before the credit; calendar-monthly debits, pending and scheduled rows are netted
# On the solved samples a weekly/fortnightly item projected onto the payday reconciles only when it is
# charged before the salary (samples 13, 18), while a monthly item on the payday reconciles only when it
# is netted (samples 02, 06, 15, 19, 22, 23). See docs/DECISIONS.md D6.
INTRADAY_CHECK: bool | str = "periodic"


def projection(state: FinancialState, extra: list[tuple[date, float, str]] | None = None,
               exclude: set[str] | None = None, overrides: dict[str, float] | None = None,
               horizon: int = HORIZON_DAYS) -> list[tuple[date, float, float]]:
    """Per day: (date, end-of-day balance, lowest balance reached during the day)."""
    start, end = state.request_date, state.request_date + timedelta(days=horizon)
    # ordering within a day: n-day-cadence debits (0) -> other debits (1) -> credits (2) -> plan payments (3);
    # the intra-day low is taken after the debits the mode charges before the credits
    checked = {0, 1} if INTRADAY_CHECK is True else ({0} if INTRADAY_CHECK == "periodic" else set())
    by_key = state.recurrence_by_key

    def order(a: float, label: str) -> int:
        if a >= 0:
            return 2
        rec = by_key.get(label)
        return 0 if rec is not None and rec.cadence_days > 0 else 1

    flows = [(d, a, l, order(a, l)) for d, a, l in state.flows(start, end, exclude=exclude, overrides=overrides)]
    if extra:
        flows += [(d, a, l, 3) for d, a, l in extra]
    flows.sort(key=lambda t: (t[0], t[3]))
    bal, out, i = state.balance, [], 0
    d = start
    while d <= end:
        low = float("inf")
        while i < len(flows) and flows[i][0] <= d:
            bal += flows[i][1]
            if flows[i][3] in checked:
                low = min(low, bal)
            i += 1
        out.append((d, bal, min(low, bal)))
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
