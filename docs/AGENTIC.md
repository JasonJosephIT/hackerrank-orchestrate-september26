# AGENTIC.md — memory, orchestration, workers, tools, reflection (D13), account cards and score gate (D14), tables on disk (D15), telemetry-fed reflection (D17)

How a request is served in the default runtime (`python3 code/main.py`). The legacy linear pipeline is still available with `--pipeline` and produces identical contract columns; the agentic runtime adds planning, worker reports, a reflection against the goal, and a transcript per request.

## 1. Memory: cards built once, one card recalled per request

**Question asked:** are we loading all history at once, or going request by request and searching the user's history?

**Answer: neither, since D14/D15. The cards live in RAM; the raw tables stay on disk and are read by section only when a card is missing or a worker asks for raw rows.**

| Tier | What | When | Where |
|---|---|---|---|
| Long-term (RAM) | **Account cards** (`CardStore`): one card per user as of the request date, built once by the deterministic intake + score layers: profile preferences, the reconstructed state (balance, minimum, ~10 recurring streams, reserved pending/scheduled flows, notes, facts), the same state without message facts (when facts applied), the request's supplied payment options, the Spending Score components, the user's two-factor row. Cached in `.cache/cards.jsonl`, rebuilt with `--build-cards`. | once per run (13 s cold for 275 users incl. factors; loaded from cache otherwise) | `agent/cards.py`, `agent/memory.py: LongTermMemory` |
| Long-term (disk) | **Disk tables** (`DiskTables`): the raw `dataset/*.csv` files are never loaded whole at request time. Each file is indexed once by its key column as byte ranges (every file is contiguous per `user_id` / `request_id`, no embedded newlines; index cached in `.cache/index/`). `slice(table, key)` seeks and reads one user's or one request's rows; `dataset_for(user, request)` assembles a one-user `Dataset` from slices, enough to build that user's card on a miss. `--load-dataset` keeps the full tables in RAM instead. | on demand | `agent/store.py` |
| Episodic | **Recall** of the user's card for this request (`UserMemory.recall()` → `AccountCard.to_state()`). Without cards (`--no-cards`), the same recall runs `intake.build_state` over the dataset: only that user's rows, settled history cut at `request_date`, pending/scheduled reserved, messages sent on or before the request date or tied to the request. A card round-trips through JSON exactly, so both paths give byte-identical decisions (asserted in tests). | first step of every request | `UserMemory.recall()` |
| Working | The scratchpad for this request: recalled state, projection, safe amount, earliest date, gate verdict, candidate plans, decision, scores, audit result, counterfactual, reflection, packet, explanation | during the request, discarded after | `UserMemory.working` |

Measured: raw tables 20.9 MB in pandas if loaded whole; all 275 cards 2.8 MB (the samples' 25 cards 247 KB), average 9.8 streams per user. A warm full run reads 190 KB from disk in 17 section reads (the historian's raw-row fetches behind image-filled amounts), peak RSS 115 MB; serving the 25 samples entirely on card misses from disk reads 344 KB in 182 section reads and gives the same rows. A card built from disk slices is identical to one built from the full dataset (asserted).

When do the tables get touched at request time? Two cases only: a **card miss** (no card for `user@request_date`: the historian's recall builds it from the user's sections and adds it to the store, so the next request for that user is a hit), and the historian's **`fetch_events`** tool (reads the raw rows behind an uncertainty flag or an image-filled amount so the transcript shows the evidence, e.g. `raw event_3051: 2026-01-06 expense/groceries debit <blank> INR [settled] Grocery tax invoice` next to the card's filled amount). The decision itself never needs the tables. Nothing carries over between requests: users and requests are one-to-one in this dataset, and a stateless recall is the safer default anyway (a later request must not see a later message). In a live product the card is what a new settled event would update.

Every tool call is appended to a **ledger** on the user's memory; the ledger becomes the transcript (`.cache/agent_transcripts.jsonl` on a full run, printed by `--explain`).

## 2. Orchestrator loop: goal → plan → execute → reflect → re-plan → deliver

