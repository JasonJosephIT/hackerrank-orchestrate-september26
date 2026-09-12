"""Spending Score + Expense Impact (D4): pure arithmetic over the reconstructed state.

Every component is 0-100. The composite never decides a contract column; it orders and
explains. Weights are documented here and tuned only for interpretability.
"""
from __future__ import annotations

from datetime import timedelta
from statistics import mean, pstdev

from .forecast import projection
from .intake import FinancialState

WEIGHTS = {"liquidity_buffer": 0.25, "commitment_load": 0.20, "spending_volatility": 0.10,
           "flexibility": 0.10, "income_reliability": 0.20, "savings_behaviour": 0.15}


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def components(state: FinancialState, extra=None, exclude=None, overrides=None) -> dict:
    proj = projection(state, extra=extra, exclude=exclude, overrides=overrides)
    trough = min(b for _, b in proj)
    income_m = state.monthly_income()
    outflow_m = state.monthly_outflow()
    fixed_m = state.monthly_outflow(only_fixed=True)
    essential_m = sum(r.amount * (30.0 / (r.cadence_days or 30)) for r in state.recurring if r.direction == "debit" and r.protected) or outflow_m
    headroom = trough - state.minimum
    runway_months = headroom / essential_m if essential_m > 0 else 3.0
    liquidity = _clip(runway_months / 3.0 * 100)                       # 3 months of essentials = 100
    commitment = _clip(100 * (1 - fixed_m / income_m)) if income_m > 0 else 0.0
    var_hist = [a for r in state.recurring if r.direction == "debit" and r.flexibility != "fixed" for a in r.history]
    if len(var_hist) >= 3 and mean(var_hist) > 0:
        volatility = _clip(100 * (1 - pstdev(var_hist) / mean(var_hist)))
    else:
        volatility = 50.0
    flexible_m = sum(r.amount * (30.0 / (r.cadence_days or 30)) for r in state.recurring if r.direction == "debit" and r.flexibility != "fixed" and not r.protected)
    flexibility = _clip(100 * flexible_m / outflow_m) if outflow_m > 0 else 0.0
    inc_recs = [r for r in state.recurring if r.direction == "credit"]
    if not inc_recs:
        reliability = 0.0
    else:
        stable = all(len(set(round(a, 2) for a in r.history)) <= 1 for r in inc_recs if r.history)
        reliability = 100.0 if stable else 60.0
        if any("pending" in n or "not forecast" in n for n in state.notes):
            reliability -= 20
    savings = _clip(100 * (income_m - outflow_m) / income_m) if income_m > 0 else 0.0
    return {"liquidity_buffer": round(liquidity, 1), "commitment_load": round(commitment, 1),
            "spending_volatility": round(volatility, 1), "flexibility": round(flexibility, 1),
            "income_reliability": round(reliability, 1), "savings_behaviour": round(savings, 1),
            "_trough": round(trough, 2), "_headroom": round(headroom, 2), "_monthly_income": round(income_m, 2),
            "_monthly_outflow": round(outflow_m, 2)}


def composite(c: dict) -> float:
    return round(sum(WEIGHTS[k] * c[k] for k in WEIGHTS), 1)


def spending_score(state: FinancialState) -> dict:
    c = components(state)
    c["composite"] = composite(c)
    return c


def expense_impact(state: FinancialState, dec) -> dict:
    """Re-run the components with the request injected (chosen plan, else full payment today)."""
    if dec.plan:
        extra = [(d, -a, "plan") for d, a in dec.plan.payments]
        exclude = {ch.event_id for ch in dec.plan.changes if ch.kind == "stop"}
        overrides = {ch.event_id: ch.new_amount for ch in dec.plan.changes if ch.kind == "reduce_to"}
    else:
        extra, exclude, overrides = [(dec.request.request_date, -dec.request.amount, "request")], set(), {}
    before = components(state)
    after = components(state, extra=extra, exclude=exclude, overrides=overrides)
    delta = {k: round(after[k] - before[k], 1) for k in WEIGHTS}
    headroom = max(before["_headroom"], 1e-9)
    hurt = _clip(100 * dec.request.amount / headroom) if before["_headroom"] > 0 else 100.0
    band = "fine" if after["_trough"] >= state.minimum and hurt < 50 else ("caution" if after["_trough"] >= state.minimum else "unsafe")
    drivers = sorted(delta.items(), key=lambda kv: kv[1])[:2]
    return {"composite_before": composite(before), "composite_after": composite(after), "delta": delta,
            "hurt": round(hurt, 1), "band": band, "top_drivers": [k for k, _ in drivers],
            "trough_after": after["_trough"]}
