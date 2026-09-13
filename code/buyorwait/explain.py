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
from .ratelimit import pace_after_call, pace_before_call

# Groq retired llama-3.3-70b-versatile in Sept 2026. Default: openai/gpt-oss-120b (D7); allam-2-7b was a short POC (D9)
# and is selectable via BUYORWAIT_EXPLAIN_MODEL.
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
    out = {"spending_score": ss.get("composite"), "score_after_request": ei.get("composite_after"),
           "hurt_0_100": ei.get("hurt"), "band": ei.get("band"), "weakest_components": dict(weakest),
           "top_impact_drivers": ei.get("top_drivers")}
    absorbed = ss.get("_absorbed") or []
    if absorbed:   # long-standing habits already reflected in the balance (docs/ABSORPTION.md)
        out["established_habits_already_in_balance"] = [a["stream"] for a in absorbed[:2]]
    unproven = [d for d in (ss.get("_income_detail") or []) if d["increase"] > 0 and d["trust"] < 0.9]
    if unproven:
        out["income_increase_not_yet_proven"] = [d["stream"] for d in unproven[:2]]
    return out


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


SYSTEM_PROMPT = """You write decision_explanation for a personal-finance affordability engine, addressed to the user.
The JSON you receive is final: copy its numbers exactly, never recompute or contradict them, and follow its "method".
Output 1-3 plain sentences (max 50 words), no labels, no headings, no bullets, no event ids, no score numbers, no JSON words.
Sentence 1 states the recommendation by method:
  full_payment -> "Pay <requested_amount> in full today." (add "after you stop/reduce the <description>" when spending_changes is non-empty)
  partial_payment -> "Pay <first amount> today and <second amount> on <second date>."
  installments -> "Use <n> installments of <amount>, starting <first date>."
  wait -> "Wait until <plan date>, then pay <amount> in full."
  not_recommended -> "Do not proceed with the <requested_amount> request."
Sentence 2 gives the key fact: the minimum balance kept, the salary date, a pending bill, or the shortfall between amount_safe_to_pay and requested_amount.
Optional sentence 3 names the weakest score component in plain words.
Money: currency code then amount with thousands separators (EUR 620.40, INR 197,400). Dates as "15 June 2024".
Examples:
  {"method":"wait","payment_plan":[["2024-06-15",12693000]],"currency":"IDR","minimum_balance":30686600} -> Wait until 15 June 2024, then pay IDR 12,693,000 in full. Paying sooner would put the IDR 30,686,600 minimum at risk.
  {"method":"not_recommended","requested_amount":15488,"currency":"ZAR","amount_safe_to_pay":737} -> Do not proceed with the ZAR 15,488 request. Only ZAR 737 is safe to pay today, and none of the available options keeps the minimum balance protected."""


def _numbers_in(text: str) -> set[str]:
    return {t.replace(",", "") for t in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def grounded(text: str, packet: dict) -> bool:
    """Deterministic guard on the prose: it must state the decision the engine made.

    - quotes every amount of the recommended plan (or the requested amount when nothing is recommended)
    - names only event ids the decision actually changes
    - is consistent with the method: a rejection reads as "do not", a wait names its date and does not
      tell the user to pay today, installments/partial mention their schedule
    """
    low = text.lower()
    if any(tok in text for tok in ("{", "}", '"request_id"', "payment_plan", "amount_safe_to_pay")) or len(text.split()) > 90:
        return False   # echoed packet, field names or a wall of text
    nums = _numbers_in(text)

    def has(x: float) -> bool:
        s = f"{x:.2f}".rstrip("0").rstrip(".")
        return s in nums or f"{x:.2f}" in nums or f"{round(x):d}" in nums

    plan = packet.get("payment_plan") or []
    required = [a for _, a in plan] or [packet["requested_amount"]]
    if not all(has(float(a)) for a in required):
        return False
    allowed_events = {c["event"] for c in packet.get("spending_changes", [])}
    if set(re.findall(r"event_\d+", text)) - allowed_events:
        return False
    method = packet.get("method")
    says_pay_today = bool(re.search(r"\bpay\b[^.]{0,60}\btoday\b", low))
    if method == "not_recommended":
        return ("do not" in low or "not recommended" in low or "don't" in low) and not says_pay_today
    if "do not proceed" in low or "not recommended" in low:
        return False
    if method == "wait":
        d = date.fromisoformat(plan[0][0])
        return ("wait" in low or "until" in low) and (str(d) in text or _human_date(d).lower() in low) and not says_pay_today
    if method == "installments":
        return "installment" in low and len(plan) > 0 and str(len(plan)) in text
    if method == "partial_payment":
        return "today" in low and (str(date.fromisoformat(plan[1][0])) in text or _human_date(date.fromisoformat(plan[1][0])).lower() in low)
    if method == "full_payment":
        ok = "today" in low or str(packet["request_date"]) in text
        if packet.get("spending_changes"):
            ok = ok and ("stop" in low or "reduce" in low)
        return ok
    return True


_UNICODE_FIXES = str.maketrans({"\u202f": " ", "\u00a0": " ", "\u2011": "-", "\u2013": "-", "\u2014": "-",
                                "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


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
    kwargs = dict(model=MODEL, temperature=0.0, max_tokens=400,
                  messages=[{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": json.dumps(packet, default=str)}])
    if "gpt-oss" in MODEL:
        kwargs["reasoning_effort"] = "low"    # hidden reasoning tokens count against the per-minute budget
    usage: dict = {"model": MODEL, "input_tokens": 0, "output_tokens": 0}
    for attempt in range(MAX_RETRIES + 1):
        try:
            pace_before_call()
            resp = client.chat.completions.create(**kwargs)
            pace_after_call(resp.usage.total_tokens or (resp.usage.prompt_tokens + resp.usage.completion_tokens))
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
        text = " ".join((resp.choices[0].message.content or "").translate(_UNICODE_FIXES).split())
        if text and not grounded(text, packet):
            usage["error"] = "ungrounded completion (amounts/events do not match the decision)"
            return None, usage
        if text:
            return text, usage
        if attempt == MAX_RETRIES:
            break
        kwargs["max_tokens"] = min(1200, kwargs["max_tokens"] * 2)   # empty content: the budget went to reasoning
    usage["error"] = "empty completion"
    return None, usage
