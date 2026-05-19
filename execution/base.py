"""Executor protocol — all modes implement this."""

from __future__ import annotations

from typing import Protocol

from llm.schema import Decision
from pipeline.types import Bar


class Executor(Protocol):
    def fire(self, decision: Decision, bar: Bar) -> dict:
        """Execute the decision. Returns a result dict (mode-specific)."""
        ...
