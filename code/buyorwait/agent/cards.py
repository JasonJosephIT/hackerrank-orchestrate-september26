"""Account cards: the compact per-user memory the orchestrator serves requests from (D16).

A card is everything request time needs about one user as of one request date, built once from the
dataset by the deterministic intake and score layers:

    profile preferences   accepted methods, max installment months, protected / reducible / stoppable categories
    state                 balance, minimum, ~10 recurring streams, reserved pending/scheduled flows, notes, facts
    no-message state      the same reconstruction ignoring message facts (only when facts were applied)
    options               the seller/provider payment options supplied for the request
    score                 Spending Score components + composite (from the state)
    factors               the user's row of the cross-user two-factor profile

Cards are ~2 KB each (all 275 users: ~0.5 MB) against ~21 MB for the raw tables, and a card round-trips
through JSON exactly (floats and ISO dates), so a decision served from a card is byte-identical to one
served from the dataset. `CardStore` builds, saves, loads and hands out cards; once it exists the
dataset can be dropped (`LongTermMemory.ds = None`) and every request still runs.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from ..evidence import Fact
from ..intake import Dataset, FinancialState, Recurrence, build_state
from ..plans import Request
from ..profile import profile_features
from ..score import spending_score

CARD_VERSION = 2          # 2: carries reserve / irregular_income (D13) and the account-profile features (D14)
PROFILE_FIELDS = ("user_id", "home_currency", "current_available_balance", "minimum_balance_to_keep", "financial_priorities",
                  "expense_categories_to_protect", "expense_categories_user_is_willing_to_reduce",
                  "expense_categories_user_is_willing_to_stop", "payment_methods_user_will_consider", "max_installment_months")
OPTION_FIELDS = ("payment_option_id", "request_id", "payment_method", "number_of_payments", "payment_amount",
                 "total_payable_amount", "financing_fee", "payment_frequency_days", "first_payment_date")


def _rec_to_dict(r: Recurrence) -> dict:
    d = asdict(r)
    d["next_date"] = str(r.next_date)
    d["end_date"] = str(r.end_date) if r.end_date else None
    return d


def _rec_from_dict(d: dict) -> Recurrence:
    d = dict(d)
    d["next_date"] = date.fromisoformat(d["next_date"])
    d["end_date"] = date.fromisoformat(d["end_date"]) if d["end_date"] else None
    return Recurrence(**d)


def _fact_to_dict(f: Fact) -> dict:
    d = asdict(f)
    d["on"] = str(f.on) if f.on else None
    return d


def _fact_from_dict(d: dict) -> Fact:
    d = dict(d)
    d["on"] = date.fromisoformat(d["on"]) if d["on"] else None
    return Fact(**d)


def _state_to_dict(st: FinancialState) -> dict:
    return dict(currency=st.currency, balance=st.balance, minimum=st.minimum,
                reserve=float(getattr(st, "reserve", 0.0) or 0.0), irregular_income=bool(getattr(st, "irregular_income", False)),
                recurring=[_rec_to_dict(r) for r in st.recurring],
                fixed_flows=[(str(d), a, l) for d, a, l in st.fixed_flows],
                notes=list(st.notes), facts=[_fact_to_dict(f) for f in st.facts],
                profile_features=profile_features(st))      # D14 packet, built while the raw events are at hand


def _state_from_dict(d: dict, user_id: str, as_of: date) -> FinancialState:
    return FinancialState(user_id=user_id, request_date=as_of, currency=d["currency"], balance=d["balance"], minimum=d["minimum"],
                          recurring=[_rec_from_dict(r) for r in d["recurring"]],
                          fixed_flows=[(date.fromisoformat(x), a, l) for x, a, l in d["fixed_flows"]],
                          notes=list(d["notes"]), facts=[_fact_from_dict(f) for f in d["facts"]],
                          events=pd.DataFrame({"user_id": pd.Series([user_id] * 0, dtype=str)}),
                          reserve=float(d.get("reserve", 0.0) or 0.0), irregular_income=bool(d.get("irregular_income", False)))


@dataclass
class AccountCard:
    user_id: str
    as_of: str                       # request_date the card was built for (ISO)
    request_id: str
    profile: dict                    # PROFILE_FIELDS
    state: dict                      # _state_to_dict
    options: list[dict]              # OPTION_FIELDS rows for request_id
    score: dict                      # spending_score components (no private keys)
    factors: dict | None = None      # spending/stability factor + bands
    no_message_state: dict | None = None
    version: int = CARD_VERSION

    @property
    def key(self) -> str:
        return f"{self.user_id}@{self.as_of}"

    # ---- typed views ---------------------------------------------------------------------
    def profile_row(self) -> pd.Series:
        return pd.Series(self.profile)

    def options_frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self.options, columns=list(OPTION_FIELDS))
        if df.empty:
            return df
        for c in ("payment_amount", "total_payable_amount", "financing_fee"):
            df[c] = df[c].astype(float)
        df["number_of_payments"] = df["number_of_payments"].astype(int)
        df["payment_frequency_days"] = pd.to_numeric(df["payment_frequency_days"], errors="coerce")
        return df

    def to_state(self, use_messages: bool = True) -> FinancialState:
        src = self.state if use_messages or self.no_message_state is None else self.no_message_state
        return _state_from_dict(src, self.user_id, date.fromisoformat(self.as_of))

    def profile_features(self) -> dict | None:
        """The account-profile packet (D14) built at card time; None for cards built before version 2."""
        return self.state.get("profile_features")

    def summary(self) -> dict:
        st = self.state
        return dict(user_id=self.user_id, as_of=self.as_of, currency=st["currency"], balance=st["balance"], minimum=st["minimum"],
                    streams=len(st["recurring"]), reserved=len(st["fixed_flows"]), facts=len(st["facts"]),
                    composite=self.score.get("composite"), options=len(self.options),
                    factors=self.factors, bytes=len(json.dumps(asdict(self))))


def build_card(ds: Dataset, req: Request, image_facts: dict[str, float], factors_row=None, cfg: dict | None = None) -> AccountCard:
    st = build_state(ds, req.user_id, req.request_date, image_facts, cfg=cfg, request_id=req.request_id)
    prof = ds.profiles.loc[req.user_id]
    profile = {k: (float(prof[k]) if k in ("current_available_balance", "minimum_balance_to_keep") else str(prof[k])) for k in PROFILE_FIELDS}
    opts = ds.options[ds.options.request_id == req.request_id]
    options = []
    for o in opts.itertuples(index=False):
        row = {k: getattr(o, k) for k in OPTION_FIELDS}
        for c in ("payment_amount", "total_payable_amount", "financing_fee"):
            row[c] = float(row[c])
        row["number_of_payments"] = int(row["number_of_payments"])
        pf = row["payment_frequency_days"]
        row["payment_frequency_days"] = None if pd.isna(pf) else float(pf)
        options.append(row)
    score = {k: v for k, v in spending_score(st).items() if not k.startswith("_")}
    factors = None
    if factors_row is not None:
        factors = dict(spending_factor=float(factors_row.spending_factor), spending_band=str(factors_row.spending_band),
                       stability_factor=float(factors_row.stability_factor), stability_band=str(factors_row.stability_band))
    nomsg = None
    if st.facts:
        nomsg = _state_to_dict(build_state(ds, req.user_id, req.request_date, image_facts, cfg={**(cfg or {}), "use_messages": False},
                                           request_id=req.request_id))
    return AccountCard(req.user_id, str(req.request_date), req.request_id, profile, _state_to_dict(st), options, score, factors, nomsg)


class CardStore:
    """Cards keyed by `user@as_of`, with a request_id index. Build once, save, load."""

    def __init__(self, cards: dict[str, AccountCard] | None = None):
        self.cards: dict[str, AccountCard] = cards or {}
        self.by_request: dict[str, str] = {c.request_id: k for k, c in self.cards.items()}
        self.built_in_s: float | None = None

    def __len__(self) -> int:
        return len(self.cards)

    def get(self, user_id: str, as_of: date, request_id: str | None = None) -> AccountCard | None:
        c = self.cards.get(f"{user_id}@{as_of}")
        if c is None and request_id:
            k = self.by_request.get(request_id)
            c = self.cards.get(k) if k else None
        return c

    def add(self, card: AccountCard) -> None:
        self.cards[card.key] = card
        self.by_request[card.request_id] = card.key

    @classmethod
    def build(cls, ds: Dataset, image_facts: dict[str, float], rows, with_factors: bool = True, cfg: dict | None = None) -> "CardStore":
        from ..plans import request_from_row
        t0 = time.perf_counter()
        table = None
        if with_factors:
            from ..factors import account_factors
            table = account_factors(ds)
        store = cls()
        for row in rows:
            req = request_from_row(row)
            frow = table.loc[req.user_id] if table is not None and req.user_id in table.index else None
            store.add(build_card(ds, req, image_facts, frow, cfg=cfg))
        store.built_in_s = round(time.perf_counter() - t0, 2)
        return store

    def save(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for c in self.cards.values():
                f.write(json.dumps(asdict(c)) + "\n")
        return path.stat().st_size

    @classmethod
    def load(cls, path: Path) -> "CardStore":
        store = cls()
        with path.open(encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                if d.get("version") != CARD_VERSION:
                    raise ValueError(f"card version {d.get('version')} != {CARD_VERSION}; rebuild with --build-cards")
                store.add(AccountCard(**d))
        return store

    def stats(self) -> dict:
        n = len(self.cards)
        size = sum(len(json.dumps(asdict(c))) for c in self.cards.values())
        streams = sum(len(c.state["recurring"]) for c in self.cards.values())
        return dict(cards=n, bytes=size, avg_bytes=round(size / n) if n else 0, avg_streams=round(streams / n, 1) if n else 0,
                    with_no_message_state=sum(c.no_message_state is not None for c in self.cards.values()), built_in_s=self.built_in_s)
