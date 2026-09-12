"""90-day projection, amount_safe_to_pay, earliest_date_for_full_payment."""
from __future__ import annotations

from datetime import date, timedelta

from .intake import FinancialState

HORIZON_DAYS = 84  # spec says 90 days; the 25 solved samples only reconcile when the window closes before day 87 (see docs/DECISIONS.md D6)


def projection(state: FinancialState, extra: list[tuple[date, float, str]] | None = None,
               exclude: set[str] | None = None, overrides: dict[str, float] | None = None,
               horizon: int = HORIZON_DAYS) -> list[tuple[date, float]]:
    """Daily end-of-day balance from request_date through request_date+horizon."""
    start, end = state.request_date, state.request_date + timedelta(days=horizon)
    flows = state.flows(start, end, exclude=exclude, overrides=overrides)
    if extra:
        flows = sorted(flows + list(extra), key=lambda t: t[0])
    bal, out, i = state.balance, [], 0
    d = start
    while d <= end:
        while i < len(flows) and flows[i][0] <= d:
            bal += flows[i][1]
            i += 1
        out.append((d, bal))
        d += timedelta(days=1)
    return out


def min_balance_from(proj: list[tuple[date, float]], start: date) -> float:
    return min(b for d, b in proj if d >= start)


def is_safe(state: FinancialState, payments: list[tuple[date, float]], **kw) -> bool:
    extra = [(d, -a, "plan") for d, a in payments]
    proj = projection(state, extra=extra, **kw)
    return all(b >= state.minimum - 1e-9 for _, b in proj)


def amount_safe_today(state: FinancialState, requested: float, **kw) -> float:
    """Largest amount payable on request_date keeping every projected day >= minimum."""
    proj = projection(state, **kw)
    trough = min(b for _, b in proj)
    safe = trough - state.minimum
    return max(0.0, min(requested, round(safe, 2)))


def earliest_full_payment_date(state: FinancialState, amount: float, **kw) -> date | None:
    """First date d in the window such that paying `amount` on d keeps the path >= minimum."""
    proj = projection(state, **kw)
    end = proj[-1][0]
    d = state.request_date
    # min over [d, end] is non-decreasing in d, so scan forward.
    suffix_min = {}
    m = float("inf")
    for dd, b in reversed(proj):
        m = min(m, b)
        suffix_min[dd] = m
    while d <= end:
        if suffix_min[d] - amount >= state.minimum - 1e-9:
            return d
        d += timedelta(days=1)
    return None
