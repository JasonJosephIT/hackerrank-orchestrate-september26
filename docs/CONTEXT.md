# docs — Design record (ICM Layer 2)

## Purpose

The interview script and the append-only design log. Read `DECISIONS.md` before changing behaviour: most rules were calibrated on the samples and the reasons are recorded.

## Folder structure

```
docs/
├── DECISIONS.md         # D1–D16, dated: Decision / Why / Rejected (append-only)
├── AGENTIC.md           # Memory tiers, orchestrator loop, workers, tools, gate, disk store (D13–D15)
├── SCORING_FACTORS.md   # Two-factor account profile (D12 groundwork)
├── ABSORPTION.md        # Symmetric absorption/trust weighting in the score layer (D12)
├── PLAN.md              # Timebox and progress of the build session
└── INTERVIEW.md         # Talking points
```

## Routing

| Task | Go to | Load first |
|---|---|---|
| Why does the engine do X | `DECISIONS.md` (search `D<n>`) | — |
| How a request is served | `AGENTIC.md` | `_config/glossary.md` |
| Add a decision | append to `DECISIONS.md` | `_config/voice.md` |
