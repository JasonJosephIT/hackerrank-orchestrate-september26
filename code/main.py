"""Buy or Wait? — entry point.

    python3 code/main.py                # dataset/requests.csv -> ./output.csv (repo root)
    python3 code/main.py --samples      # dataset/sample_requests.csv -> code/evaluation/sample_output.csv
    python3 code/main.py --no-llm       # template explanations only (no API calls)
    python3 code/main.py --explain request_42   # print the agent transcript, decision packet + score report for one request
    python3 code/main.py --pipeline     # legacy linear pipeline instead of the orchestrator (same columns)

Default runtime (D13): an orchestrator per request sets a goal from the request and the user's criteria, plans which
tools its workers (historian, forecaster, planner, scorer, auditor, explainer) run, executes them over the user's
memory, reflects on the outcome against the goal and re-plans on an audit failure. Every tool wraps the deterministic
engine (intake -> forecast -> plans -> score -> verify), so the contract columns are identical to `--pipeline`.
The LLM only writes decision_explanation (and, opt-in, plans or critiques); every other column is computed deterministically.
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
from buyorwait.agent.cards import CardStore  # noqa: E402
from buyorwait.agent.memory import LongTermMemory  # noqa: E402
from buyorwait.agent.orchestrator import Orchestrator  # noqa: E402
from buyorwait.agent.store import DiskTables  # noqa: E402
from buyorwait.evidence import image_amounts  # noqa: E402
from buyorwait.explain import decision_packet, llm_explanation, template_explanation  # noqa: E402
from buyorwait.forecast import projection  # noqa: E402
from buyorwait.intake import Dataset, build_state  # noqa: E402
from buyorwait.plans import decide, request_from_row  # noqa: E402
from buyorwait.render import render_row  # noqa: E402,F401  (re-exported for tests)
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


def run_one_pipeline(ds, facts, row, use_llm: bool, tracer, client=None):
    """Legacy linear pipeline (kept as the parity oracle for the orchestrator)."""
    req = request_from_row(row)
    with tracer.span("buyorwait.request", request_id=req.request_id, user_id=req.user_id,
                     request_type=req.request_type, requested_amount=req.amount) as root:
        with tracer.span("intake"):
            state = build_state(ds, req.user_id, req.request_date, facts, request_id=req.request_id)
        with tracer.span("forecast+plans") as sp:
            dec = decide(ds, state, req)
            sp.set(amount_safe_to_pay=dec.safe_today, earliest=str(dec.earliest), status=dec.status, method=dec.method,
                   plan_candidates=len(dec.candidates))
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
        explanation, usage = None, {}
        if use_llm:
            with tracer.span("explain") as sp:
                explanation, usage = llm_explanation(packet, client=client)
                sp.set_genai("groq", usage, fallback_used=explanation is None)
        if not explanation:
            explanation = template_explanation(dec)
        root.set(final_status=dec.status)
    return dec, render_row(dec, explanation), packet, usage


def _explanation_cache(path: Path) -> dict:
    """request_id -> explanation for rows of an earlier transcript whose explanation came from the model (not the template)."""
    out, model = {}, None
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue                      # a partial last line from an interrupted run
            led = [e for e in t.get("ledger", []) if e.get("tool") == "write_explanation"]
            if led and str(led[-1].get("summary", "")).startswith("LLM") and t.get("row", {}).get("decision_explanation"):
                out[t["request_id"]] = t["row"]["decision_explanation"]
    out["__model__"] = os.environ.get("BUYORWAIT_EXPLAIN_MODEL", "openai/gpt-oss-120b")
    return out


def _rows(disk: DiskTables, samples: bool):
    """The request rows for this run, read once from disk (250 or 25 small rows)."""
    import pandas as pd
    table = "samples" if samples else "requests"
    idx = disk.index(table)
    df = pd.read_csv(DATASET / idx["file"], dtype=str, keep_default_na=False)
    df["requested_amount"] = df["requested_amount"].astype(float)
    return df


def _cards(ds, facts, rows_in, mode: str, rebuild: bool):
    """Account cards (D14): load the cached store when it covers every request, else load the dataset once,
    build and save it. Returns (store, dataset-or-None)."""
    path = ROOT / ".cache" / ("cards.jsonl" if mode in ("full", "partial") else f"cards_{mode}.jsonl")
    if mode == "explain":
        path = ROOT / ".cache" / "cards.jsonl"
    need = {r.request_id for r in rows_in.itertuples(index=False)}
    if path.exists() and not rebuild:
        try:
            store = CardStore.load(path)
            if need <= set(store.by_request):
                print(f"cards: loaded {len(store)} from {path.relative_to(ROOT)} ({path.stat().st_size / 1e3:.0f} KB)", file=sys.stderr)
                return store, ds
        except Exception as e:
            print(f"cards: cache unusable ({e}); rebuilding", file=sys.stderr)
    ds = ds or Dataset.load(DATASET)
    rows = list(ds.requests.itertuples(index=False)) + list(ds.samples.itertuples(index=False)) if mode != "samples" else list(rows_in.itertuples(index=False))
    store = CardStore.build(ds, facts, rows)
    size = store.save(path)
    st = store.stats()
    print(f"cards: built {st['cards']} in {st['built_in_s']}s ({size / 1e3:.0f} KB, avg {st['avg_streams']} streams/user) -> {path.relative_to(ROOT)}",
          file=sys.stderr)
    return store, ds


def run_one(orch: Orchestrator, row):
    """Agentic path: the orchestrator serves the request end to end."""
    res = orch.handle_row(row)
    return res.decision, res.row, res.packet, res.usage, res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", action="store_true", help="run on dataset/sample_requests.csv instead")
    ap.add_argument("--no-llm", action="store_true", help="skip Groq; template explanations only")
    ap.add_argument("--explain", metavar="REQUEST_ID", help="print the decision packet for one request and exit")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pipeline", action="store_true", help="legacy linear pipeline instead of the orchestrator")
    ap.add_argument("--planner", choices=["rules", "llm"], default=None, help="orchestrator planner (default: BUYORWAIT_AGENT_PLANNER or rules)")
    ap.add_argument("--llm-reflect", action="store_true", help="add a model critique to the orchestrator's reflection (advisory only)")
    ap.add_argument("--reuse-explanations", metavar="TRANSCRIPTS", help="reuse model-written explanations from an earlier run's agent_transcripts.jsonl (re-verified against the new packet; template for the rest)")
    ap.add_argument("--build-cards", action="store_true", help="rebuild the account cards (.cache/cards*.jsonl) even if present")
    ap.add_argument("--no-cards", action="store_true", help="serve requests from the dataset instead of account cards")
    ap.add_argument("--load-dataset", action="store_true", help="keep the full dataset in RAM at request time (default: cards in RAM, tables on disk)")
    args = ap.parse_args()
    _load_env()

    facts = image_amounts()
    disk = DiskTables(DATASET)
    ds = None                                    # loaded only when something needs the whole table
    rows_in = _rows(disk, args.samples)
    if args.explain:
        rows_in = rows_in[rows_in.request_id == args.explain]
        if rows_in.empty:
            rows_in = _rows(disk, True)
            rows_in = rows_in[rows_in.request_id == args.explain]
    if args.limit:
        rows_in = rows_in.head(args.limit)
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

    requests_file = "sample_requests.csv" if args.samples else "requests.csv"
    orch = None
    if args.pipeline or args.no_cards or args.load_dataset:
        ds = Dataset.load(DATASET)
    if not args.pipeline:
        cards = None
        if not args.no_cards:
            cards, ds = _cards(ds, facts, rows_in, mode, rebuild=args.build_cards)
            if not args.load_dataset:
                ds = None                        # request time: cards in RAM, tables on disk by section
        lt = LongTermMemory(ds, facts, verify_context=load_context(DATASET, requests_file), cards=cards, disk=disk,
                            explanation_cache=_explanation_cache(Path(args.reuse_explanations)) if args.reuse_explanations else None)
        if lt.explanation_cache:
            print(f"explanations: reusing {len(lt.explanation_cache) - 1} model-written explanation(s) from {args.reuse_explanations}", file=sys.stderr)
        print(f"memory: {'cards in RAM, tables on disk' if ds is None else 'cards + full dataset in RAM'}", file=sys.stderr)
        orch = Orchestrator(lt, tracer=tracer, use_llm=use_llm, client=client, planner=args.planner,
                            llm_reflect=True if args.llm_reflect else None)
    transcripts = None
    if orch is not None and mode == "full":
        (ROOT / ".cache").mkdir(exist_ok=True)
        transcripts = (ROOT / ".cache" / "agent_transcripts.jsonl").open("w", encoding="utf-8")

    out_rows, packets, fallbacks, last_error = [], [], 0, None
    for i, row in enumerate(rows_in.itertuples(index=False), 1):
        if orch is None:
            dec, out, packet, usage = run_one_pipeline(ds, facts, row, use_llm, tracer, client)
            res = None
        else:
            dec, out, packet, usage, res = run_one(orch, row)
            if transcripts is not None:
                transcripts.write(json.dumps(res.summary(), default=str) + "\n")
        out_rows.append(out)
        packets.append(packet)
        if use_llm and usage.get("error"):
            fallbacks, last_error = fallbacks + 1, usage["error"]
        if args.explain:
            if res is not None:
                print(json.dumps(res.summary(), indent=2, default=str))
            print(json.dumps(packet, indent=2, default=str))
            print(json.dumps(out, indent=2))
            tracer.flush()
            return 0
        if i % 25 == 0:
            print(f"  {i}/{len(rows_in)}", file=sys.stderr)

    if use_llm and fallbacks:
        print(f"WARNING: {fallbacks}/{len(rows_in)} explanations fell back to the template; last error: {last_error}",
              file=sys.stderr)

    if transcripts is not None:
        transcripts.close()
    if orch is not None:
        import resource
        dstat = disk.stats()
        print(f"disk reads at request time: {dstat['reads']} section read(s), {dstat['bytes'] / 1e3:.0f} KB; "
              f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB", file=sys.stderr)
        if mode == "full":
            tracer.flush()
            from buyorwait.agent.run_reflection import write_report
            rpt = write_report(tracer_path, ROOT / ".cache" / "agent_transcripts.jsonl", ROOT / "code" / "evaluation" / "run_reflection.md")
            print(f"run reflection -> {rpt.relative_to(ROOT)}", file=sys.stderr)
    ctx = load_context(DATASET, requests_file)
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
