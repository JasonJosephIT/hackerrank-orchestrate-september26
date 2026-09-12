"""Independent contract validator for output.csv (AGENTS.md §6.2). Runnable standalone:

    python3 code/buyorwait/verify.py [output.csv] [--self-test]
"""
from __future__ import annotations

import csv
import re
import sys
from datetime import date, timedelta
from pathlib import Path

COLUMNS = ["request_id", "amount_safe_to_pay", "affordability_status", "recommended_payment_method",
           "payment_plan", "earliest_date_for_full_payment", "spending_changes_needed", "decision_explanation"]
STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
PLAN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}:-?\d+(\.\d+)?$")
CHANGE_RE = re.compile(r"^(stop:event_\d+|reduce_to:event_\d+:\d+(\.\d+)?)$")
FLEX_OK = {"reducible", "stoppable", "reducible_or_stoppable"}


def _num(s: str) -> float:
    return float(str(s).replace(",", ""))


def parse_plan(s: str) -> list[tuple[date, float]]:
    if s == "none":
        return []
    out = []
    for part in s.split("|"):
        if not PLAN_RE.match(part):
            raise ValueError(f"bad plan entry {part!r}")
        d, a = part.split(":")
        out.append((date.fromisoformat(d), float(a)))
    return out


def load_context(dataset_dir: Path, requests_file: str = "requests.csv"):
    def rd(n):
        with open(dataset_dir / n, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    ctx = dict(requests={r["request_id"]: r for r in rd(requests_file)},
               profiles={p["user_id"]: p for p in rd("financial_profiles.csv")},
               events={e["event_id"]: e for e in rd("financial_events.csv")},
               options={})
    for o in rd("request_payment_options.csv"):
        ctx["options"].setdefault(o["request_id"], []).append(o)
    return ctx


def option_schedule(o) -> list[tuple[date, float]]:
    first = date.fromisoformat(o["first_payment_date"])
    n = int(o["number_of_payments"])
    if n <= 1:
        return [(first, float(o["payment_amount"]))]
    step = int(float(o["payment_frequency_days"]))
    return [(first + timedelta(days=step * k), float(o["payment_amount"])) for k in range(n)]


def check_row(row: dict, ctx: dict) -> list[str]:
    errs: list[str] = []
    rid = row["request_id"]
    req = ctx["requests"].get(rid)
    if req is None:
        return [f"{rid}: unknown request_id"]
    prof = ctx["profiles"][req["user_id"]]
    requested = _num(req["requested_amount"])
    rd = date.fromisoformat(req["request_date"])
    deadline = date.fromisoformat(req["desired_completion_date"])
    methods = set(prof["payment_methods_user_will_consider"].split("|"))
    try:
        safe = _num(row["amount_safe_to_pay"])
    except ValueError:
        return [f"{rid}: amount_safe_to_pay not numeric"]
    if not (0 <= safe <= requested + 1e-6):
        errs.append(f"{rid}: amount_safe_to_pay {safe} outside [0, {requested}]")
    status, method = row["affordability_status"], row["recommended_payment_method"]
    if status not in STATUSES:
        errs.append(f"{rid}: bad status {status!r}")
    if method not in METHODS:
        errs.append(f"{rid}: bad method {method!r}")
    try:
        plan = parse_plan(row["payment_plan"])
    except ValueError as e:
        return errs + [f"{rid}: {e}"]
    if any(b[0] < a[0] for a, b in zip(plan, plan[1:])):
        errs.append(f"{rid}: payment_plan not chronological")
    ed_raw = row["earliest_date_for_full_payment"]
    ed = date.fromisoformat(ed_raw) if ed_raw else None
    if ed is not None and ed < rd:
        errs.append(f"{rid}: earliest date before request_date")
    if status == "affordable_now" and ed != rd:
        errs.append(f"{rid}: affordable_now requires earliest == request_date")
    if method != "not_recommended" and method not in methods and not (method == "wait" and "full_payment" in methods):
        errs.append(f"{rid}: method {method} not in user's accepted methods {sorted(methods)}")
    if method == "full_payment":
        if status not in ("affordable_now", "affordable_with_plan"):
            errs.append(f"{rid}: full_payment with status {status}")
        if plan != [(rd, requested)] and not (len(plan) == 1 and plan[0][0] == rd and abs(plan[0][1] - requested) < 0.006):
            errs.append(f"{rid}: full_payment plan must be request_date:requested_amount")
        if status == "affordable_now" and row["spending_changes_needed"] != "none":
            errs.append(f"{rid}: affordable_now must not need spending changes")
        if status == "affordable_with_plan" and row["spending_changes_needed"] == "none":
            errs.append(f"{rid}: affordable_with_plan + full_payment needs spending changes")
    elif method == "partial_payment":
        if status != "affordable_with_plan":
            errs.append(f"{rid}: partial_payment requires affordable_with_plan")
        if req["allows_partial_payment"].strip().lower() != "true":
            errs.append(f"{rid}: partial_payment but request disallows partial")
        if not (0 < safe < requested):
            errs.append(f"{rid}: partial_payment requires 0 < safe < requested")
        if ed is None or ed > deadline:
            errs.append(f"{rid}: partial_payment requires earliest <= deadline")
        exp = [(rd, safe), (ed, round(requested - safe, 2))] if ed else None
        if len(plan) != 2 or exp is None or plan[0][0] != rd or plan[1][0] != ed or abs(plan[0][1] - safe) > 0.006 or abs(plan[0][1] + plan[1][1] - requested) > 0.006:
            errs.append(f"{rid}: partial plan must be rd:safe | earliest:(requested-safe)")
    elif method == "installments":
        if status != "affordable_with_plan":
            errs.append(f"{rid}: installments require affordable_with_plan")
        opts = [o for o in ctx["options"].get(rid, []) if o["payment_method"] == "installments"]
        match = None
        for o in opts:
            sched = option_schedule(o)
            if len(sched) == len(plan) and all(a[0] == b[0] and abs(a[1] - b[1]) < 0.006 for a, b in zip(sched, plan)):
                match = o
        if match is None:
            errs.append(f"{rid}: installment plan does not match any supplied option")
        else:
            mm = prof["max_installment_months"].strip()
            if not mm:
                errs.append(f"{rid}: user will not consider installments (max_installment_months blank)")
            elif int(match["number_of_payments"]) > int(float(mm)):
                errs.append(f"{rid}: option exceeds max_installment_months")
    elif method == "wait":
        if status != "affordable_later":
            errs.append(f"{rid}: wait requires affordable_later")
        if ed is None or plan != [(ed, requested)] and not (len(plan) == 1 and plan[0][0] == ed and abs(plan[0][1] - requested) < 0.006):
            errs.append(f"{rid}: wait plan must be earliest:requested_amount")
        if ed is not None and ed <= rd:
            errs.append(f"{rid}: wait requires earliest after request_date")
    elif method == "not_recommended":
        if status != "not_affordable":
            errs.append(f"{rid}: not_recommended requires not_affordable")
        if plan:
            errs.append(f"{rid}: not_recommended must have plan none")
    # spending changes
    sc = row["spending_changes_needed"]
    if sc != "none":
        parts = sc.split("|")
        if len(parts) > 3:
            errs.append(f"{rid}: more than three spending changes")
        seen = {}
        reduce_ok = set(prof["expense_categories_user_is_willing_to_reduce"].split("|"))
        stop_ok = set(prof["expense_categories_user_is_willing_to_stop"].split("|"))
        for p in parts:
            if not CHANGE_RE.match(p):
                errs.append(f"{rid}: bad change {p!r}")
                continue
            kind, eid = p.split(":")[0], p.split(":")[1]
            ev = ctx["events"].get(eid)
            if ev is None or ev["user_id"] != req["user_id"]:
                errs.append(f"{rid}: change references {eid} not owned by user")
                continue
            if ev["flexibility"] not in FLEX_OK:
                errs.append(f"{rid}: {eid} is not flexible")
            if eid in seen and seen[eid] != kind:
                errs.append(f"{rid}: stop and reduce on the same event {eid}")
            seen[eid] = kind
            if kind == "stop":
                if ev["flexibility"] not in ("stoppable", "reducible_or_stoppable"):
                    errs.append(f"{rid}: {eid} not stoppable")
                if ev["category"] not in stop_ok:
                    errs.append(f"{rid}: user unwilling to stop {ev['category']}")
            else:
                new_amt = float(p.split(":")[2])
                if ev["flexibility"] not in ("reducible", "reducible_or_stoppable"):
                    errs.append(f"{rid}: {eid} not reducible")
                if ev["category"] not in reduce_ok:
                    errs.append(f"{rid}: user unwilling to reduce {ev['category']}")
                mn = ev["minimum_allowed_amount"].strip()
                if mn and new_amt < float(mn) - 0.006:
                    errs.append(f"{rid}: reduce below minimum_allowed_amount for {eid}")
                if ev["amount"].strip() and new_amt >= float(ev["amount"]):
                    errs.append(f"{rid}: reduce_to not lower than current amount for {eid}")
    if not row["decision_explanation"].strip():
        errs.append(f"{rid}: empty decision_explanation")
    return errs


def verify_file(path: Path, dataset_dir: Path, requests_file: str = "requests.csv") -> list[str]:
    ctx = load_context(dataset_dir, requests_file)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            return [f"columns must be exactly {COLUMNS}, got {reader.fieldnames}"]
        rows = list(reader)
    errs: list[str] = []
    ids = [r["request_id"] for r in rows]
    if ids != list(ctx["requests"].keys()):
        missing = set(ctx["requests"]) - set(ids)
        extra = set(ids) - set(ctx["requests"])
        dup = len(ids) - len(set(ids))
        errs.append(f"request ids differ from {requests_file}: missing={sorted(missing)[:5]} extra={sorted(extra)[:5]} duplicates={dup}")
    for r in rows:
        errs.extend(check_row(r, ctx))
    return errs


def verify_rows(rows: list[dict], ctx: dict) -> list[str]:
    errs = []
    for r in rows:
        errs.extend(check_row(r, ctx))
    return errs


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent.parent
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--self-test" in sys.argv:
        errs = verify_file(root / "dataset" / "sample_requests.csv", root / "dataset", "sample_requests.csv") if False else []
        # sample_requests.csv carries extra input columns; project it to the output columns first
        import io
        with open(root / "dataset" / "sample_requests.csv", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        ctx = load_context(root / "dataset", "sample_requests.csv")
        errs = verify_rows([{c: r[c] for c in COLUMNS} for r in rows], ctx)
        print("self-test on sample_requests.csv:", "OK" if not errs else "\n".join(errs))
        sys.exit(1 if errs else 0)
    target = Path(args[0]) if args else root / "output.csv"
    errs = verify_file(target, root / "dataset")
    if errs:
        print(f"INVALID ({len(errs)} problems):")
        for e in errs[:200]:
            print(" -", e)
        sys.exit(1)
    print(f"{target}: valid")
