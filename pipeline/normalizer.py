"""Dispatch ingress payload to format-specific parser → canonical Bar."""

from __future__ import annotations

from .parsers import atas_v1, binance_v1, bybit_v1, capital_v1, exness_v1, userscript_v1
from .types import Bar


class UnknownFormatError(ValueError):
    pass


def normalize(payload: dict) -> Bar:
    fmt = payload.get("format")
    if fmt == "atas_v1":
        return atas_v1.parse(payload)
    if fmt == "binance_v1":
        return binance_v1.parse(payload)
    if fmt == "bybit_v1":
        return bybit_v1.parse(payload)
    if fmt == "capital_v1":
        return capital_v1.parse(payload)
    if fmt == "exness_v1":
        return exness_v1.parse(payload)
    if fmt == "userscript_v1":
        return userscript_v1.parse(payload)
    raise UnknownFormatError(f"unknown ingress format: {fmt!r}")
