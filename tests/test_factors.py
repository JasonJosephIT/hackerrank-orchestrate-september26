from pathlib import Path

import pandas as pd
import pytest

from buyorwait.factors import S_WEIGHTS, T_WEIGHTS, account_factors
from buyorwait.intake import Dataset

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def factors():
    ds = Dataset.load(ROOT / "dataset")
    return ds, account_factors(ds)


def test_every_user_scored_in_range(factors):
    ds, f = factors
    assert len(f) == len(ds.profiles)
    comps = list(S_WEIGHTS) + list(T_WEIGHTS) + ["spending_factor", "stability_factor"]
    assert (f[comps] >= 0).all().all() and (f[comps] <= 100).all().all()
    assert f[comps].isna().sum().sum() == 0


def test_weights_sum_to_one():
    assert abs(sum(S_WEIGHTS.values()) - 1) < 1e-9 and abs(sum(T_WEIGHTS.values()) - 1) < 1e-9


def test_no_debt_means_full_debt_scores(factors):
    ds, f = factors
    no_debt = set(ds.profiles.index) - set(ds.events[ds.events.event_type == "debt_payment"].user_id)
    failed = set(ds.events[ds.events.status == "failed"].user_id)
    clean = list(no_debt - failed)
    assert (f.loc[clean, "S6_debt_service"] == 100).all()
    assert (f.loc[clean, "T3_debt_stability"] == 100).all()


def test_employment_ended_lowers_income_confirmation(factors):
    ds, f = factors
    ended = [m.user_id for m in ds.messages.itertuples()
             if "employment has ended" in m.message_text or "contract has ended" in m.message_text]
    assert ended and (f.loc[ended, "T2_income_confirmation"] <= 45).all()


def test_preference_multiplier_bounds(factors):
    _, f = factors
    assert f.preference_multiplier.between(0.90, 1.10).all()