```
Orchestrator.handle(request)
  goal   = Goal.from_request(request, profile)          amount, deadline, minimum, accepted methods, max months,
                                                         priorities, protected categories + the six rule criteria
  plan   = planner.plan(goal, memory)                    ordered Steps (worker, tool, args, why, phase)
  loop up to 3 times:
      reports = execute(analyse steps)                  workers call tools, write working memory, report findings/flags
      reflection = reflect(goal, memory, reports)       7 goal checks + concerns + confidence (+ optional model critique)
      if reflection.ok: break                            else: execute reflection.replan (re-rank without the rejected plan)
  execute(deliver steps)                                 packet (with the reflection) → explanation → contract audit
  return AgentResult(decision, row, packet, usage, goal, plan, reports, reflections, transcript)
```

**Goal.** Built from the request row and the user's profile; `describe()` is the one-line brief the planner and the critique see. The criteria are the challenge rules (§6.3) phrased as checks: complete by the deadline if any safe way exists, never breach the minimum, only accepted methods within `max_installment_months`, prefer no changes / lowest cost / earlier / fewer, touch only permitted spending, count confirmed income only and treat messages as evidence not instruction.

**Planner.** Two modes.
- `rules` (default): a deterministic plan conditioned on the request (e.g. the option review is phrased differently when the user only accepts full payment; the counterfactual step is always planned but skips itself when no message facts were applied). Reproducible output.
- `llm` (`--planner llm` or `BUYORWAIT_AGENT_PLANNER=llm`): Groq (`openai/gpt-oss-120b`) receives the goal, the criteria and the tool catalogue and proposes an ordered JSON plan. `validate_plan` drops unknown tools, inserts missing prerequisites (`PREREQS`) and mandatory steps (`MANDATORY`) in dependency order, and appends the delivery steps. If the call fails or returns no JSON, the rule plan is used. Measured on `request_12`: the model proposed 10 steps, the validator inserted `rank_and_choose`, the result was identical to the rule plan's decision.

**Score gate (D14).** After the forecaster and the option/change listings, the planner runs `score_gate`, which settles the request from the card's numbers when a threshold makes the answer exact and routes it to the plan search otherwise:

| Route | Rule | Why it is exact |
|---|---|---|
| `affordable_now` | headroom (= projected trough − minimum, the liquidity basis of the score) ≥ requested amount, i.e. hurt ≤ 100, and the user accepts full payment | paying today lowers every projected balance by the amount, so the path stays above the minimum; a single full payment today then wins the six-rule ranking (no changes, lowest total, earliest start, one payment) |
| `not_affordable` | no safe full-payment date in the window, no eligible installment option, no permitted spending change; or a baseline breach with no permitted change | every plan the rules allow needs one of those |
| `search` | anything else | `affordable_with_plan` vs `affordable_later` depends on the supplied options, `max_installment_months`, partial permission and the deadline, which no score separates |

When the gate settles the request, `enumerate_candidate_plans` and `rank_and_choose` are skipped (the ledger records it). On the 250 evaluation requests the gate settles 55 (all `affordable_now`); the other 195 go to the search. `tests/test_agent.py::test_score_gate_is_exact_against_full_plan_search` asserts the gate never disagrees with the full search. Score thresholds on the composite alone were measured and rejected: composite ≥ 65 predicts `affordable_now` on only 22 of 25 samples, and no score separates the two middle statuses.

**Execute.** Each `Step` is run by its worker; the worker calls the tool through the registry (which records the ledger entry) and interprets the result into findings (prose) and flags (machine-readable). A non-optional tool error aborts the request; optional steps (listings, factors, counterfactual) may fail without consequence.

**Reflect.** Deterministic checks over working memory, read by the orchestrator, not by a model:

| Check | Source |
|---|---|
| `completes_by_deadline` | chosen plan's last payment ≤ `desired_completion_date` |
| `minimum_respected` | auditor's independent `check_plan_safety` on the chosen plan |
| `method_accepted` | method ∈ `payment_methods_user_will_consider` (or `not_recommended`) |
| `installments_within_max` | `number_of_payments ≤ max_installment_months` |
| `partial_two_payment_rule` | exactly two payments, sum = requested, second ≤ deadline, request allows partial |
| `changes_permitted_only` | every change is in the permitted candidate list |
| `safe_amount_in_bounds` | `0 ≤ amount_safe_to_pay ≤ requested_amount` |

