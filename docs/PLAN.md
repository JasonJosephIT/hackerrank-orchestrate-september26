# PLAN.md — Buy or Wait? (HackerRank Orchestrate, September 2026)

**Kickoff:** Sat 2026-09-12 13:40 EDT · **Deadline:** Sun 2026-09-13 08:30 EDT (18:00 IST / 12:30 UTC) · **Time at kickoff:** 18h 50m
**Submit at:** https://www.hackerrank.com/contests/hackerrank-orchestrate-september26/challenges/buy-or-wait/submission
**Hard rule:** nothing new starts after 06:40 EDT (last 10%); that window is packaging only. Target upload by **07:30 EDT**.

## The task in five lines

1. Predict 7 fields for each of 250 requests: `amount_safe_to_pay`, `affordability_status`, `recommended_payment_method`, `payment_plan`, `earliest_date_for_full_payment`, `spending_changes_needed`, `decision_explanation`.
2. Four scored artifacts: `output.csv` (30%), `code.zip` incl. `evaluation/usage_report.md` (30%), 30-min AI Judge voice interview (30%), `log.txt` transcript (10%).
3. Safety = 90-day balance forecast never below `minimum_balance_to_keep`, request completed by `desired_completion_date`.
4. Ranking among safe eligible plans: deadline met → no spending changes → min total paid → earlier start → fewer payments → lowest `payment_option_id`.
5. Biggest risk: a malformed `output.csv` scores zero on 30%. The validator is built first and runs before every write.

## Dataset facts that shape the build (measured at kickoff)

- 250 eval requests, 25 solved samples (3 affordable_now / 9 with_plan / 6 later / 7 not_affordable; methods: 7 not_recommended, 6 full, 6 wait, 5 installments, 1 partial).
- 25,342 events, ~92 per user. Status: 25,148 settled, 71 pending, 70 scheduled, 22 cancelled, 21 failed, 10 unrealized. Types: expense 20.5k, subscription 2.5k, income 1.7k, debt_payment 567, investment_* 44, refund 22.
- Flexibility: fixed 21.1k, reducible 2.7k (with `minimum_allowed_amount`), stoppable 1.3k, reducible_or_stoppable 225.
- 16 events with blank `amount` → exactly the 16 images (payslips/bills). 5 of them belong to sample users (03, 16, 17, 19, 20) — free validation of the image layer.
- 140 foreign-currency events; FX pairs EUR↔USD, EUR→ZAR, USD→IDR, USD→INR, dated.
- 215 messages (126 employer / 31 service_provider / 23 financial_service / 18 bank / 17 merchant), multilingual (Indonesian, others). 128 tied to a request, 39 to an event.
- Payment options: 2–4 per request, 275 full_payment + 515 installments; installment counts 2/3/4/6/15/18/21/24. Profiles: 119 users won't consider installments (`max_installment_months` blank); 60 accept full_payment only.
- Requests: 80 allow partial payment, 170 do not. Request dates span 2023-01 → 2026-09, so "today" is always `request_date`, never the wall clock.

## Architecture (see docs/DECISIONS.md D1–D3)

Deterministic pipeline → Spending Score + Expense Impact (D4) → typed decision packet → Groq Llama 3.3 writes `decision_explanation` and the coaching narrative in terms of the score drivers (template fallback). Contract columns are never touched by the LLM or the score. Evidence layer is regex-first with LLM structured-output fallback; image amounts extracted once via Groq Llama-4 Scout into a hand-verified JSON cache. Independent verifier gates `output.csv`. Sample scorer is the iteration signal.

```
code/
  main.py                      entry: python3 code/main.py  → ./output.csv
  buyorwait/
    intake.py                  load/join/FX/dedupe/status/recurrence/conflicts
    evidence.py                messages + image facts (deterministic-first, cached)
    forecast.py                90-day projection, amount_safe_to_pay, earliest_date
    plans.py                   candidate plans, eligibility, ranking, spending changes
    score.py                   Spending Score components + Expense Impact delta (D4)
    verify.py                  contract validator (also runnable standalone)
    explain.py                 Groq client wrapper + template fallback
    telemetry.py               OpenTelemetry tracer, GenAI span attrs, JSONL exporter (+ optional OTLP)
  evidence/image_facts.json    cached image extractions (source, confidence)
  evaluation/
    score_samples.py           per-field match vs sample_requests.csv
    build_usage_report.py      traces.jsonl (gen_ai.* spans) → usage_report.md
    usage_report.md
tests/                         pytest, one file per module
docs/PLAN.md, docs/DECISIONS.md
```

