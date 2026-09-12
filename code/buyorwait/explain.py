"""decision_explanation: Groq LLM over a typed decision packet, with a deterministic template fallback.

The LLM never touches the contract columns; it only writes prose from numbers already computed.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date

from .formatting import fmt_amount
from .plans import Decision

# Groq retired llama-3.3-70b-versatile in Sept 2026; gpt-oss-120b is the strongest text model on the free tier.
MODEL = os.environ.get("BUYORWAIT_EXPLAIN_MODEL", "openai/gpt-oss-120b")
MAX_RETRIES = int(os.environ.get("BUYORWAIT_LLM_RETRIES", "8"))


def _human_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def _money(x: float, cur: str) -> str:
    s = fmt_amount(x, cur)
    if "." in s:
        whole, frac = s.split(".")
        return f"{cur} {int(whole):,}.{frac}"
    return f"{cur} {int(s):,}"


def decision_packet(dec: Decision, min_projected: float | None = None, score: dict | None = None) -> dict:
    st, req, p = dec.state, dec.request, dec.plan
    return {
        "request_id": req.request_id, "request_type": req.request_type, "currency": st.currency,
        "requested_amount": req.amount, "request_date": str(req.request_date), "deadline": str(req.deadline),
        "balance": st.balance, "minimum_balance": st.minimum, "amount_safe_to_pay": dec.safe_today,
        "earliest_full_payment": str(dec.earliest) if dec.earliest else None,
        "status": dec.status, "method": dec.method,
        "payment_plan": [(str(d), a) for d, a in (p.payments if p else [])],
        "total_paid": p.total_paid if p else None, "option_id": p.option_id if p else None,
        "spending_changes": [dict(kind=c.kind, event=c.event_id, description=c.description, category=c.category,
                                  new_amount=c.new_amount, saving_per_occurrence=c.saving) for c in (p.changes if p else [])],
        "rejected_plans": [dict(method=q.method, option_id=q.option_id, total=q.total_paid, changes=len(q.changes),
                                completes_by_deadline=q.last_date <= req.deadline) for q in dec.candidates[1:4]],
        "min_projected_balance_after_plan": min_projected,
        "evidence": st.notes[:4],
        "score": _compact_score(score),
    }


def _compact_score(score: dict | None) -> dict | None:
    """Keep the prompt small: composite before/after, hurt band and the two weakest components."""
    if not score:
        return None
    ss, ei = score.get("spending_score", {}), score.get("expense_impact", {})
    comps = {k: v for k, v in ss.items() if not k.startswith("_") and k != "composite"}
    weakest = sorted(comps.items(), key=lambda kv: kv[1])[:2]
    return {"spending_score": ss.get("composite"), "score_after_request": ei.get("composite_after"),
            "hurt_0_100": ei.get("hurt"), "band": ei.get("band"), "weakest_components": dict(weakest),
            "top_impact_drivers": ei.get("top_drivers")}


def template_explanation(dec: Decision) -> str:
    st, req, p = dec.state, dec.request, dec.plan
    cur = st.currency
    amt = _money(req.amount, cur)
    mn = _money(st.minimum, cur)
    if dec.status == "affordable_now":
        return f"Pay {amt} today. This keeps the {mn} minimum available over the next 12 weeks."
    if dec.method == "wait":
        return f"Pay {amt} in full on {_human_date(p.first_date)}. Paying earlier would take the balance below the {mn} minimum."
    if dec.method == "partial_payment":
        return (f"Pay {_money(p.payments[0][1], cur)} today and the remaining {_money(p.payments[1][1], cur)} on "
                f"{_human_date(p.payments[1][0])}. This completes the full request and keeps the {mn} minimum protected.")
    if dec.method == "installments":
        lead = ""
        if p.changes:
            lead = _changes_phrase(p, cur) + ", then use"
        else:
            lead = "Use"
        return (f"{lead} {p.n_payments} installments of {_money(p.payments[0][1], cur)}, starting {_human_date(p.first_date)}. "
                f"This leaves at least {mn} available.")
    if dec.method == "full_payment":
        return f"{_changes_phrase(p, cur)}, then pay {amt} today. This leaves at least {mn} available."
    if dec.safe_today > 0 and dec.earliest is None:
        return (f"Do not proceed with the {amt} request. Although {_money(dec.safe_today, cur)} is available today, "
                f"the full amount cannot be completed safely within the 12-week forecast.")
    return f"Do not make this payment by {_human_date(req.deadline)}. None of the available options keeps the {mn} minimum protected."


def _changes_phrase(p, cur: str) -> str:
    parts = []
    for c in p.changes:
        d = c.description.lower()
        if c.kind == "stop":
            parts.append(f"stop the {d}")
        else:
            parts.append(f"reduce the {d} to {_money(c.new_amount, cur)}")
    s = " and ".join(parts)
    return s[0].upper() + s[1:]


SYSTEM_PROMPT = """You write the decision_explanation for a personal-finance affordability engine, addressed to the user.
You receive JSON with numbers that are already final. Never change, recompute, or contradict them, and never mention the JSON,
"the packet", fields, or the engine. Write 1-3 plain sentences (at most 60 words) that:
1. state the recommendation exactly as method and payment_plan say (pay in full today / pay X today and Y on DATE /
   N installments of X starting DATE / wait until DATE / do not proceed), including any spending change (stop or reduce the named expense);
