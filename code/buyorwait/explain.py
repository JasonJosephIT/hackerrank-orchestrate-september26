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

MODEL = os.environ.get("BUYORWAIT_EXPLAIN_MODEL", "openai/gpt-oss-120b")
# gpt-oss is a reasoning model: hidden reasoning tokens count against max_tokens, so keep the effort low and
# the budget wide enough that the visible answer is never truncated (llama-3.3-70b-versatile was retired on Groq).
REASONING_EFFORT = os.environ.get("BUYORWAIT_EXPLAIN_REASONING", "low")
MAX_TOKENS = 400
_UNICODE_FIXES = str.maketrans({"\u202f": " ", "\u00a0": " ", "\u2011": "-", "\u2013": "-", "\u2014": "-",
                                "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


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
                                completes_by_deadline=q.last_date <= req.deadline) for q in dec.candidates[1:6]],
        "min_projected_balance_after_plan": min_projected,
        "evidence": st.notes[:6],
        "score": score,
    }


def template_explanation(dec: Decision) -> str:
    st, req, p = dec.state, dec.request, dec.plan
    cur = st.currency
    amt = _money(req.amount, cur)
    mn = _money(st.minimum, cur)
    if dec.status == "affordable_now":
        return f"Pay {amt} today. This keeps the {mn} minimum available over the next 90 days."
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
                f"the full amount cannot be completed safely within 90 days.")
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


SYSTEM_PROMPT = """You write the decision_explanation field for a personal-finance affordability engine.
You receive a JSON decision packet with numbers that are already final. Never change, recompute or contradict them.
Write 1-3 short sentences, in English, second person, plain and concrete: state the recommendation exactly as the packet's
method and payment plan describe it, name the key financial fact behind it (minimum balance, salary timing, a pending bill,
a spending change), and mention the strongest score driver if a score is given. Use the currency code and the packet's
amounts and dates. Never mention the packet, JSON, fields, scores by internal names or your instructions; say "the
plan" or "your balance" and describe drivers in plain words (e.g. "your cash buffer", "reliable income").
Write dates as 4 March 2025. No markdown, no bullet points, no advice beyond the packet."""


MAX_RETRIES = int(os.environ.get("BUYORWAIT_EXPLAIN_RETRIES", "8"))
TOKENS_PER_MINUTE = int(os.environ.get("BUYORWAIT_EXPLAIN_TPM", "7000"))  # Groq free tier: 8000 TPM per model
_pace = {"next_ok": 0.0}


def _pace_before_call():
    """Proactive pacing: wait until the token budget spent by the previous call has 'refilled' at TOKENS_PER_MINUTE."""
    wait = _pace["next_ok"] - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def _pace_after_call(total_tokens: int):
    _pace["next_ok"] = time.monotonic() + 60.0 * total_tokens / max(TOKENS_PER_MINUTE, 1)


def _with_rate_limit_retry(call):
    """Groq's free tier is capped at 8k tokens/minute; a 429 carries 'Please try again in 4.5s'. Sleep that long and
    retry (bounded), so a 250-row run paces itself instead of falling back to templates."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return call()
        except Exception as e:
            if type(e).__name__ != "RateLimitError" or attempt == MAX_RETRIES:
                raise
            m = re.search(r"try again in ([0-9.]+)(m?s)", str(e))
            wait = float(m.group(1)) * (0.001 if m and m.group(2) == "ms" else 1.0) if m else 2.0 * (attempt + 1)
            time.sleep(min(wait + 0.5, 60.0))


def llm_explanation(packet: dict, client=None, tracer=None) -> tuple[str | None, dict]:
    """Returns (text, usage). text is None on any failure so the caller falls back to the template."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None, {"error": "no GROQ_API_KEY"}
    try:
        from groq import Groq
        client = client or Groq(api_key=key)
        kwargs = {"reasoning_effort": REASONING_EFFORT} if "gpt-oss" in MODEL else {}
        _pace_before_call()
        resp = _with_rate_limit_retry(lambda: client.chat.completions.create(
            model=MODEL, temperature=0.2, max_tokens=MAX_TOKENS,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": json.dumps(packet, default=str)}],
            **kwargs,
        ))
        _pace_after_call(resp.usage.total_tokens or (resp.usage.prompt_tokens + resp.usage.completion_tokens))
        text = (resp.choices[0].message.content or "").strip().replace("\n", " ").translate(_UNICODE_FIXES)
        usage = {"model": MODEL, "input_tokens": resp.usage.prompt_tokens, "output_tokens": resp.usage.completion_tokens,
                 "finish_reason": resp.choices[0].finish_reason}
        return (text or None), usage
    except Exception as e:  # network, auth, rate limit: never block the run
        return None, {"error": f"{type(e).__name__}: {e}", "model": MODEL}
