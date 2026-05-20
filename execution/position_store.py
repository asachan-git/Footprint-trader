"""Persisted position state — survives Flask restarts.

Each position event is appended to data/positions.jsonl as one JSON row.
Events: open | add_leg | close | invalidate | sl_adjust

On startup, replays all events to reconstruct in-memory active positions.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock

_IST = timezone(timedelta(hours=5, minutes=30))


def _ts_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_IST).strftime("%Y-%m-%d %H:%M:%S IST")
from typing import Literal

from llm.schema import Decision

ROOT = Path(__file__).resolve().parent.parent
POSITIONS_LOG = ROOT / "data" / "positions.jsonl"

EventType = Literal["open", "add_leg", "close", "invalidate", "sl_adjust"]


@dataclass
class GridLeg:
    leg: int
    entry: float
    stop_loss: float
    take_profit: float
    opened_ts: int
    bar_id: str
    confidence: float
    rationale: str


@dataclass
class GridPosition:
    position_id: str
    symbol: str
    tf: str
    side: str              # "long" | "short"
    legs: list[GridLeg] = field(default_factory=list)
    status: str = "open"   # "open" | "closed" | "invalidated"
    realized_r: float = 0.0
    opened_ts: int = 0
    closed_ts: int = 0
    close_reason: str = ""
    broker_ticket: str = ""   # MetaApi positionId — empty for paper

    @property
    def avg_entry(self) -> float:
        return sum(l.entry for l in self.legs) / len(self.legs) if self.legs else 0.0

    @property
    def stop_loss(self) -> float:
        return self.legs[-1].stop_loss if self.legs else 0.0

    @property
    def take_profit(self) -> float:
        return self.legs[-1].take_profit if self.legs else 0.0

    @property
    def leg_count(self) -> int:
        return len(self.legs)


class PositionStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._positions: dict[str, GridPosition] = {}
        self._daily_r: float = 0.0
        self._daily_date: str = ""
        POSITIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        self._replay()

    def _replay(self) -> None:
        if not POSITIONS_LOG.exists():
            return
        with POSITIONS_LOG.open() as fh:
            for line in fh:
                try:
                    self._apply(json.loads(line))
                except Exception:
                    pass

    def _apply(self, event: dict) -> None:
        etype = event["type"]
        pid = event["position_id"]
        if etype == "open":
            leg = GridLeg(
                leg=1,
                entry=event["entry"],
                stop_loss=event["stop_loss"],
                take_profit=event["take_profit"],
                opened_ts=event["ts"],
                bar_id=event["bar_id"],
                confidence=event.get("confidence", 0),
                rationale=event.get("rationale", ""),
            )
            self._positions[pid] = GridPosition(
                position_id=pid,
                symbol=event["symbol"],
                tf=event["tf"],
                side=event["side"],
                legs=[leg],
                opened_ts=event["ts"],
                broker_ticket=event.get("broker_ticket", ""),
            )
        elif etype == "add_leg" and pid in self._positions:
            leg = GridLeg(
                leg=len(self._positions[pid].legs) + 1,
                entry=event["entry"],
                stop_loss=event["stop_loss"],
                take_profit=event["take_profit"],
                opened_ts=event["ts"],
                bar_id=event["bar_id"],
                confidence=event.get("confidence", 0),
                rationale=event.get("rationale", ""),
            )
            self._positions[pid].legs.append(leg)
        elif etype in ("close", "invalidate") and pid in self._positions:
            self._positions[pid].status = etype + "d" if etype == "close" else "invalidated"
            self._positions[pid].closed_ts = event["ts"]
            self._positions[pid].close_reason = event.get("reason", "")
            self._positions[pid].realized_r = event.get("realized_r", 0.0)
            self._daily_r += event.get("realized_r", 0.0)
        elif etype == "sl_adjust" and pid in self._positions:
            if self._positions[pid].legs:
                self._positions[pid].legs[-1].stop_loss = event["new_sl"]

    def _write(self, event: dict) -> None:
        now = int(time.time())
        event["ts"] = now
        event["ts_ist"] = _ts_ist(now)
        with POSITIONS_LOG.open("a") as fh:
            fh.write(json.dumps(event) + "\n")
        self._apply(event)

    def open_position(
        self,
        decision: Decision,
        bar_id: str,
        symbol: str,
        tf: str,
        broker_ticket: str = "",
        fill_type: str = "paper_simulated",
    ) -> GridPosition:
        import uuid
        pid = uuid.uuid4().hex[:12]
        with self._lock:
            self._write({
                "type": "open",
                "position_id": pid,
                "symbol": symbol,
                "tf": tf,
                "side": decision.side,
                "entry": decision.entry,
                "stop_loss": decision.stop_loss,
                "take_profit": decision.take_profit,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "invalidation_note": decision.invalidation_note,
                "bar_id": bar_id,
                "broker_ticket": broker_ticket,
                "fill_type": fill_type,
            })
        return self._positions[pid]

    def add_leg(self, position_id: str, decision: Decision, bar_id: str) -> None:
        with self._lock:
            pos = self._positions.get(position_id)
            if not pos or pos.status != "open":
                return
            self._write({
                "type": "add_leg",
                "position_id": position_id,
                "entry": decision.entry,
                "stop_loss": decision.stop_loss,
                "take_profit": decision.take_profit,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "bar_id": bar_id,
            })

    def close_position(self, position_id: str, reason: str, realized_r: float) -> None:
        with self._lock:
            self._write({
                "type": "close",
                "position_id": position_id,
                "reason": reason,
                "realized_r": realized_r,
            })

    def invalidate_position(self, position_id: str, reason: str) -> None:
        with self._lock:
            self._write({
                "type": "invalidate",
                "position_id": position_id,
                "reason": reason,
                "realized_r": -1.0,
            })

    def adjust_sl(self, position_id: str, new_sl: float, reason: str) -> None:
        with self._lock:
            self._write({
                "type": "sl_adjust",
                "position_id": position_id,
                "new_sl": new_sl,
                "reason": reason,
            })

    def open_positions(self, symbol: str | None = None) -> list[GridPosition]:
        with self._lock:
            return [
                p for p in self._positions.values()
                if p.status == "open"
                and (symbol is None or p.symbol == symbol)
            ]

    def by_broker_ticket(self, ticket: str) -> GridPosition | None:
        if not ticket:
            return None
        with self._lock:
            for p in self._positions.values():
                if p.broker_ticket == ticket and p.status == "open":
                    return p
            return None

    def daily_realized_r(self) -> float:
        return self._daily_r


_store: PositionStore | None = None
_store_lock = Lock()


def position_store() -> PositionStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = PositionStore()
    return _store
