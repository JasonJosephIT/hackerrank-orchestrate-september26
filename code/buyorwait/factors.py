"""Two-factor account profile: Spending Factor and Stability Factor (docs/SCORING_FACTORS.md).

Vectorised pandas over the whole event table: one groupby pass per component, no per-user loops,
so all 275 users score in well under a second. Both factors are 0-100 (higher = steadier) and never
touch a contract column.

    from buyorwait.factors import account_factors
    table = account_factors(ds)            # one row per user, every component + composites + bands
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from .evidence import extract_message_facts

DISCRETIONARY = {"dining", "shopping", "entertainment", "streaming", "gym", "delivery_membership",
                 "music_subscription", "cloud_storage"}
COMMITMENT_CATS = {"rent", "housing", "utilities", "insurance", "education", "debt_repayment", "streaming",
                   "cloud_storage", "music_subscription", "delivery_membership", "gym", "family_support", "healthcare"}
ONE_OFF_INCOME = ("arrears", "bonus", "prorated", "reimbursement", "prize", "proceeds", "commission", "one-time", "refund",
                  "reversal", "adjustment", "net salary")

S_WEIGHTS = {"S1_outflow_regularity": 0.20, "S2_committed_share": 0.15, "S3_commitment_load": 0.20,
             "S4_commitment_fluctuation": 0.10, "S5_discretionary_share": 0.15, "S6_debt_service": 0.10,
             "S7_payment_friction": 0.10}
T_WEIGHTS = {"T1_income_regularity": 0.25, "T2_income_confirmation": 0.25, "T3_debt_stability": 0.15,
             "T4_liability_stability": 0.15, "T5_savings_behaviour": 0.20}
# Message effects on T2 (start from 60; each fact kind adds once per user)
T2_EFFECTS = {"salary_increase": 20, "first_salary": 20, "salary_resumes": 20, "foreign_salary_confirmed": 15,
              "invoice_approved": 10, "employment_ended": -40, "payout_pending": -25, "salary_temporary": -15,
              "salary_reduced_next": -15, "salary_date_moved": -10, "household_income_ended": -10}


def _clip(s):
    return s.clip(0, 100)


def _cv(x: pd.Series) -> float:
    m = x.mean()
    return float(x.std(ddof=0) / m) if m > 0 and len(x) > 1 else 0.0


def _prep(ds) -> pd.DataFrame:
    ev = ds.events.copy()
    ev = ev[ev.amount.notna()]
    ev["home"] = ev.apply(lambda r: float(r.amount) if r.currency == ds.profiles.loc[r.user_id].home_currency
                          else float(r.amount) * ds.fx_rate(r.currency, ds.profiles.loc[r.user_id].home_currency,
                                                             pd.Timestamp(r.settlement_date or r.event_date).date()), axis=1)
    ev["sdate"] = pd.to_datetime(ev.settlement_date.where(ev.settlement_date != "", ev.event_date))
    ev["month"] = ev.sdate.dt.to_period("M")
    return ev


def spending_factor(ev: pd.DataFrame, profiles: pd.DataFrame) -> pd.DataFrame:
    settled = ev[ev.status == "settled"]
    deb = settled[settled.direction == "debit"]
    inc = settled[(settled.direction == "credit") & (settled.event_type == "income")]
    inc = inc[~inc.description.str.lower().str.contains("|".join(ONE_OFF_INCOME))]
    # drop partial first/last months so monthly CVs are not distorted by the history boundary
    span = deb.groupby("user_id").month.agg(["min", "max"])
    deb_full = deb.merge(span, left_on="user_id", right_index=True)
    deb_full = deb_full[(deb_full.month > deb_full["min"]) & (deb_full.month < deb_full["max"])]
    monthly_out = deb_full.groupby(["user_id", "month"]).home.sum()
    monthly_inc = inc.groupby(["user_id", "month"]).home.sum()
    income_m = monthly_inc.groupby("user_id").median()
    out_m = monthly_out.groupby("user_id").median()
    users = profiles.index
    f = pd.DataFrame(index=users)
    # Scales: this dataset's households are far steadier than real ones (JPMC Institute: median month-to-month
    # income change 36%), so CVs are mapped on tight ranges -> CV 0 = 100, CV 0.20 (outflow) / 0.10 (streams) = 0.
    f["S1_outflow_regularity"] = _clip(100 * (1 - monthly_out.groupby("user_id").agg(_cv) / 0.20)).reindex(users).fillna(50)
    committed = deb[deb.category.isin(COMMITMENT_CATS) | (deb.event_type.isin(["subscription", "debt_payment"]))]
    f["S2_committed_share"] = (100 * committed.groupby("user_id").home.sum() / deb.groupby("user_id").home.sum()).reindex(users).fillna(0)
    fixed_m = committed.merge(span, left_on="user_id", right_index=True)
    fixed_m = fixed_m[(fixed_m.month > fixed_m["min"]) & (fixed_m.month < fixed_m["max"])].groupby(["user_id", "month"]).home.sum().groupby("user_id").median()
    f["S3_commitment_load"] = _clip(100 * (1 - fixed_m / income_m)).reindex(users).fillna(0)
    stream_cv = committed.groupby(["user_id", "category"]).home.agg(_cv).groupby("user_id").mean()
    f["S4_commitment_fluctuation"] = _clip(100 * (1 - stream_cv / 0.10)).reindex(users).fillna(100)
    disc_share = (deb[deb.category.isin(DISCRETIONARY)].groupby("user_id").home.sum() / deb.groupby("user_id").home.sum()).reindex(users).fillna(0)
    f["S5_discretionary_share"] = _clip(100 * (0.30 - disc_share) / 0.25)          # 5% -> 100, 30% -> 0
    debt_m = deb[deb.event_type == "debt_payment"].groupby(["user_id", "month"]).home.sum().groupby("user_id").median()
    dti = (debt_m / income_m).reindex(users).fillna(0)
    f["S6_debt_service"] = _clip(100 * (1 - dti / 0.30))                            # 0% -> 100, 30% of income -> 0
    last90 = ev[ev.sdate >= ev.groupby("user_id").sdate.transform("max") - timedelta(days=90)]
    friction = (15 * (last90.status == "failed") + 10 * (last90.status == "cancelled")
                + 10 * last90.description.str.lower().str.contains("duplicate|retry")).groupby(last90.user_id).sum()
    f["S7_payment_friction"] = _clip(100 - friction.reindex(users).fillna(0))
    f["spending_factor"] = sum(f[k] * w for k, w in S_WEIGHTS.items()).round(1)
    f["_monthly_income"] = income_m.reindex(users)
    f["_monthly_outflow"] = out_m.reindex(users)
    f["_dti"] = dti
    f["_discretionary_share"] = disc_share
    return f


def stability_factor(ev: pd.DataFrame, ds, base: pd.DataFrame) -> pd.DataFrame:
    profiles = ds.profiles
    users = profiles.index
    settled = ev[ev.status == "settled"]
    inc = settled[(settled.direction == "credit") & (settled.event_type == "income")]
    inc = inc[~inc.description.str.lower().str.contains("|".join(ONE_OFF_INCOME))]
    f = pd.DataFrame(index=users)
    # T1: regular identical-amount stream -> 100; identical but not monthly -> 80; variable -> 100*(1-CV of monthly income)
    g = inc.groupby("user_id")
    identical = g.home.agg(lambda a: a.max() - a.min() < 1e-9)
    gaps = g.sdate.agg(lambda s: s.sort_values().diff().dt.days.median())
    monthly_cv = inc.groupby(["user_id", "month"]).home.sum().groupby("user_id").agg(_cv)
    t1 = pd.Series(np.where(identical & gaps.between(27, 32), 100, np.where(identical, 80, 100 * (1 - monthly_cv))), index=identical.index)
    f["T1_income_regularity"] = _clip(t1).reindex(users).fillna(0)
    # T2: messages
    t2 = pd.Series(60.0, index=users)
    sched = ev[(ev.status == "scheduled") & (ev.direction == "credit") & (ev.event_type == "income")].user_id.unique()
    t2[sched] += 25
    final = inc.sort_values("sdate").groupby("user_id").tail(1)
    t2[final[final.description.str.lower().str.contains("final")].user_id] -= 40
    for m in ds.messages.itertuples():
        kinds = {x.kind for x in extract_message_facts(m.message_id, m.message_text)}
        t2[m.user_id] += sum(T2_EFFECTS.get(k, 0) for k in kinds)
    f["T2_income_confirmation"] = _clip(t2)
    # T3: debt stability
    debt = settled[settled.event_type == "debt_payment"]
    dcv = debt.groupby("user_id").home.agg(_cv)
    retries = ev[ev.description.str.lower().str.contains("retry") | (ev.status == "failed")].groupby("user_id").size()
    t3 = (100 - 100 * (dcv / 0.10).clip(upper=1)).reindex(users).fillna(100) - 25 * retries.reindex(users).fillna(0)
    f["T3_debt_stability"] = _clip(t3)
    # T4: upcoming liabilities in the next 30 days after the last settled date, relative to monthly income
    asof = settled.groupby("user_id").sdate.max()
    upcoming = ev[(ev.status.isin(["pending", "scheduled"])) & (ev.direction == "debit")]
    upcoming = upcoming[upcoming.sdate <= upcoming.user_id.map(asof) + timedelta(days=30)]
    liab = upcoming.groupby("user_id").home.sum().reindex(users).fillna(0)
    income_ref = base["_monthly_income"].fillna(base["_monthly_outflow"])
    t4 = 100 * (1 - liab / income_ref.replace(0, np.nan)).fillna(100)
    rent_up = {m.user_id for m in ds.messages.itertuples() if any(x.kind == "rent_increase" for x in extract_message_facts(m.message_id, m.message_text))}
    t4[t4.index.isin(rent_up)] -= 10
    f["T4_liability_stability"] = _clip(t4)
    # T5: savings proxy = surplus rate, +10 if investment purchases recur, -20 if outflow exceeds income
    surplus = ((base["_monthly_income"] - base["_monthly_outflow"]) / base["_monthly_income"]).fillna(-1)
    t5 = _clip(100 * surplus / 0.30)
    invest = settled[settled.event_type == "investment_purchase"].groupby("user_id").size()
    t5[invest[invest >= 2].index] += 10
    t5[surplus < 0] -= 20
    f["T5_savings_behaviour"] = _clip(t5)
    base_t = sum(f[k] * w for k, w in T_WEIGHTS.items())
    # preference multiplier
    pr = profiles
    mult = pd.Series(1.0, index=users)
    mult += 0.03 * pr.financial_priorities.str.contains("emergency_savings|debt_repayment")
    mult += 0.02 * ((pr.expense_categories_user_is_willing_to_reduce != "") & (pr.expense_categories_user_is_willing_to_stop != ""))
    buffer_months = (pr.minimum_balance_to_keep / base["_monthly_outflow"]).fillna(0)
    mult += 0.02 * (buffer_months >= 1) - 0.02 * (buffer_months < 0.25)
    methods = pr.payment_methods_user_will_consider
    mult += 0.02 * methods.str.contains("full_payment") - 0.03 * (~methods.str.contains("full_payment"))
    f["preference_multiplier"] = mult.clip(0.90, 1.10).round(3)
    f["stability_factor"] = _clip(base_t * f["preference_multiplier"]).round(1)
    return f


def account_factors(ds) -> pd.DataFrame:
    ev = _prep(ds)
    s = spending_factor(ev, ds.profiles)
    t = stability_factor(ev, ds, s)
    out = pd.concat([s, t], axis=1)
    band = lambda x: np.where(x >= 70, "steady", np.where(x >= 45, "mixed", "irregular"))
    out["spending_band"] = band(out.spending_factor)
    out["stability_band"] = np.where(out.stability_factor >= 70, "dependable", np.where(out.stability_factor >= 45, "watch", "fragile"))
    return out
