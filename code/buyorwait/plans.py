"""Candidate plans, eligibility, spending changes and the six-rule ranking."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from . import score
from .forecast import HORIZON_DAYS, amount_safe_today, earliest_full_payment_date, is_safe
from .intake import Dataset, FinancialState, Recurrence


@dataclass
class Change:
    kind: str            # stop | reduce_to
    event_id: str
    category: str
    description: str
    new_amount: float | None
    saving: float        # per occurrence, home currency

    def render(self, fmt) -> str:
        return f"stop:{self.event_id}" if self.kind == "stop" else f"reduce_to:{self.event_id}:{fmt(self.new_amount)}"


@dataclass
class Plan:
    method: str                       # full_payment | partial_payment | installments | wait
    payments: list[tuple[date, float]]
    changes: list[Change] = field(default_factory=list)
    option_id: str | None = None
    total_paid: float = 0.0
    safe: bool = False
    note: str = ""

    @property
    def n_payments(self) -> int:
        return len(self.payments)

    @property
    def first_date(self) -> date:
        return self.payments[0][0]

    @property
    def last_date(self) -> date:
        return self.payments[-1][0]

    def rank_key(self, deadline: date):
        opt = int(self.option_id.split("_")[-1]) if self.option_id else 10**9
        return (self.last_date > deadline, bool(self.changes), round(self.total_paid, 2), self.first_date, self.n_payments, opt)


@dataclass
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    amount: float
    deadline: date
    allows_partial: bool
    text: str


def request_from_row(row) -> Request:
    return Request(request_id=row.request_id, user_id=row.user_id, request_date=date.fromisoformat(row.request_date),
                   request_type=row.request_type, amount=float(row.requested_amount),
                   deadline=date.fromisoformat(row.desired_completion_date),
                   allows_partial=str(row.allows_partial_payment).strip().lower() == "true", text=row.request_text)


def option_schedule(opt) -> list[tuple[date, float]]:
    first = date.fromisoformat(opt.first_payment_date)
    n = int(opt.number_of_payments)
    if n <= 1:
        return [(first, float(opt.payment_amount))]
    step = int(opt.payment_frequency_days)
    return [(first + timedelta(days=step * k), float(opt.payment_amount)) for k in range(n)]


def _plan_horizon(state: FinancialState, payments) -> int:
    return max(HORIZON_DAYS, (payments[-1][0] - state.request_date).days + 1)


def _change_kwargs(changes: list[Change]):
    exclude = {c.event_id for c in changes if c.kind == "stop"}
    overrides = {c.event_id: c.new_amount for c in changes if c.kind == "reduce_to"}
    return dict(exclude=exclude, overrides=overrides)


def candidate_changes(state: FinancialState, profile) -> list[Change]:
    """Flexible recurring expenses the user permits changing, one action per event."""
    reduce_ok = {c for c in str(profile.expense_categories_user_is_willing_to_reduce).split("|") if c}
    stop_ok = {c for c in str(profile.expense_categories_user_is_willing_to_stop).split("|") if c}
    protected = {c for c in str(profile.expense_categories_to_protect).split("|") if c}
    out: list[Change] = []
    for rec in state.recurring:
        if rec.direction != "debit" or rec.flexibility == "fixed" or rec.category in protected:
            continue
        can_reduce = rec.flexibility in ("reducible", "reducible_or_stoppable") and rec.category in reduce_ok and rec.minimum_allowed_amount is not None
        can_stop = rec.flexibility in ("stoppable", "reducible_or_stoppable") and rec.category in stop_ok
        if can_reduce and rec.minimum_allowed_amount < rec.amount:
            out.append(Change("reduce_to", rec.last_event_id, rec.category, rec.description, rec.minimum_allowed_amount, rec.amount - rec.minimum_allowed_amount))
        elif can_stop:
            out.append(Change("stop", rec.last_event_id, rec.category, rec.description, None, rec.amount))
    return sorted(out, key=lambda c: (c.saving, c.event_id))


def with_changes(state: FinancialState, payments, candidates: list[Change]) -> list[Change] | None:
    """Accumulate permitted changes until the payments are safe, then prune (D13).

    Candidates are tried in order of how closely their own Spending Score effect (score.change_impact)
    offsets this payment's Spending Score hit (score.payment_impact) -- reaching for a cut whose impact
    matches the purchase's impact, rather than always the cheapest dollar saving in isolation. Raw
    dollar saving is only the tie-break. Safety is still the one hard gate: a change set that doesn't
    keep the projected balance >= minimum_balance_to_keep is never accepted, regardless of score.
    """
    if not candidates:
        return None
    baseline = score.components(state)
    hit = score.payment_impact(state, payments, before=baseline)["composite_delta"]  # <= 0: how much this hurts
    ranked = sorted(candidates, key=lambda c: (
        abs(score.change_impact(state, c, before=baseline)["composite_delta"] + hit), c.saving, c.event_id))
    chosen: list[Change] = []
    for c in ranked:
        chosen.append(c)
        if is_safe(state, payments, horizon=_plan_horizon(state, payments), **_change_kwargs(chosen)):
            break
    else:
        return None
    for c in list(chosen):
        trial = [x for x in chosen if x is not c]
        if is_safe(state, payments, horizon=_plan_horizon(state, payments), **_change_kwargs(trial)):
            chosen = trial
    return chosen[:3] if len(chosen) <= 3 else None


def enumerate_plans(ds: Dataset, state: FinancialState, req: Request, safe_today: float, earliest: date | None) -> list[Plan]:
    profile = ds.profiles.loc[req.user_id]
    methods = set(str(profile.payment_methods_user_will_consider).split("|"))
    max_months = profile.max_installment_months
    max_months = int(float(max_months)) if str(max_months).strip() else None
    cands = candidate_changes(state, profile)
    plans: list[Plan] = []

    def add(plan: Plan):
        plan.total_paid = round(sum(a for _, a in plan.payments), 2)
        plan.safe = is_safe(state, plan.payments, horizon=_plan_horizon(state, plan.payments), **_change_kwargs(plan.changes))
        if plan.safe:
            plans.append(plan)
        return plan

    if "full_payment" in methods:
        full = [(req.request_date, req.amount)]
        p = add(Plan("full_payment", full))
        if not p.safe:
            ch = with_changes(state, full, cands)
            if ch:
                add(Plan("full_payment", full, changes=ch, note="full payment today after spending changes"))
        if earliest is not None and earliest > req.request_date:
            add(Plan("wait", [(earliest, req.amount)]))

    if "partial_payment" in methods and req.allows_partial and 0 < safe_today < req.amount and earliest is not None and earliest <= req.deadline:
        add(Plan("partial_payment", [(req.request_date, safe_today), (earliest, round(req.amount - safe_today, 2))]))

    if "installments" in methods and max_months is not None:
        opts = ds.options[(ds.options.request_id == req.request_id) & (ds.options.payment_method == "installments")]
        for o in opts.itertuples():
            if int(o.number_of_payments) > max_months:
                continue
            sched = option_schedule(o)
            p = add(Plan("installments", sched, option_id=o.payment_option_id))
            if not p.safe:
                ch = with_changes(state, sched, cands)
                if ch:
                    add(Plan("installments", sched, changes=ch, option_id=o.payment_option_id))
    return plans


@dataclass
class Decision:
    request: Request
    safe_today: float
    earliest: date | None
    plan: Plan | None
    status: str
    method: str
    candidates: list[Plan]
    state: FinancialState


def decide(ds: Dataset, state: FinancialState, req: Request) -> Decision:
    safe_today = amount_safe_today(state, req.amount)
    earliest = earliest_full_payment_date(state, req.amount)
    plans = enumerate_plans(ds, state, req, safe_today, earliest)
    plans.sort(key=lambda p: p.rank_key(req.deadline))
    best = plans[0] if plans else None
    if best is None:
        status, method = "not_affordable", "not_recommended"
    elif best.method == "full_payment" and not best.changes:
        status, method = "affordable_now", "full_payment"
    elif best.method == "wait":
        status, method = "affordable_later", "wait"
    else:
        status, method = "affordable_with_plan", best.method
    return Decision(req, safe_today, earliest, best, status, method, plans, state)
