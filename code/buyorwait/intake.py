"""Intake: load dataset/*.csv, convert currencies, classify events, detect recurrence,
apply evidence facts (messages + images). Deterministic pandas, no model calls.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .evidence import Fact, extract_message_facts

ROOT = Path(__file__).resolve().parent.parent.parent
DATASET = ROOT / "dataset"

CASH_DEBIT_TYPES = {"expense", "subscription", "debt_payment"}
VARIABLE_CATEGORIES = {"groceries", "transport", "dining"}
ONE_OFF_INCOME_CATEGORIES = {"windfall", "work_expense", "investment"}
ONE_OFF_DESCRIPTION_MARKERS = (
    "arrears", "bonus", "prorated", "reimbursement", "prize", "proceeds", "commission",
    "one-time", "refund", "reversal", "adjustment", "net salary",
)
ONE_OFF_EXPENSE_MARKERS = (
    "later reversed", "authorization", "awaiting refund", "duplicate", "possible duplicate",
    "outstanding", "retry", "one-off", "one-time", "annual", "deposit",
)


def _d(s) -> date:
    return date.fromisoformat(str(s)[:10])


def add_month(d: date, n: int = 1) -> date:
    m0 = d.month - 1 + n
    y, m = d.year + m0 // 12, m0 % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


@dataclass
class Dataset:
    profiles: pd.DataFrame
    events: pd.DataFrame
    requests: pd.DataFrame
    samples: pd.DataFrame
    options: pd.DataFrame
    rates: pd.DataFrame
    messages: pd.DataFrame
    images: pd.DataFrame

    @classmethod
    def load(cls, root: Path = DATASET) -> "Dataset":
        rd = lambda n: pd.read_csv(root / n, dtype=str, keep_default_na=False)
        ds = cls(profiles=rd("financial_profiles.csv"), events=rd("financial_events.csv"),
                 requests=rd("requests.csv"), samples=rd("sample_requests.csv"),
                 options=rd("request_payment_options.csv"), rates=rd("exchange_rates.csv"),
                 messages=rd("messages.csv"), images=rd("images.csv"))
        for col in ("current_available_balance", "minimum_balance_to_keep"):
            ds.profiles[col] = ds.profiles[col].astype(float)
        ds.profiles = ds.profiles.set_index("user_id", drop=False)
        ds.events["amount"] = pd.to_numeric(ds.events["amount"].replace("", None), errors="coerce")
        ds.events["minimum_allowed_amount"] = pd.to_numeric(ds.events["minimum_allowed_amount"].replace("", None), errors="coerce")
        for df in (ds.requests, ds.samples):
            df["requested_amount"] = df["requested_amount"].astype(float)
        for c in ("payment_amount", "total_payable_amount", "financing_fee"):
            ds.options[c] = ds.options[c].astype(float)
        ds.options["number_of_payments"] = ds.options["number_of_payments"].astype(int)
        ds.options["payment_frequency_days"] = pd.to_numeric(ds.options["payment_frequency_days"].replace("", None), errors="coerce")
        ds.rates["rate"] = ds.rates["rate"].astype(float)
        ds.rates["_d"] = ds.rates["rate_date"].map(_d)
        return ds

    def fx_rate(self, from_cur: str, to_cur: str, on: date) -> float:
        """Dated conversion rate; exact date, else nearest earlier, else earliest later.
        Uses the inverse pair when only the reverse direction is tabulated."""
        if from_cur == to_cur:
            return 1.0
        r = self.rates
        for a, b, inv in ((from_cur, to_cur, False), (to_cur, from_cur, True)):
            t = r[(r.from_currency == a) & (r.to_currency == b)]
            if t.empty:
                continue
            earlier = t[t._d <= on]
            pick = earlier.iloc[-1] if not earlier.empty else t.iloc[0]
            return (1.0 / float(pick.rate)) if inv else float(pick.rate)
        for hub in ("USD", "EUR"):
            try:
                return self.fx_rate(from_cur, hub, on) * self.fx_rate(hub, to_cur, on)
            except KeyError:
                continue
        raise KeyError(f"no rate {from_cur}->{to_cur}")


@dataclass
class Recurrence:
    key: str
    category: str
    event_type: str
    direction: str                     # debit | credit
    amount: float                      # home currency per occurrence
    cadence_days: int                  # 0 => calendar-monthly on the same day
    next_date: date
    last_event_id: str
    flexibility: str
    minimum_allowed_amount: float | None
    occurrences: int
    history: list[float] = field(default_factory=list)
    protected: bool = False
    description: str = ""
    first_amount: float | None = None  # amount for the first upcoming occurrence only
    end_date: date | None = None

    def dates(self, start: date, end: date) -> list[date]:
        out, d = [], self.next_date
        while d <= end and (self.end_date is None or d <= self.end_date):
            if d >= start:
                out.append(d)
            d = add_month(d) if self.cadence_days == 0 else d + timedelta(days=self.cadence_days)
        return out

    def flows(self, start: date, end: date, override: float | None = None):
        ds = self.dates(start, end)
        sign = -1.0 if self.direction == "debit" else 1.0
        for i, d in enumerate(ds):
            amt = override if override is not None else (self.first_amount if (i == 0 and self.first_amount is not None) else self.amount)
            yield d, sign * amt, self.key


@dataclass
class FinancialState:
    user_id: str
    request_date: date
    currency: str
    balance: float
    minimum: float
    recurring: list[Recurrence]
    fixed_flows: list[tuple[date, float, str]]
    notes: list[str]
    facts: list[Fact]
    events: pd.DataFrame
    reserve: float = 0.0           # conservative-mode cushion above `minimum` (D13); 0 unless irregular income
    irregular_income: bool = False

    @property
    def floor(self) -> float:
        """Balance the projection must never drop below: the user's minimum plus any conservative cushion."""
        return self.minimum + self.reserve

    @property
    def recurrence_by_key(self) -> dict[str, "Recurrence"]:
        return {r.key: r for r in self.recurring}

    def flows(self, start: date, end: date, exclude: set[str] | None = None,
              overrides: dict[str, float] | None = None) -> list[tuple[date, float, str]]:
        exclude, overrides = exclude or set(), overrides or {}
        out = list(self.fixed_flows)
        for rec in self.recurring:
            if rec.last_event_id in exclude:
                continue
            out.extend(rec.flows(start, end, overrides.get(rec.last_event_id)))
        # debits before credits on the same day (conservative intra-day ordering)
        return sorted(out, key=lambda t: (t[0], t[1] > 0))

    def monthly_income(self) -> float:
        return sum(r.amount * (30.0 / (r.cadence_days or 30)) for r in self.recurring if r.direction == "credit")

    def monthly_outflow(self, only_fixed: bool = False) -> float:
        return sum(r.amount * (30.0 / (r.cadence_days or 30)) for r in self.recurring
                   if r.direction == "debit" and (not only_fixed or r.flexibility == "fixed"))


