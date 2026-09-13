# DECISIONS.md — Buy or Wait? (HackerRank Orchestrate, Sep 2026)

Append-only log of design decisions. Each entry: what, why, what was rejected. This is the interview script.

## D1 — Architecture: deterministic core, LLM as the final interpretation layer (2026-09-12 13:50 EDT)

**Decision.** The financial engine is pure Python/pandas with no model calls:
1. `intake/` — load + join `dataset/*.csv`, convert foreign-currency cash events with the dated rate, de-duplicate, apply status rules (settled/pending/scheduled/cancelled/failed/unrealized), detect recurring income/expenses from history, resolve conflicts (cancellation > newer same-source > settled > safer).
2. `forecast/` — 90-day daily balance projection from `request_date`; safety = balance never below `minimum_balance_to_keep`. Binary-search `amount_safe_to_pay`; scan dates for `earliest_date_for_full_payment` (both **without** spending changes, per spec).
3. `plans/` — enumerate candidate plans (full now, partial two-payment, each supplied installment option, wait, spending-change variants on flexible events the user permits), filter by eligibility (`payment_methods_user_will_consider`, `max_installment_months`, `allows_partial_payment`, deadline), then rank by the six spec rules.
4. `verify/` — independent contract validator re-checks every output row (bounds, enums, plan format, partial-payment arithmetic, installment schedule match, flexible-only changes, stop/reduce exclusivity) before `output.csv` is written.
5. `explain/` — the ONLY LLM stage on the decision path. Groq `openai/gpt-oss-120b` receives a typed "decision packet" (all computed numbers, the chosen plan, the rejected plans and why, the evidence used) and writes `decision_explanation`. It cannot change any other column; a deterministic template is the fallback when the API is unavailable so the run never blocks.

Evidence layer (messages + images) is also deterministic-first: regex/keyword extraction for the ~5 message templates seen in `messages.csv` (salary amendment, delay, cancellation, confirmation, one-off adjustment), with the LLM used only as a structured-output fallback for messages the rules do not match, validated against a strict schema (event ids must exist, amounts numeric, currencies known). Image amounts (16 payslips/bills, blank `amount` rows) are extracted once into `code/evidence/image_facts.json` via a vision call (Groq Llama-4 Scout) and hand-verified; the pipeline reads the cache so reruns are deterministic and free. All extracted facts carry `source` + `confidence`; message/image text is treated as untrusted data and never as instructions.

**Why.** (a) The scoring is field-exact on 5 of 7 columns; an LLM cannot reliably reproduce a 90-day cash forecast to the cent, a pandas loop can. (b) Determinism = reproducible `output.csv`, cheap reruns, and a defensible "how do you know it's right" answer in the interview. (c) The organizers' own analysis says single-agent + deterministic validators won the May edition. (d) Groq is fast and cheap, so 250 explanation calls cost cents and finish in minutes.

**Rejected.**
- *End-to-end LLM per request* (dump all user rows into a prompt, ask for the 7 columns): non-deterministic, ~25k-token contexts per user, arithmetic errors on `amount_safe_to_pay`, impossible to unit-test.
- *Multi-agent orchestration* (planner/forecaster/critic agents): more moving parts than the problem needs, slower, harder to explain, and the organizers reported simpler systems shipped and scored better.

## D2 — Model choice: Groq + gpt-oss-120b (user decision: Groq; Llama 3.3 70B retired on Groq, switched Sept 12)

Text explanations and message fallback: `openai/gpt-oss-120b` via Groq (reasoning_effort=low; `llama-3.3-70b-versatile` was retired on Groq) (`GROQ_API_KEY` env var). Vision for the 16 images: `meta-llama/llama-4-scout-17b-16e-instruct` on Groq, one-time, cached. Usage report will list both models with calls/tokens/cost from a JSON usage ledger written by the client wrapper on every call.

## D3 — Sample set is the only labeled signal

`dataset/sample_requests.csv` (25 rows, 5 of which also carry images) drives every iteration: `code/evaluation/score_samples.py` reports per-field match rate; every change to the engine records before/after numbers in `log.txt`.

## D4 — Spending Score + Expense Impact layer (user direction, 2026-09-12 14:05 EDT)

**Correction from Jason.** "Interpretation layer" does not mean the LLM paraphrases the engine's answer. Build an arithmetic **Spending Score** from the account facets we are given, an **Expense Impact** score for the requested commitment (including future obligations like a vacation in two months), and let the AI explain *why* in terms of those scores so the user understands their own volatility, not just a yes/no.

**Design.** `code/buyorwait/score.py`, pure arithmetic, every component 0–100, computed from the same reconstructed state the forecast uses (so the numbers never disagree):

| Component | Facet of the account it reads | Formula sketch |
|---|---|---|
| Liquidity buffer | balance, `minimum_balance_to_keep`, projected essential outflow | headroom = (min projected 90-day balance − minimum) / avg monthly essential spend → capped runway months |
| Commitment load | recurring fixed expenses, debt_payment, scheduled/pending debits vs recurring income | 100 × (1 − fixed obligations / income), floored at 0 |
| Spending volatility | monthly discretionary spend history (dining, shopping, entertainment, delivery…) | 100 × (1 − coefficient of variation), clipped |
| Flexibility | share of recurring spend that is `reducible`/`stoppable` AND in a category the user is willing to change | share × 100 |
| Income reliability | salary cadence regularity, confirmed next salary, pending-credit dependence | penalise gaps, unconfirmed/unsettled credits, windfalls |
| Savings behaviour | median monthly net surplus / income | surplus rate × scale |
| Obligation trend | debt_payment and subscription count/amount slope over last 6 months | rising → lower |