## Timebox (18.8h clock, EDT)

| # | Block | Window | Goal | Exit criterion |
|---|---|---|---|---|
| 0 | Orientation | 13:40–14:15 ✅ | Spec digest, dataset profile, plan, baseline run | Done: baseline `output.csv` (250 rows) written |
| 1 | Contract first | 14:15–15:45 | `verify.py` (all §6.2 rules, `--self-test` on samples), `score_samples.py`, Groq wrapper instrumented with OpenTelemetry spans (D5) + JSONL span exporter, `GROQ_API_KEY` set | Validator passes on samples & baseline; scorer prints 25-row diff |
| 2 | Financial state | 15:45–19:00 | intake: FX, dedupe, statuses, recurrence detection, conflict rules; forecast: 90-day path, `amount_safe_to_pay`, `earliest_date` | `amount_safe_to_pay` within 1% on ≥15/25 samples |
| 3 | Plan generation | 19:00–22:15 | eligibility, installment matching, partial rule, wait, spending changes (stop/reduce, ≤3, flexible + permitted only), 6-rule ranking | status + method match ≥18/25; plan strings exact on installments/partial/wait |
| 3b | Spending Score + Expense Impact | 22:15–23:45 | `score.py`: 7 arithmetic components → composite; impact delta by injecting the request/obligation into the 90-day state; threshold bands aligned with the safety rule; `scores.csv` | Score separates affordable_now from not_affordable on the 25 samples (rank correlation reported); impact bands never contradict engine status |
| 4 | Evidence layer | 23:45–01:15 | message regex catalogue (multilingual), LLM fallback with schema + injection guard, image cache; apply amendments to forecast | 16 blank amounts resolved; the 5 sample-user images match sample outcomes; reruns identical |
| 5 | Explanations + full run | 01:15–02:45 | `explain.py` prompt from decision packet, 250-row run, ledger → usage report | root `output.csv` validated; `usage_report.md` has real numbers |
| 6 | Error analysis / stretch (or sleep) | 02:45–05:15 | per-sample diff → fix largest error class → re-score; every change logged with before/after | Score trend recorded in log.txt; no regressions |
| 7 | Packaging | 05:15–07:00 | README (setup/run), `code.zip` (exclude dataset/, venv, caches), final run, `log.txt` check, upload all three | Submission confirmed on HackerRank |
| 8 | Interview prep + buffer | 07:00–08:30 | DECISIONS.md → talking points; `/hackathon-judge-prep` mock | Can explain architecture, trade-offs, safety, evidence in 5 min |

Rules: a block overrunning by 25% is cut to its exit criterion. Every block ends with a commit and a `log.txt` entry stating what was measured. If block 3 exit isn't met by 23:00, cut block 6 and shrink block 4 to messages-only (images stay on the cached JSON).

## Open questions to settle in block 1–2 (read the samples, don't guess)

- How is "recurring" detected — same category+description at ~monthly cadence with ≥2 settled occurrences? Confirm on sample users.
- Does `amount_safe_to_pay` allow the balance to touch exactly `minimum_balance_to_keep` (sample 01: 58,481 balance, min 18,000, safe 25,256 → other commitments explain the gap; reverse-engineer on samples 01/09/16).
- `earliest_date_for_full_payment` on samples 03/08/13/18/23 = `desired_completion_date` exactly — is the date the first safe day, or clamped to the deadline? Check with a sample where salary lands before the deadline (04: 06-15; 19: 09-15; 02: 09-15).
- Installment schedule dates in samples = `first_payment_date` + k·`payment_frequency_days`; verify on 02/07/12/17/22.
- Whether a plan with spending changes counts as "affordable_with_plan + full_payment" only when full payment is otherwise unsafe (samples 06/11/21).

## Resume command

`/hackathon-checkpoint` — reports time left, block progress, sample score trend, keep/cut decision.
