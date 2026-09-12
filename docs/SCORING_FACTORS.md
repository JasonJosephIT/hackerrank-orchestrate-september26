# Two-factor account profile: Spending Factor and Stability Factor

Plan for a per-user pair of scores that sit beside the contract engine (D4 boundary: they never decide a contract column). Both are 0–100, computed from the same reconstructed state the forecast uses (`intake.py` recurrences, `forecast.py` projection), so numbers never disagree with the recommendation. This document says what each factor is made of, which dataset input feeds it, what has to be assumed, and how to validate it.

## 0. What the data gives us (measured on the dataset, 275 users)

| Need | Available? | Where | Notes |
|---|---|---|---|
| Transaction history | yes, ~6 months per user (169–196 days) | `financial_events.csv` settled rows | enough for monthly aggregates (5–6 points), not for seasonality |
| Commitments (rent, utilities, insurance, education, subscriptions) | yes | recurrences detected by `intake.py` | fixed vs variable amount already known per stream |
| Debt | payments only (112 users have a `debt_payment` stream) | `event_type = debt_payment`, retry/failed rows, bank messages | **no principal, rate, or credit limit** — debt burden must be a flow ratio |
| Liabilities (upcoming) | yes | `pending` / `scheduled` debits, rent-increase messages | |
| Income | yes; 165/275 users have a perfectly regular salary, the rest are gig/freelance/mixed | `event_type = income`, scheduled `Next confirmed salary` (47 users), employer/service messages (126 + 31 users) | messages carry the forward-looking signal |
| Savings history | **not provided** | — | only proxies: monthly net surplus, `investment_purchase` rows (29 users), balance headroom over `minimum_balance_to_keep` |
| Preferences | yes | `financial_profiles.csv`: priorities, protected categories, willing-to-reduce/stop, accepted methods, `max_installment_months`, minimum balance | |
| Balance history | **no** (only the current balance) | — | volatility of the balance itself cannot be measured; use projected path instead |
| Credit score, employment tenure, dependants, assets | no | — | must not be assumed |

All money is normalised to the user's home currency; every component is a ratio or a coefficient of variation so INR and EUR users are comparable.

## 1. Spending Factor — "how stable are this person's outflows and habits" (0–100, higher = steadier)

| # | Component | Formula (monthly buckets over the settled history) | Inputs | Weight |
|---|---|---|---|---|
| S1 | Outflow regularity | `100 × (1 − CV(monthly total debits))`, CV = std/mean, clipped 0–100 | all settled debits | 0.20 |
| S2 | Committed share | share of outflow that belongs to detected recurrences (fixed-amount or monthly cadence) vs one-off rows: `100 × committed / total` | recurrences vs unmatched rows | 0.15 |
| S3 | Commitment load | `100 × (1 − fixed monthly commitments / monthly income)`, floored 0; if no income, 0 | rent, utilities, insurance, education, subscriptions, `debt_payment`; income streams | 0.20 |
| S4 | Commitment fluctuation | `100 × (1 − mean CV of amount within each variable commitment stream)` (utilities, healthcare, groceries…); fixed subscriptions contribute CV 0 | per-stream `history` from `Recurrence` | 0.10 |
| S5 | Discretionary share | `100 × (1 − discretionary / total outflow)` where discretionary = dining, shopping, entertainment, streaming, gym, delivery, music, cloud; dataset median 0.17 (p10 0.08, p90 0.27) so rescale 0.30→0, 0.05→100 | categories | 0.15 |
| S6 | Debt service | `100 × (1 − debt_payment / income)` rescaled so 0% → 100 and 30% → 0; users without a debt stream get 100 | `debt_payment` rows | 0.10 |
| S7 | Payment friction | start at 100, −15 per failed debit, −10 per cancelled/duplicate charge, −10 per bank "debit retry" or "two card minimums" message in the last 90 days, floor 0 | `status`, bank messages | 0.10 |

Spending Factor = weighted mean. Interpretation bands: ≥70 steady, 45–69 mixed, <45 irregular.