Spending Score = weighted mean (weights in a config dict, documented; tuned on the 25 samples so higher score correlates with `affordable_now`, lower with `not_affordable`).

Expense Impact = re-run the same components with the request injected into the 90-day state (full payment on `request_date`, or the chosen installment/partial schedule, or a future obligation at its date) and report the delta per component + composite. Headline "hurt" = amount ÷ available headroom, expressed 0–100, with a threshold band (fine / caution / unsafe) aligned to the safety rule so the two views never contradict.

**Boundary (important for the 30% output score).** The five field-exact columns (`amount_safe_to_pay`, `affordability_status`, `recommended_payment_method`, `payment_plan`, `earliest_date_for_full_payment`, `spending_changes_needed`) are decided by the contract engine and the spec's ranking rules only — hidden ground truth follows those rules, not our score. The score layer (a) orders which flexible expenses to cut first (biggest impact recovery per unit of user pain), (b) feeds the decision packet so the LLM explanation says *which* facet is the problem and what would change the answer, and (c) is exposed as a `--explain <request_id>` CLI report (and, stretch, a small HTML page) showing score, impact, and the AI narrative. Both scores are also written to `code/evaluation/scores.csv` alongside the output for the judge to inspect.

**AI's roles, precisely.** (1) Intake: turn untrusted messages/images into typed facts (schema-validated). (2) Interpretation: given the decision packet — engine decision, plan, score components before/after, top drivers — write a grounded, personalised `decision_explanation` and the longer coaching narrative. It never edits numbers.

**Cost.** ~1.5h in the timebox; placed after the contract engine scores well on samples, before explanations, so it cannot displace the artifact that carries 30% of the grade.

## D5 — Observability with OpenTelemetry (user request, 2026-09-12 14:20 EDT)

**Decision.** Instrument the pipeline with the OpenTelemetry Python SDK (1.44). One trace per request (`buyorwait.request`, attrs: request_id, user_id, request_type, requested_amount, home_currency) with child spans per stage: `intake`, `evidence`, `forecast`, `score`, `plans`, `verify`, `explain`. Every Groq call is a span following the GenAI semantic conventions (`gen_ai.system=groq`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, plus `buyorwait.cache_hit` and `buyorwait.fallback_used`). Key engine numbers are span attributes too (`amount_safe_to_pay`, `min_projected_balance`, `plan_candidates`, `chosen_plan_rank_reason`) so a trace explains a decision.

**Exporters.** Default: a `JsonFileSpanExporter` that appends spans to `.cache/traces.jsonl` — no collector, no network, so the judge can run it as-is. Optional: if `OTEL_EXPORTER_OTLP_ENDPOINT` is set, also export OTLP/HTTP to whatever backend Jason points at (Grafana Tempo, Honeycomb, Jaeger…). Never blocks the run; exporter failures are logged and ignored.

**Why it pays for itself.** `evaluation/build_usage_report.py` derives the mandatory `usage_report.md` (per-model calls, tokens, cost, per-request averages) from the `gen_ai.*` span attributes of the final run — one source of truth instead of a separate ledger. Stage spans give per-stage latency and the cache-hit rate for the token-efficiency story; span attributes on the engine make "why did request_X get this answer" answerable from the trace, which is interview material and a concrete safety/observability mechanism.

**Scope guard.** ~40 min inside block 1 (the tracing wrapper replaces the planned usage ledger). No metrics/logs signals, no collector, no dashboard unless block 6 has time.

## D6 — What the solved samples taught the forecast (2026-09-12, build session)

Calibration was done only against `dataset/sample_requests.csv` (25 rows) with `code/evaluation/score_samples.py`; every rule below moved the per-field match and is a documented deviation from a naive reading of the spec.

