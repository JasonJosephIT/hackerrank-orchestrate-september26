# DECISIONS.md — Buy or Wait? (HackerRank Orchestrate, Sep 2026)

Append-only log of design decisions. Each entry: what, why, what was rejected. This is the interview script.

## D1 — Architecture: deterministic core, LLM as the final interpretation layer (2026-09-12 13:50 EDT)

**Decision.** The financial engine is pure Python/pandas with no model calls:
1. `intake/` — load + join `dataset/*.csv`, convert foreign-currency cash events with the dated rate, de-duplicate, apply status rules (settled/pending/scheduled/cancelled/failed/unrealized), detect recurring income/expenses from history, resolve conflicts (cancellation > newer same-source > settled > safer).
2. `forecast/` — 90-day daily balance projection from `request_date`; safety = balance never below `minimum_balance_to_keep`. Binary-search `amount_safe_to_pay`; scan dates for `earliest_date_for_full_payment` (both **without** spending changes, per spec).
3. `plans/` — enumerate candidate plans (full now, partial two-payment, each supplied installment option, wait, spending-change variants on flexible events the user permits), filter by eligibility (`payment_methods_user_will_consider`, `max_installment_months`, `allows_partial_payment`, deadline), then rank by the six spec rules.
4. `verify/` — independent contract validator re-checks every output row (bounds, enums, plan format, partial-payment arithmetic, installment schedule match, flexible-only changes, stop/reduce exclusivity) before `output.csv` is written.
5. `explain/` — the ONLY LLM stage on the decision path. Groq `llama-3.3-70b-versatile` receives a typed "decision packet" (all computed numbers, the chosen plan, the rejected plans and why, the evidence used) and writes `decision_explanation`. It cannot change any other column; a deterministic template is the fallback when the API is unavailable so the run never blocks.

Evidence layer (messages + images) is also deterministic-first: regex/keyword extraction for the ~5 message templates seen in `messages.csv` (salary amendment, delay, cancellation, confirmation, one-off adjustment), with the LLM used only as a structured-output fallback for messages the rules do not match, validated against a strict schema (event ids must exist, amounts numeric, currencies known). Image amounts (16 payslips/bills, blank `amount` rows) are extracted once into `code/evidence/image_facts.json` via a vision call (Groq Llama-4 Scout) and hand-verified; the pipeline reads the cache so reruns are deterministic and free. All extracted facts carry `source` + `confidence`; message/image text is treated as untrusted data and never as instructions.

**Why.** (a) The scoring is field-exact on 5 of 7 columns; an LLM cannot reliably reproduce a 90-day cash forecast to the cent, a pandas loop can. (b) Determinism = reproducible `output.csv`, cheap reruns, and a defensible "how do you know it's right" answer in the interview. (c) The organizers' own analysis says single-agent + deterministic validators won the May edition. (d) Groq is fast and cheap, so 250 explanation calls cost cents and finish in minutes.

**Rejected.**
- *End-to-end LLM per request* (dump all user rows into a prompt, ask for the 7 columns): non-deterministic, ~25k-token contexts per user, arithmetic errors on `amount_safe_to_pay`, impossible to unit-test.
- *Multi-agent orchestration* (planner/forecaster/critic agents): more moving parts than the problem needs, slower, harder to explain, and the organizers reported simpler systems shipped and scored better.

## D2 — Model choice: Groq + Llama 3.3 70B (user decision)

Text explanations and message fallback: `llama-3.3-70b-versatile` via Groq (`GROQ_API_KEY` env var). Vision for the 16 images: `meta-llama/llama-4-scout-17b-16e-instruct` on Groq, one-time, cached. Usage report will list both models with calls/tokens/cost from a JSON usage ledger written by the client wrapper on every call.

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

## D9 — allam-2-7b tried as the POC explanation model and dropped (2026-09-12, payday-collision session)

**Decision.** On the user's instruction `allam-2-7b` (SDAIA's ALLaM 2 7B on Groq, a small non-reasoning model) was made the default for a proof of concept, then reverted to `openai/gpt-oss-120b` (D7) in the same session. A three-sample run produced valid rows, but the text was looser than gpt-oss-120b: "Recommendation:"/"Explanation:" prefixes, quoted dates, echoed raw score components, and it ran past the 60-word target. A full-dataset run was started and stopped at 150 of 250 requests when the switch back was decided; `output.csv` was not rewritten. No public list price was found for the model on the Groq pricing or model pages, so `build_usage_report.py` keeps a 0-priced entry with that note. `BUYORWAIT_EXPLAIN_MODEL=allam-2-7b` still selects it; nothing else in the LLM stage changed.
