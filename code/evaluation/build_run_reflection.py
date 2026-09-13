"""Build evaluation/run_reflection.md from the last full run's traces and transcripts (D19).

    python3 code/evaluation/build_run_reflection.py [.cache/traces.jsonl] [.cache/agent_transcripts.jsonl]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "code"))

from buyorwait.agent.run_reflection import write_report  # noqa: E402

if __name__ == "__main__":
    traces = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".cache" / "traces.jsonl"
    transcripts = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / ".cache" / "agent_transcripts.jsonl"
    out = write_report(traces, transcripts, ROOT / "code" / "evaluation" / "run_reflection.md")
    print(out)
    print(out.read_text())