| Rule | Evidence | Rejected alternative |
|---|---|---|
| Forecast window = 84 days, not 90 | Sample 13's `earliest_date_for_full_payment` (2024-05-15) is only feasible if the 2024-06-02 rent (day 87) is outside the window; samples 05 and 10 (no income) put the trough before their day-87/88 rent as well | 90 days (earliest-date matches 19/25 vs 22/25) |
| Recurring amount = mean of the settled history (per category+event type), constant items = last value | Item-by-item means reproduce sample 18's pre-salary trough to within 1.6 EUR; last/median/max are further away on the grid | last value, median, 3-month mean |
| Cadence = median gap; a 21-day item's next occurrence is 14 days after the last one when that date is still ahead (then every 21 days), otherwise the plain sequence | Samples 02, 03 and 21 need the 3-week dining/transport item before the next payday; remapping the whole cadence to 14 days breaks samples 07 and 12 | pure 21-day; 21→14 remap |
| Income: a stream recurs only if its amounts are stable (±1% of the median for ≥60% of occurrences) and monthly; a scheduled `Next confirmed salary` row anchors the stream; a `Final employer payroll`, an employment/contract-ended message, or a platform payout-pending message stops it; varying freelance/gig income is forecast at its mean only when nothing marks it uncertain | Samples 01 (prorated first salary + scheduled next), 05 (final payroll), 10 (gig income + payout pending → 0 income), 09 (freelance income counted), 13 (second, varying household income not counted) | forecast every credit stream; forecast none |
| Payday collisions (`INTRADAY_CHECK="periodic"`): an n-day-cadence debit (weekly, fortnightly, …) that lands on a payday is charged before the salary, so the balance after it is safety-checked; a calendar-monthly debit, a pending or a scheduled row on the payday is netted against the salary (end-of-day check only) | Weekly/fortnightly collisions reconcile only pre-salary: sample 13 (weekly transport on 2024-05-15, residual +57.40 EUR end-of-day vs +12.57 pre-salary), sample 18 (fortnightly dining on 2026-07-15, +84.05 vs −1.59), sample 04 moves from +2.2M to +0.7M IDR. Monthly collisions reconcile only netted: sample 02 (+23.8K vs −1.27M IDR), 19 (+1,203 vs −11,447 INR), 22 (−10.85 vs −31.72 EUR), plus 06, 15, 23 in the same direction. Samples 03, 08, 21 have no collision and were unaffected (their earlier "intra-day" fit came from the plan-payment ordering, now the default). `amount_safe_to_pay` exact 10→11, within 5% 16→18, all-six-exact rows 9→10; the one cost is sample 06's earliest date (the second payday's weekly dining now fails by 21 EUR on a path that already sits 80 EUR below the reference) | end-of-day only (16/25 within 5%); every debit pre-salary (`True`: breaks 02, 19, 22; earliest-date 22/25) |
| Message facts that change the forecast: salary increase/reduction/temporary pay/first salary/date moved/employment ended/household income ended/resumes; approved invoice (one-off credit); rent +N% | Samples 06, 07, 08, 11, 14, 15 | LLM free-text interpretation |
| "Confirmed base salary" / "remaining confirmed salary" never raises the salary above settled history | Sample 11's earliest date (2025-07-15) reconciles only with the settled 23.256M base, not the 38.76M stated in the message (safer interpretation, conflict rule 4) | trust the message |
| One-off amounts > 3× the group median are excluded from the recurring level | Sample 17: a 41,272 INR bulk grocery bill (from image_03) otherwise inflates the weekly grocery mean | none |
| Spending changes: candidates = permitted flexible recurrences (reduce → `minimum_allowed_amount`, else stop); accumulate smallest saving first until the plan is safe, then drop any change that is redundant given the others; max 3 | Reproduces samples 06 (one stop), 21 (stop + reduce), 11 (reduce only, the cheaper cloud stop is redundant) | largest-first; single sufficient change |
| Installment eligibility: `number_of_payments ≤ max_installment_months` | All 5 installment samples and every rejected option | payments × frequency ≤ months × 30 |
| Status/method follow the winning plan; `earliest_date_for_full_payment` is blank when the status is `not_affordable` | All 7 not_affordable samples leave it blank | always print the computed date |

**Residual error.** After D6 (with the payday-collision rule) the engine matches the samples on status 24/25, method 24/25, plan 23/25, earliest date 23/25, spending changes 23/25; `amount_safe_to_pay` is exact on 11/25 and within 5% on 18/25 (10 rows exact on all six columns). The remaining gap is the reference generator's private estimate of variable spending (a few percent per category), which flips borderline cases such as sample 06 (a 17 EUR shortfall).

**Environment note.** The build sandbox's egress policy blocks `api.groq.com`, so all runs here used the template explanations; the Groq stage, the OpenTelemetry `gen_ai.*` spans and `build_usage_report.py` are wired and must be exercised in the final local run (`python3 code/main.py`, then `python3 code/evaluation/build_usage_report.py`).

## D7 — Explanation model switched to Groq `openai/gpt-oss-120b` (2026-09-12 19:30 UTC)

**Decision.** When the network opened, Groq's model list no longer contained `llama-3.3-70b-versatile` or the Llama-4 Scout vision model (D2). The strongest text model available to the key is `openai/gpt-oss-120b` ($0.15 / $0.60 per 1M tokens), so `explain.py` defaults to it (`BUYORWAIT_EXPLAIN_MODEL` overrides). It is a reasoning model: hidden reasoning tokens are billed and count against the free tier's 8,000 tokens-per-minute cap, so calls use `reasoning_effort="low"`, a 400-token budget that doubles once if the content comes back empty, and a retry loop that sleeps for the window the 429 response names. The decision packet was trimmed (top three rejected plans, four evidence notes, compact score) to roughly 650 input tokens. Image amounts stay in the hand-verified cache (D1), so no vision model is needed at run time.

## D8 — Block 6 error analysis: the payday-ordering split (2026-09-12 19:55 UTC)

With plan payments ordered last on their day, re-testing `INTRADAY_CHECK=True` (debits charged before the same-day salary) reconciles five samples almost exactly (03: +7,696 IDR, 08: +0.63 EUR, 13: +12.57 EUR, 18: −1.59 EUR, 21: +5.14 USD) but breaks five others that only fit end-of-day balances (06, 15, 19, 23, 25, errors of 3–12% of balance). End-of-day stays the default (earliest-date matches 24 vs 22, within-5% amounts 16 vs 15). The split is not random and is the largest remaining source of `amount_safe_to_pay` error; it is queued as a standalone calibration task (find what distinguishes the two groups: cadence type, scheduled vs projected salary, exact-date coincidence). Everything else in the table is within 1.5% of balance.

**Resolved (2026-09-12, payday-collision session).** The distinguishing feature is the cadence type of the debit that shares the payday, not the salary's origin (scheduled vs projected), the expense type, or the trough day: n-day-cadence items (weekly/fortnightly variable spending) are charged before the salary, calendar-monthly items are netted against it. Samples 03, 08 and 21 belonged to the "intra-day" group only because of the plan-payment ordering, which is now always applied. Implemented as `INTRADAY_CHECK="periodic"` (default; see the D6 row). On `dataset/requests.csv` the rule changes `amount_safe_to_pay` on 41 of 250 rows and a decision column on 5–10 rows, always in the conservative direction (lower safe amount, later date). Evidence scripts: `scratch/payday_rule.py` (per-sample payday flows and residuals under each mode) and `scratch/impact_requests.py`.

