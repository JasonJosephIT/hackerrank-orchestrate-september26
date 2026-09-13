# code/buyorwait/agent — Stage contracts (ICM Layer 2)

## Purpose

The agentic runtime: an orchestrator serves one request at a time over the user's memory, with six workers that own a slice of the tool catalogue. Every tool wraps an engine function in `code/buyorwait/`, so contract columns never depend on this package. Full rationale: `docs/AGENTIC.md`, decisions D15–D17.

## Folder structure

```
agent/
├── memory.py        # LongTermMemory (cards in RAM, DiskTables, optional dataset), UserMemory (recall, working memory, ledger)
├── cards.py         # AccountCard + CardStore: build once, save/load .cache/cards.jsonl, JSON-exact round trip
├── store.py         # DiskTables: byte-range index per user/request (.cache/index/), slice(), dataset_for(), events()
├── tools.py         # 21 schema-described tools (registry); each reads/writes working memory and records a ledger entry
├── workers.py       # Step, WorkerReport, the six workers (interpret tool results into findings + flags)
├── orchestrator.py  # Goal, RulePlanner / LLMPlanner + validate_plan, execute, reflect (+ run log per user), AgentResult
└── run_reflection.py  # run-level reflection over traces + transcripts -> code/evaluation/run_reflection.md
```

## Stage contracts

Each row is the contract a worker fulfils. Inputs are working-memory keys (or the card); outputs are the keys it writes; flags feed the orchestrator's reflection.

| Worker | Tools (in plan order) | Inputs | Outputs | Flags → reflection |
|---|---|---|---|---|
| historian | `recall_user_history`, `list_commitments`, `list_reserved_flows`, `list_evidence`, `fetch_events` (on demand) | card via `UserMemory.card` (miss → `build_card` over `DiskTables.dataset_for`) | `state`, `change_candidates`, `recall_source` | `no_income_forecast`, `structural_deficit`, `evidence_uncertainty`, `image_evidence`, `message_facts_applied` |
| forecaster | `project_balance`, `amount_safe_today`, `earliest_full_payment_date` | `state` | `projection`, `safe_today`, `earliest` | `baseline_breach`, `nothing_safe_today`, `no_full_payment_date` |
| planner | `list_payment_options`, `candidate_spending_changes`, `score_gate`, `enumerate_candidate_plans`, `rank_and_choose` | `state`, `safe_today`, `earliest`, profile + options from the card | `options`, `gate`, `candidates`, `decision` | `no_eligible_option`, `no_safe_plan`, `not_affordable`, `misses_deadline`, `needs_spending_changes` |
| scorer | `account_profile` (D14: cached model call or rules baseline), `spending_score`, `expense_impact`, `account_factors` | `state` (+ the card's profile features), `decision` | `account_profile`, `score`, `impact`, `factors` | `impact_caution`, `impact_unsafe`, `fragile_account` |
| auditor | `check_plan_safety`, `counterfactual_without_messages`, `audit_output_row` | `decision`, `state`, `explanation` | `counterfactual`, `row`, `audit_errors` | `chosen_plan_unsafe` (triggers re-plan), `depends_on_messages`, `contract_violation` |
| explainer | `build_decision_packet`, `write_explanation` | `decision`, `score`, `impact`, `reflection` | `packet`, `explanation`, `usage` | `template_explanation` |

Process rules the orchestrator enforces (`orchestrator.py`): `PREREQS` (what must run before each tool), `MANDATORY` (steps every plan contains), `GATED` (steps skipped when `score_gate` settles the request), `DELIVER` (the three closing steps), `MAX_ITERATIONS = 3` re-plans.

## Routing

| Task | Go to | Load first |
|---|---|---|
| Add a tool | `tools.py` (`@registry.register`, owner = worker) → `orchestrator.PREREQS` → `RulePlanner.plan` | `_config/glossary.md` |
| Change what a worker concludes | `workers.py` `interpret()` (findings + flags) | this file |
| Change a reflection check or the re-plan rule | `orchestrator.py` `reflect()` (span attrs follow automatically) | `docs/AGENTIC.md` §2 |
| Change what the run-level reflection reports | `run_reflection.py` `summarise()` / `render()` | `docs/AGENTIC.md` §5 |
| Change the gate rules | `tools.py` `score_gate` (+ `tests/test_agent.py::test_score_gate_is_exact_against_full_plan_search`) | `docs/DECISIONS.md` D16 |
| Change what a card carries or how sections are read | `cards.py`, `store.py`, `memory.py` | `docs/DECISIONS.md` D17 |
| Prove nothing changed | `python3 -m pytest -q tests/test_agent.py` (parity: pipeline = cards = disk) | — |
