# Buy or Wait? — Routing (ICM Layer 1)

## Session start

1. Read `AGENTS.md` (rules, contract, logging) and append the `SESSION START` log entry it requires.
2. Read `IDENTITY.md` for the map. Do not explore the tree; route from the table below.
3. Load only the `_config/` file the task needs (`conventions.md`, `glossary.md`, `voice.md`).

## What do you want to do?

| Task | Go to | Load first |
|---|---|---|
| Produce / regenerate `output.csv` | `code/main.py` (`python3 code/main.py`) | `_config/conventions.md` |
| Understand one request end to end | `python3 code/main.py --explain <request_id> [--samples]` | `docs/AGENTIC.md` §2 |
| Change how a worker decides or what a tool returns | `code/buyorwait/agent/CONTEXT.md` → `tools.py`, `workers.py`, `orchestrator.py` | `_config/glossary.md` |
| Change memory (cards, disk sections, recall) | `code/buyorwait/agent/cards.py`, `store.py`, `memory.py` | `docs/AGENTIC.md` §1 |
| Change the financial rules (recurrence, forecast, plans, ranking) | `code/buyorwait/CONTEXT.md` → `intake.py`, `forecast.py`, `plans.py` | `docs/DECISIONS.md` D6, D11 |
| Change the score or factors | `code/buyorwait/score.py`, `factors.py` | `docs/SCORING_FACTORS.md`, `docs/ABSORPTION.md` |
| Change the explanation prompt or grounding guard | `code/buyorwait/explain.py` | `_config/voice.md` |
| Check accuracy on the 25 solved samples | `code/evaluation/score_samples.py` | `tests/test_engine_samples.py` (floors) |
| Build the usage report / cost | `code/evaluation/build_usage_report.py` (reads `.cache/traces.jsonl`) | `AGENTS.md` §6.5 |
| See what the orchestrator did across the run | `code/evaluation/run_reflection.md` (rebuild: `build_run_reflection.py`) | `docs/AGENTIC.md` §5 |
| Package for submission | `code/package.py` → `code.zip` | `AGENTS.md` §6.5 |
| Record a design decision | `docs/DECISIONS.md` (append `D<n>`) | `_config/voice.md` |
| Prepare the interview | `docs/INTERVIEW.md`, `docs/DECISIONS.md` | `docs/AGENTIC.md` |

## Pipeline: one request through the orchestrator (virtual stages = workers)

`Orchestrator.handle(request)` sets a **Goal** from the request and the profile, plans the steps below (rule planner; `--planner llm` proposes, the validator enforces prerequisites), runs them over the user's **memory**, **reflects** against the goal, re-plans on an audit failure, then delivers. Stage contracts with exact inputs, tools, outputs and flags: `code/buyorwait/agent/CONTEXT.md`.

| # | Stage (worker) | Reads | Writes (working memory) | Routing |
|---|---|---|---|---|
| 1 | historian | card (or dataset on a miss), disk sections on demand | `state`, `change_candidates`, evidence findings | → 2 |
| 2 | forecaster | `state` | `projection`, `safe_today`, `earliest` | → 3 |
| 3 | planner | `state`, `safe_today`, `earliest`, options, changes | `gate`, `candidates`, `decision` | gate settles → 4; else search → 4 |
| 4 | scorer | `state`, `decision` | `score`, `impact`, `factors` | → 5 |
| 5 | auditor | `decision`, `state` | `counterfactual`, `chosen_plan_unsafe` flag | unsafe → back to 3 (re-rank without it); else → reflect |
| — | reflect (orchestrator) | all reports | `reflection` (checks, concerns, confidence) | ok → 6; replan → 3 |
| 6 | explainer | `decision`, `score`, `impact`, `reflection` | `packet`, `explanation`, `usage` | → 7 |
| 7 | auditor | rendered row | `row`, `audit_errors` | contract OK → output row |

Review gate for a human: `--explain <request_id>` prints goal, plan, gate, findings, reflections and the ledger for one request; `.cache/agent_transcripts.jsonl` holds them for a full run.

## Shared config (Layer 3)

| File | Contents |
|---|---|
| `_config/conventions.md` | Folders, caches, code rules, commands, design-log rule (re-export of `AGENTS.md` + `README.md`) |
| `_config/glossary.md` | Domain and runtime terms: state, stream, headroom, hurt, card, gate, worker, reflection, packet |
| `_config/voice.md` | `decision_explanation` style (from `explain.py`), engineer-facing docs style, log-entry rules |