## D9 — POC explanation model switched to Groq `allam-2-7b` (2026-09-12, payday-collision session)

**Decision.** On the user's instruction the default `decision_explanation` model is `allam-2-7b` (SDAIA's ALLaM 2 7B on Groq) for the proof of concept; `BUYORWAIT_EXPLAIN_MODEL=openai/gpt-oss-120b` restores D7. It is not a reasoning model, so the `reasoning_effort` branch is skipped and the 400-token budget is spent on content only. A three-sample run through the proxy (34 input / 36 output tokens on a probe, full packets about 650 input tokens) produced valid rows, but the text is looser than gpt-oss-120b: it adds "Recommendation:"/"Explanation:" prefixes, quotes dates, echoes raw score components and runs past the 60-word target. The verifier still accepts the rows (the explanation column is free text). No public list price was found for `allam-2-7b` on the Groq pricing or model pages when checked, so `build_usage_report.py` prices it at 0 and says so; replace the entry once the price is confirmed. Nothing else in the LLM stage changed.

## D10 — Default explanation model restored to `openai/gpt-oss-120b`; branches consolidated (2026-09-12, consolidation session)

**Decision.** The `allam-2-7b` POC (D9) is reverted: `explain.py` defaults to `openai/gpt-oss-120b` again (D7), which is the model behind the committed `output.csv` and `evaluation/usage_report.md`. `allam-2-7b` stays selectable through `BUYORWAIT_EXPLAIN_MODEL`. The three working branches were merged into one line: the D7/D8 engine and the method-aware grounding guard, the payday-collision forecast rule (`INTRADAY_CHECK="periodic"`, D8 resolution) with its tests and scratch evidence, and the proactive token pacing plus Unicode normalisation of the Groq wrapper.

**Consequence.** The committed root `output.csv` predates the payday-collision rule (it was produced before D8 was resolved). The final submission run must be repeated locally with `GROQ_API_KEY` set (`python3 code/main.py`, then `python3 code/evaluation/build_usage_report.py`) so that the contract columns, the explanations and the usage report all come from the same engine.

## D11 — Sample 06: sub-weekly stream due on the request date is not projected (2026-09-12, consolidation session)

**Finding.** Sample 06 was not a ranking miss. The reference chooses `full_payment` today after `stop:event_476` because `wait` only becomes safe on 2026-02-15, after the 2026-01-14 deadline, so rule 1 (complete by the deadline) already prefers the spending-change plan; the engine's ranking agrees. The engine rejected that plan because its pre-payday trough sat 79.46 EUR below the reference (safe amount 523.84 vs 603.30), which made stopping the 19 EUR streaming plan insufficient.

**Diagnosis.** A subset search over the projected items and a grid over the forecast settings both point at user_06's 5-day transport stream (3 occurrences before payday, 80.94 EUR). Dropping it reproduces the reference: safe amount 604.76, status, method, plan, earliest date and spending change all match. Samples 24 and 25 carry the same kind of 5-day transport stream, and sample 24 fits within 1% only when it is kept, so the rule is narrower than "drop sub-weekly streams": in sample 06 the stream's next occurrence falls on `request_date` itself, in 24 and 25 it falls after.

**Decision.** `skip_subweekly_due_today=True` in `intake.DEFAULT_CFG`: a recurring debit with a cadence under 7 days whose next projected occurrence is the request date is treated as day-to-day spending the reference does not carry forward, and is not projected. Affects 8 of the 250 evaluation requests (43 carry a sub-weekly stream at all). Samples: status 25/25, method 25/25, plan 24/25, earliest date 24/25, spending changes 24/25, exact safe amount 12/25, within 5% 19/25, 11 rows exact on all six columns (before: 24/24/23/23/23, 11 exact, 10 rows). Regression floors in `tests/test_engine_samples.py` raised accordingly; unit test in `tests/test_evidence.py`.

**Rejected.** Dropping every sub-weekly stream (sample 24 moves from 0.9% to 21% off); dropping any debit due on the request date (sample 06 overshoots to 620.40 and rent is clearly counted by the reference); changing the variable-amount statistic, the first-gap map or the request-date inclusion flag (none reach the reference for 06 without losing other samples).

## D12 — Absorption and trust: symmetric evidence weighting in the score layer (2026-09-12, from docs/ABSORPTION.md)

**Implemented in `code/buyorwait/score.py`** behind `ABSORPTION = True`, constants next to `WEIGHTS`: `TAU = 3.0` months, `LAMBDA = 0.6` (40% floor weight), `MESSAGE_SEED = 0.25`. `established(r) = (1 − e^(−tenure/TAU)) × stability` with tenure in months (occurrences × cadence / 30, or occurrences for calendar-monthly streams) and stability = 1 − CV of the settled amounts. Expense side: each fixed recurring debit enters `commitment_load` at weight `1 − LAMBDA × established`; the plan injected by `expense_impact()` (installments, partial, full payment) has no history and is added at full weight as a monthly equivalent. Income side: `trusted_income = proven level + increase × trust`, where the proven level is the median of settled history, trust follows the same curve over payslips already settled at the new level and is seeded at 0.25 when only a message or scheduled row backs the increase; decreases are taken at face value. `commitment_load` and `savings_behaviour` divide by trusted income; `income_reliability = 100 × trusted/face × (income-weighted stability) − 20` for the existing pending/not-forecast penalty.

