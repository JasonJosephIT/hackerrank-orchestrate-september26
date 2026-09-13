"""D14 account profile: deterministic archetype rules and the closed-schema validator."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from buyorwait.profile import deterministic_archetype, validate  # noqa: E402


def _f(streams=(), one_off=(), pending=(), facts=(), notes=(), pool=False):
    return {"income_streams": list(streams), "one_off_credits": list(one_off), "pending_or_scheduled_credits": list(pending),
            "message_facts": list(facts), "income_notes": list(notes), "variable_pool": pool, "primary_income_share": 1.0}


def _s(desc, eid="event_1"):
    return {"id": eid, "description": desc, "cadence_days": 0, "occurrences": 6, "median": 1000, "forecast_amount": 1000, "cv": 0.0, "last_amounts": [1000]}


def test_archetype_rules():
    assert deterministic_archetype(_f()) == "no_income"
    assert deterministic_archetype(_f(streams=[_s("Payroll credit")])) == "salaried"
    assert deterministic_archetype(_f(streams=[_s("Delivery platform payout")], pool=True)) == "gig"
    pooled = _f(streams=[_s("variable income")], pool=True)
    pooled["income_descriptions"] = [{"description": "Driver platform payout", "count": 9}]
    assert deterministic_archetype(pooled) == "gig"          # pooled streams keep their settled descriptions
    assert deterministic_archetype(_f(streams=[_s("Consulting invoice payment")], pool=True)) == "freelance"
    assert deterministic_archetype(_f(streams=[_s("Primary household salary"), _s("Second household income", "event_2")])) == "mixed_household"
    assert deterministic_archetype(_f(streams=[_s("Payroll credit"), _s("Website project payment", "event_2")])) == "salaried_plus_side"
    assert deterministic_archetype(_f(streams=[_s("Payroll credit")], one_off=[{"id": "event_9", "description": "Monthly sales commission"}])) == "salaried_plus_variable"
    assert deterministic_archetype(_f(streams=[_s("Payroll credit")], notes=["event_5: final employer payroll; no salary forecast"])) == "transition"


def test_validate_accepts_only_the_closed_schema():
    f = _f(streams=[_s("Payroll credit")], facts=[{"id": "message_3", "kind": "salary_increase"}])
    good = '{"archetype": "salaried", "adjustment": 1, "confidence": 0.8, "rationale": "Regular payroll with a confirmed raise.", "evidence_ids": ["event_1", "message_3"]}'
    out = validate(good, f)
    assert out == {"archetype": "salaried", "adjustment": 1, "confidence": 0.8, "rationale": "Regular payroll with a confirmed raise.", "evidence_ids": ["event_1", "message_3"]}
    assert validate("```json\n" + good + "\n```", f) == out              # fenced output still parses
    assert validate(good.replace('"salaried"', '"banker"'), f) is None    # unknown archetype
    assert validate(good.replace('"adjustment": 1', '"adjustment": 3'), f) is None      # out of range
    assert validate(good.replace('"adjustment": 1', '"adjustment": true'), f) is None   # bool is not an int
    assert validate(good.replace("event_1", "event_999"), f) is None      # evidence must come from the packet
    assert validate(good.replace("confirmed raise", "raise of 500"), f) is None         # an amount in the rationale
    assert validate(good.replace("confirmed raise", "raise every 17 days"), f) is not None   # small numbers are fine
    assert validate("not json", f) is None


def test_profile_adjustment_is_capped_in_the_score():
    from datetime import date
    import pandas as pd
    from buyorwait.intake import FinancialState, Recurrence
    from buyorwait.score import components
    inc = Recurrence(key="salary/income", category="salary", event_type="income", direction="credit", amount=1000, cadence_days=0,
                     next_date=date(2026, 1, 10), last_event_id="event_1", flexibility="fixed", minimum_allowed_amount=None,
                     occurrences=6, history=[1000] * 6, description="Payroll credit")
    st = FinancialState(user_id="u", request_date=date(2026, 1, 1), currency="EUR", balance=5000, minimum=500,
                        recurring=[inc], fixed_flows=[], notes=[], facts=[], events=pd.DataFrame())
    base = components(st)["income_reliability"]
    st.profile = {"archetype": "salaried", "adjustment": -2}
    assert components(st)["income_reliability"] == base - 10
    st.profile = {"archetype": "salaried", "adjustment": 7}   # anything beyond ±2 is clamped
    assert components(st)["income_reliability"] == min(100.0, base + 10)
