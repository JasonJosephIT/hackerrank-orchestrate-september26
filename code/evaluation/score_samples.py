"""Per-field match of code/evaluation/sample_output.csv against dataset/sample_requests.csv.

    python3 code/main.py --samples --no-llm && python3 code/evaluation/score_samples.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FIELDS = ["amount_safe_to_pay", "affordability_status", "recommended_payment_method", "payment_plan",
          "earliest_date_for_full_payment", "spending_changes_needed"]


def _num(s):
    try:
        return float(s)
    except ValueError:
        return None


def same(field, a, b):
    if field == "amount_safe_to_pay":
        x, y = _num(a), _num(b)
        return x is not None and y is not None and abs(x - y) <= max(0.01, 0.01 * abs(y))
    if field == "payment_plan":
        if a == b:
            return True
        pa, pb = a.split("|"), b.split("|")
        if len(pa) != len(pb):
            return False
        for u, v in zip(pa, pb):
            if ":" not in u or ":" not in v:
                return False
            du, au = u.split(":"); dv, av = v.split(":")
            if du != dv or abs(float(au) - float(av)) > 0.006:
                return False
        return True
    return a == b


def main():
    truth = {r["request_id"]: r for r in csv.DictReader(open(ROOT / "dataset" / "sample_requests.csv", encoding="utf-8"))}
    mine = {r["request_id"]: r for r in csv.DictReader(open(ROOT / "code" / "evaluation" / "sample_output.csv", encoding="utf-8"))}
    hits = {f: 0 for f in FIELDS}
    rows = []
    for rid, t in truth.items():
        m = mine.get(rid)
        if not m:
            continue
        marks = []
        for f in FIELDS:
            ok = same(f, m[f], t[f])
            hits[f] += ok
            marks.append("." if ok else "X")
        rows.append((rid, "".join(marks), m, t))
    n = len(rows)
    print("field match (n=%d): " % n + "  ".join(f"{f.split('_')[0]}={hits[f]}" for f in FIELDS))
    print("row all-6 exact:", sum(1 for r in rows if "X" not in r[1]))
    verbose = "-v" in sys.argv
    for rid, marks, m, t in rows:
        if "X" in marks:
            print(f"{rid} {marks}")
            for f, mk in zip(FIELDS, marks):
                if mk == "X" or verbose:
                    print(f"    {f:32s} mine={m[f]!r:45s} truth={t[f]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
