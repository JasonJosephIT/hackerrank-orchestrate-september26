# Buy or Wait? — HackerRank Orchestrate, September 2026

An affordability agent for the **Buy or Wait?** challenge. For each row in `dataset/requests.csv` it decides whether the user should pay in full, pay partially, use a supplied installment option, wait, or not proceed, and writes `output.csv`.

Design in one line: a **deterministic financial engine** (pandas, no model calls) reconstructs the user's cash position, forecasts it, enumerates and ranks plans and verifies the output contract; an **orchestrator** per request sets a goal from the request and the user's criteria, plans which tools its six workers run, executes them over the user's **account card** (a compact per-user memory built once from the dataset; a **score gate** settles the exact cases before any plan search) and reflects on the result against the goal (re-planning on an audit failure); an **LLM** (Groq, `openai/gpt-oss-120b`; `BUYORWAIT_EXPLAIN_MODEL` overrides) only writes `decision_explanation` from a typed decision packet, with a template fallback so the run never blocks. Full rationale in [`docs/DECISIONS.md`](docs/DECISIONS.md) (D1–D20), the agentic runtime in [`docs/AGENTIC.md`](docs/AGENTIC.md) and the timebox in [`docs/PLAN.md`](docs/PLAN.md).

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
python3 code/main.py --conservative  # irregular-income safety levers: income haircut + reserve cushion (D13; or BUYORWAIT_CONSERVATIVE=1)
python3 code/main.py --no-profile    # skip the LLM account-profile stage (rules-only archetype, no score supplement)
python3 code/main.py --refresh-profiles   # re-query every account profile instead of reading code/evidence/account_profiles.json
python3 code/main.py --samples       # dataset/sample_requests.csv -> code/evaluation/sample_output.csv
python3 code/evaluation/score_samples.py          # per-field match against the 25 solved samples
python3 code/buyorwait/verify.py output.csv       # standalone contract validator (also runs inside main.py)
python3 code/evaluation/build_usage_report.py     # evaluation/usage_report.md from the run's OpenTelemetry spans
python3 code/evaluation/build_run_reflection.py   # evaluation/run_reflection.md: what the orchestrator did across the run (also written after every full run)
python3 code/main.py --explain request_42         # agent transcript (goal, plan, findings, reflection) + decision packet for one request
python3 code/main.py --planner llm --llm-reflect  # opt-in: Groq proposes the tool plan and critiques the decision (advisory only)
python3 code/main.py --pipeline                   # legacy linear pipeline (same contract columns; parity oracle in tests)
python3 code/main.py --build-cards                # rebuild .cache/cards.jsonl (loads the dataset once, then drops it)
python3 code/main.py --load-dataset               # keep the full tables in RAM at request time (default: cards in RAM, tables on disk by section)
python3 code/main.py --reuse-explanations .cache/agent_transcripts.jsonl   # reuse an earlier run's model explanations (re-verified), e.g. after a quota cut-off
python3 -m pytest -q tests                        # unit + regression tests
python3 code/package.py                           # build code.zip for submission (no dataset, secrets or caches)
```

`main.py` refuses to write `output.csv` if the verifier finds a contract violation.

## How it works

```
agent/cards.py        account cards: one compact card per user (state, prefs, options, scores) built once; ~2.8 MB vs 21 MB raw
agent/store.py        disk tables: byte-range index per user/request; one section read on a card miss or a raw-row fetch
agent/memory.py       long-term (cards in RAM, tables on disk) · episodic (this user's card) · working (per request scratchpad + ledger)
agent/orchestrator.py Goal -> plan (rules | llm) -> execute workers -> reflect (7 goal checks, concerns, confidence) -> re-plan -> deliver
agent/workers.py      historian · forecaster · planner (incl. score gate) · scorer · auditor · explainer (findings + flags)
agent/tools.py        21 schema-described tools, each wrapping one of the engine functions below

dataset/*.csv ──► intake.py    join, dated FX, status rules, recurrence + income streams, evidence facts
                  evidence.py  messages: 30 regex templates (EN + Indonesian) -> typed facts; images: cached amounts
              ──► forecast.py  84-day balance path, amount_safe_to_pay, earliest_date_for_full_payment
              ──► plans.py     full / partial / installments / wait / spending-change variants, eligibility, 6-rule ranking
              ──► profile.py   account profile (D14): typed income features -> rules archetype -> Groq JSON (archetype, ±2 adjustment, evidence ids), cached per user
              ──► score.py     Spending Score (7 arithmetic components, income_reliability ± profile supplement) + Expense Impact delta
              ──► verify.py    independent contract validator (bounds, enums, plan formats, option match, flexible-only changes)
              ──► explain.py   Groq openai/gpt-oss-120b (BUYORWAIT_EXPLAIN_MODEL overrides) writes decision_explanation from the decision packet (template fallback, 429 backoff)
              ──► telemetry.py OpenTelemetry: one trace per request, stage spans, gen_ai.* attributes -> .cache/traces.jsonl
```

Key rules (details and evidence in D6):

- **Safety** = the projected balance never drops below `minimum_balance_to_keep`; the window that reproduces the solved samples is 84 days.
- **Recurrence** is detected per category from settled history (cadence = median gap, amount = history mean; constant items keep their value). Pending debits are reserved on their settlement date; pending credits, failed/cancelled rows and unrealized valuations are ignored; scheduled rows count on their date.
- **Income** recurs only when it is a regular salary (stable amount, monthly) or an explicitly confirmed stream (scheduled row, employer message). Final payroll, ended contracts and platform payouts marked pending stop the forecast.
- **Messages and images are untrusted data.** Only narrow, validated facts are extracted (amounts, dates, percentages, event ids); embedded instructions are ignored. The 16 blank-amount events are filled from `code/evidence/image_facts.json`, extracted once with a vision pass and hand-verified.
- **Account profile (D14).** One model call per user classifies the earner (salaried, salaried plus side income, freelance, gig, mixed household, transition, no income) from a typed packet: income streams with cadence and variability, one-off and pending credits, the typed message facts, never raw message text. The answer is validated against a closed schema (archetype enum, adjustment in -2..2, evidence ids that were in the packet) and cached; it moves `income_reliability` by at most ±10 points and gives the explanation its income sentence. It never touches a contract column.
- **Plans** are ranked by: completes by the deadline → no spending changes → lowest total paid → earlier start → fewer payments → lowest `payment_option_id`. Installments must match a supplied option and `number_of_payments ≤ max_installment_months`; partial payment follows the two-payment rule exactly; spending changes touch only flexible, non-protected events in categories the user permits (reduce to `minimum_allowed_amount`, otherwise stop), smallest saving first, at most three.

## Orientation for agents (ICM layer, D18)

`IDENTITY.md` is the workspace map, `CONTEXT.md` routes tasks to files and lists the six worker contracts, `_config/` holds conventions, glossary and voice as re-exports of `AGENTS.md` / this README / `explain.py`, and each code folder carries a short `CONTEXT.md`. `AGENTS.md` remains the authority; `CLAUDE.md` imports both.

## Files

```
code/main.py                      entry point
code/buyorwait/                   engine modules (see above)
code/buyorwait/agent/             orchestrator, workers, tools, memory (docs/AGENTIC.md)
code/evidence/image_facts.json    cached image extractions (event_id -> amount, field, confidence)
code/evidence/account_profiles.json  cached account profiles (user@request_date -> archetype, adjustment, rationale, evidence ids)
code/evaluation/score_samples.py  sample scorer
code/evaluation/build_usage_report.py, usage_report.md   token usage + cost of the final run
tests/                            pytest suite (contract, forecast, evidence, sample regression, agent parity + re-planning)
docs/PLAN.md, docs/DECISIONS.md   timebox and decision log
output.csv                        predictions for dataset/requests.csv
```

## Observability

Every run writes `.cache/traces.jsonl` (one JSON span per line: one trace per request, a span per orchestrator step with worker/tool/flags, an `agent.reflect` span per iteration with the goal checks and confidence, `gen_ai.*` usage on model calls) and, on a full run, `.cache/agent_transcripts.jsonl` (goal, plan, findings, reflections and ledger per request). Set `OTEL_EXPORTER_OTLP_ENDPOINT` to also export OTLP/HTTP to Tempo, Jaeger, Honeycomb, etc. `BUYORWAIT_TRACING=0` disables tracing.
