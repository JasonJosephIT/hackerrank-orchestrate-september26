import csv
from pathlib import Path

from buyorwait.verify import COLUMNS, check_row, load_context, verify_rows

ROOT = Path(__file__).resolve().parent.parent
DS = ROOT / "dataset"


def _ctx():
    return load_context(DS, "sample_requests.csv")


def _sample_rows():
    with open(DS / "sample_requests.csv", newline="", encoding="utf-8") as f:
        return [{c: r[c] for c in COLUMNS} for r in csv.DictReader(f)]


def test_samples_pass_contract():
    assert verify_rows(_sample_rows(), _ctx()) == []


def test_bounds_and_enums():
    ctx = _ctx()
    row = _sample_rows()[0]
    bad = dict(row, amount_safe_to_pay="999999999")
    assert any("outside" in e for e in check_row(bad, ctx))
    bad = dict(row, affordability_status="maybe")
    assert any("bad status" in e for e in check_row(bad, ctx))


def test_affordable_now_needs_request_date():
    ctx = _ctx()
    row = dict(_sample_rows()[0], earliest_date_for_full_payment="2024-03-04")
    assert any("affordable_now requires earliest" in e for e in check_row(row, ctx))


def test_installments_must_match_option():
    ctx = _ctx()
    rows = {r["request_id"]: r for r in _sample_rows()}
    row = dict(rows["request_02"], payment_plan="2025-08-08:15952906.67|2025-09-07:15952906.67")
    assert any("does not match" in e for e in check_row(row, ctx))


def test_partial_arithmetic():
    ctx = _ctx()
    rows = {r["request_id"]: r for r in _sample_rows()}
    row = dict(rows["request_19"], payment_plan="2024-09-04:28820|2024-09-15:10000")
    assert any("partial plan" in e for e in check_row(row, ctx))


def test_spending_change_rules():
    ctx = _ctx()
    rows = {r["request_id"]: r for r in _sample_rows()}
    row = dict(rows["request_21"], spending_changes_needed="stop:event_1816|reduce_to:event_1816:23.50")
    assert any("same event" in e for e in check_row(row, ctx))
    row = dict(rows["request_21"], spending_changes_needed="reduce_to:event_1816:10")
    assert any("below minimum_allowed_amount" in e for e in check_row(row, ctx))
    row = dict(rows["request_21"], spending_changes_needed="stop:event_1807")  # rent: fixed
    assert any("not flexible" in e for e in check_row(row, ctx))
