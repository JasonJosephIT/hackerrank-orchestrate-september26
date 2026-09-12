# Token Usage and Cost Analysis

Final full-dataset run: 2026-09-12 18:52 UTC · 250 requests processed · source: `.cache/traces.jsonl` (OpenTelemetry spans, `gen_ai.*` attributes).

The deterministic engine (intake, forecast, plans, verification) makes no model calls. The LLM writes `decision_explanation` only; a template fallback is used when a call fails, so fallbacks are listed too.

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) | Fallbacks |
|---|---|---|---|---|---|---|---|
| **Overall** | | 0 | 0 | 0 | 0 | 0.0000 | 0 |

> **This run made no model calls** (no `GROQ_API_KEY`, `--no-llm`, or the network blocked `api.groq.com`): every `decision_explanation` came from the deterministic template. Re-run `python3 code/main.py` with the key available, then this script, to record the LLM usage of the final run.

- Requests: 250
- Model calls per request: 0.00
- Average tokens per request: 0.0 (input 0.0, output 0.0)
- Estimated total cost: USD 0.0000 · per request: USD 0.000000

Prices: Groq list prices per 1M tokens — llama-3.3-70b-versatile $0.59 in / $0.79 out; llama-4-scout-17b-16e-instruct $0.11 in / $0.34 out. The 16 image amounts were extracted once into `code/evidence/image_facts.json` (vision pass, hand-verified) and are read from that cache during the run, so they add no per-run tokens.
