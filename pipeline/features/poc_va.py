"""POC + value area wrappers over FootprintMatrix."""

from __future__ import annotations

from ..footprint import FootprintMatrix


def poc(fp: FootprintMatrix) -> float | None:
    return fp.poc_price


def value_area(fp: FootprintMatrix, pct: float = 0.70) -> tuple[float, float] | None:
    return fp.value_area(pct)
