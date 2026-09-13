"""Tool catalogue: the engine's functions exposed as named, schema-described tools.

A tool is `fn(mem: UserMemory, **args) -> dict` (JSON-serialisable). Tools read and write the user's
working memory rather than passing large objects around, so the orchestrator and an LLM planner see
the same small, typed surface. `registry.schemas()` renders OpenAI-style function schemas for
tool-calling models; `registry.call()` executes and records the ledger entry.

Nothing here computes anything new: every tool delegates to intake / forecast / plans / score /
factors / verify / explain. The contract columns therefore stay byte-identical to the linear pipeline.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import date
from typing import Callable

from ..explain import decision_packet, llm_explanation, template_explanation
from ..forecast import HORIZON_DAYS, amount_safe_today, earliest_full_payment_date, is_safe, projection
from ..plans import Change, Decision, Plan, candidate_changes, choose, decide_for, enumerate_plans_for
from ..render import render_row
from ..score import expense_impact, spending_score
from ..verify import verify_rows
from .memory import Stopwatch, UserMemory


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                 # JSON schema for the arguments (without `mem`)
    fn: Callable[..., dict]
    owner: str                       # worker that normally runs it


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, description: str, parameters: dict | None, owner: str):
        def deco(fn):
            params = parameters or {"type": "object", "properties": {}, "required": []}
            self._tools[name] = Tool(name, inspect.cleandoc(description), params, fn, owner)
            return fn
        return deco

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def names(self) -> list[str]:
        return list(self._tools)

    def for_worker(self, worker: str) -> list[Tool]:
        return [t for t in self._tools.values() if t.owner == worker]

    def schemas(self) -> list[dict]:
        """OpenAI-style function schemas (what a tool-calling model sees)."""
        return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in self._tools.values()]

    def call(self, name: str, mem: UserMemory, worker: str | None = None, **args) -> dict:
        tool = self._tools[name]
        with Stopwatch() as sw:
            try:
                out = tool.fn(mem, **args)
                ok = True
            except Exception as e:
                out = {"error": f"{type(e).__name__}: {e}"}
                ok = False
        mem.record(worker or tool.owner, name, args, out.get("summary", "") if isinstance(out, dict) else str(out)[:120], sw.ms, ok)
        return out


registry = ToolRegistry()


# ------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------
def _rec_view(r) -> dict:
    return dict(key=r.key, direction=r.direction, amount=round(r.amount, 2), cadence_days=r.cadence_days,
                next_date=str(r.next_date), flexibility=r.flexibility, protected=r.protected,
                occurrences=r.occurrences, last_event_id=r.last_event_id, description=r.description,
                minimum_allowed_amount=r.minimum_allowed_amount, first_amount=r.first_amount)


def _plan_view(p: Plan, deadline: date) -> dict:
    return dict(method=p.method, option_id=p.option_id, payments=[(str(d), a) for d, a in p.payments],
                changes=[c.render(lambda x: f"{x:.2f}") for c in p.changes], total_paid=p.total_paid,
                completes_by_deadline=p.last_date <= deadline, n_payments=p.n_payments, safe=p.safe)


def _payments(mem: UserMemory, payments: list) -> list[tuple[date, float]]:
    return [(date.fromisoformat(str(d)), float(a)) for d, a in payments]


def _change_kwargs(changes: list[Change]):
    return dict(exclude={c.event_id for c in changes if c.kind == "stop"},
                overrides={c.event_id: c.new_amount for c in changes if c.kind == "reduce_to"})


def _resolve_changes(mem: UserMemory, rendered: list[str]) -> list[Change]:
    """Map 'stop:event_x' / 'reduce_to:event_x:amt' strings back to Change objects from the candidate list."""
    cands = {c.render(lambda x: f"{x:.2f}"): c for c in mem.get("change_candidates", [])}
    out = []
    for s in rendered:
        if s in cands:
            out.append(cands[s])
            continue
        parts = s.split(":")
        kind, eid = parts[0], parts[1]
        match = [c for c in cands.values() if c.event_id == eid and c.kind == kind]
        if not match:
            raise ValueError(f"{s} is not a permitted spending change for this user")
        out.append(match[0])
    return out


# ------------------------------------------------------------------------------------------
# Historian tools: recall and describe the user's history
# ------------------------------------------------------------------------------------------
@registry.register("recall_user_history", """
    Retrieve this user's financial history as of the request date: balance, minimum to keep, recurring
    commitments detected from settled history, reserved pending/scheduled flows, message and image facts.
    Must run first; every other tool reads the state it stores.""",
    {"type": "object", "properties": {"use_messages": {"type": "boolean", "description": "apply message facts (default true)"}}, "required": []},
    owner="historian")
def recall_user_history(mem: UserMemory, use_messages: bool = True) -> dict:
    st = mem.recall(use_messages=use_messages)
    mem.put("state", st)
    mem.put("change_candidates", candidate_changes(st, mem.profile))
    debits = [r for r in st.recurring if r.direction == "debit"]
    credits = [r for r in st.recurring if r.direction == "credit"]
    src = mem.get("recall_source")
    return dict(source=src, currency=st.currency, balance=st.balance, minimum_balance_to_keep=st.minimum,
                recurring_debits=len(debits), recurring_credits=len(credits), reserved_flows=len(st.fixed_flows),
                facts=len(st.facts), notes=st.notes[:6], monthly_income=round(st.monthly_income(), 2),
                monthly_outflow=round(st.monthly_outflow(), 2),
                summary=f"[{src}] {st.currency} balance {st.balance:,.2f} (keep {st.minimum:,.2f}); {len(debits)} recurring debits, "
                        f"{len(credits)} income streams, {len(st.fixed_flows)} reserved flows, {len(st.facts)} evidence facts")


@registry.register("list_commitments", """
    List the recurring streams recalled for the user (debits and credits): amount, cadence, next date,
    flexibility, protection, and how many settled occurrences support each.""",
    {"type": "object", "properties": {"direction": {"type": "string", "enum": ["debit", "credit"]}}, "required": []},
    owner="historian")
def list_commitments(mem: UserMemory, direction: str | None = None) -> dict:
    st = mem.require("state")
    recs = [r for r in st.recurring if direction is None or r.direction == direction]
    return dict(streams=[_rec_view(r) for r in recs], summary=f"{len(recs)} recurring stream(s)")


@registry.register("list_reserved_flows", """
    List pending debits, scheduled rows, approved invoices and arrears already reserved on their dates
    (these are counted before anything else is judged affordable).""", None, owner="historian")
def list_reserved_flows(mem: UserMemory) -> dict:
    st = mem.require("state")
    flows = [dict(date=str(d), amount=round(a, 2), label=l) for d, a, l in sorted(st.fixed_flows)]
    return dict(flows=flows, summary=f"{len(flows)} reserved flow(s), net {sum(f['amount'] for f in flows):,.2f}")


@registry.register("list_evidence", """
    Evidence used and its trust: typed facts extracted from messages (untrusted text, rule-validated),
    image-filled blank amounts, and the intake notes explaining every adjustment. Flags uncertainty:
    events ignored for lack of an amount, income kept at the safer (lower) figure, streams not forecast.""",
    None, owner="historian")
def list_evidence(mem: UserMemory) -> dict:
    st = mem.require("state")
    facts = [dict(kind=f.kind, message_id=f.message_id, amount=f.amount, currency=f.currency, on=str(f.on) if f.on else None,
                  source=f.source) for f in st.facts]
    uncertain = [n for n in st.notes if "ignored" in n or "keeping" in n or "not forecast" in n or "not projected" in n]
    return dict(facts=facts, notes=st.notes, uncertainty_flags=uncertain,
                summary=f"{len(facts)} fact(s), {len(st.notes)} note(s), {len(uncertain)} uncertainty flag(s)")


# ------------------------------------------------------------------------------------------
# Forecaster tools
# ------------------------------------------------------------------------------------------
@registry.register("project_balance", """
    Project the daily balance from the request date over the horizon, optionally with extra payments
    (the plan under test) and permitted spending changes. Returns the trough, its date, the first day
    the balance would fall under the minimum (if any) and the end-of-horizon balance.""",
    {"type": "object", "properties": {
        "horizon_days": {"type": "integer"},
        "payments": {"type": "array", "items": {"type": "array", "items": {}}, "description": "[[YYYY-MM-DD, amount], ...] to charge"},
        "changes": {"type": "array", "items": {"type": "string"}, "description": "['stop:event_x', 'reduce_to:event_y:amt']"}},
     "required": []}, owner="forecaster")
def project_balance(mem: UserMemory, horizon_days: int = HORIZON_DAYS, payments: list | None = None, changes: list | None = None) -> dict:
    st = mem.require("state")
    extra = [(d, -a, "plan") for d, a in _payments(mem, payments or [])]
    kw = _change_kwargs(_resolve_changes(mem, changes or []))
    proj = projection(st, extra=extra or None, horizon=horizon_days, **kw)
    trough_d, _, trough = min(proj, key=lambda t: (t[2], t[0]))
    breach = next((str(d) for d, _, low in proj if low < st.minimum - 1e-9), None)
    if not payments and not changes:
        mem.put("projection", proj)
    return dict(trough=round(trough, 2), trough_date=str(trough_d), end_balance=round(proj[-1][1], 2),
                first_breach=breach, headroom=round(trough - st.minimum, 2), horizon_days=horizon_days,
                summary=f"trough {trough:,.2f} on {trough_d} (headroom {trough - st.minimum:,.2f}); breach: {breach or 'none'}")


@registry.register("amount_safe_today", """
    Largest amount payable on the request date, capped at the requested amount, that keeps every
    projected balance at or above the minimum. Computed without spending changes (contract rule).""",
    None, owner="forecaster")
def tool_amount_safe_today(mem: UserMemory) -> dict:
    st = mem.require("state")
    v = amount_safe_today(st, mem.request.amount)
    mem.put("safe_today", v)
    return dict(amount_safe_to_pay=v, requested=mem.request.amount,
                summary=f"safe today {v:,.2f} of {mem.request.amount:,.2f}")


@registry.register("earliest_full_payment_date", """
    First date within the forecast on which paying the full requested amount keeps the balance path
    at or above the minimum. None when no such date exists in the window.""", None, owner="forecaster")
def tool_earliest(mem: UserMemory) -> dict:
    st = mem.require("state")
    d = earliest_full_payment_date(st, mem.request.amount)
    mem.put("earliest", d)
    return dict(earliest_date_for_full_payment=str(d) if d else None,
                summary=f"earliest full payment {d}" if d else "no safe full-payment date in the window")


# ------------------------------------------------------------------------------------------
# Planner tools
# ------------------------------------------------------------------------------------------
@registry.register("list_payment_options", """
    Seller/provider options supplied for this request, each judged against the user's preferences:
    method accepted, installment count within max_installment_months, partial payment allowed.""",
    None, owner="planner")
def list_payment_options(mem: UserMemory) -> dict:
    req, prof = mem.request, mem.profile
    methods = set(str(prof.payment_methods_user_will_consider).split("|"))
    mm = prof.max_installment_months
    max_months = int(float(mm)) if str(mm).strip() else None
    opts = []
    for o in mem.options.itertuples():
        reasons = []
        if o.payment_method not in methods:
            reasons.append("method not accepted by user")
        if o.payment_method == "installments" and (max_months is None or int(o.number_of_payments) > max_months):
            reasons.append("exceeds max_installment_months" if max_months is not None else "user will not consider installments")
        if o.payment_method == "partial_payment" and not req.allows_partial:
            reasons.append("request does not allow partial payment")
        opts.append(dict(option_id=o.payment_option_id, method=o.payment_method, n=int(o.number_of_payments),
                         payment_amount=o.payment_amount, total=o.total_payable_amount, fee=o.financing_fee,
                         first_payment_date=o.first_payment_date, eligible=not reasons, rejected_because=reasons))
    mem.put("options", opts)
    return dict(accepted_methods=sorted(methods), max_installment_months=max_months, allows_partial=req.allows_partial,
                options=opts, summary=f"{sum(o['eligible'] for o in opts)}/{len(opts)} supplied option(s) eligible")


@registry.register("candidate_spending_changes", """
    Spending changes the user permits: flexible, non-protected recurring expenses in categories the
    user is willing to reduce (to minimum_allowed_amount) or stop, smallest saving first.""", None, owner="planner")
def tool_candidate_changes(mem: UserMemory) -> dict:
    cands = mem.get("change_candidates") or candidate_changes(mem.require("state"), mem.profile)
    mem.put("change_candidates", cands)
    return dict(changes=[dict(action=c.render(lambda x: f"{x:.2f}"), category=c.category, description=c.description,
                              saving_per_occurrence=round(c.saving, 2)) for c in cands],
                summary=f"{len(cands)} permitted change(s)")


@registry.register("enumerate_candidate_plans", """
    Build every plan the rules allow (full today, full after permitted changes, wait, two-payment partial,
    each eligible installment option with/without changes) and keep the ones that are safe.""", None, owner="planner")
def tool_enumerate(mem: UserMemory) -> dict:
    st, req = mem.require("state"), mem.request
    plans = enumerate_plans_for(mem.profile, mem.options, st, req, mem.require("safe_today"), mem.require("earliest"))
    mem.put("candidates", plans)
    return dict(candidates=[_plan_view(p, req.deadline) for p in plans], summary=f"{len(plans)} safe candidate plan(s)")


@registry.register("check_plan_safety", """
    Re-test one payment schedule (with optional permitted changes) against the minimum-balance rule over
    a horizon covering its last payment. Used by the auditor to double-check the chosen plan.""",
    {"type": "object", "properties": {
        "payments": {"type": "array", "items": {"type": "array", "items": {}}},
        "changes": {"type": "array", "items": {"type": "string"}}}, "required": ["payments"]}, owner="auditor")
def check_plan_safety(mem: UserMemory, payments: list, changes: list | None = None) -> dict:
    st = mem.require("state")
    pays = _payments(mem, payments)
    horizon = max(HORIZON_DAYS, (pays[-1][0] - st.request_date).days + 1) if pays else HORIZON_DAYS
    kw = _change_kwargs(_resolve_changes(mem, changes or []))
    safe = is_safe(st, pays, horizon=horizon, **kw)
    proj = projection(st, extra=[(d, -a, "plan") for d, a in pays], horizon=horizon, **kw)
    trough = min(low for _, _, low in proj)
    return dict(safe=safe, trough=round(trough, 2), minimum=st.minimum, horizon_days=horizon,
                summary=f"{'safe' if safe else 'UNSAFE'}: trough {trough:,.2f} vs minimum {st.minimum:,.2f}")


@registry.register("rank_and_choose", """
    Rank the safe candidates by the six rules (completes by deadline, no spending changes, lowest total
    paid, earlier start, fewer payments, lowest option id) and choose the best. Candidates named in
    `exclude` (method or option id) are skipped, which is how the orchestrator re-plans after an audit failure.""",
    {"type": "object", "properties": {"exclude": {"type": "array", "items": {"type": "string"}}}, "required": []}, owner="planner")
def rank_and_choose(mem: UserMemory, exclude: list | None = None) -> dict:
    req, st = mem.request, mem.require("state")
    plans = [p for p in mem.require("candidates") if not exclude or (p.method not in exclude and p.option_id not in exclude)]
    dec = choose(plans, req, st, mem.require("safe_today"), mem.require("earliest"))
    mem.put("decision", dec)
    best = dec.plan
    return dict(status=dec.status, method=dec.method, chosen=_plan_view(best, req.deadline) if best else None,
                rejected=[_plan_view(p, req.deadline) for p in dec.candidates[1:4]],
                summary=f"{dec.status} / {dec.method}" + (f" via {best.option_id}" if best and best.option_id else ""))


@registry.register("score_gate", """
    Score gate (D14): settle the request from the card's numbers when a threshold makes the answer exact,
    otherwise route it to the plan search. headroom = projected trough - minimum (the liquidity basis of the
    score); hurt = requested / headroom. Rules: headroom >= requested and full payment accepted -> affordable_now
    (paying today keeps every projected balance above the minimum, and a single full payment today wins the
    six-rule ranking). No safe full-payment date, no eligible installment option and no permitted spending
    change (or a baseline breach with no permitted change) -> not_affordable. Everything else -> search.""",
    None, owner="planner")
def score_gate(mem: UserMemory) -> dict:
    st, req, prof = mem.require("state"), mem.request, mem.profile
    safe_today, earliest = mem.require("safe_today"), mem.require("earliest")
    proj = mem.get("projection") or projection(st)
    trough = min(low for _, _, low in proj)
    headroom = round(trough - st.minimum, 2)
    hurt = round(100.0 * req.amount / headroom, 1) if headroom > 0 else None
    methods = set(str(prof.payment_methods_user_will_consider).split("|"))
    opts = mem.get("options")
    if opts is None:
        opts = list_payment_options(mem)["options"]
    eligible_installments = [o["option_id"] for o in opts if o["eligible"] and o["method"] == "installments"]
    cands = mem.get("change_candidates") or []
    route, rule = "search", None
    if "full_payment" in methods and safe_today >= req.amount - 1e-9:
        route, rule = "affordable_now", f"headroom {headroom:,.2f} >= requested {req.amount:,.2f} (hurt {hurt}) and full payment accepted"
        plans = [Plan("full_payment", [(req.request_date, req.amount)], total_paid=req.amount, safe=True)]
    elif earliest is None and not eligible_installments and not cands:
        route, rule = "not_affordable", "no safe full-payment date in the window, no eligible installment option, no permitted spending change"
        plans = []
    elif headroom < 0 and not cands:
        route, rule = "not_affordable", f"baseline breach (headroom {headroom:,.2f}) and no permitted spending change"
        plans = []
    out = dict(route=route, rule=rule, headroom=headroom, hurt=hurt, composite=(mem.get("score") or {}).get("composite"),
               eligible_installments=eligible_installments, permitted_changes=len(cands))
    mem.put("gate", out)
    if route != "search":
        mem.put("candidates", plans)
        mem.put("decision", choose(plans, req, st, safe_today, earliest))
        out["summary"] = f"gate -> {route}: {rule}; plan search skipped"
    else:
        out["summary"] = f"gate -> search (headroom {headroom:,.2f}, hurt {hurt}, {len(eligible_installments)} eligible installment option(s), {len(cands)} permitted change(s))"
    return out


# ------------------------------------------------------------------------------------------
# Scorer tools
# ------------------------------------------------------------------------------------------
@registry.register("spending_score", """
    Spending Score: six 0-100 components (liquidity buffer, commitment load, spending volatility,
    flexibility, income reliability, savings behaviour) and the weighted composite, over the recalled state.""",
    None, owner="scorer")
def tool_spending_score(mem: UserMemory) -> dict:
    s = spending_score(mem.require("state"))
    mem.put("score", s)
    weakest = sorted(((k, v) for k, v in s.items() if not k.startswith("_") and k != "composite"), key=lambda kv: kv[1])[:2]
    return dict(composite=s["composite"], components={k: v for k, v in s.items() if not k.startswith("_")},
                weakest=weakest, summary=f"spending score {s['composite']} (weakest: {', '.join(k for k, _ in weakest)})")


@registry.register("expense_impact", """
    Expense Impact: re-score with the chosen plan injected; composite before/after, hurt (0-100 share of
    headroom the request consumes), band fine/caution/unsafe and the two components that move most.""",
    None, owner="scorer")
def tool_expense_impact(mem: UserMemory) -> dict:
    imp = expense_impact(mem.require("state"), mem.require("decision"))
    mem.put("impact", imp)
    return dict(**imp, summary=f"impact band {imp['band']}, hurt {imp['hurt']}, composite {imp['composite_before']} -> {imp['composite_after']}")


@registry.register("account_factors", """
    Two-factor account profile for this user from the whole event table (Spending Factor, Stability
    Factor, bands). Cross-user, vectorised, cached for the run; informs confidence, never a contract column.""",
    None, owner="scorer")
def tool_account_factors(mem: UserMemory) -> dict:
    out = mem.factors()
    if out is None:
        return dict(summary="no factor row for user")
    mem.put("factors", out)
    return dict(**out, summary=f"spending {out['spending_factor']} ({out['spending_band']}), stability {out['stability_factor']} ({out['stability_band']})")


# ------------------------------------------------------------------------------------------
# Auditor tools
# ------------------------------------------------------------------------------------------
@registry.register("audit_output_row", """
    Render the decision as an output row and run the independent contract validator on it (bounds,
    enums, plan format, partial arithmetic, installment option match, flexible-only changes).""",
    None, owner="auditor")
def audit_output_row(mem: UserMemory) -> dict:
    dec = mem.require("decision")
    row = render_row(dec, mem.get("explanation") or "pending")
    errs = verify_rows([row], mem.long_term.verify_context) if mem.long_term.verify_context else []
    mem.put("row", row)
    mem.put("audit_errors", errs)
    return dict(errors=errs, row={k: v for k, v in row.items() if k != "decision_explanation"},
                summary="contract OK" if not errs else f"{len(errs)} contract violation(s)")


@registry.register("counterfactual_without_messages", """
    Rebuild the user's state ignoring message facts and decide again. If the status, method or safe
    amount changes, the recommendation depends on untrusted message evidence: the reflection lowers
    confidence and the explanation names the dependency. Never changes the delivered decision.""",
    None, owner="auditor")
def counterfactual_without_messages(mem: UserMemory) -> dict:
    st_base = mem.require("state")
    dec = mem.require("decision")
    if not st_base.facts:
        return dict(depends_on_messages=False, summary="no message facts applied; counterfactual skipped")
    st_cf = mem.recall(use_messages=False)
    dec_cf = decide_for(mem.profile, mem.options, st_cf, mem.request)
    diff = {k: (a, b) for k, a, b in (("status", dec.status, dec_cf.status), ("method", dec.method, dec_cf.method),
                                       ("amount_safe_to_pay", dec.safe_today, dec_cf.safe_today))
            if a != b}
    mem.put("counterfactual", dict(differs=diff))
    return dict(depends_on_messages=bool(diff), differences=diff,
                summary=("decision depends on message evidence: " + ", ".join(f"{k} {a}->{b}" for k, (a, b) in diff.items()))
                if diff else "decision unchanged without message evidence")


# ------------------------------------------------------------------------------------------
# Explainer tools
# ------------------------------------------------------------------------------------------
@registry.register("build_decision_packet", """
    Assemble the typed decision packet (numbers, chosen and rejected plans, evidence, scores, reflection)
    that the explanation model receives. The packet is the only thing the LLM sees.""", None, owner="explainer")
def build_decision_packet(mem: UserMemory) -> dict:
    dec = mem.require("decision")
    min_proj = None
    if dec.plan:
        extra = [(d, -a, "plan") for d, a in dec.plan.payments]
        kw = _change_kwargs(dec.plan.changes)
        min_proj = round(min(low for _, _, low in projection(dec.state, extra=extra, **kw)), 2)
    packet = decision_packet(dec, min_proj, {"spending_score": mem.get("score") or {}, "expense_impact": mem.get("impact") or {}})
    refl = mem.get("reflection")
    if refl:
        packet["reflection"] = {"confidence": refl["confidence"], "concerns": refl["concerns"][:2]}
    mem.put("packet", packet)
    return dict(fields=len(packet), summary=f"packet with {len(packet)} fields")


@registry.register("write_explanation", """
    Write decision_explanation from the packet: Groq model (grounding-guarded) when use_llm, else the
    deterministic template. Records token usage.""",
    {"type": "object", "properties": {"use_llm": {"type": "boolean"}}, "required": []}, owner="explainer")
def write_explanation(mem: UserMemory, use_llm: bool = False) -> dict:
    dec, packet = mem.require("decision"), mem.require("packet")
    text, usage = None, {}
    if use_llm:
        text, usage = llm_explanation(packet, client=mem.get("llm_client"))
    if not text:
        text = template_explanation(dec)
        usage = {**usage, "fallback": True}
    mem.put("explanation", text)
    mem.put("usage", usage)
    return dict(explanation=text, usage=usage, summary=("LLM" if not usage.get("fallback") else "template") + f": {text[:80]}")
