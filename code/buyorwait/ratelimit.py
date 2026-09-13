"""Shared Groq tokens-per-minute pacing (D13), used by both explain.py and evidence.py so a single
run never exceeds the account's per-minute budget across both call sites.
"""
from __future__ import annotations

import os
import time

TOKENS_PER_MINUTE = int(os.environ.get("BUYORWAIT_EXPLAIN_TPM", "7000"))  # Groq free tier: 8000 TPM per model
_pace = {"next_ok": 0.0}


def pace_before_call() -> None:
    wait = _pace["next_ok"] - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def pace_after_call(total_tokens: int) -> None:
    _pace["next_ok"] = time.monotonic() + 60.0 * total_tokens / max(TOKENS_PER_MINUTE, 1)