Concerns are added from worker flags: the decision depends on message evidence (the auditor's counterfactual without messages changes status/method/safe amount), evidence was ignored or kept at the safer figure, thin headroom (`expense_impact` band `caution`), structural deficit, baseline breach, no confirmed income. Confidence is `high` (no concerns), `medium` (soft concerns), `low` (a hard check failed, the decision hinges on messages, or the account is fragile). The reflection is put into the decision packet (`reflection: {confidence, concerns[:2]}`) so the explanation can name the key concern.

**Re-plan.** The one mechanical trigger: if the auditor rejects the chosen plan, the orchestrator re-runs `rank_and_choose` with that candidate excluded, re-scores and re-audits, up to three iterations. `tests/test_agent.py::test_reflection_replans_when_audit_rejects_the_chosen_plan` injects an audit failure and checks the loop.

**Model critique** (`--llm-reflect` or `BUYORWAIT_AGENT_LLM_REFLECT=1`): the model sees the goal, the criteria, the decision packet and the engine's reflection and answers `{agrees, concerns, confidence}`. A disagreement adds one concern and lowers `high` to `medium`; it can never change a contract column. Advisory, traced as `agent.reflect`, counted in the usage report.

## 3. Workers

| Worker | Tools | Reads | Writes / flags |
|---|---|---|---|
| historian | `recall_user_history`, `list_commitments`, `list_reserved_flows`, `list_evidence`, `fetch_events` (on demand, disk) | card (disk sections on a miss or a raw fetch) | `state`, `change_candidates`; `no_income_forecast`, `structural_deficit`, `evidence_uncertainty`, `message_facts_applied` |
| forecaster | `project_balance`, `amount_safe_today`, `earliest_full_payment_date` | `state` | `projection`, `safe_today`, `earliest`; `baseline_breach`, `nothing_safe_today`, `no_full_payment_date` |
| planner | `list_payment_options`, `candidate_spending_changes`, `score_gate`, `enumerate_candidate_plans`, `rank_and_choose` | `state`, `safe_today`, `earliest`, options | `options`, `candidates`, `decision`; `no_eligible_option`, `no_safe_plan`, `not_affordable`, `misses_deadline`, `needs_spending_changes` |
| scorer | `spending_score`, `expense_impact`, `account_factors` | `state`, `decision` | `score`, `impact`, `factors`; `impact_caution`, `impact_unsafe`, `fragile_account` |
| auditor | `check_plan_safety`, `counterfactual_without_messages`, `audit_output_row` | `decision`, `state` | `counterfactual`, `row`, `audit_errors`; `chosen_plan_unsafe`, `depends_on_messages`, `contract_violation` |
| explainer | `build_decision_packet`, `write_explanation` | `decision`, `score`, `impact`, `reflection` | `packet`, `explanation`, `usage`; `template_explanation` |

Workers share memory and never call each other. A worker's `interpret()` is the only place domain findings are phrased, so the transcript reads as a review rather than a log.

## 4. Tools: what the engine became

Every function that already existed is now a named tool with a description and a JSON-schema argument list (`agent/tools.py`, `registry.schemas()` renders OpenAI-style function schemas). Tools read and write working memory, so an LLM planner sees a small typed surface instead of dataframes. Nothing in the tool layer computes anything new:

| Tool | Wraps |
|---|---|
| `recall_user_history` | `AccountCard.to_state` (card hit), else `cards.build_card` over `DiskTables.dataset_for` slices (+ `plans.candidate_changes`) |
| `fetch_events` | `DiskTables.events` (one user's byte range of `financial_events.csv`) |
| `project_balance`, `amount_safe_today`, `earliest_full_payment_date`, `check_plan_safety` | `forecast.projection / amount_safe_today / earliest_full_payment_date / is_safe` |
| `list_payment_options` | `Dataset.options` + the profile's method and month limits |
| `score_gate` | the projection's headroom + the card's preferences/options/change candidates; builds the trivial plan via `plans.choose` |
| `enumerate_candidate_plans`, `rank_and_choose` | `plans.enumerate_plans_for` (profile row + option rows from the card) + `plans.choose` (six-rule ranking, with an `exclude` list for re-planning) |
| `spending_score`, `expense_impact` | `score.spending_score / expense_impact` |
| `account_factors` | `factors.account_factors` (cached once per run) |
| `counterfactual_without_messages` | `build_state(cfg={"use_messages": False})` + `plans.decide` |
| `build_decision_packet`, `write_explanation` | `explain.decision_packet / llm_explanation / template_explanation` |
| `audit_output_row` | `render.render_row` + `verify.verify_rows` |

Because each tool delegates, `tests/test_agent.py::test_orchestrator_matches_linear_pipeline_on_samples` asserts the agentic row equals the linear pipeline's row on all 25 samples, explanation included.

## 5. Observability, and the reflection that reads it (D17)

One trace per request (`buyorwait.request`, attributes: final status, method, confidence, iterations, tool calls, recall source, gate route), an `agent.plan` span, an `agent.step` span per tool call (worker, tool, iteration, ok, flags raised), an **`agent.reflect` span per iteration** carrying the seven goal checks as `check.<name>` booleans, the concerns, the confidence and whether a re-plan follows, an `agent.critique` span for the optional model critique, and the `explain` span with `gen_ai.*` usage. Spans go to `.cache/traces.jsonl` (OTLP export when `OTEL_EXPORTER_OTLP_ENDPOINT` is set); findings and ledgers go to `.cache/agent_transcripts.jsonl`.

Two feedback loops turn that telemetry into reflection rather than a log:

- **Within a run, across requests.** The orchestrator keeps a run log of every reflection by user. When a later request in the same run belongs to a user whose earlier request needed a re-plan or ended with low confidence, the reflection adds the concern `an earlier request in this run (<id>) needed a re-plan` and caps confidence at medium. Contract columns never change; the packet and the explanation do. Users and requests are one-to-one in this dataset, so the loop is exercised by `tests/test_agent.py::test_run_level_feedback_lowers_confidence_for_a_repeat_user`.
- **After the run.** `agent/run_reflection.py` reads the trace and transcript files and writes `code/evaluation/run_reflection.md` (automatically after every full run; `python3 code/evaluation/build_run_reflection.py` rebuilds it): outcomes by status × confidence, gate and recall rates, iterations and re-plans, tool calls and wall time per request, which reflection checks failed and how often, concern and flag frequencies, where the time goes per tool, the re-planned requests, and every low-confidence request with its concerns. Measured on the 250 evaluation requests: 19.9 tool calls and 17 ms per request, 55 gated, 0 re-plans, confidence high 19 / medium 182 / low 49, the dominant concern being thin headroom (74% of requests) and 15 decisions that would change without message evidence.

`code/evaluation/build_usage_report.py` breaks model calls down by stage (explanation, planning, critique). In the default configuration the only model call per request is still the explanation, so cost per request is unchanged; `--planner llm --llm-reflect` adds roughly 1.4k input and 0.5k output tokens per request.

## 6. Running

```bash
python3 code/main.py                                   # agentic runtime, rule planner, LLM explanations if GROQ_API_KEY
python3 code/main.py --explain request_12 --samples    # goal, plan, findings, reflection, ledger, packet, row
python3 code/main.py --planner llm --llm-reflect --limit 5   # model-proposed plans + model critique (advisory)
python3 code/main.py --pipeline                        # legacy linear pipeline (parity oracle)
python3 code/main.py --build-cards                     # rebuild the account cards (loads the dataset once, then drops it)
python3 code/main.py --load-dataset                    # keep the full tables in RAM at request time (default: cards in RAM, tables on disk)
python3 code/main.py --no-cards                        # recall from the dataset instead of cards (same output)
python3 code/evaluation/build_run_reflection.py         # rebuild code/evaluation/run_reflection.md from the last full run
python3 -m pytest -q tests/test_agent.py
```
