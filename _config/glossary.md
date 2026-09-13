# Glossary — Buy or Wait? (Layer 3)

> Canonical definitions live in `problem_statement.md` (challenge terms), `AGENTS.md` §6 (contract) and `docs/AGENTIC.md` (runtime). This is the quick reference.

| Term | Meaning |
|---|---|
| request | One row of `dataset/requests.csv`: user, date, type, amount, desired completion date, partial allowed. |
| profile | One row of `financial_profiles.csv`: balance, `minimum_balance_to_keep`, priorities, protected / reducible / stoppable categories, accepted methods, `max_installment_months`. |
| state | `intake.FinancialState`: balance, minimum, recurring streams, reserved flows, notes, facts, as of `request_date`. |
| stream | `intake.Recurrence`: a recurring debit or credit detected from settled history (amount, cadence, next date, flexibility, protected). |
| reserved flow | A pending debit, scheduled row, approved invoice or arrears placed on its date before anything is judged affordable. |
| fact | `evidence.Fact`: a narrow, rule-validated fact extracted from a message (untrusted text) or an image cache entry. |
| projection | `forecast.projection`: 84-day daily balance path; the safety rule is that it never drops under the minimum. |
| headroom | projected trough minus `minimum_balance_to_keep`; the liquidity basis of the score and of the gate. |
| hurt | 100 × requested / headroom (clipped at 100); how much of the headroom the request consumes. |
| safe today | `amount_safe_to_pay`: largest amount payable on `request_date`, capped at the request, keeping the path above the minimum; no spending changes. |
| earliest | `earliest_date_for_full_payment`: first day one full payment is safe; empty when none exists in the window. |
| plan | `plans.Plan`: method, dated payments, changes, option id; ranked by the six rules (deadline, no changes, total, start, count, option id). |
| change | `plans.Change`: `stop:<event_id>` or `reduce_to:<event_id>:<amount>` on a flexible, non-protected, permitted stream. |
| card | `agent.cards.AccountCard`: one user's compact memory (profile prefs, state, no-message state, options, score, factors). |
| card store / disk tables | `CardStore` (cards in RAM, `.cache/cards.jsonl`) and `DiskTables` (byte-range index per user/request, `.cache/index/`, read by section). |
| recall | The historian's first step: this user's card (or `build_state` over the dataset) as of the request date. |
| gate | `score_gate`: settles `affordable_now` / `not_affordable` from headroom when exact; otherwise routes to the plan search. |
| worker | historian, forecaster, planner, scorer, auditor, explainer; each owns tools and reports findings + flags. |
| reflection | Seven goal checks + concerns + confidence the orchestrator runs after the analyse phase; may re-plan. |
| ledger / transcript | Every tool call recorded on the user's memory; written per request to `.cache/agent_transcripts.jsonl`. |
| packet | `explain.decision_packet`: the only thing the explanation model sees. |
| Spending Score / Expense Impact | `score.py`: six 0–100 components + composite; the same re-scored with the plan injected. |
| Spending / Stability Factor | `factors.py`: cross-user two-factor profile with bands; never a contract column. |
