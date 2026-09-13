# Conventions — Buy or Wait? (Layer 3)

> Canonical sources: `AGENTS.md` (contract §6, logging §2/§5, rules §4) and `README.md` (setup, run commands). This file is the quick reference; when they disagree, `AGENTS.md` wins.

## Files and folders

- Engine modules live in `code/buyorwait/`; the agentic runtime in `code/buyorwait/agent/`; the entry point is `code/main.py`.
- Derived caches go under `.cache/` (gitignored): `cards.jsonl`, `index/`, `traces*.jsonl`, `agent_transcripts.jsonl`. Rebuild, never commit.
- `output.csv` (repo root) is the submission artefact; `code/evaluation/usage_report.md` must describe the run that produced it.
- Organizer-only files stay outside `dataset/` and are never read by the solution.
- `log.txt` is append-only, gitignored, one per checkout (AGENTS.md §2).

## Code

- Python 3.11, pandas; deterministic engine, no model calls on the decision path. The LLM writes `decision_explanation` only (opt-in: plans, critiques).
- Every tool wraps an existing engine function; a new capability goes in the engine first, then gets a tool.
- Contract columns must stay byte-identical across the linear pipeline, the card path and the disk path: extend `tests/test_agent.py` parity tests with any change.
- Numbers: `formatting.fmt_amount` / `fmt_plain` for output; ISO dates in data, `15 June 2024` in prose.
- Config via environment variables only (`GROQ_API_KEY`, `BUYORWAIT_*`); never commit `.env`.

## Commands

```
python3 code/main.py                  # full run -> output.csv (cards in RAM, tables on disk)
python3 code/main.py --samples --no-llm
python3 code/main.py --explain request_12 --samples
python3 -m pytest -q tests
python3 code/evaluation/score_samples.py
python3 code/evaluation/build_usage_report.py
python3 code/package.py               # code.zip
```

## Design log

- Append a `D<n>` entry to `docs/DECISIONS.md` for every user-directed design change: Decision / Why / Rejected, with measurements.
