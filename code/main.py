"""Buy or Wait? — entry point.

Run from the repo root:  python3 code/main.py
Writes ./output.csv (repo root) with one row per request in dataset/requests.csv.

Baseline (block 1): emits a contract-valid, conservative row for every request
(not_affordable / not_recommended) so a submission-shaped artifact exists from hour one.
The real engine replaces `decide()` in later blocks.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "dataset"
OUTPUT = ROOT / "output.csv"

COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]


def load_requests() -> list[dict]:
    with (DATASET / "requests.csv").open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def decide(req: dict) -> dict:
    """Baseline placeholder: safest possible answer. Replaced by the engine in block 2-3."""
    return {
        "request_id": req["request_id"],
        "amount_safe_to_pay": "0",
        "affordability_status": "not_affordable",
        "recommended_payment_method": "not_recommended",
        "payment_plan": "none",
        "earliest_date_for_full_payment": "",
        "spending_changes_needed": "none",
        "decision_explanation": (
            f"Baseline: no safe plan computed yet for the {req['requested_amount']} request."
        ),
    }


def main() -> int:
    requests = load_requests()
    rows = [decide(r) for r in requests]
    with OUTPUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUTPUT} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