2. give the key financial fact behind it (minimum balance kept, salary timing, a pending bill, the shortfall);
3. optionally name the weakest score component in plain words (e.g. "liquidity buffer", "commitment load").
Money: currency code then the amount with thousands separators and two decimals only when the amount has cents (EUR 620.40, INR 197,400).
Dates as "15 June 2024". No markdown, no bullet points, no extra advice."""


def llm_explanation(packet: dict, client=None, tracer=None) -> tuple[str | None, dict]:
    """Returns (text, usage). text is None on any failure so the caller falls back to the template."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None, {"error": "no GROQ_API_KEY"}
    try:
        from groq import Groq, RateLimitError
        client = client or Groq(api_key=key, max_retries=0)
    except Exception as e:  # pragma: no cover
        return None, {"error": f"{type(e).__name__}: {e}", "model": MODEL}
    kwargs = dict(model=MODEL, temperature=0.2, max_tokens=400,
                  messages=[{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": json.dumps(packet, default=str)}])
    if "gpt-oss" in MODEL:
        kwargs["reasoning_effort"] = "low"    # hidden reasoning tokens count against the per-minute budget
    usage: dict = {"model": MODEL, "input_tokens": 0, "output_tokens": 0}
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
        except RateLimitError as e:  # free tier: 8k tokens/minute; wait for the window the API asks for
            m = re.search(r"try again in ([\d.]+)s", str(e))
            wait = min(60.0, float(m.group(1)) + 0.5) if m else 6.0 * (attempt + 1)
            usage["rate_limit_waits"] = usage.get("rate_limit_waits", 0) + 1
            if attempt == MAX_RETRIES:
                usage["error"] = f"RateLimitError after {MAX_RETRIES} retries"
                return None, usage
            time.sleep(wait)
            continue
        except Exception as e:  # network, auth: never block the run
            usage["error"] = f"{type(e).__name__}: {e}"
            return None, usage
        usage["input_tokens"] += resp.usage.prompt_tokens
        usage["output_tokens"] += resp.usage.completion_tokens
        usage["finish_reason"] = resp.choices[0].finish_reason
        text = (resp.choices[0].message.content or "").strip().replace("\n", " ")
        if text:
            return text, usage
        if attempt == MAX_RETRIES:
            break
        kwargs["max_tokens"] = min(1200, kwargs["max_tokens"] * 2)   # empty content: the budget went to reasoning
    usage["error"] = "empty completion"
    return None, usage
