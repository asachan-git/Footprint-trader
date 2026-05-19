"""Abstract broker adapter — concrete brokers (mt5, ibkr, etc) implement this."""

from __future__ import annotations

from typing import Protocol

from llm.schema import Decision
from pipeline.types import Bar


class BrokerAdapter(Protocol):
    def submit_order(self, decision: Decision, bar: Bar) -> dict:
        ...
