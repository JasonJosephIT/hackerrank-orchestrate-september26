"""Compute the two account factors for every user and run the label-free checks (docs/SCORING_FACTORS.md §5).

    python3 code/evaluation/run_factors.py        # -> code/evaluation/factors.csv + printed test report
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "code"))

from buyorwait.factors import S_WEIGHTS, T_WEIGHTS, account_factors, stability_factor, _prep, spending_factor  # noqa: E402
from buyorwait.intake import Dataset  # noqa: E402


def main() -> int:
    ds = Dataset.load(ROOT / "dataset")
    t0 = time.time()
    f = account_factors(ds)
    dt = time.time() - t0
    out = ROOT / "code" / "evaluation" / "factors.csv"
    f.round(2).to_csv(out, index_label="user_id")
    print(f"computed {len(f)} users in {dt:.2f}s -> {out}\n")

    comps = list(S_WEIGHTS) + list(T_WEIGHTS) + ["spending_factor", "stability_factor"]
    print("1) distribution (p10 / median / p90 / share at floor or ceiling):")
    for c in comps:
        s = f[c]
        print(f"   {c:28s} {s.quantile(.1):6.1f} {s.median():6.1f} {s.quantile(.9):6.1f}   floor {(s <= 0).mean():.0%}  ceiling {(s >= 100).mean():.0%}")
    print("   bands:", f.spending_band.value_counts().to_dict(), f.stability_band.value_counts().to_dict())

    print("\n2) ordering on the 25 solved samples (mean factor by truth status):")
    sm = ds.samples.set_index("user_id")
    j = f.join(sm[["affordability_status"]], how="inner")
    print(j.groupby("affordability_status")[["spending_factor", "stability_factor"]].mean().round(1).to_string())
    order = {"affordable_now": 3, "affordable_with_plan": 2, "affordable_later": 1, "not_affordable": 0}
    rank = j.affordability_status.map(order)
    sp = lambda a, b: a.rank().corr(b.rank())   # Spearman without scipy
    print("   Spearman(stability, status rank) = %.2f ; Spearman(spending, status rank) = %.2f"
          % (sp(j.stability_factor, rank), sp(j.spending_factor, rank)))
    headroom = (ds.profiles.loc[j.index].current_available_balance - ds.profiles.loc[j.index].minimum_balance_to_keep)
    req = sm.loc[j.index].requested_amount.astype(float)
    print("   for comparison, Spearman(headroom / requested, status rank) = %.2f  (the engine's own driver)" % sp(headroom / req, rank))

    print("\n3) message ablation: T2 with messages removed")
    ds_nomsg = Dataset.load(ROOT / "dataset")
    ds_nomsg.messages = ds_nomsg.messages.iloc[0:0]
    ev = _prep(ds_nomsg)
    t_no = stability_factor(ev, ds_nomsg, spending_factor(ev, ds_nomsg.profiles))
    changed = (t_no.T2_income_confirmation != f.T2_income_confirmation)
    with_msg = set(ds.messages.user_id)
    print(f"   users whose T2 changes: {int(changed.sum())}; all of them have a message: {set(changed[changed].index) <= with_msg}")

    print("\n4) invariants:")
    ok_range = f[comps].min().min() >= 0 and f[comps].max().max() <= 100
    print(f"   all components in [0,100]: {ok_range}")
    no_debt = set(ds.profiles.index) - set(ds.events[ds.events.event_type == "debt_payment"].user_id)
    print(f"   users without debt have S6 = T3 = 100 (unless a failed debit): {(f.loc[list(no_debt), 'S6_debt_service'] == 100).all()}")
    ended = [m.user_id for m in ds.messages.itertuples() if "employment has ended" in m.message_text or "contract has ended" in m.message_text]
    print(f"   employment-ended users have T2 <= 45: {(f.loc[ended, 'T2_income_confirmation'] <= 45).all()} (n={len(ended)})")
    print("\n5) five lowest / highest stability users:")
    cols = ["spending_factor", "stability_factor", "T1_income_regularity", "T2_income_confirmation", "T5_savings_behaviour", "preference_multiplier"]
    print(pd.concat([f.nsmallest(5, "stability_factor")[cols], f.nlargest(5, "stability_factor")[cols]]).round(1).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
