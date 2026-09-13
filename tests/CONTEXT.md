# tests — Regression and parity (ICM Layer 2)

## Purpose

`python3 -m pytest -q tests` must stay green before any push. No network: explanations use the template.

## Folder structure

```
tests/
├── conftest.py              # puts code/ on sys.path
├── test_engine_samples.py   # end-to-end floors on the 25 samples (status, method, plan, dates, changes, amount within 5%)
├── test_agent.py            # tool catalogue, recall scope, plan validation, pipeline = agent parity, re-plan on audit failure,
│                            #   cards served without the dataset, gate exactness, disk slices = dataset, misses served from disk
├── test_forecast.py, test_score.py, test_factors.py, test_evidence.py, test_verify.py   # unit tests per module
```

## Routing

| Task | Go to | Load first |
|---|---|---|
| Changed the engine | run all; check `test_engine_samples.py` floors | `docs/DECISIONS.md` D6 |
| Changed the agent runtime, cards or disk store | `test_agent.py` (extend the parity tests) | `code/buyorwait/agent/CONTEXT.md` |
| Changed the output contract | `test_verify.py` + `verify.py --self-test` | `AGENTS.md` §6.2 |