**Two deliberate deviations from the note.** (1) Reliability keeps a stability term: the note's pure `trusted/face` gives 100 to variable freelance income and to a temporarily reduced salary (samples 09 and 06 would have jumped from 60 to 100); multiplying by the streams' amount stability keeps that signal, now smooth instead of a 100/60 step. (2) Only `flexibility == "fixed"` debits enter the weighted commitment sum, as today; flexible spend is scored by `flexibility`.

**Guardrails held.** Contract columns on the 25 samples unchanged: status 25, method 25, plan 24, earliest 24, spending 24, `amount_safe_to_pay` exact 12 (identical with the flag off and on). 29 tests pass, including the curve's reference points (3 months → 0.632, 6 → 0.865, 12 → 0.982), the floor weight, progressive trust of a raise, and face-value decreases.

**Before → after (flag off → on) on the samples.** Commitment load rises 25–45 points across the board because almost every stream carries ~6 months of tenure (e.g. 06: 48.0 → 71.8, 24: 28.1 → 62.7, 25: 19.3 → 56.9); it now separates long habits from new commitments instead of measuring size. Income reliability: 02 (raise announced by message, 33.3M → 42.75M IDR) 100 → 83.5; 06 (temporary lower pay in history) 60 → 84.6; 08 60 → 80.2; 09 (variable freelance) 60 → 85.3; users with a fully proven salary stay at 100. Composite before/after the request moves with them (06: 56.9/35.9 → 66.5/45.6). The decision packet now carries `established_habits_already_in_balance` (top two absorbed streams) and `income_increase_not_yet_proven` so the explanation can say what moved the score and what did not.

## D13 — Conservative mode for irregular income: haircut + reserve, behind a flag (2026-09-13, score-influence session)

**Question from Jason.** The Spending Score only reached `decision_explanation`. Can it make the *decision* safer, not just describe the account?

**Decision.** Two deterministic safety levers, driven by the same irregularity signal the score reads (a user whose income is a variable pool rather than a regular salary), applied only in the safer direction and only when enabled:

- **Income haircut** — the variable income pool is forecast at the `income_haircut_quantile` (0.25) of its settled history instead of the mean (`intake.py`, `_stat("q0.25")`).
- **Reserve cushion** — `FinancialState.reserve = reserve_months (0.1) × monthly essential outflow` (protected recurring debits, else all recurring debits). Every safety check in `forecast.py` compares against `state.floor = minimum + reserve`; `score.py` headroom and the impact band use the same floor so the two views never disagree. The reserve stays even when a message later removes the pool (user_12): the account is still irregular.

Enable with `python3 code/main.py --conservative` or `BUYORWAIT_CONSERVATIVE=1`. Off by default. The LLM never touches either lever; a future account-profile stage may only feed the explanation and cut ordering (the one-way valve).

**Evidence.** Only 3 of the 25 samples are irregular (09, 10, 12). Sweep on the samples (per-field matches, baseline `amount=12 affordability=25 recommended=25 payment=24 earliest=24 spending=24`):

| Setting | Result |
|---|---|
| haircut q ∈ {0.35, 0.25, 0.1}, no reserve | identical to baseline (the trough falls before the next variable credit) |
| reserve 0.1 months, no haircut | identical match counts; sample 10 moves 32,526.82 → 18,434.45 toward the truth of 12,700 |
| reserve 0.25 months | sample 10 → 0; sample 12 gains an unneeded spending change (`spending=23`) |
| reserve 0.5 months | `amount=11 earliest=23 spending=22` |

Defaults are therefore the largest no-regression values (q=0.25, 0.1 months). On the full 250-request run the flag changes 5 `amount_safe_to_pay` values, 1 earliest date and 2 spending-change rows, and no status or method. Because the hidden truth follows the spec's arithmetic and the default reproduces the samples exactly, the submitted `output.csv` is produced with the flag **off**; the flag is the product-side "make me safer" setting, documented and tested (`tests/test_forecast.py`, `tests/test_evidence.py`).

## D14 — LLM account profile: archetype + capped income-reliability supplement (2026-09-13, score-influence session)

**Question from Jason.** Let the model recognise *what kind of earner* an account is (freelancer, gig worker, salaried with a side income, household with two streams, someone between jobs) from the income pattern and the messages, and give a supplemental score, up or down, that arithmetic alone cannot produce.

**Design** (`code/buyorwait/profile.py`, one call per user and request date):

1. **Typed features, never raw text.** `profile_features(state)` packs each income stream (description, cadence, occurrences, median, coefficient of variation, last six amounts), settled one-off credits, pending/scheduled credits, the typed message facts from `evidence.py`, the income notes and the variable-pool flag. Messages stay untrusted: only their validated facts reach the model, and the system prompt frames the packet as data.
2. **Rules baseline first.** `deterministic_archetype()` labels the account from the same features (salaried, salaried_plus_side, salaried_plus_variable, freelance, gig, mixed_household, transition, no_income). The model starts from that label and may only move away from it when the packet supports it.
3. **Closed-schema answer.** Groq `openai/gpt-oss-120b` (`BUYORWAIT_PROFILE_MODEL` overrides), temperature 0, JSON mode. `validate()` rejects anything outside the schema: archetype not in the enum, adjustment outside -2..2 or non-integer, confidence outside 0..1, a rationale with digits or event ids, or an evidence id that was not in the packet. A rejected answer falls back to the rules label with adjustment 0, so the run never blocks and never trusts free text.
4. **Cache.** `code/evidence/account_profiles.json`, keyed `user@request_date` with a hash of the features; a changed state invalidates the entry, `--refresh-profiles` ignores it, `--no-profile` skips the stage.
5. **Where it lands.** `score.py` adds `PROFILE_POINTS (5) × adjustment` to `income_reliability` (±10 points at most; the composite weight of that component is 0.20, so ±2 composite points). The explanation packet carries `score.income_pattern` (archetype in words + the rationale) and the prompt prefers that note for its income sentence. `--explain` prints the full profile with evidence ids.

