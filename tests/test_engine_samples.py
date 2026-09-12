"""End-to-end: the engine must reproduce the solved samples on the decision columns (regression guard)."""
import csv
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bw_main", ROOT / "code" / "main.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

from buyorwait.evidence import image_amounts  # noqa: E402
from buyorwait.intake import Dataset, build_state  # noqa: E402
from buyorwait.plans import decide, request_from_row  # noqa: E402
from buyorwait.verify import COLUMNS, load_context, verify_rows  # noqa: E402

FLOOR = {"affordability_status": 23, "recommended_payment_method": 24, "payment_plan": 23,
         "earliest_date_for_full_payment": 23, "spending_changes_needed": 22}


def test_samples_regression():
    ds = Dataset.load(ROOT / "dataset")
    facts = image_amounts()
    truth = {r["request_id"]: r for r in csv.DictReader(open(ROOT / "dataset" / "sample_requests.csv", encoding="utf-8"))}
    rows, hits = [], {k: 0 for k in FLOOR}
    for row in ds.samples.itertuples(index=False):
        req = request_from_row(row)
        st = build_state(ds, req.user_id, req.request_date, facts, request_id=req.request_id)
        out = M.render_row(decide(ds, st, req), "x")
        rows.append(out)
        for k in FLOOR:
            hits[k] += out[k] == truth[req.request_id][k]
    within5 = 0
    for out in rows:
        t = float(truth[out["request_id"]]["amount_safe_to_pay"])
        within5 += abs(float(out["amount_safe_to_pay"]) - t) <= 0.05 * max(t, 1)
    assert within5 >= 15, f"amount_safe_to_pay within 5%: {within5} < 15"
    assert verify_rows(rows, load_context(ROOT / "dataset", "sample_requests.csv")) == []
    for k, floor in FLOOR.items():
        assert hits[k] >= floor, f"{k}: {hits[k]} < {floor}"
