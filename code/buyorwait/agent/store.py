"""Disk-backed tables: the raw dataset stays on disk; sections are read on demand (D17).

`DiskTables` indexes each CSV once by its key column (`user_id` for profiles, events, messages, images;
`request_id` for requests, samples, options) as byte ranges, since every file is contiguous per key
and has no embedded newlines. The index is cached under `.cache/index/`. `slice(table, key)` seeks and
reads only that key's lines; `dataset_for(user_id, request_id)` assembles a one-user `Dataset` from
slices (rates and images are small and read whole), which is all `intake.build_state` needs to build
or rebuild one account card.

Request time therefore holds only the cards in RAM; the tables are touched only on a card miss or when
a worker asks for raw rows (the historian fetching the events behind an uncertainty flag).
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pandas as pd

from ..intake import DATASET, Dataset

TABLES = {
    "profiles": ("financial_profiles.csv", "user_id"),
    "events": ("financial_events.csv", "user_id"),
    "messages": ("messages.csv", "user_id"),
    "images": ("images.csv", "user_id"),
    "requests": ("requests.csv", "request_id"),
    "samples": ("sample_requests.csv", "request_id"),
    "options": ("request_payment_options.csv", "request_id"),
}
SMALL = ("rates",)          # read whole: exchange_rates.csv is ~130 rows


class DiskTables:
    def __init__(self, root: Path = DATASET, index_dir: Path | None = None):
        self.root = Path(root)
        self.index_dir = index_dir or (self.root.parent / ".cache" / "index")
        self._index: dict[str, dict] = {}
        self._header: dict[str, str] = {}
        self._rates: pd.DataFrame | None = None
        self.reads: list[tuple[str, str, int]] = []          # (table, key, bytes) for the transcript / stats

    # ---- index -----------------------------------------------------------------------------
    def _build_index(self, table: str) -> dict:
        fname, keycol = TABLES[table]
        path = self.root / fname
        raw = path.read_bytes()
        lines = raw.split(b"\n")
        header = lines[0].decode("utf-8")
        cols = next(csv.reader([header]))
        ki = cols.index(keycol)
        ranges: dict[str, list[int]] = {}
        off = len(lines[0]) + 1
        for line in lines[1:]:
            n = len(line) + 1
            if line:
                key = next(csv.reader([line.decode("utf-8")]))[ki]
                r = ranges.get(key)
                if r is None:
                    ranges[key] = [off, off + n]
                elif r[1] == off:
                    r[1] = off + n
                else:                                   # not contiguous: fall back to a list of ranges
                    ranges[key] = r + [off, off + n]
            off += n
        idx = {"file": fname, "key": keycol, "header": header, "size": len(raw), "mtime": path.stat().st_mtime, "ranges": ranges}
        self.index_dir.mkdir(parents=True, exist_ok=True)
        (self.index_dir / f"{table}.json").write_text(json.dumps(idx))
        return idx

    def index(self, table: str) -> dict:
        if table not in self._index:
            fname, _ = TABLES[table]
            path = self.root / fname
            cached = self.index_dir / f"{table}.json"
            idx = None
            if cached.exists():
                try:
                    idx = json.loads(cached.read_text())
                    if idx.get("size") != path.stat().st_size or abs(idx.get("mtime", 0) - path.stat().st_mtime) > 1e-6:
                        idx = None                      # file changed: rebuild
                except Exception:
                    idx = None
            self._index[table] = idx or self._build_index(table)
        return self._index[table]

    def keys(self, table: str) -> list[str]:
        return list(self.index(table)["ranges"])

    # ---- reads -----------------------------------------------------------------------------
    def slice(self, table: str, key: str) -> pd.DataFrame:
        """Only this key's rows, typed as strings like `Dataset.load` (typing is applied by `Dataset.from_frames`)."""
        idx = self.index(table)
        rng = idx["ranges"].get(key)
        header = idx["header"]
        if not rng:
            self.reads.append((table, key, 0))
            return pd.read_csv(io.StringIO(header + "\n"), dtype=str, keep_default_na=False)
        chunks, total = [], 0
        with (self.root / idx["file"]).open("rb") as f:
            for a, b in zip(rng[::2], rng[1::2]):
                f.seek(a)
                chunk = f.read(b - a)
                chunks.append(chunk)
                total += len(chunk)
        self.reads.append((table, key, total))
        text = header + "\n" + b"".join(chunks).decode("utf-8")
        return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)

    def rates(self) -> pd.DataFrame:
        if self._rates is None:
            self._rates = pd.read_csv(self.root / "exchange_rates.csv", dtype=str, keep_default_na=False)
            self.reads.append(("rates", "*", (self.root / "exchange_rates.csv").stat().st_size))
        return self._rates

    def dataset_for(self, user_id: str, request_id: str) -> Dataset:
        """A one-user Dataset assembled from slices: enough to build this user's card for this request."""
        req = self.slice("requests", request_id)
        smp = self.slice("samples", request_id)
        return Dataset.from_frames(profiles=self.slice("profiles", user_id), events=self.slice("events", user_id),
                                   requests=req, samples=smp, options=self.slice("options", request_id),
                                   rates=self.rates().copy(), messages=self.slice("messages", user_id),
                                   images=self.slice("images", user_id))

    def events(self, user_id: str, event_ids: list[str] | None = None) -> pd.DataFrame:
        """Raw event rows for one user, optionally only the given ids (a worker's on-demand fetch)."""
        ev = self.slice("events", user_id)
        if event_ids:
            ev = ev[ev.event_id.isin(event_ids)]
        return ev

    def stats(self) -> dict:
        by_table: dict[str, int] = {}
        for t, _, n in self.reads:
            by_table[t] = by_table.get(t, 0) + n
        return dict(reads=len(self.reads), bytes=sum(n for _, _, n in self.reads), by_table=by_table,
                    indexed=sorted(self._index))
