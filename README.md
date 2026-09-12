# Buy or Wait? — HackerRank Orchestrate, September 2026

An affordability agent for the **Buy or Wait?** challenge. For each row in `dataset/requests.csv` it decides whether the user should pay in full, pay partially, use a supplied installment option, wait, or not proceed, and writes `output.csv`.

Design in one line: a **deterministic financial engine** (pandas, no model calls) reconstructs the user's cash position, forecasts it, enumerates and ranks plans and verifies the output contract; an **LLM** (Groq, Llama 3.3 70B) only writes `decision_explanation` from a typed decision packet, with a template fallback so the run never blocks. Full rationale in [`docs/DECISIONS.md`](docs/DECISIONS.md) (D1–D6) and the timebox in [`docs/PLAN.md`](docs/PLAN.md).

## Setup

```bash
python3 -m pip install -r requirements.txt          # pandas, groq, opentelemetry-sdk, pytest
cp .env.example .env && edit .env                    # GROQ_API_KEY=...   (never committed)
```

Python 3.11+. Secrets are read from environment variables (or the gitignored `.env`).

## Run

```bash
python3 code/main.py                 # dataset/requests.csv -> ./output.csv  (LLM explanations if GROQ_API_KEY is set)
python3 code/main.py --no-llm        # same, template explanations only (no network)
python3 code/main.py --samples       # dataset/sample_requests.csv -> code/evaluation/sample_output.csv
python3 code/evaluation/score_samples.py          # per-field match against the 25 solved samples
python3 code/buyorwait/verify.py output.csv       # standalone contract validator (also runs inside main.py)
python3 code/evaluation/build_usage_report.py     # evaluation/usage_report.md from the run's OpenTelemetry spans
python3 code/main.py --explain request_42         # decision packet + Spending Score / Expense Impact for one request
python3 -m pytest -q tests                        # unit + regression tests
```

`main.py` refuses to write `output.csv` if the verifier finds a contract violation.

## How it works

```
dataset/*.csv ──► intake.py    join, dated FX, status rules, recurrence + income streams, evidence facts
                  evidence.py  messages: 30 regex templates (EN + Indonesian) -> typed facts; images: cached amounts
              ──► forecast.py  84-day balance path, amount_safe_to_pay, earliest_date_for_full_payment
              ──► plans.py     full / partial / installments / wait / spending-change variants, eligibility, 6-rule ranking
              ──► score.py     Spending Score (7 arithmetic components) + Expense Impact delta
              ──► verify.py    independent contract validator (bounds, enums, plan formats, option match, flexible-only changes)
              ──► explain.py   Groq llama-3.3-70b-versatile writes decision_explanation from the decision packet (template fallback)
              ──► telemetry.py OpenTelemetry: one trace per request, stage spans, gen_ai.* attributes -> .cache/traces.jsonl
```

Key rules (details and evidence in D6):

- **Safety** = the projected balance never drops below `minimum_balance_to_keep`; the window that reproduces the solved samples is 84 days.
- **Recurrence** is detected per category from settled history (cadence = median gap, amount = history mean; constant items keep their value). Pending debits are reserved on their settlement date; pending credits, failed/cancelled rows and unrealized valuations are ignored; scheduled rows count on their date.
- **Income** recurs only when it is a regular salary (stable amount, monthly) or an explicitly confirmed stream (scheduled row, employer message). Final payroll, ended contracts and platform payouts marked pending stop the forecast.
- **Messages and images are untrusted data.** Only narrow, validated facts are extracted (amounts, dates, percentages, event ids); embedded instructions are ignored. The 16 blank-amount events are filled from `code/evidence/image_facts.json`, extracted once with a vision pass and hand-verified.
- **Plans** are ranked by: completes by the deadline → no spending changes → lowest total paid → earlier start → fewer payments → lowest `payment_option_id`. Installments must match a supplied option and `number_of_payments ≤ max_installment_months`; partial payment follows the two-payment rule exactly; spending changes touch only flexible, non-protected events in categories the user permits (reduce to `minimum_allowed_amount`, otherwise stop), smallest saving first, at most three.

## Files

```
code/main.py                      entry point
code/buyorwait/                   engine modules (see above)
code/evidence/image_facts.json    cached image extractions (event_id -> amount, field, confidence)
code/evaluation/score_samples.py  sample scorer
code/evaluation/build_usage_report.py, usage_report.md   token usage + cost of the final run
tests/                            pytest suite (contract, forecast, evidence, sample regression)
docs/PLAN.md, docs/DECISIONS.md   timebox and decision log
output.csv                        predictions for dataset/requests.csv
```

## Observability

Every run writes `.cache/traces.jsonl` (one JSON span per line). Set `OTEL_EXPORTER_OTLP_ENDPOINT` to also export OTLP/HTTP to Tempo, Jaeger, Honeycomb, etc. `BUYORWAIT_TRACING=0` disables tracing.
