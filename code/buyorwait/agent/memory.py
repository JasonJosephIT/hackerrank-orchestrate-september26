"""Memory for the agentic runtime (docs/AGENTIC.md, D13).

Three tiers, all keyed to one user and one request:

* long-term  — the dataset loaded once per process (`Dataset`), read-only, shared by every request;
               plus the cross-user account factors table, computed lazily once and cached.
* episodic   — the user's financial history recalled *at request time*: `build_state` filters the
               event, message and image evidence for this user as of `request_date`. Nothing from other
               users, nothing dated after the request (except messages tied to the request itself).
* working    — the scratchpad the orchestrator and its workers write to while handling the request:
               projection, candidate plans, decision, scores, audit, reflection. Cleared per request.

Every tool call is appended to the ledger, which becomes the agent transcript.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..intake import Dataset, FinancialState, build_state
from ..plans import Request


@dataclass
class LedgerEntry:
    seq: int
    worker: str
    tool: str
    args: dict
    summary: str
    ms: float
    ok: bool = True


@dataclass
class LongTermMemory:
    """Process-wide, read-only. One instance per run."""
    ds: Dataset
    image_facts: dict[str, float]
    verify_context: dict | None = None   # verify.load_context(...) for the auditor
    _factors: Any = None      # pandas DataFrame from factors.account_factors, computed on first use

    def account_factors(self):
        if self._factors is None:
            from ..factors import account_factors
            self._factors = account_factors(self.ds)
        return self._factors

    def profile(self, user_id: str):
        return self.ds.profiles.loc[user_id]


@dataclass
class UserMemory:
    """Everything the orchestrator knows about one user while serving one request."""
    long_term: LongTermMemory
    request: Request
    state: FinancialState | None = None            # episodic recall (set by recall_user_history)
    working: dict[str, Any] = field(default_factory=dict)
    ledger: list[LedgerEntry] = field(default_factory=list)
    _seq: int = 0

    # ---- episodic recall -------------------------------------------------------------
    def recall(self, use_messages: bool = True, request_date: date | None = None) -> FinancialState:
        """Retrieve this user's history as of the request date. Only the user's own rows are read;
        settled history before request_date drives recurrence, pending/scheduled rows are reserved,
        messages are those sent on or before request_date or tied to this request."""
        st = build_state(self.long_term.ds, self.request.user_id, request_date or self.request.request_date,
                         self.long_term.image_facts, cfg={"use_messages": use_messages},
                         request_id=self.request.request_id)
        if use_messages and request_date is None:
            self.state = st
        return st

    @property
    def profile(self):
        return self.long_term.profile(self.request.user_id)

    # ---- working memory ----------------------------------------------------------------
    def put(self, key: str, value: Any) -> None:
        self.working[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.working.get(key, default)

    def require(self, key: str) -> Any:
        if key not in self.working:
            raise KeyError(f"working memory has no {key!r}; a prerequisite step has not run")
        return self.working[key]

    # ---- ledger ------------------------------------------------------------------------
    def record(self, worker: str, tool: str, args: dict, summary: str, ms: float, ok: bool = True) -> LedgerEntry:
        self._seq += 1
        e = LedgerEntry(self._seq, worker, tool, args, summary, round(ms, 1), ok)
        self.ledger.append(e)
        return e

    def transcript(self) -> list[dict]:
        return [dict(seq=e.seq, worker=e.worker, tool=e.tool, args=e.args, summary=e.summary, ms=e.ms, ok=e.ok)
                for e in self.ledger]


class Stopwatch:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *a):
        self.ms = (time.perf_counter() - self.t0) * 1000.0
