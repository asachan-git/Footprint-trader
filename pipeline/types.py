"""Canonical types shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Source = Literal["live", "replay"]


@dataclass(frozen=True)
class Level:
    price: float
    vol: float
    # OPERATIONAL-ONLY (2026-08-03) — literal Jun22 predates this field. The live
    # footprint files this branch shares with the main repo (via a data/footprint
    # symlink, so it reads the same real feed rather than a stale duplicate) are
    # written by the current ingest pipeline and carry `cnt` on every bid/ask level.
    # Added with the SAME default as the current codebase purely so deserialization
    # doesn't crash on an unknown field — nothing in Jun22-era strategy code reads
    # `cnt` (it's a later tick-VP concept), so this cannot change any arm/exit
    # decision, only whether the file parses at all.
    cnt: float = 0.0


@dataclass(frozen=True)
class OHLC:
    o: float
    h: float
    l: float
    c: float


@dataclass(frozen=True)
class Bar:
    bar_id: str
    symbol: str
    tf: str
    close_ts: int
    source: Source
    ohlc: OHLC
    bid_ladder: tuple[Level, ...]
    ask_ladder: tuple[Level, ...]
    poc: float | None = None
    delta: float | None = None
    # Intra-bar CVD path (live builder populates; None for historical/synthetic).
    cvd_open: float | None = None
    cvd_high: float | None = None
    cvd_low: float | None = None
    cvd_close: float | None = None
