"""MetaTrader 5 broker adapter — placeholder.

Real implementation calls MT5 Python API or a bridging MQL5 EA over HTTP.
Will be filled in only after journal + paper modes prove the loop.
"""

from __future__ import annotations

from llm.schema import Decision
from pipeline.types import Bar


class MT5Adapter:
    def submit_order(self, decision: Decision, bar: Bar) -> dict:
        raise NotImplementedError("MT5 adapter not implemented yet")
