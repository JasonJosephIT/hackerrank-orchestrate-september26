# Absorption and trust: symmetric evidence weighting in the score layer

Status: design note, not implemented. Written 2026-09-12 for a follow-up session.
Scope: `code/buyorwait/score.py` only. See D4 in `docs/DECISIONS.md` for the boundary rule.

## Motivation

A long-standing, frequent, stable expense (user_06's transport habit: about 27 EUR every
5 days, 35 settled occurrences) is something the user has visibly been living with. Their
balance history already reflects it, so it says little about *new* risk. Today the score
layer counts it at full weight in `commitment_load`, the same as a brand-new 24-month
installment plan.

On the income side the layer is a blunt step: `income_reliability` is 100 when every
income stream has a constant amount, 60 otherwise, minus 20 when a note mentions a
pending payout or a stream that is not forecast. An employer message announcing a raise
is either believed fully or not at all.

The idea: use one evidence curve on both sides. A frequent recurring expense
progressively has *less* impact on the commitment factor as it becomes established,
in the same way a recurring income increase progressively has *more* impact on the
reliability factor as it is confirmed.

## Current code (for reference)

`components()` in `code/buyorwait/score.py`:

- `commitment_load = 100 * (1 - fixed_m / income_m)` where `fixed_m` is the monthly
  sum of recurring debits with `flexibility == "fixed"`, every stream at face value.
- `income_reliability`: 100 if all income streams have a single distinct amount in
  history, else 60; minus 20 if any note contains "pending" or "not forecast".
- `savings_behaviour = 100 * (income_m - outflow_m) / income_m`.
- `income_m`, `outflow_m` come from `FinancialState.monthly_income()` and
  `monthly_outflow()` in `code/buyorwait/intake.py`, both face value.

Each `Recurrence` already exposes what is needed: `amount`, `cadence_days`
(0 = calendar-monthly), `occurrences`, `history` (list of settled amounts, home
currency), `flexibility`, `protected`, `direction`, `first_amount`, `description`,
`next_date`. Income streams created from messages carry `occurrences=0` and
`history=[]` (see `_apply_facts` in intake.py), which is exactly the "unproven
increase" case.

## The shared mechanism: an evidence curve

For every recurring stream compute one number in [0, 1] saying how established it is.

```
tenure      = months of settled history for the stream
              (occurrences * cadence_days / 30, or occurrences for monthly items)
stability   = max(0, 1 - pstdev(history) / mean(history))   # 1 for constant amounts
established = (1 - exp(-tenure / TAU)) * stability           # TAU = 3.0 months
```

Reference points with stability 1: 3 months -> 0.63, 6 months -> 0.86, 12 months -> 0.98.

Tenure is in months, not occurrences, so a 5-day item is not absorbed faster merely
because it fires more often; more observations only tighten `stability`.

## Expense side: absorption

Weight of a recurring debit in the commitment factor decays with `established` but
never reaches zero, because the cash still leaves the account.

```
LAMBDA = 0.6                                   # floor weight = 1 - LAMBDA = 0.4
expense_weight  = 1 - LAMBDA * established
commitment_load = 100 * (1 - sum(amount_m(r) * expense_weight(r) for fixed debits) / trusted_income)
```

Examples: user_06's transport stream (35 occurrences, cadence 5 -> tenure about 5.8
months, moderate variance) lands near 0.5. A new installment plan injected by
`expense_impact()` has no history, so `established = 0` and it counts at full weight.
That is the behaviour a "commitment" score should have: novelty and rigidity matter,
not just size.

Decide explicitly whether `flexibility != "fixed"` debits also enter the weighted sum;
today only fixed ones do, and `flexibility` is scored separately.

## Income side: trust

Mirror image, applied to *increases* only. Split each income stream into the level
proven by settled history and any increase above it (from a payroll message, a
scheduled row, or a `first_amount` override).

```
proven_level    = median of settled history at the old level (0 if no history)
increase        = max(0, forecast_amount - proven_level)
trust           = established computed with tenure = settled payslips at the NEW level
trusted_income  = proven_level + increase * trust        # per stream, then summed monthly
```

A raise announced by the employer with zero payslips behind it counts partially
(`trust` from the message alone can be seeded at a small constant such as 0.25 so
a confirmed message is not ignored). After two or three settled credits at the new
level it counts nearly in full. Decreases are always taken at face value (safer
interpretation, conflict rule 4 in the problem statement).

`income_reliability` then becomes a smooth number instead of a step:

```
income_reliability = 100 * trusted_income / face_value_income        # 100 when nothing is unproven
                     - 20 if any note mentions "pending" or "not forecast"  (keep existing penalty)
```

Use `trusted_income` as the denominator in `commitment_load` and `savings_behaviour`
so the three components move together.

## Guardrails

1. Score layer only. The forecast (`forecast.py`), plan enumeration (`plans.py`) and all
   seven output columns keep every stream at face value. The hidden ground truth is
   computed from actual cash flow; D4 fixes that boundary and nothing here may cross it.
   `expense_impact()` must keep injecting the request at full value into the forecast.
2. Constants (`TAU`, `LAMBDA`, the message seed) live next to `WEIGHTS` with a comment,
   tuned only for interpretability, never against the samples' contract columns.
3. Keep the floor. With `LAMBDA = 0.6` an absorbed habit still explains 40% of what it
   would otherwise, which reads sensibly in an explanation.
4. Put it behind a config flag (`ABSORPTION = True`) so the packets and `--explain`
   output can be compared before and after.

## Where it lands

- `code/buyorwait/score.py`: add `established(r)`, `expense_weight(r)`,
  `trusted_income(state)`; change the `commitment_load` line, the `income_reliability`
  block, and the two `income_m` denominators.
- `code/buyorwait/intake.py`: no changes needed; optionally add a helper that returns
  the proven level per income stream so score.py does not re-derive it.
- `code/buyorwait/explain.py`: the decision packet already carries the two weakest
  components and top impact drivers; consider adding one line per absorbed stream
  ("transport spend is a long-standing habit already reflected in the balance") so the
  LLM can say what moved the score and what did not.
- `docs/DECISIONS.md`: record as D12 with the constants and the before/after table.

## Validation plan

- `python3 code/main.py --samples --no-llm` then `python3 code/evaluation/score_samples.py`
  must show the contract columns unchanged (25/25/24/24/24, 12 exact amounts as of D11).
- `python3 -m pytest -q tests` (19 tests) unchanged.
- Print `composite_before`, `composite_after`, `commitment_load`, `income_reliability`
  for all 25 samples with the flag off and on. Expected direction: samples with old
  stable habits (06, 24, 25 transport) see commitment load rise (less loaded);
  samples whose income comes from a fresh message (first salary in 15, raise in 36/54
  of the evaluation set) see reliability fall below 100 until confirmed.
- Spot-check `python3 code/main.py --explain request_06` reads correctly.

## Background: why sample 06 prompted this

Sample 06 was fixed in D11 by not projecting a sub-weekly stream due on the request
date. That fix is in the forecast and is what the reference does. This note is the
separate, score-only question it raised: how the *explanation* should weigh an
established habit against a new commitment.
