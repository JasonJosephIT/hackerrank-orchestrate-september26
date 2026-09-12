# Interview talking points

## 1. The 60-second pitch

For each of 250 requests, "Buy or Wait?" decides: pay in full, partially, by installment option, wait, or do not proceed. A **deterministic pandas engine** reconstructs the cash position, forecasts it daily, ranks plans and verifies the contract. The **LLM** (Groq `openai/gpt-oss-120b`) writes only `decision_explanation` from a typed packet, with a template fallback. On the 25 solved samples: status 24/25, method 24/25, plan 23/25, earliest date 24/25, spending changes 23/25; `amount_safe_to_pay` exact 10/25, within 5% 16/25. The packet is about 650 input tokens (D7); the sample run measured 742 tokens and USD 0.000177 per request, no fallbacks.

## 2. Architecture in five stages

**Intake (`code/buyorwait/intake.py`).** Joins `dataset/*.csv`, converts foreign currency at the dated rate, de-duplicates, applies status rules (pending debits reserved, pending credits ignored), detects recurrence from settled history, resolves conflicts (cancellation, then newer same-source, settled, safer).

**Evidence (`code/buyorwait/evidence.py`).** Thirty regex templates (English and Indonesian) turn messages into validated facts; every message in the dataset matches a template, so the LLM fallback hook stays unused. The 16 blank-amount events come from `code/evidence/image_facts.json`, a one-time hand-verified vision pass.

**Forecast (`code/buyorwait/forecast.py`).** Projects the balance 84 days from `request_date`; safe means never below `minimum_balance_to_keep`. `amount_safe_to_pay` is the projected trough minus the minimum (capped at the request), `earliest_date_for_full_payment` comes from a forward date scan on suffix minima, both without spending changes.

**Plans (`code/buyorwait/plans.py`).** Enumerates full-now, partial, each installment option, wait, and spending-change variants; filters by accepted methods, `max_installment_months`, partial permission, deadline; ranks by the spec's six rules: deadline, no changes, lowest total, earlier start, fewer payments, lowest `payment_option_id`.

**Verify and explain (`verify.py`, `explain.py`).** An independent validator re-checks every row (bounds, enums, plan format, installment match, flexible-only changes); `main.py` refuses to write `output.csv` on any violation. The packet (final numbers, chosen plan, top three rejected plans, evidence notes, compact score) then goes to the LLM for one to three sentences.

## 3. Safety and trust

- **Untrusted inputs.** Messages and images are data, never instructions: regex cannot execute text, and only amounts, dates, percentages and event ids are extracted.
- **Prompt injection.** The LLM never sees raw messages, only final numbers it cannot change.
- **Verifier gate.** Contract violations block the write.
- **Determinism.** Same inputs, same columns; `--no-llm` reproduces the run offline.
- **Secrets.** `GROQ_API_KEY` from the environment or gitignored `.env`; `package.py` excludes dataset, `.env`, caches.
- **OpenTelemetry.** One trace per request with stage spans and `gen_ai.*` attributes in `.cache/traces.jsonl`; `usage_report.md` derives from them.

## 4. Hard calls (chose / evidence / rejected)

- **84-day window.** Sample 13's earliest date only works if its day-87 rent is outside the window; samples 05 and 10 agree. Rejected 90 days (19/25 vs 22/25 earliest dates).
- **Income streams.** Recur only if stable (within 1% of median for at least 60% of occurrences) and monthly; a scheduled next salary anchors; final payroll, ended employment or pending payout stops. Samples 01, 05, 09, 10, 13. Rejected forecasting all credits, or none.
- **Three-week cadence.** A 21-day item's next hit is 14 days after the last when still ahead, then every 21. Samples 02, 03, 21 need it; rejected pure 21-day and a full 14-day remap, which breaks 07 and 12.
- **Safer salary.** "Confirmed base salary" never raises salary above settled history. Sample 11 reconciles only with the settled 23.256M, not the message's 38.76M (conflict rule 4). Rejected trusting the message.
- **Spending changes.** Permitted flexible recurrences (reduce to `minimum_allowed_amount`, else stop), smallest saving first until safe, prune redundant, max three. Samples 06, 21, 11. Rejected largest-first and single-change.
- **Payday ordering (D8, open).** Intra-day (debits before same-day salary) fixes samples 03, 08, 13, 18, 21 and breaks 06, 15, 19, 23, 25. End-of-day stays default (24 vs 22 earliest dates, 16 vs 15 within-5%); queued as a calibration task.

## 5. Spending Score and Expense Impact

`code/buyorwait/score.py`, pure arithmetic, each component 0-100 from the forecast's own state: liquidity buffer, commitment load, spending volatility, flexibility, income reliability, savings behaviour (D4 also sketches obligation trend). Expense Impact re-runs them with the request injected and reports the delta plus a fine / caution / unsafe band aligned to the safety rule. It never decides a contract column, because hidden ground truth follows the spec's ranking rules. It orders which flexible expense to cut, and names the weak facet for the explanation.

## 6. Judge questions

**Why not end-to-end LLM?** Five of seven columns are field-exact; an LLM cannot reproduce a cash forecast to the cent, a loop can. Plus roughly 25k-token contexts per user, no determinism, no unit tests.

**How do you know `amount_safe_to_pay` is right?** Binary search on the projection, calibrated on 25 samples: exact 10/25, within 5% 16/25. The residual is the reference generator's private variable-spend estimate plus the D8 payday split.

**Another day?** Resolve D8 (what separates the two groups), then variable-spend estimation, the largest remaining error.

**What breaks on new data?** Unknown message templates go to the LLM fallback; new images need a vision pass into the cache; a new currency needs FX rows.

**Why gpt-oss-120b?** Llama 3.3 70B and Llama-4 Scout had vanished from the key's model list; it was the strongest model left, $0.15/$0.60 per 1M tokens (D7).

**Rate limits?** Free tier is 8,000 tokens per minute: low reasoning effort, a 400-token budget doubling once if empty, and sleeping for the window the 429 names.

**Where can injection land?** Only in the explanation text; numbers are final first.

**Image layer validation?** Five of 16 images belong to sample users (03, 16, 17, 19, 20), so their outcomes checked the extraction.