**Boundary.** Same as D4 and D13: the profile never reaches a contract column. The deterministic levers of D13 are the only things that make the *decision* safer; the profile makes the *score and the explanation* right about why. A one-way valve keeps a hallucinated "this freelancer is fine" harmless: at worst the explanation is warmer, never the amount larger.

**Cost.** One extra call per request (about 0.7–1.0k input tokens, ≤150 output); cached across runs, so the final full-dataset run reports it once. Telemetry: a `profile` span per request with `gen_ai.*` attributes, `archetype`, `adjustment`, `source` and `cache_hit`; `build_usage_report.py` counts it with the explanation calls.

**Evidence (25 samples, `openai/gpt-oss-20b`).** Contract fields identical to the baseline (`amount=12 affordability=25 recommended=25 payment=24 earliest=24 spending=24`), as the boundary requires. Profiles: 20 salaried at adjustment 0 (the intended neutral case), user_11 salaried_plus_variable at 0 (settled commissions), user_02 and user_14 +1 (a confirmed raise; a salary that resumed), user_06 −1 (temporary reduced pay, message_04), user_10 −1 (platform payout pending, message_07), user_05 and user_12 −2 (final payroll; employment ended, message_09). Every non-zero call cites the message or event that supports it. One answer was rejected by the validator on the first pass (a rationale containing "every 17 days" under the original no-digits rule); the rule now blocks only amounts (three or more digits) and ids, and the re-run accepted it.

**Model note and cache state.** Groq's free tier caps each model at 200k tokens per day (rolling). `openai/gpt-oss-120b` ran out during this session, so the 250-request cache was built on `openai/gpt-oss-20b` (`BUYORWAIT_PROFILE_MODEL`); the 20b quota then ran out during a rebuild triggered by adding `income_descriptions` to the packet, and the 120b quota that had come back covered only a handful more. Result: 275 cached answers (250 requests + 25 samples), 49 with the current features hash and 226 from the first build. Rather than drop those to the rules baseline, `account_profile()` reuses the earlier validated answer for the same user when the model is unavailable or answers badly, marks it `source=llm-stale`, and never writes the stale copy back; `--refresh-profiles` when quota is available brings everything current. Rate-limit handling gives up immediately on a "tokens per day" error instead of retrying eight times, and parses millisecond retry hints. Both quotas were exhausted at the end of the session, so the final full run (`output.csv` + `usage_report.md` with the profile calls) is still to be done once a window is available.

Distribution of the 275 cached profiles: salaried 219, transition 27, freelance 22, salaried_plus_variable 6, gig 1; adjustments −2: 17, −1: 18, 0: 218, +1: 22; 3 non-zero answers cite no evidence id (all three are −2 "final payroll / employment ended" calls that rest on a note rather than an id).

## D15 — Agentic runtime: orchestrator, workers, tools, reflection (user direction, 2026-09-13 morning IST)

**Direction from Jason.** Keep the system, make it agentic: be explicit about memory (all history at once vs. per-request retrieval), add an orchestrator that manages the user's history and plans how to use the available functions, workers that each own one quantitative aspect (account reconstruction, forecast, plans, score, audit, explanation), a goal the orchestrator reflects against, and turn what exists into functions/tools the workers and the orchestrator call.

**Memory answer.** The dataset was already loaded once per process and every request already recalled one user's history as of `request_date` (`build_state` filters by `user_id`, cuts settled history at the request date, reserves pending/scheduled rows, applies only messages sent on or before the request date or tied to the request). D15 names the tiers (long-term = dataset + cached factors table; episodic = per-user recall at request time; working = per-request scratchpad + ledger) in `code/buyorwait/agent/memory.py` and keeps recall stateless: nothing from another user, nothing dated after the request, nothing carried between requests.

**Decision.** `code/buyorwait/agent/` (docs/AGENTIC.md):
- `tools.py` — 18 tools wrapping the existing engine (intake, forecast, plans, score, factors, verify, explain), each with a description and JSON-schema arguments, reading/writing working memory; `registry.schemas()` renders OpenAI-style function schemas.
- `workers.py` — historian, forecaster, planner, scorer, auditor, explainer; each runs its tools and turns results into findings (prose) and flags (read by the reflection).
- `orchestrator.py` — `Goal.from_request` (request + profile + the §6.3 rules as criteria), a rule planner (default, deterministic) or an LLM planner (`--planner llm`, Groq proposes the ordered tool plan; `validate_plan` inserts prerequisites/mandatory steps, drops unknown tools, falls back to rules), execution with a span per step, a deterministic reflection (7 goal checks, concerns from worker flags, confidence high/medium/low), re-planning when the auditor rejects the chosen plan (re-rank without it, ≤3 iterations), and an optional model critique (`--llm-reflect`) that can only add a concern.
- `main.py` uses the orchestrator by default; `--pipeline` keeps the linear path as the parity oracle; `--explain` prints goal, plan, findings, reflections and the ledger; a full run writes `.cache/agent_transcripts.jsonl`.

