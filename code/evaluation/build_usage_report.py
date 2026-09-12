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
PRICES = {  # USD per 1M tokens (input, output) — Groq public pricing, checked Sept 2026
    "openai/gpt-oss-120b": (0.15, 0.75),
    "llama-3.3-70b-versatile": (0.59, 0.79),  # retired on Groq (404 model_not_found) Sept 2026; kept for old traces
    "meta-llama/llama-4-scout-17b-16e-instruct": (0.11, 0.34),
}
PROVIDER = {"groq": "Groq"}


def main(path: Path) -> int:
    spans = [json.loads(l) for l in path.open(encoding="utf-8")] if path.exists() else []
    requests = {s["trace_id"] for s in spans if s["name"] == "buyorwait.request"}
    per_model = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "fallbacks": 0, "provider": "?"})
    for s in spans:
        a = s.get("attributes", {})
        if "gen_ai.request.model" not in a:
            continue
        m = per_model[a["gen_ai.request.model"]]
        m["provider"] = PROVIDER.get(a.get("gen_ai.system", ""), a.get("gen_ai.system", "?"))
        if a.get("buyorwait.fallback_used"):
            m["fallbacks"] += 1
            continue
        m["calls"] += 1
        m["input"] += int(a.get("gen_ai.usage.input_tokens") or 0)
        m["output"] += int(a.get("gen_ai.usage.output_tokens") or 0)
    n_req = len(requests) or 1
    lines = ["# Token Usage and Cost Analysis", "",
             f"Final full-dataset run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
             f"{len(requests)} requests processed · source: `.cache/traces.jsonl` (OpenTelemetry spans, `gen_ai.*` attributes).", "",
             "The deterministic engine (intake, forecast, plans, verification) makes no model calls. The LLM writes "
             "`decision_explanation` only; a template fallback is used when a call fails, so fallbacks are listed too.", "",
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
    lines += ["", f"- Requests: {len(requests)}",
              f"- Model calls per request: {tot['calls'] / n_req:.2f}",
              f"- Average tokens per request: {total_tokens / n_req:,.1f} (input {tot['input'] / n_req:,.1f}, output {tot['output'] / n_req:,.1f})",
              f"- Estimated total cost: USD {tot['cost']:.4f} · per request: USD {tot['cost'] / n_req:.6f}", "",
              "Prices: Groq list prices per 1M tokens — openai/gpt-oss-120b $0.15 in / $0.75 out; llama-3.3-70b-versatile $0.59 in / $0.79 out (retired); "
              "llama-4-scout-17b-16e-instruct $0.11 in / $0.34 out. The 16 image amounts were extracted once into "
              "`code/evidence/image_facts.json` (vision pass, hand-verified) and are read from that cache during the run, "
              "so they add no per-run tokens.", ""]
    out = ROOT / "code" / "evaluation" / "usage_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".cache" / "traces.jsonl"))
