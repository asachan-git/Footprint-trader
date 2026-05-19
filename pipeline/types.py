"""Canonical types shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Source = Literal["live", "replay"]


@dataclass(frozen=True)
class Level:
    price: float
    vol: float


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
