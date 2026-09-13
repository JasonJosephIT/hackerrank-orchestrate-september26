"""Render a Decision as one output.csv row (contract columns, AGENTS.md §6.2)."""
from __future__ import annotations

from .formatting import fmt_amount, fmt_plain


def render_row(dec, explanation: str) -> dict:
    st, req, p = dec.state, dec.request, dec.plan
    cur = st.currency
    plan = "|".join(f"{d}:{fmt_amount(a, cur)}" for d, a in p.payments) if p else "none"
    changes = "|".join(c.render(lambda x: fmt_amount(x, cur)) for c in p.changes) if p and p.changes else "none"
    if dec.status == "affordable_now":
        earliest = str(req.request_date)
    elif dec.status == "not_affordable":
        earliest = ""
    else:
        earliest = str(dec.earliest) if dec.earliest else ""
    return {
        "request_id": req.request_id,
        "amount_safe_to_pay": fmt_plain(dec.safe_today),
        "affordability_status": dec.status,
        "recommended_payment_method": dec.method,
        "payment_plan": plan,
        "earliest_date_for_full_payment": earliest,
        "spending_changes_needed": changes,
        "decision_explanation": explanation,
    }
