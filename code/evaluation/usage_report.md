# Token Usage and Cost Analysis

Run recorded in `traces_qwen_partial.jsonl`: 2026-09-13 08:18 UTC · 224 requests processed (OpenTelemetry spans, `gen_ai.*` attributes). `python3 code/main.py` writes this file only on a full run; `--samples`, `--explain` and `--limit` use separate trace files.

The deterministic engine (intake, forecast, plans, verification) makes no model calls. In the default agentic run (D13) the LLM writes `decision_explanation` only; a template fallback is used when a call fails, so fallbacks are listed too. The orchestrator's LLM planner and critique are opt-in and appear as separate stages when used.

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) | Fallbacks |
|---|---|---|---|---|---|---|---|
| Groq | qwen/qwen3.8-27b | 216 | 194,596 | 11,056 | 205,652 | 0.0630 | 48 |
| **Overall** | | 216 | 194,596 | 11,056 | 205,652 | 0.0630 | 48 |

| Stage | Calls | Input tokens | Output tokens |
|---|---|---|---|
| decision_explanation (explainer worker) | 224 | 194,596 | 11,056 |

- Requests: 224
- Model calls per request: 0.96
- Average tokens per request: 918.1 (input 868.7, output 49.4)
- Estimated total cost: USD 0.0630 · per request: USD 0.000281

Prices: Groq list prices per 1M tokens — allam-2-7b $0.00 in / $0.00 out; openai/gpt-oss-120b $0.15 in / $0.60 out; openai/gpt-oss-20b $0.10 in / $0.50 out; qwen/qwen3.8-27b $0.29 in / $0.59 out; llama-3.3-70b-versatile $0.59 in / $0.79 out; meta-llama/llama-4-scout-17b-16e-instruct $0.11 in / $0.34 out. The 16 image amounts were extracted once into `code/evidence/image_facts.json` (vision pass, hand-verified) and are read from that cache during the run, so they add no per-run tokens.

## Run note

The final output.csv was produced in two steps on 2026-09-13 because the Groq daily token quotas of openai/gpt-oss-120b (the default model) and openai/gpt-oss-20b were exhausted: (1) a full run with BUYORWAIT_EXPLAIN_MODEL=qwen/qwen3.8-27b served 224 of 250 requests before hitting that model's daily cap (the spans above: 176 grounded model explanations, 40 rejected by the grounding guard, 8 rate-limit fallbacks, 205,652 tokens); (2) python3 code/main.py --no-llm --reuse-explanations <transcripts of step 1> recomputed every contract column deterministically, re-verified each of the 176 model explanations against the freshly built packet and reused them, and wrote the template explanation for the other 74 rows. No further model calls were made in step 2, so this table is the complete model usage behind output.csv.
