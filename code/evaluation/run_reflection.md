# Run Reflection

Generated 2026-09-13 06:10 UTC from `traces.jsonl` and `agent_transcripts.jsonl`: 250 requests served by the orchestrator (D13–D17). The per-request reflection reads worker flags; this report reads the telemetry of the whole run so the same signals can be judged across requests.

## Outcomes

| Status | Requests | high | medium | low |
|---|---|---|---|---|
| affordable_later | 65 (26%) | 0 | 54 | 11 |
| affordable_now | 55 (22%) | 10 | 32 | 13 |
| affordable_with_plan | 83 (33%) | 9 | 54 | 20 |
| not_affordable | 47 (19%) | 0 | 42 | 5 |

- Confidence overall: high 19 (8%), low 49 (20%), medium 182 (73%)
- Score gate: affordable_now 55 (22%), search 195 (78%); plan-search steps skipped: 110
- Recall source: card 250
- Iterations: 1× 250; re-planned requests: 0
- Tool calls per request: 19.9; wall time per request: 17 ms
- Raw rows fetched from disk (historian): 17 request(s)
- Explanations: 0 model call(s), 250 from the template (a --no-llm run); critiques 0, model-planned 0; tokens 0
- Cross-request feedback applied (earlier request of the same user re-planned / low confidence): 0

## Reflection checks that failed

| Check | Requests |
|---|---|
| completes_by_deadline | 54 |

## Concerns raised by the reflection

| Concern | Requests |
|---|---|
| the plan leaves thin headroom above the minimum | 184 (74%) |
| no safe plan completes by the desired completion date | 47 (19%) |
| some evidence was ignored or kept at the safer figure | 43 (17%) |
| the recommendation depends on message evidence | 15 (6%) |
| recurring outflow exceeds recurring income | 15 (6%) |
| no confirmed income is forecast | 13 (5%) |
| the best safe plan completes on <date>, after the desired date <date> | 7 (3%) |
| the balance breaches the minimum even without this request | 4 (2%) |

## Worker flags

| Flag | Requests |
|---|---|
| message_facts_applied | 192 (77%) |
| impact_caution | 184 (74%) |
| no_safe_plan | 47 (19%) |
| not_affordable | 47 (19%) |
| impact_unsafe | 46 (18%) |
| no_full_payment_date | 44 (18%) |
| evidence_uncertainty | 43 (17%) |
| needs_spending_changes | 33 (13%) |
| no_eligible_option | 28 (11%) |
| no_income_forecast | 21 (8%) |
| depends_on_messages | 15 (6%) |
| structural_deficit | 15 (6%) |

## Where the time goes

| Tool | Avg ms | Calls |
|---|---|---|
| expense_impact | 3.8 | 250 |
| enumerate_candidate_plans | 2.7 | 195 |
| counterfactual_without_messages | 2.6 | 250 |
| spending_score | 2.0 | 250 |
| list_payment_options | 1.8 | 250 |
| recall_user_history | 0.5 | 250 |
| check_plan_safety | 0.4 | 203 |
| list_evidence | 0.3 | 250 |
| score_gate | 0.3 | 250 |
| project_balance | 0.3 | 250 |

## Low-confidence requests

| Request | Status / method | Concerns |
|---|---|---|
| request_27 | affordable_now / full_payment | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum |
| request_29 | not_affordable / not_recommended | no safe plan completes by the desired completion date; the recommendation depends on message evidence (status would be affordable_now, method would be full_payment, amount_safe_to_pay would be 51524.0); some evidence was ignored or kept at the safer figure (see notes) |
| request_36 | affordable_later / wait | the recommendation depends on message evidence (status would be not_affordable, method would be not_recommended); the plan leaves thin headroom above the minimum |
| request_37 | affordable_later / wait | the recommendation depends on message evidence (amount_safe_to_pay would be 3826382.63); the plan leaves thin headroom above the minimum |
| request_42 | affordable_now / full_payment | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum; recurring outflow exceeds recurring income |
| request_45 | affordable_later / wait | the recommendation depends on message evidence (status would be not_affordable, method would be not_recommended); the plan leaves thin headroom above the minimum |
| request_47 | not_affordable / not_recommended | no safe plan completes by the desired completion date; the recommendation depends on message evidence (status would be affordable_now, method would be full_payment, amount_safe_to_pay would be 38114000.0); some evidence was ignored or kept at the safer figure (see notes) |
| request_50 | affordable_with_plan / full_payment | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum; recurring outflow exceeds recurring income |
| request_58 | affordable_later / wait | the best safe plan completes on 2025-02-15, after the desired date 2025-01-25; some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum |
| request_59 | affordable_with_plan / installments | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum; no confirmed income is forecast |
| request_61 | affordable_with_plan / installments | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum; no confirmed income is forecast |
| request_73 | affordable_later / wait | the best safe plan completes on 2023-02-15, after the desired date 2023-02-13; the recommendation depends on message evidence (status would be affordable_now, method would be full_payment, amount_safe_to_pay would be 71400.0); the plan leaves thin headroom above the minimum |
| request_75 | affordable_with_plan / installments | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum; no confirmed income is forecast |
| request_92 | affordable_with_plan / full_payment | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum |
| request_98 | affordable_later / wait | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum |
| request_104 | affordable_later / wait | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum; recurring outflow exceeds recurring income |
| request_107 | affordable_with_plan / installments | the recommendation depends on message evidence (status would be not_affordable, method would be not_recommended, amount_safe_to_pay would be 0.0); the plan leaves thin headroom above the minimum; recurring outflow exceeds recurring income |
| request_111 | affordable_now / full_payment | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum |
| request_114 | affordable_now / full_payment | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum |
| request_123 | affordable_now / full_payment | some evidence was ignored or kept at the safer figure (see notes); the plan leaves thin headroom above the minimum |

## Reading this

- A concern that appears on most requests is a property of the dataset, not of those users (e.g. thin headroom when requests are sized near the limit).
- `completes_by_deadline` fails whenever no safe plan meets the desired date (a `not_affordable`, or a `wait` past it); it never triggers a re-plan. Any other failed check on the final iteration means the re-plan budget ran out: inspect the request's transcript.
- Low confidence never changes a contract column; it changes what the explanation says and what a reviewer looks at first.