What it does **not** need: any assumption. Every component is computable from the files. Caveat: with ~6 monthly points, CVs are noisy; use population-median smoothing (shrink each user's CV halfway toward the dataset median when fewer than 5 months exist).

## 2. Stability Factor — "how dependable are the inflows and obligations going forward" (0–100, higher = more stable), then × preference multiplier

| # | Component | Formula | Inputs | Weight |
|---|---|---|---|---|
| T1 | Income regularity | 100 if one stream with identical amounts monthly; 80 if identical amounts, non-monthly cadence; `100 × (1 − CV(monthly income))` for variable/gig income; 0 if no income | settled income rows, `intake.py` stream detection | 0.25 |
| T2 | Income confirmation (messages) | start at the T1 value's sign-neutral 60; +25 scheduled `Next confirmed salary` row; +20 "first salary confirmed" / "salary resumes" / "salary increased"; +10 "approved invoice"; −40 "employment ended" / "seasonal contract ended" / "final employer payroll"; −25 "platform payout pending"; −15 "temporary reduced pay" / "next salary reduced"; −10 "salary date moved"; 0 for bonus/commission pending (they are simply excluded); clip 0–100 | `evidence.py` facts (already typed and validated) | 0.25 |
| T3 | Debt stability | 100 if `debt_payment` amounts are identical month to month and no retries; −20 per amount change > 5%; −25 per retry/failed debit; users with no debt: 100 | `debt_payment`, retry rows | 0.15 |
| T4 | Liability stability | `100 × (1 − (pending + scheduled debits in the next 30 days) / monthly income)`, −10 if a rent-increase message exists, −5 per new subscription started in the last 60 days | pending/scheduled rows, messages | 0.15 |
| T5 | Savings behaviour (proxy) | `surplus rate = (income − outflow) / income` per month → `100 × clip(mean rate / 0.30)`; bonus +10 if `investment_purchase` rows recur; −20 if the 84-day projected trough is below `minimum_balance_to_keep` even without the request | forecast projection, income/outflow, investment rows | 0.20 |

Base Stability = weighted mean.

**Preference multiplier** `m` (0.90–1.10), from `financial_profiles.csv`:

- +0.03 if `emergency_savings` or `debt_repayment` is a stated priority (self-declared prudence)
- +0.02 if the user is willing to reduce **and** stop at least one category each (they have levers)
- +0.02 if `minimum_balance_to_keep ≥ 1 month of outflow` (self-imposed buffer), −0.02 if `< 0.25 month`
- −0.03 if the only accepted method is `installments` or `partial_payment` (never full payment: liquidity-constrained by preference)
- +0.02 if the accepted methods include `full_payment`

Stability Factor = clip(Base × m, 0, 100). Bands: ≥70 dependable, 45–69 watch, <45 fragile.

Assumptions this factor makes (state them in the UI/explanation):

1. Savings behaviour is inferred from cash surplus and investment purchases because no savings account is supplied.
2. Debt stability is inferred from payment flows because no principal or limit is supplied; a stable payment on an unknown balance is treated as stable.
3. Message effects are point estimates chosen by hand (the ± values above); they are the only tunable constants without ground truth.
4. Stated preferences are trusted as declared; there is no behavioural check that a user actually keeps their minimum balance.

## 3. How the two factors combine with a request (Expense Impact)

Keep the existing Expense Impact mechanic (D4): re-run the components with the request injected and report deltas. With two factors the headline becomes:

- **Hurt** = `requested_amount / headroom` (headroom = projected trough − minimum), 0–100.
- **Absorb capacity** = `0.5 × Spending + 0.5 × Stability` (a steady spender with dependable income absorbs a given hurt better).
- **Verdict band** = fine if hurt < 0.5 × capacity, caution if hurt < capacity, unsafe otherwise — always consistent with the engine because a plan that breaks the minimum balance already has hurt ≥ 100.

Adding a *future* obligation (a trip in two months) is the same injection with a later date; the deltas on S3 (commitment load), T4 (liabilities) and T5 (savings) become the "how much will this hurt my score" answer.

## 4. Mapping to the current `score.py`

| Current component | Becomes |
|---|---|
| liquidity_buffer | T5 input (trough check) and Hurt denominator |
| commitment_load | S3 |
| spending_volatility | S1 + S4 |
| flexibility | preference multiplier (+0.02 lever rule) and S5 |
| income_reliability | T1 + T2 |
| savings_behaviour | T5 |
| (new) | S2, S6, S7, T3, T4 |

## 5. Validation without labels

1. Distribution sanity: both factors should spread across 0–100 on the 275 users (report deciles); if S or T collapses into one band, rescale that component.
2. Ordering check on the 25 samples: users with `affordable_now` should have higher Stability on average than `not_affordable`; report the rank correlation (D4 target).
3. Message ablation: recompute T2 with messages removed; the users whose band changes must be exactly the 126 with employer messages.
4. Consistency invariant (unit test): the verdict band can never be "fine" when the engine says `not_affordable`.
5. Explanation audit: sample 20 explanations and check that the "weakest component" named by the LLM is the arithmetic minimum of the factor's components.

## 6. Build order (fits one block, ~90 min)

1. `score.py`: add `spending_factor(state)` and `stability_factor(state, facts, profile)` returning component dicts + composite; keep the old `spending_score` as a thin wrapper for compatibility.
2. `main.py`: write both factors and the bands into `code/evaluation/scores.csv` (one row per request) and into the decision packet's compact score (composite S, composite T, band, weakest component of each).
3. Tests: invariant 4, a synthetic user with a `final payroll` row (T2 must drop), a user with no debt (S6 = T3 = 100).
4. `--explain` CLI prints the two factor tables.
