"""Delta = ask traded volume − bid traded volume."""

from __future__ import annotations

from typing import Iterable

from ..footprint import FootprintMatrix


def bar_delta(fp: FootprintMatrix) -> int:
    return fp.delta


def cumulative_delta(bars: Iterable[FootprintMatrix]) -> int:
    return sum(b.delta for b in bars)