**Why.** (a) Contract columns are unchanged by construction: every tool delegates to the same functions, and `tests/test_agent.py` asserts row-for-row equality with the linear pipeline on the 25 samples (explanation included) — the D1 rejection of "multi-agent orchestration" was about letting agents *compute*; here they *coordinate* a verified engine. (b) The reflection turned up a real bug in its own first version (it flagged `wait` as a non-accepted method on six samples) — a goal check that can disagree with the planner is worth having. (c) The per-request transcript is the interview story: goal → plan → what each worker found → what the reflection worried about → the row. (d) Cost is unchanged in the default configuration (one explanation call per request); the LLM planner + critique add ~1.9k tokens per request and stay opt-in.

**Rejected.** Letting the LLM planner run tools directly in a tool-calling loop (unbounded calls, non-deterministic output; the validated one-shot plan gives the same plan quality with a fixed budget). Sharing recalled state across requests of the same user (not needed: users and requests are one-to-one; and a later request must not see later messages). Cross-request memory of decisions (no signal to learn from without labels).

## D16 — Account cards as memory, score gate before the plan search (user direction, 2026-09-13)

**Direction from Jason.** Go the score route: get the score deterministically once, apply the request to it, and let a threshold decide when it can; keep the scores in RAM rather than the eight CSVs.

**Measured first.** Raw tables 20.9 MB in pandas vs 0.5–2.8 MB of per-user cards (compact reconstructed state + scores), built once in ~6 s. A composite-score threshold alone predicts `affordable_now` on 22/25 samples and cannot separate `affordable_with_plan` from `affordable_later` (they depend on options, `max_installment_months`, partial permission and the deadline); and the contract needs exact amounts, dates, option ids and event ids that no score emits.

