"""Build evaluation/usage_report.md from the OpenTelemetry span file of the final run.

    python3 code/evaluation/build_usage_report.py [.cache/traces.jsonl]

Reads gen_ai.* attributes from every LLM span (one per model call), aggregates per model and
overall, and prices them with the public list prices below (USD per 1M tokens).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PRICES = {  # USD per 1M tokens (input, output) — Groq public pricing (console.groq.com), checked 2026-09-12
    "allam-2-7b": (0.0, 0.0),           # POC model (D9): no public list price found on 2026-09-12; reported at 0 until confirmed
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.10, 0.50),
    "qwen/qwen3.8-27b": (0.29, 0.59),   # no public list price found on 2026-09-13; priced at the Qwen3 32B rate as an upper bound
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "meta-llama/llama-4-scout-17b-16e-instruct": (0.11, 0.34),
}
PROVIDER = {"groq": "Groq"}
STAGES = {"explain": "decision_explanation (explainer worker)", "profile": "account profile (scorer worker, D14; cached)",
          "agent.plan": "orchestrator planning (opt-in, --planner llm)",
          "agent.critique": "orchestrator critique (opt-in, --llm-reflect)"}
PRICE_NOTE = "; ".join(f"{m} ${pi:.2f} in / ${po:.2f} out" for m, (pi, po) in PRICES.items())


def main(path: Path, note: str | None = None) -> int:
    spans = [json.loads(l) for l in path.open(encoding="utf-8")] if path.exists() else []
    requests = {s["trace_id"] for s in spans if s["name"] == "buyorwait.request"}
    per_model = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "fallbacks": 0, "provider": "?"})
    per_stage = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0})
    for s in spans:
        a = s.get("attributes", {})
        if "gen_ai.request.model" not in a:
            continue
        st = per_stage[STAGES.get(s["name"], s["name"])]
        st["calls"] += 1
        st["input"] += int(a.get("gen_ai.usage.input_tokens") or 0)
        st["output"] += int(a.get("gen_ai.usage.output_tokens") or 0)
        m = per_model[a["gen_ai.request.model"]]
        m["provider"] = PROVIDER.get(a.get("gen_ai.system"), a.get("gen_ai.system") or "?")
        inp, outp = int(a.get("gen_ai.usage.input_tokens") or 0), int(a.get("gen_ai.usage.output_tokens") or 0)
        if a.get("buyorwait.fallback_used"):
            m["fallbacks"] += 1          # template used; any tokens a failed/empty call consumed are still billed
        if inp or outp or not a.get("buyorwait.fallback_used"):
            m["calls"] += 1
        m["input"] += inp
        m["output"] += outp
    n_req = len(requests) or 1
    lines = ["# Token Usage and Cost Analysis", "",
             f"Run recorded in `{path.name}`: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
             f"{len(requests)} requests processed (OpenTelemetry spans, `gen_ai.*` attributes). "
             f"`python3 code/main.py` writes this file only on a full run; `--samples`, `--explain` and `--limit` use separate trace files.", "",
             "The deterministic engine (intake, forecast, plans, verification) makes no model calls. In the default agentic "
             "run (D15) the LLM is called for the account profile (D14: archetype + a capped income-reliability supplement, "
             "score/explanation layer only, cached in `code/evidence/account_profiles.json` so a cache hit makes no call) and "
             "to write `decision_explanation`; fallbacks (rules baseline / template) are listed too. The orchestrator's LLM "
             "planner and critique are opt-in and appear as separate stages when used.", "",
             "| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) | Fallbacks |",
             "|---|---|---|---|---|---|---|---|"]
    tot = {"calls": 0, "input": 0, "output": 0, "cost": 0.0, "fallbacks": 0}
    for model, m in sorted(per_model.items()):
        pi, po = PRICES.get(model, (0.0, 0.0))
        cost = m["input"] / 1e6 * pi + m["output"] / 1e6 * po
        lines.append(f"| {m['provider']} | {model} | {m['calls']} | {m['input']:,} | {m['output']:,} | {m['input'] + m['output']:,} | {cost:.4f} | {m['fallbacks']} |")
        for k in ("calls", "input", "output", "fallbacks"):
            tot[k] += m[k]
        tot["cost"] += cost
    lines.append(f"| **Overall** | | {tot['calls']} | {tot['input']:,} | {tot['output']:,} | {tot['input'] + tot['output']:,} | {tot['cost']:.4f} | {tot['fallbacks']} |")
    total_tokens = tot["input"] + tot["output"]
    if tot["calls"] == 0:
        lines += ["", "> **This run made no model calls** (no `GROQ_API_KEY`, `--no-llm`, or the network blocked "
                  "`api.groq.com`): every `decision_explanation` came from the deterministic template. Re-run "
                  "`python3 code/main.py` with the key available, then this script, to record the LLM usage of the final run."]
    if per_stage:
        lines += ["", "| Stage | Calls | Input tokens | Output tokens |", "|---|---|---|---|"]
        lines += [f"| {k} | {v['calls']} | {v['input']:,} | {v['output']:,} |" for k, v in sorted(per_stage.items())]
    lines += ["", f"- Requests: {len(requests)}",
              f"- Model calls per request: {tot['calls'] / n_req:.2f}",
              f"- Average tokens per request: {total_tokens / n_req:,.1f} (input {tot['input'] / n_req:,.1f}, output {tot['output'] / n_req:,.1f})",
              f"- Estimated total cost: USD {tot['cost']:.4f} · per request: USD {tot['cost'] / n_req:.6f}", "",
              f"Prices: Groq list prices per 1M tokens — {PRICE_NOTE}. The 16 image amounts were extracted once into "
              "`code/evidence/image_facts.json` (vision pass, hand-verified) and are read from that cache during the run, "
              "so they add no per-run tokens.", ""]
    if note:
        lines += ["## Run note", "", note, ""]
    out = ROOT / "code" / "evaluation" / "usage_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".cache" / "traces.jsonl"
    n = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(main(p, n))
