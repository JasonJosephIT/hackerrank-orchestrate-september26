# Token Usage and Cost Analysis

Run recorded in `traces_samples.jsonl`: 2026-09-12 19:09 UTC · 25 requests processed (OpenTelemetry spans, `gen_ai.*` attributes). `python3 code/main.py` writes this file only on a full run; `--samples`, `--explain` and `--limit` use separate trace files.

The deterministic engine (intake, forecast, plans, verification) makes no model calls. The LLM writes `decision_explanation` only; a template fallback is used when a call fails, so fallbacks are listed too.

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) | Fallbacks |
|---|---|---|---|---|---|---|---|
| Groq | openai/gpt-oss-120b | 25 | 14,938 | 3,621 | 18,559 | 0.0044 | 0 |
| **Overall** | | 25 | 14,938 | 3,621 | 18,559 | 0.0044 | 0 |

- Requests: 25
- Model calls per request: 1.00
- Average tokens per request: 742.4 (input 597.5, output 144.8)
- Estimated total cost: USD 0.0044 · per request: USD 0.000177

Prices: Groq list prices per 1M tokens — openai/gpt-oss-120b $0.15 in / $0.60 out; openai/gpt-oss-20b $0.10 in / $0.50 out; llama-3.3-70b-versatile $0.59 in / $0.79 out; meta-llama/llama-4-scout-17b-16e-instruct $0.11 in / $0.34 out. The 16 image amounts were extracted once into `code/evidence/image_facts.json` (vision pass, hand-verified) and are read from that cache during the run, so they add no per-run tokens.
