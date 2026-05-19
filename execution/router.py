"""Dispatch a Decision to the configured executor.

Mode resolution:
- journal | paper → just instantiate and call
- live → require env ALLOW_LIVE=1; refuse otherwise
"""

from __future__ import annotations

import os
from functools import lru_cache

from llm.schema import Decision
from pipeline.types import Bar

from .base import Executor
from .journal import JournalExecutor
from .paper import PaperExecutor
from .live.broker_base import BrokerAdapter
from .live.mt5_adapter import MT5Adapter


class LiveTripwireError(RuntimeError):
    pass


class LiveExecutor:
    def __init__(self, broker: BrokerAdapter) -> None:
        self._broker = broker

    def fire(self, decision, bar) -> dict:
        result = self._broker.submit_order(decision, bar)
        return {"mode": "live", **result}


@lru_cache(maxsize=1)
def _journal() -> Executor:
    return JournalExecutor()


@lru_cache(maxsize=1)
def _paper() -> Executor:
    return PaperExecutor()


def _live() -> Executor:
    if os.environ.get("ALLOW_LIVE") != "1":
        raise LiveTripwireError(
            "mode=live but ALLOW_LIVE env var is not '1' — refusing to fire real orders"
        )
    return LiveExecutor(MT5Adapter())


def _resolve(mode: str) -> Executor:
    if mode == "journal":
        return _journal()
    if mode == "paper":
        return _paper()
    if mode == "live":
        return _live()
    raise ValueError(f"unknown mode: {mode!r}")


def dispatch(decision: Decision, bar: Bar, settings: dict) -> dict:
    return _resolve(settings["mode"]).fire(decision, bar)
