"""Buy or Wait? — entry point.

    python3 code/main.py                # dataset/requests.csv -> ./output.csv (repo root)
    python3 code/main.py --samples      # dataset/sample_requests.csv -> code/evaluation/sample_output.csv
    python3 code/main.py --no-llm       # template explanations only (no API calls)
    python3 code/main.py --explain request_42   # print the decision packet + score report for one request

Pipeline: intake -> evidence -> forecast -> plans -> verify -> explain (LLM, template fallback).
The LLM only writes decision_explanation; every other column is computed deterministically.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))

from buyorwait import telemetry  # noqa: E402
from buyorwait.evidence import image_amounts  # noqa: E402
from buyorwait.explain import decision_packet, llm_explanation, template_explanation  # noqa: E402
from buyorwait.forecast import projection  # noqa: E402
from buyorwait.formatting import fmt_amount, fmt_plain  # noqa: E402
from buyorwait.intake import Dataset, build_state  # noqa: E402
from buyorwait.plans import decide, request_from_row  # noqa: E402
from buyorwait.profile import account_profile, load_cache, save_cache  # noqa: E402
from buyorwait.score import expense_impact, spending_score  # noqa: E402
from buyorwait.verify import COLUMNS, load_context, verify_rows  # noqa: E402

DATASET = ROOT / "dataset"


def _load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def render_row(dec, explanation: str) -> dict:
    st, req, p = dec.state, dec.request, dec.plan
    cur = st.currency
    plan = "|".join(f"{d}:{fmt_amount(a, cur)}" for d, a in p.payments) if p else "none"
    changes = "|".join(c.render(lambda x: fmt_amount(x, cur)) for c in p.changes) if p and p.changes else "none"
    if dec.status == "affordable_now":
        earliest = str(req.request_date)
    elif dec.status == "not_affordable":
        earliest = ""
    else:
        earliest = str(dec.earliest) if dec.earliest else ""
    return {
        "request_id": req.request_id,
        "amount_safe_to_pay": fmt_plain(dec.safe_today),
        "affordability_status": dec.status,
        "recommended_payment_method": dec.method,
        "payment_plan": plan,
        "earliest_date_for_full_payment": earliest,
        "spending_changes_needed": changes,
        "decision_explanation": explanation,
    }


def run_one(ds, facts, row, use_llm: bool, tracer, client=None, cfg: dict | None = None,
            profile_cache: dict | None = None, use_profile: bool = True, refresh_profiles: bool = False):
    req = request_from_row(row)
    with tracer.span("buyorwait.request", request_id=req.request_id, user_id=req.user_id,
                     request_type=req.request_type, requested_amount=req.amount) as root:
        with tracer.span("intake"):
            state = build_state(ds, req.user_id, req.request_date, facts, cfg=cfg, request_id=req.request_id)
        with tracer.span("forecast+plans") as sp:
            dec = decide(ds, state, req)
            sp.set(amount_safe_to_pay=dec.safe_today, earliest=str(dec.earliest), status=dec.status, method=dec.method,
                   plan_candidates=len(dec.candidates))
        with tracer.span("profile") as sp:   # D14: account archetype + capped income-reliability supplement
            state.profile, pusage = account_profile(state, client=client, cache=profile_cache,
                                                    use_llm=use_llm and use_profile, refresh=refresh_profiles)
            sp.set(archetype=state.profile["archetype"], adjustment=state.profile["adjustment"], source=state.profile["source"],
                   cache_hit=bool(pusage.get("cache_hit")))
            if use_llm and use_profile and not pusage.get("cache_hit"):
                sp.set_genai("groq", pusage, fallback_used=state.profile["source"] != "llm")
        with tracer.span("score"):
            score = spending_score(state)
            impact = expense_impact(state, dec)
        min_proj = None
        if dec.plan:
            extra = [(d, -a, "plan") for d, a in dec.plan.payments]
            exclude = {c.event_id for c in dec.plan.changes if c.kind == "stop"}
            overrides = {c.event_id: c.new_amount for c in dec.plan.changes if c.kind == "reduce_to"}
            min_proj = round(min(low for _, _, low in projection(state, extra=extra, exclude=exclude, overrides=overrides)), 2)
        packet = decision_packet(dec, min_proj, {"spending_score": score, "expense_impact": impact})
        packet["account_profile"] = {k: state.profile.get(k) for k in ("archetype", "adjustment", "confidence", "rationale", "evidence_ids", "source")}
        explanation, usage = None, {}
        if use_llm:
            with tracer.span("explain") as sp:
                explanation, usage = llm_explanation(packet, client=client)
                sp.set_genai("groq", usage, fallback_used=explanation is None)
        if not explanation:
            explanation = template_explanation(dec)
        root.set(final_status=dec.status)
    return dec, render_row(dec, explanation), packet, usage


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", action="store_true", help="run on dataset/sample_requests.csv instead")
    ap.add_argument("--no-llm", action="store_true", help="skip Groq; template explanations only")
    ap.add_argument("--explain", metavar="REQUEST_ID", help="print the decision packet for one request and exit")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-profile", action="store_true", help="skip the LLM account-profile stage (rules baseline, adjustment 0)")
    ap.add_argument("--refresh-profiles", action="store_true", help="ignore code/evidence/account_profiles.json and re-query every profile")
    ap.add_argument("--profiles-only", action="store_true",
                    help="build/refresh the account-profile cache for the selected rows and exit; writes no output.csv")
    ap.add_argument("--conservative", action="store_true",
                    help="irregular-income safety levers (income haircut + reserve cushion); also BUYORWAIT_CONSERVATIVE=1")
    args = ap.parse_args()
    _load_env()

    ds = Dataset.load(DATASET)
    facts = image_amounts()
    rows_in = ds.samples if args.samples else ds.requests
    if args.explain:
        rows_in = rows_in[rows_in.request_id == args.explain]
        if rows_in.empty:
            rows_in = ds.samples[ds.samples.request_id == args.explain]
    if args.limit:
        rows_in = rows_in.head(args.limit)
    conservative = args.conservative or os.environ.get("BUYORWAIT_CONSERVATIVE", "0") not in ("", "0", "false", "no")
    cfg = {"conservative_income": True} if conservative else None
    use_llm = not args.no_llm and bool(os.environ.get("GROQ_API_KEY"))
    if not use_llm and not args.no_llm:
        print("GROQ_API_KEY not set: using template explanations", file=sys.stderr)
    # only a full run overwrites the trace file the usage report is built from
    mode = "explain" if args.explain else ("samples" if args.samples else ("partial" if args.limit else "full"))
    tracer_path = ROOT / ".cache" / ("traces.jsonl" if mode == "full" else f"traces_{mode}.jsonl")
    tracer = telemetry.get_tracer(tracer_path)

    client = None
    if use_llm:
        from groq import Groq
        client = Groq(api_key=os.environ["GROQ_API_KEY"])

    profile_cache = load_cache() if use_llm and not args.no_profile else None
    if args.profiles_only:
        if profile_cache is None:
            print("--profiles-only needs GROQ_API_KEY and no --no-llm/--no-profile", file=sys.stderr)
            return 2
        done = 0
        for i, row in enumerate(rows_in.itertuples(index=False), 1):
            req = request_from_row(row)
            state = build_state(ds, req.user_id, req.request_date, facts, cfg=cfg, request_id=req.request_id)
            prof, pusage = account_profile(state, client=client, cache=profile_cache, refresh=args.refresh_profiles)
            done += prof["source"] == "llm"
            if prof["source"] == "llm":
                save_cache(profile_cache)
            print(f"[{i}/{len(rows_in)}] {req.request_id} {req.user_id}: {prof['archetype']} {prof['adjustment']:+d} "
                  f"({'cache' if pusage.get('cache_hit') else prof['source']}{'; ' + pusage['error'] if pusage.get('error') else ''})", file=sys.stderr)
        print(f"profiles from the model: {done}/{len(rows_in)} -> {len(profile_cache)} cached", file=sys.stderr)
        return 0
    out_rows, packets, fallbacks, last_error = [], [], 0, None
    for i, row in enumerate(rows_in.itertuples(index=False), 1):
        dec, out, packet, usage = run_one(ds, facts, row, use_llm, tracer, client, cfg=cfg, profile_cache=profile_cache,
                                          use_profile=not args.no_profile, refresh_profiles=args.refresh_profiles)
        if profile_cache is not None and packet["account_profile"]["source"] == "llm":
            save_cache(profile_cache)   # incremental: a crash or rate-limit stop keeps what was already classified
        out_rows.append(out)
        packets.append(packet)
        if use_llm and usage.get("error"):
            fallbacks, last_error = fallbacks + 1, usage["error"]
        if args.explain:
            print(json.dumps(packet, indent=2, default=str))
            print(json.dumps(out, indent=2))
            tracer.flush()
            return 0
        if i % 25 == 0:
            print(f"  {i}/{len(rows_in)}", file=sys.stderr)

    if use_llm and fallbacks:
        print(f"WARNING: {fallbacks}/{len(rows_in)} explanations fell back to the template; last error: {last_error}",
              file=sys.stderr)

    ctx = load_context(DATASET, "sample_requests.csv" if args.samples else "requests.csv")
    errs = verify_rows(out_rows, ctx)
    if errs:
        print("VERIFY FAILED:\n - " + "\n - ".join(errs[:50]), file=sys.stderr)
        return 2
    target = (ROOT / "code" / "evaluation" / "sample_output.csv") if args.samples else (ROOT / "output.csv")
    with target.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(out_rows)
    (ROOT / "code" / "evaluation").mkdir(exist_ok=True)
    with (ROOT / "code" / "evaluation" / ("sample_packets.jsonl" if args.samples else "packets.jsonl")).open("w") as f:
        for p in packets:
            f.write(json.dumps(p, default=str) + "\n")
    tracer.flush()
    print(f"wrote {target} ({len(out_rows)} rows), verified OK; traces -> {tracer_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
