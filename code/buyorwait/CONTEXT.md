# code/buyorwait — Deterministic engine (ICM Layer 2)

## Purpose

Pure Python/pandas, no model calls: reconstruct the user's position, project it, enumerate and rank plans, score, verify. The agentic runtime in `agent/` wraps these functions as tools; `code/main.py --pipeline` runs them linearly as the parity oracle.

## Folder structure

```
buyorwait/
├── intake.py      # Dataset.load / from_frames, dated FX, status rules, recurrence + income streams, message facts -> FinancialState
├── evidence.py    # 30 regex templates (EN + ID) -> typed Facts; image_facts.json cache
├── forecast.py    # 84-day projection, amount_safe_today, earliest_full_payment_date, is_safe
├── plans.py       # Plan/Change/Request, enumerate_plans[_for], candidate_changes, with_changes, choose (six-rule ranking), decide[_for]
├── score.py       # Spending Score (6 components) + Expense Impact; absorption/trust weighting (D12)
├── factors.py     # Cross-user Spending Factor + Stability Factor (vectorised)
├── verify.py      # Independent output-contract validator (also standalone CLI)
├── explain.py     # decision_packet, template_explanation, llm_explanation (Groq, grounding guard, pacing)
├── render.py      # Decision -> output row
├── formatting.py  # fmt_amount / fmt_plain
├── telemetry.py   # OpenTelemetry spans -> .cache/traces*.jsonl (gen_ai.* on model calls)
└── agent/         # Orchestrator, workers, tools, memory (own CONTEXT.md)
```

## Routing

| Task | Go to | Load first |
|---|---|---|
| Recurrence, income streams, message facts | `intake.py` (`build_state`, `_apply_facts`, `DEFAULT_CFG`) | `docs/DECISIONS.md` D6, D11 |
| Safety rule, horizon, payday ordering | `forecast.py` (`HORIZON_DAYS`, `INTRADAY_CHECK`) | `docs/DECISIONS.md` D6, D8 |
| Eligibility, spending changes, ranking | `plans.py` | `AGENTS.md` §6.2–6.3 |
| Output contract check | `verify.py` (`python3 code/buyorwait/verify.py output.csv`) | `AGENTS.md` §6.2 |
| Explanation prompt / guard | `explain.py` | `_config/voice.md` |
| Regression floors on the samples | `tests/test_engine_samples.py` | `code/evaluation/score_samples.py` |