**Decision.**
- `code/buyorwait/agent/cards.py`: `AccountCard` (profile preferences, state, no-message state when facts applied, the request's options, score components, factors row) and `CardStore` (build / save / load, `.cache/cards.jsonl`). Cards round-trip through JSON exactly. `LongTermMemory` is card-backed; `--no-dataset` drops the dataset after the build and every request still runs (asserted in tests on the samples; measured on all 250 requests with zero column differences against the linear pipeline).
- `plans.enumerate_plans_for` / `decide_for` / `choose`: the enumeration and ranking now take a profile row and option rows, so request time needs no dataset.
- `score_gate` tool (planner worker) before the search: `affordable_now` when headroom ≥ requested and full payment is accepted; `not_affordable` when no safe full-payment date, no eligible installment option and no permitted change (or a baseline breach with no permitted change); otherwise `search`. Both settled routes are exact by construction, and `enumerate_candidate_plans` / `rank_and_choose` are skipped when the gate settles (55 of 250 requests).

**Why.** The card is the memory the user asked for: small, deterministic, built once, and the thing a new settled event would update. The gate is the threshold the user asked for, restricted to the cases where a threshold is provably the same answer as the search, so the contract columns stay byte-identical (parity tests on 25 samples and 250 requests).

**Rejected.** Deciding the middle statuses from a fitted score threshold (25 labels, and the answer depends on per-request options); storing cards in the repo (derived data; the cache rebuilds in 6 s); keeping raw events on the card (the streams already carry their history lists).

## D17 — Cards in RAM, tables on disk by section (user direction, 2026-09-13)

**Direction from Jason.** House the cards in RAM and retrieve certain sections of the tables from disk when needed; confirm decisions are made off the cards.

**Decision.** `code/buyorwait/agent/store.py` (`DiskTables`): each CSV is indexed once by its key column as byte ranges (all seven keyed files are contiguous per `user_id` / `request_id` and have no embedded newlines; index cached in `.cache/index/`, invalidated on size/mtime change). `slice(table, key)` reads one user's or one request's rows by seeking; `dataset_for(user, request)` assembles a one-user `Dataset` (rates read whole, ~130 rows) via the new `Dataset.from_frames`, which is all `build_card` needs. `LongTermMemory` now carries `cards` (RAM) and `disk`; `UserMemory.card` builds a missing card from disk sections and adds it to the store; the historian's `fetch_events` tool reads raw rows on demand (used when the evidence carries an uncertainty flag or an image-filled amount). `main.py` no longer loads the dataset when the card cache covers the run (`--load-dataset` restores the old behaviour); it reports section reads and peak RSS.

**Measured.** Card from disk slices == card from the full dataset on 65 users (0 mismatches, 81 ms per build). Warm full run: 0 KB of tables loaded, 190 KB read in 17 section reads, peak RSS 115 MB, 250 rows identical to the linear pipeline. All 25 samples served on card misses from disk: 344 KB read, rows identical. Decisions are made off the cards: nothing on the decision path reads a table (D16 parity holds).

**Rejected.** A database (SQLite/DuckDB) for the tables: the byte-range index gives per-user reads in one seek with no dependency, and the dataset is fixed for the challenge. Memory-mapping the frames: pandas would still parse whole files. Dropping the on-demand path entirely: a miss must be servable without the whole table, otherwise "cards in RAM" is only true for pre-built users.

## D18 — ICM workspace layer over the orchestration (user direction, 2026-09-13)

**Direction from Jason.** Use the uploaded ICM template (Interpretable Context Methodology skill: `icm-scaffold`, `icm-sync`, `icm-context-scaffold`) to coordinate the file structure for the agent orchestration.

**Decision.** Quick mode (the skill's default for existing projects: routing and reference layers, no restructuring), with the six workers as **virtual stages**:
- `IDENTITY.md` (Layer 0): workspace map with one comment per folder, six workspace-specific rules; points to `AGENTS.md` as the authority.
- `CONTEXT.md` (Layer 1): session-start protocol, task → destination routing table, the orchestrator pipeline as a stage table (reads / writes / routing per worker, the reflection gate, the human review gate via `--explain`), shared-config index.
- `_config/` (Layer 3): `conventions.md`, `glossary.md`, `voice.md` as re-exports (canonical source named at the top, quick reference, link back), never duplicating `AGENTS.md`, `README.md` or `explain.py`.
- Layer 2 contracts where a folder holds 3+ files: `code/buyorwait/agent/CONTEXT.md` (the stage contracts: tools in plan order, inputs, outputs, flags, and the orchestrator's `PREREQS` / `MANDATORY` / `GATED` / `DELIVER` rules), `code/buyorwait/CONTEXT.md`, `code/evaluation/CONTEXT.md`, `tests/CONTEXT.md`, `docs/CONTEXT.md`. Skipped: `dataset/` (data), `scratch/` (throwaway), `code/evidence/` (single file), `.cache/` (Layer 4 artefacts).
- Model adapter: `CLAUDE.md` keeps `@AGENTS.md` (required by the challenge) and adds `@IDENTITY.md`; the template's "copy IDENTITY.md into CLAUDE.md" is not applied because `AGENTS.md` must stay the single source of truth (§7 of that file).
- `code/package.py` ships the ICM files in `code.zip`.

**Why.** The orchestration already had a folder per concern; what it lacked was a map an agent can read in one turn and a contract per worker in the place the code lives. The ICM budgets (map < 1,500 tokens, routing < 2,000, contracts < 500) kept each file to what a session needs at that stage, matching the runtime's own principle that a request loads only its card.

**Rejected.** Full mode with physical `stages/NN/` folders and `output/` directories: the stages here are Python workers exchanging working-memory keys, not markdown hand-offs, and a human review gate between workers would break a 250-request batch; the `--explain` transcript is the review surface instead. Replacing `CLAUDE.md` with the identity file (loses the mandatory logging protocol).

## D19 — Telemetry feeds the reflection: reflect spans and a run-level reflection (user direction, 2026-09-13)

**Direction from Jason.** Is telemetry involved in the reflection? It was write-only. Add both proposed pieces.

**Decision.**
- `agent.reflect` span per iteration with `check.<name>` booleans, concerns, confidence, `replan`, `prior_requests`; the optional model critique's span is renamed `agent.critique` (usage report stage updated).
- Run log on the orchestrator (`run_log`, per user): a later request for a user whose earlier request in the same run was re-planned or ended low-confidence gets a concern and a confidence cap at medium. Per-run state, never persisted, never a contract column.
- `agent/run_reflection.py` + `code/evaluation/build_run_reflection.py`: read `.cache/traces.jsonl` and `.cache/agent_transcripts.jsonl`, write `code/evaluation/run_reflection.md` after every full run (outcomes × confidence, gate/recall rates, iterations, tool calls, wall time, failed checks, concern and flag frequencies, per-tool timings, re-plans, low-confidence requests).

**Measured** (250 requests, template explanations): 250 reflect spans, 4,593 step spans; 19.9 tool calls and 17 ms per request; confidence high 19 / medium 182 / low 49; `completes_by_deadline` false on 54 (47 `not_affordable` + 7 `wait` past the desired date); thin headroom on 184; 15 decisions depend on message evidence; 0 re-plans. Contract columns unchanged (0 diffs vs the linear pipeline).

**Rejected.** Letting the run-level statistics change a decision (no labels to justify it; the loop only moves confidence and the explanation). Persisting the run log across runs (users are one-to-one with requests here; a persisted log would need invalidation rules the dataset cannot exercise).

## D20 — output.csv regenerated under exhausted quotas: qwen/qwen3.8-27b run + reused explanations (user direction, 2026-09-13)

**Direction from Jason.** Regenerate `output.csv` with `openai/gpt-oss-20b`.

**What happened.** The committed `output.csv` predated the D8/D11 forecast changes (97 rows differed on contract columns). The 120b daily quota was spent before the session; the 20b run stalled at 31 requests in 30 minutes because its daily quota was also spent (199,416 of 200,000). Of the models this key can reach, `qwen/qwen3.8-27b` produced grounded explanations on 3/3 probe packets at ~950 tokens per call; `allam-2-7b` echoed the packet, `groq/compound-mini` cost 3× the tokens, the llama models are not accessible. The qwen run served 224 requests (176 grounded model explanations, 40 rejected by the grounding guard, 8 rate-limit fallbacks; 205,652 tokens) before its own daily cap turned every remaining call into eight minutes of retry sleep for a template result, with the deadline four hours away.

**Decision.** Stop the qwen run, keep its traces and transcripts, and add `--reuse-explanations <transcripts>`: a run takes the model-written explanation of any request present in an earlier transcript, re-verifies it with `explain.grounded()` against the freshly built packet, and uses the template for the rest; contract columns are always recomputed. `python3 code/main.py --no-llm --reuse-explanations .cache/agent_transcripts_qwen_partial.jsonl` produced the committed `output.csv`: 250 rows, verifier OK, 0 contract-column differences against the linear pipeline, 176 reused model explanations + 74 template. `usage_report.md` is built from the qwen run's traces with a run note describing the two steps; `build_usage_report.py` gained a note argument. The default explanation model stays `openai/gpt-oss-120b`.

**Why.** Correct contract columns on every row matter more than prose on the last 74; the reuse path keeps the 176 grounded explanations without a single extra token; everything is stated in the usage report rather than hidden.

**Rejected.** Waiting for the 120b quota (resets after the deadline). Re-running everything with the template only (loses 176 grounded explanations). Splicing rows by hand (the reuse option makes the result the product of one documented command and re-checks every reused sentence against today's numbers).
