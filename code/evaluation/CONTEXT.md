# code/evaluation — Scoring and reporting (ICM Layer 2)

## Purpose

Measure the engine against the 25 solved samples and report the model usage of the final run (`AGENTS.md` §6.5).

## Folder structure

```
evaluation/
├── score_samples.py        # Per-field match of sample_output.csv vs dataset/sample_requests.csv
├── build_usage_report.py   # .cache/traces.jsonl -> usage_report.md (per model, per stage, cost)
├── usage_report.md         # Required in code.zip; must describe the run that produced output.csv
├── build_run_reflection.py # traces + transcripts -> run_reflection.md (written automatically after a full run)
├── run_reflection.md       # What the orchestrator did across the run: outcomes x confidence, checks, concerns, timings
├── sample_output.csv       # Last `--samples` run
├── packets.jsonl / sample_packets.jsonl   # Decision packets of the last full / samples run
├── run_factors.py, factors.csv            # Two-factor profile for all users (D12)
└── main.py                 # Legacy evaluation entry (kept)
```

## Routing

| Task | Go to | Load first |
|---|---|---|
| Score the samples | `python3 code/main.py --samples --no-llm && python3 code/evaluation/score_samples.py` | — |
| Regenerate the usage report after a full run | `python3 code/evaluation/build_usage_report.py` | `AGENTS.md` §6.5 |
| Inspect a decision packet | `packets.jsonl` (one JSON per request) | `_config/glossary.md` |
