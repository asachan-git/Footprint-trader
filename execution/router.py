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
        # Record live fill into position_store (for reconciliation, journals, etc.)
        broker_ticket = ""
        if isinstance(result, dict):
            order = result.get("order") or {}
            broker_ticket = str(order.get("positionId") or order.get("orderId") or "")
        if broker_ticket and not result.get("error") and not result.get("skipped"):
            try:
                from .position_store import position_store
                pos = position_store().open_position(
                    decision=decision, bar_id=bar.bar_id,
                    symbol=bar.symbol, tf=bar.tf,
                    broker_ticket=broker_ticket,
                    fill_type="vantage_mt5_live",
                )
                result["position_id"] = pos.position_id
                # Mirror paper: open a cycle for hedge/recovery tracking
                try:
                    from .cycle_store import cycle_store
                    cycle_store().open_cycle(
                        symbol=bar.symbol, tf=bar.tf,
                        direction=pos.side,
                        position_id=pos.position_id,
                        parent_cycle_id=decision.parent_position_id,
                    )
                except Exception as e:
                    import logging as _l
                    _l.getLogger(__name__).warning(f"[live] cycle_store.open_cycle failed: {e}")
                try:
                    from utils.notify import notify
                    notify(
                        "🟢 ORDER FILLED",
                        f"{result.get('symbol_broker', bar.symbol)} {decision.side} "
                        f"{result.get('lots')} lot @ {decision.entry}\n"
                        f"SL {decision.stop_loss}  TP {decision.take_profit}  "
                        f"conf {decision.confidence:.2f}",
                    )
                except Exception:
                    pass
            except Exception as e:
                # Never let store failure mask a successful broker fill
                import logging
                logging.getLogger(__name__).exception(f"[live] position_store.open_position failed: {e}")
                result["position_store_error"] = str(e)
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
