"""Memory for the agentic runtime (docs/AGENTIC.md, D13).

Three tiers, all keyed to one user and one request:

* long-term  — the account cards (`CardStore`, D14): one compact card per user as of the request date,
               built once by the deterministic intake + score layers, ~0.5 MB for all users. The raw
               dataset (~21 MB) is only needed to build cards; once they exist it can be dropped.
* episodic   — the user's card recalled *at request time* (or, without cards, `build_state` over the
               dataset): only this user's history, cut at `request_date`. Nothing from other users,
               nothing dated after the request (except messages tied to the request itself).
* working    — the scratchpad the orchestrator and its workers write to while handling the request:
               projection, candidate plans, decision, scores, audit, reflection. Cleared per request.

Every tool call is appended to the ledger, which becomes the agent transcript.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from ..intake import Dataset, FinancialState, build_state
from ..plans import Request
from .cards import AccountCard, CardStore


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
    """Process-wide, read-only. One instance per run. With `cards` set, `ds` may be None."""
    ds: Dataset | None
    image_facts: dict[str, float]
    verify_context: dict | None = None   # verify.load_context(...) for the auditor
    cards: CardStore | None = None
    _factors: Any = None      # pandas DataFrame from factors.account_factors, computed on first use

    def account_factors(self):
        if self._factors is None:
            if self.ds is None:
                raise RuntimeError("no dataset loaded; account factors come from the card")
            from ..factors import account_factors
            self._factors = account_factors(self.ds)
        return self._factors

    def profile(self, user_id: str):
        if self.ds is None:
            raise RuntimeError("no dataset loaded; the profile comes from the card")
        return self.ds.profiles.loc[user_id]

    @property
    def source(self) -> str:
        return "cards" if self.cards is not None else "dataset"


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
    @property
    def card(self) -> AccountCard | None:
        if self.long_term.cards is None:
            return None
        return self.long_term.cards.get(self.request.user_id, self.request.request_date, self.request.request_id)

    def recall(self, use_messages: bool = True) -> FinancialState:
        """Retrieve this user's history as of the request date: from the account card when one exists,
        else rebuilt from the dataset. Only the user's own rows; settled history before request_date drives
        recurrence, pending/scheduled rows are reserved, messages are those sent on or before request_date
        or tied to this request."""
        card = self.card
        if card is not None:
            st = card.to_state(use_messages=use_messages)
            self.put("recall_source", "card")
        else:
            if self.long_term.ds is None:
                raise RuntimeError(f"no card for {self.request.user_id}@{self.request.request_date} and no dataset loaded")
            st = build_state(self.long_term.ds, self.request.user_id, self.request.request_date,
                             self.long_term.image_facts, cfg={"use_messages": use_messages},
                             request_id=self.request.request_id)
            self.put("recall_source", "dataset")
        if use_messages:
            self.state = st
        return st

    @property
    def profile(self) -> pd.Series:
        card = self.card
        return card.profile_row() if card is not None else self.long_term.profile(self.request.user_id)

    @property
    def options(self) -> pd.DataFrame:
        """The seller/provider options supplied for this request."""
        card = self.card
        if card is not None:
            return card.options_frame()
        ds = self.long_term.ds
        return ds.options[ds.options.request_id == self.request.request_id]

    def factors(self) -> dict | None:
        """This user's two-factor profile: from the card, else the cross-user table."""
        card = self.card
        if card is not None:
            return card.factors
        tbl = self.long_term.account_factors()
        if self.request.user_id not in tbl.index:
            return None
        row = tbl.loc[self.request.user_id]
        return dict(spending_factor=float(row.spending_factor), spending_band=str(row.spending_band),
                    stability_factor=float(row.stability_factor), stability_band=str(row.stability_band))

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
