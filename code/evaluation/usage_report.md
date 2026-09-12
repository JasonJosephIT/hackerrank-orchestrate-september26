# Token Usage and Cost Analysis

Run recorded in `traces.jsonl`: 2026-09-12 21:41 UTC · 250 requests processed (OpenTelemetry spans, `gen_ai.*` attributes). `python3 code/main.py` writes this file only on a full run; `--samples`, `--explain` and `--limit` use separate trace files.

The deterministic engine (intake, forecast, plans, verification) makes no model calls. The LLM writes `decision_explanation` only; a template fallback is used when a call fails, so fallbacks are listed too.

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) | Fallbacks |
|---|---|---|---|---|---|---|---|
| Groq | openai/gpt-oss-120b | 244 | 146,972 | 33,923 | 180,895 | 0.0424 | 6 |
| **Overall** | | 244 | 146,972 | 33,923 | 180,895 | 0.0424 | 6 |

- Requests: 250
- Model calls per request: 0.98
- Average tokens per request: 723.6 (input 587.9, output 135.7)
- Estimated total cost: USD 0.0424 · per request: USD 0.000170

Prices: Groq list prices per 1M tokens — openai/gpt-oss-120b $0.15 in / $0.60 out; openai/gpt-oss-20b $0.10 in / $0.50 out; llama-3.3-70b-versatile $0.59 in / $0.79 out; meta-llama/llama-4-scout-17b-16e-instruct $0.11 in / $0.34 out. The 16 image amounts were extracted once into `code/evidence/image_facts.json` (vision pass, hand-verified) and are read from that cache during the run, so they add no per-run tokens.
