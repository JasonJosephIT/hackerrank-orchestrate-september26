"""Spending Score + Expense Impact (D4): pure arithmetic over the reconstructed state.

Every component is 0-100. The composite never decides a contract column; it orders and
explains. Weights are documented here and tuned only for interpretability.
"""
from __future__ import annotations

import math
from datetime import timedelta
from statistics import mean, median, pstdev

from .forecast import projection
from .intake import FinancialState

WEIGHTS = {"liquidity_buffer": 0.25, "commitment_load": 0.20, "spending_volatility": 0.10,
           "flexibility": 0.10, "income_reliability": 0.20, "savings_behaviour": 0.15}

# Absorption / trust (docs/ABSORPTION.md, D12). Score layer only: the forecast and every contract
# column keep each stream at face value. Constants are chosen for interpretability, never tuned
# against the samples' contract columns.
ABSORPTION = True
TAU = 3.0            # months of history at which a stream is 63% "established" (1 - e^-1)
LAMBDA = 0.6         # an established expense keeps a 40% floor weight: the cash still leaves
MESSAGE_SEED = 0.25  # trust given to an income increase confirmed only by a message / scheduled row


def established(r) -> float:
    """How proven a recurring stream is, in [0, 1]: tenure in months through 1 - exp(-t / TAU), times amount stability."""
    if r.occurrences <= 0:
        return 0.0
    tenure = r.occurrences if r.cadence_days == 0 else r.occurrences * r.cadence_days / 30.0
    if len(r.history) >= 2 and mean(r.history) > 0:
        stability = max(0.0, 1.0 - pstdev(r.history) / mean(r.history))
    else:
        stability = 1.0
    return (1.0 - math.exp(-tenure / TAU)) * stability


def expense_weight(r) -> float:
    """Weight of a recurring debit in the commitment factor: novelty and rigidity count, long habits are absorbed."""
    return 1.0 - LAMBDA * established(r) if ABSORPTION else 1.0


def trusted_income(state: FinancialState) -> tuple[float, float, list[dict]]:
    """(trusted monthly income, face-value monthly income, per-stream detail).

    Each credit stream = the level proven by settled history + any increase above it. The increase
    is trusted in proportion to the payslips already settled at the new level (evidence curve),
    seeded at MESSAGE_SEED when the only backing is a message or a scheduled row. Decreases are
    taken at face value (safer interpretation).
    """
    face = trusted = 0.0
    detail = []
    for r in state.recurring:
        if r.direction != "credit":
            continue
        per_month = 30.0 / (r.cadence_days or 30)
        proven = median(r.history) if r.history else 0.0
        increase = max(0.0, r.amount - proven)
        level = min(r.amount, proven)
        if increase > 0 and ABSORPTION:
            at_new = sum(abs(a - r.amount) <= 0.01 * r.amount for a in r.history)
            tenure = at_new if r.cadence_days == 0 else at_new * r.cadence_days / 30.0
            trust = max(MESSAGE_SEED, 1.0 - math.exp(-tenure / TAU)) if at_new else MESSAGE_SEED
        else:
            trust = 1.0
        t = (level + increase * trust) * per_month
        face += r.amount * per_month
        trusted += t
        detail.append({"stream": r.description or r.key, "proven": round(level * per_month, 2), "increase": round(increase * per_month, 2), "trust": round(trust, 2)})
    return trusted, face, detail


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def components(state: FinancialState, extra=None, exclude=None, overrides=None) -> dict:
    proj = projection(state, extra=extra, exclude=exclude, overrides=overrides)
    trough = min(low for _, _, low in proj)
    trusted_m, income_m, income_detail = trusted_income(state)
    outflow_m = state.monthly_outflow()
    fixed_recs = [r for r in state.recurring if r.direction == "debit" and r.flexibility == "fixed"]
    fixed_m = sum(r.amount * (30.0 / (r.cadence_days or 30)) * expense_weight(r) for r in fixed_recs)
    # a plan injected by expense_impact() (installments, partial, full) has no history: full weight
    horizon_days = max(1, (proj[-1][0] - proj[0][0]).days)
    fixed_m += sum(-a for _, a, _ in (extra or []) if a < 0) * 30.0 / horizon_days
    absorbed = sorted(((established(r), r) for r in fixed_recs if established(r) >= 0.5), key=lambda t: -t[0])
    essential_m = sum(r.amount * (30.0 / (r.cadence_days or 30)) for r in state.recurring if r.direction == "debit" and r.protected) or outflow_m
    headroom = trough - state.minimum
    runway_months = headroom / essential_m if essential_m > 0 else 3.0
    liquidity = _clip(runway_months / 3.0 * 100)                       # 3 months of essentials = 100
    commitment = _clip(100 * (1 - fixed_m / trusted_m)) if trusted_m > 0 else 0.0
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
    elif ABSORPTION:
        # trusted share of the forecast income, discounted by how steady each stream's settled amounts are
        # (the stability term of the evidence curve), so irregular gig income still costs points smoothly
        stab = [(r.amount * (30.0 / (r.cadence_days or 30)),
                 max(0.0, 1.0 - pstdev(r.history) / mean(r.history)) if len(r.history) >= 2 and mean(r.history) > 0 else 1.0)
                for r in inc_recs]
        w_stab = sum(w * st for w, st in stab) / sum(w for w, _ in stab) if income_m > 0 else 0.0
        reliability = 100.0 * (trusted_m / income_m) * w_stab if income_m > 0 else 0.0
        if any("pending" in n or "not forecast" in n for n in state.notes):
            reliability -= 20
    else:
        stable = all(len(set(round(a, 2) for a in r.history)) <= 1 for r in inc_recs if r.history)
        reliability = 100.0 if stable else 60.0
        if any("pending" in n or "not forecast" in n for n in state.notes):
            reliability -= 20
    reliability = _clip(reliability)
    savings = _clip(100 * (trusted_m - outflow_m) / trusted_m) if trusted_m > 0 else 0.0
    return {"liquidity_buffer": round(liquidity, 1), "commitment_load": round(commitment, 1),
            "spending_volatility": round(volatility, 1), "flexibility": round(flexibility, 1),
            "income_reliability": round(reliability, 1), "savings_behaviour": round(savings, 1),
            "_trough": round(trough, 2), "_headroom": round(headroom, 2), "_monthly_income": round(income_m, 2),
            "_trusted_income": round(trusted_m, 2), "_monthly_outflow": round(outflow_m, 2),
            "_absorbed": [{"stream": r.description or r.key, "established": round(e, 2)} for e, r in absorbed[:3]],
            "_income_detail": income_detail}


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