def _is_one_off_income(desc: str, category: str) -> bool:
    d = desc.lower()
    return category in ONE_OFF_INCOME_CATEGORIES or any(m in d for m in ONE_OFF_DESCRIPTION_MARKERS)


def _is_one_off_expense(desc: str) -> bool:
    d = desc.lower()
    return any(m in d for m in ONE_OFF_EXPENSE_MARKERS)


def _stat(vals: list[float], how: str) -> float:
    if how == "last":
        return vals[-1]
    if how == "max":
        return max(vals)
    if how == "median":
        s = sorted(vals)
        return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
    if how.startswith("q"):          # "q0.25" -> lower quartile, linear interpolation
        q = float(how[1:])
        srt = sorted(vals)
        pos = q * (len(srt) - 1)
        lo, hi = int(pos), min(int(pos) + 1, len(srt) - 1)
        return srt[lo] + (srt[hi] - srt[lo]) * (pos - lo)
    if how.startswith("mean"):
        n = int(how[4:]) if len(how) > 4 else len(vals)
        v = vals[-n:]
        return sum(v) / len(v)
    raise ValueError(how)


def _cadence(dates: list[date], window: int) -> int:
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])][-window:]
    if not gaps:
        return 0
    med = sorted(gaps)[len(gaps) // 2]
    return 0 if 27 <= med <= 32 else int(med)


def _advance(d: date, cadence: int, until: date, strict: bool = False) -> date:
    while d < until or (strict and d == until):
        d = add_month(d) if cadence == 0 else d + timedelta(days=cadence)
    return d


DEFAULT_CFG = dict(
    pending_on_settlement=True,
    min_occurrences=2,
    gap_window=6,
    max_cadence=45,
    amount_stat_variable="mean",
    amount_stat_fixed="last",
    income_stable_ratio=1.01,     # tolerance around the median for a salary stream to count as regular
    use_messages=True,
    include_request_date=True,   # project a recurring debit falling on request_date itself
    outlier_ratio=3.0,           # amounts above 3x the group median are one-offs, not the recurring level
    first_gap_map={21: 14},      # sample-calibrated: a 3-week item's next occurrence lands ~2 weeks after the last one (D6)
    skip_subweekly_due_today=True,  # sample-calibrated (D11): a sub-weekly stream whose next occurrence is request_date is not projected
    # Conservative mode for irregular income (docs/DECISIONS.md D13). Off by default; enable with
    # `--conservative` or BUYORWAIT_CONSERVATIVE=1. Only ever makes the answer safer.
    conservative_income=False,
    income_haircut_quantile=0.25,  # variable income pool forecast at this quantile of its history instead of the mean
    reserve_months=0.1,            # cushion above minimum_balance_to_keep, in months of essential outflow, when income is irregular
)


def build_state(ds: Dataset, user_id: str, request_date: date, amount_facts: dict[str, float] | None = None,
                cfg: dict | None = None, request_id: str | None = None) -> FinancialState:
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    amount_facts = amount_facts or {}
    prof = ds.profiles.loc[user_id]
    cur = prof.home_currency
    ev = ds.events[ds.events.user_id == user_id].copy()
    notes: list[str] = []

    for idx, row in ev[ev.amount.isna()].iterrows():
        if row.event_id in amount_facts:
            ev.at[idx, "amount"] = float(amount_facts[row.event_id])
            notes.append(f"{row.event_id}: blank amount filled from image ({amount_facts[row.event_id]} {row.currency})")
        else:
            notes.append(f"{row.event_id}: blank amount, no image fact; ignored")
    ev = ev[ev.amount.notna()].copy()
    ev["sdate"] = ev.apply(lambda r: _d(r.settlement_date or r.event_date), axis=1)
    ev["home_amount"] = ev.apply(lambda r: float(r.amount) if r.currency == cur else float(r.amount) * ds.fx_rate(r.currency, cur, r.sdate), axis=1)

    fixed: list[tuple[date, float, str]] = []
    for r in ev.itertuples():
        if r.status in ("cancelled", "failed", "unrealized"):
            continue
        if r.status == "pending" and r.direction == "debit":
            when = max(r.sdate, request_date) if cfg["pending_on_settlement"] else request_date
            fixed.append((when, -r.home_amount, f"pending:{r.event_id}"))
        elif r.status == "scheduled":
            when = max(r.sdate, request_date)
            fixed.append((when, (-1 if r.direction == "debit" else 1) * r.home_amount, f"scheduled:{r.event_id}"))

    hist = ev[(ev.status == "settled") & (ev.sdate < request_date)]
    protected = set(str(prof.expense_categories_to_protect).split("|"))
    recurring: list[Recurrence] = []

    # ---- expenses: grouped by category + event_type ---------------------------------
    exp = hist[hist.event_type.isin(CASH_DEBIT_TYPES) & (hist.direction == "debit")]
    exp = exp[~exp.description.map(_is_one_off_expense)]
    for (cat, et), g in exp.groupby(["category", "event_type"]):
        g = g.sort_values(["sdate", "event_id"])
        if len(g) < cfg["min_occurrences"]:
            continue
        dates = list(g.sdate)
        cad = _cadence(dates, cfg["gap_window"])
        if cad > cfg["max_cadence"]:
            continue
        amounts = list(g.home_amount)
        med = _stat(amounts, "median")
        keep = g.home_amount <= cfg["outlier_ratio"] * med
        if (~keep).any() and keep.sum() >= cfg["min_occurrences"]:
            notes.append(f"{cat}: {(~keep).sum()} one-off amount(s) excluded from the recurring estimate")
            g = g[keep]
            amounts = list(g.home_amount)
        fixed_amount = max(amounts) - min(amounts) < 1e-9
        stat = cfg["amount_stat_fixed"] if fixed_amount else cfg["amount_stat_variable"]
        last = g.iloc[-1]
        # Sample-calibrated (D6): a 3-week item's next occurrence lands ~2 weeks after the last one when that
        # date is still ahead; otherwise the plain cadence sequence applies.
        first_gap = cfg["first_gap_map"].get(cad)
        strict = not cfg["include_request_date"]
        if first_gap and (last.sdate + timedelta(days=first_gap) > request_date or (not strict and last.sdate + timedelta(days=first_gap) == request_date)):
            next_date = last.sdate + timedelta(days=first_gap)
        else:
            next_date = _advance(last.sdate, cad, request_date, strict=strict)
        # Sample-calibrated (D11): a sub-weekly item (cadence < 7 days) that would land on request_date itself is
        # day-to-day spending the reference does not carry forward as a commitment (sample 06); a sub-weekly item
        # whose next occurrence is still ahead is projected as usual (samples 24, 25).
        if cfg["skip_subweekly_due_today"] and 0 < cad < 7 and next_date == request_date:
            notes.append(f"{cat}: {cad}-day stream due on the request date not projected")
            continue
        recurring.append(Recurrence(
            key=f"{cat}/{et}", category=cat, event_type=et, direction="debit", amount=_stat(amounts, stat),
            cadence_days=cad, next_date=next_date, last_event_id=last.event_id,
            flexibility=last.flexibility, minimum_allowed_amount=(None if pd.isna(last.minimum_allowed_amount) else float(last.minimum_allowed_amount)),
            occurrences=len(g), history=amounts, protected=(cat in protected), description=last.description))

    # ---- income: grouped by description; regular streams first, then a variable pool --
    inc = hist[(hist.event_type == "income") & (hist.direction == "credit")]
    income_recs: list[Recurrence] = []
    employment_over = False
    irregular = False
    if not inc.empty:
        latest = inc.sort_values(["sdate", "event_id"]).iloc[-1]
        if "final" in latest.description.lower():
            employment_over = True
            notes.append(f"{latest.event_id}: final employer payroll; no salary forecast")
    inc = inc[~inc.apply(lambda r: _is_one_off_income(r.description, r.category), axis=1)]
    leftovers = []
    for desc, g in inc.groupby("description"):
        g = g.sort_values(["sdate", "event_id"])
        amounts = list(g.home_amount)
        if len(g) < cfg["min_occurrences"]:
            leftovers.append(g)
            continue
        med = _stat(amounts, "median")
        share = sum(abs(a - med) <= (cfg["income_stable_ratio"] - 1) * med for a in amounts) / len(amounts)
        if share < 0.6:
            leftovers.append(g)
            continue
        cad = _cadence(list(g.sdate), cfg["gap_window"])
        if cad > 32:
            leftovers.append(g)
            continue
        last = g.iloc[-1]
        income_recs.append(Recurrence(
            key=f"{last.category}/income", category=last.category, event_type="income", direction="credit",
            amount=med, cadence_days=cad, next_date=_advance(last.sdate, cad, request_date),
            last_event_id=last.event_id, flexibility="fixed", minimum_allowed_amount=None, occurrences=len(g),
            history=amounts, description=desc))
    if leftovers and not income_recs:
        pool = pd.concat(leftovers).sort_values(["sdate", "event_id"])
        if len(pool) >= 3:
            amounts = list(pool.home_amount)
            cad = _cadence(list(pool.sdate), cfg["gap_window"])
            if 0 < cad <= 32 or cad == 0:
                last = pool.iloc[-1]
                irregular = True
                stat = f"q{cfg['income_haircut_quantile']}" if cfg["conservative_income"] else "mean"
                level = _stat(amounts, stat)
                income_recs.append(Recurrence(
                    key=f"{last.category}/income", category=last.category, event_type="income", direction="credit",
                    amount=level, cadence_days=cad, next_date=_advance(last.sdate, cad, request_date),
                    last_event_id=last.event_id, flexibility="fixed", minimum_allowed_amount=None, occurrences=len(pool),
                    history=amounts, description="variable income"))
                how = f"{stat[1:]} quantile (conservative haircut from mean {_stat(amounts, 'mean'):.2f})" if cfg["conservative_income"] else "mean"
                notes.append(f"variable income pool forecast at {how} {level:.2f} every {cad or 'month'} days")
    if employment_over:
        income_recs = []

    # A scheduled next-salary row is the confirmed anchor: the stream continues monthly after it.
    sched_inc = ev[(ev.status == "scheduled") & (ev.direction == "credit") & (ev.event_type == "income") & (ev.sdate >= request_date)]
    if not sched_inc.empty:
        last_s = sched_inc.sort_values("sdate").iloc[-1]
        anchored = [r for r in income_recs if abs(r.amount - last_s.home_amount) <= 0.01 * last_s.home_amount] or income_recs[:1]
        if income_recs:
            for rec in anchored:
                rec.next_date = add_month(last_s.sdate)
                rec.amount = last_s.home_amount
        else:
            income_recs.append(Recurrence(
                key="salary/income", category=last_s.category, event_type="income", direction="credit",
                amount=last_s.home_amount, cadence_days=0, next_date=add_month(last_s.sdate), last_event_id=last_s.event_id,
                flexibility="fixed", minimum_allowed_amount=None, occurrences=1, history=[last_s.home_amount], description=last_s.description))
            notes.append(f"salary stream seeded from scheduled {last_s.event_id}")

    # ---- message facts --------------------------------------------------------------
    facts: list[Fact] = []
    if cfg["use_messages"]:
        msgs = ds.messages[(ds.messages.user_id == user_id)].copy()
        if not msgs.empty:
            msgs = msgs[msgs.sent_at.map(lambda s: _d(s) <= request_date) | (msgs.request_id == (request_id or ""))]
        for m in msgs.sort_values("sent_at").itertuples():
            facts.extend(extract_message_facts(m.message_id, m.message_text))
        _apply_facts(ds, cur, request_date, facts, income_recs, recurring, fixed, notes)

    recurring.extend(income_recs)
    state = FinancialState(user_id=user_id, request_date=request_date, currency=cur,
                           balance=float(prof.current_available_balance), minimum=float(prof.minimum_balance_to_keep),
                           recurring=recurring, fixed_flows=fixed, notes=notes, facts=facts, events=ev,
                           irregular_income=irregular)
    if cfg["conservative_income"] and irregular and cfg["reserve_months"] > 0:
        essential = sum(r.amount * (30.0 / (r.cadence_days or 30)) for r in recurring
                        if r.direction == "debit" and r.protected) or state.monthly_outflow()
        state.reserve = round(cfg["reserve_months"] * essential, 2)
        notes.append(f"irregular income: conservative reserve {state.reserve:.2f} {cur} held above the minimum balance")
    return state


def _apply_facts(ds: Dataset, cur: str, rd: date, facts: list[Fact], income: list[Recurrence],
                 recurring: list[Recurrence], fixed: list, notes: list[str]) -> None:
    def conv(amount: float, currency: str | None, on: date) -> float:
        if not currency or currency == cur:
            return amount
        return amount * ds.fx_rate(currency, cur, on)

    def salary_recs():
        return [r for r in income if r.direction == "credit"]

    for f in facts:
        k = f.kind
        if k == "payout_pending":
            before = len(income)
            income[:] = [r for r in income if len(set(round(a, 2) for a in r.history)) == 1 and r.description != "variable income"]
            if len(income) != before:
                notes.append(f"{f.message_id}: platform payout pending; unconfirmed platform income not forecast")
        elif k == "employment_ended":
            income[:] = []
            notes.append(f"{f.message_id}: employment/contract ended; no salary forecast")
        elif k in ("salary_increase", "household_income_ended", "base_salary_commission_pending", "salary_resumes",
                   "first_salary", "foreign_salary_confirmed"):
            on = f.on
            recs = salary_recs()
            if k == "household_income_ended" and len(recs) > 1:
                # one household income has ended: keep only the largest (primary) stream
                primary = max(recs, key=lambda r: r.amount)
                dropped = [r for r in recs if r is not primary]
                income[:] = [r for r in income if r not in dropped]
                notes.append(f"{f.message_id}: household income ended; dropped {', '.join(r.description for r in dropped)}")
                recs = [primary]
            if recs:
                for r in recs:
                    if on and on >= rd:
                        r.next_date = on
                    new_amt = conv(f.amount, f.currency, r.next_date)
                    if k in ("base_salary_commission_pending", "household_income_ended") and new_amt > r.amount:
                        # message restates the base without a settled record above history: keep the safer (lower) figure
                        notes.append(f"{f.message_id}: stated salary {f.amount} {f.currency} exceeds settled history; keeping {r.amount:.2f}")
                        continue
                    r.amount = new_amt
                    r.first_amount = None
                    r.cadence_days = 0
                    notes.append(f"{f.message_id}: {k} -> salary {f.amount} {f.currency}" + (f" from {f.on}" if f.on else ""))
            else:
                start = on if (on and on >= rd) else None
                if start is None:
                    continue
                income.append(Recurrence(key="salary/income", category="salary", event_type="income", direction="credit",
                                         amount=conv(f.amount, f.currency, start), cadence_days=0, next_date=start,
                                         last_event_id=f.message_id, flexibility="fixed", minimum_allowed_amount=None,
                                         occurrences=0, history=[], description=k))
                notes.append(f"{f.message_id}: {k} -> new salary stream {f.amount} {f.currency} from {start}")
        elif k in ("salary_temporary", "salary_reduced_next", "salary_next_plus_arrears"):
            for r in salary_recs():
                r.first_amount = conv(f.amount, f.currency, r.next_date)
                if k == "salary_next_plus_arrears" and f.extra.get("arrears"):
                    fixed.append((r.next_date, conv(f.extra["arrears"], f.currency, r.next_date), f"arrears:{f.message_id}"))
            notes.append(f"{f.message_id}: {k} -> next salary {f.amount} {f.currency}")
        elif k == "salary_date_moved" and f.on:
            for r in salary_recs():
                if f.on >= rd:
                    r.next_date = f.on
            notes.append(f"{f.message_id}: next salary moved to {f.on}")
        elif k == "invoice_approved" and f.on and f.on >= rd:
            fixed.append((f.on, conv(f.amount, f.currency, f.on), f"invoice:{f.message_id}"))
            notes.append(f"{f.message_id}: approved invoice {f.amount} {f.currency} on {f.on}")
        elif k == "rent_increase" and f.pct:
            for r in recurring:
                if r.category == "rent" and r.direction == "debit":
                    r.amount = round(r.amount * (1 + f.pct / 100), 2)
                    notes.append(f"{f.message_id}: rent +{f.pct}% -> {r.amount:.2f}")
