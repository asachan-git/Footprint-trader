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

# Source tags written to every open/add_leg event.
# m1_claude  — leg1 triggered by Claude direction decision
# m2_rules   — leg1 triggered by M2 rules engine decision
# mechanical_grid — legs 2-5 filled by cycle_manager price touch
SourceTag = Literal["m1_claude", "m2_rules", "mechanical_grid"]


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
    lots: float = 0.0   # actual fill qty; 0.0 = unknown (older events / live fills)
    source: str = ""    # m1_claude | m2_rules | mechanical_grid


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
    source: str = ""          # m1_claude | m2_rules (from leg1 open event)
    scaled_out: bool = False  # True after a scale_out (deep legs banked, rest at BE)

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
    def __init__(self, log_path: Path | None = None) -> None:
        self._lock = Lock()
        self._positions: dict[str, GridPosition] = {}
        self._daily_r_by_date: dict[str, float] = {}   # IST date → realized R for that day
        self._log_path = log_path or POSITIONS_LOG
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._replay()

    def _replay(self) -> None:
        if not self._log_path.exists():
            return
        with self._log_path.open() as fh:
            for line in fh:
                try:
                    self._apply(json.loads(line))
                except Exception:
                    pass

    def _apply(self, event: dict) -> None:
        etype = event["type"]
        pid = event["position_id"]
        if etype == "open":
            src = event.get("source", "")
            leg = GridLeg(
                leg=1,
                entry=event["entry"],
                stop_loss=event["stop_loss"],
                take_profit=event["take_profit"],
                opened_ts=event["ts"],
                bar_id=event["bar_id"],
                confidence=event.get("confidence", 0),
                rationale=event.get("rationale", ""),
                lots=float(event.get("lots", 0.0) or 0.0),
                source=src,
            )
            self._positions[pid] = GridPosition(
                position_id=pid,
                symbol=event["symbol"],
                tf=event["tf"],
                side=event["side"],
                legs=[leg],
                opened_ts=event["ts"],
                broker_ticket=event.get("broker_ticket", ""),
                source=src,
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
                lots=float(event.get("lots", 0.0) or 0.0),
                source=str(event.get("source") or "mechanical_grid"),  # type: ignore[arg-type]
            )
            self._positions[pid].legs.append(leg)
        elif etype in ("close", "invalidate") and pid in self._positions:
            self._positions[pid].status = etype + "d" if etype == "close" else "invalidated"
            self._positions[pid].closed_ts = event["ts"]
            self._positions[pid].close_reason = event.get("reason", "")
            self._positions[pid].realized_r = event.get("realized_r", 0.0)
            # Bucket realized R by the IST calendar date of the close
            day = datetime.fromtimestamp(event["ts"], tz=_IST).strftime("%Y-%m-%d")
            self._daily_r_by_date[day] = self._daily_r_by_date.get(day, 0.0) + event.get("realized_r", 0.0)
        elif etype == "scale_out" and pid in self._positions:
            pos = self._positions[pid]
            drop = set(event.get("drop_legs") or [])
            pos.legs = [l for l in pos.legs if l.leg not in drop]
            # remaining legs ride to break-even
            new_sl = event.get("new_sl")
            if new_sl is not None and pos.legs:
                for l in pos.legs:
                    l.stop_loss = new_sl
            pos.scaled_out = True
            pr = event.get("realized_r", 0.0)
            pos.realized_r += pr
            day = datetime.fromtimestamp(event["ts"], tz=_IST).strftime("%Y-%m-%d")
            self._daily_r_by_date[day] = self._daily_r_by_date.get(day, 0.0) + pr
        elif etype == "sl_adjust" and pid in self._positions:
            if self._positions[pid].legs:
                self._positions[pid].legs[-1].stop_loss = event["new_sl"]
        elif etype == "tp_adjust" and pid in self._positions:
            if self._positions[pid].legs:
                self._positions[pid].legs[-1].take_profit = event["new_tp"]

    def _write(self, event: dict) -> None:
        now = int(time.time())
        event["ts"] = now
        event["ts_ist"] = _ts_ist(now)
        with self._log_path.open("a") as fh:
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
        lots: float = 0.0,
        source: str = "",
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
                "lots": float(lots or 0.0),
                "source": source,
            })
        return self._positions[pid]

    def add_leg(self, position_id: str, decision: Decision, bar_id: str,
                lots: float = 0.0, source: str = "mechanical_grid") -> None:
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
                "lots": float(lots or 0.0),
                "source": source,
            })

    def close_position(self, position_id: str, reason: str, realized_r: float) -> bool:
        """Close a position. No-op (returns False) if already closed/invalidated —
        prevents double-counting daily R when multiple paths (bar check +
        reconciler) try to close the same position."""
        with self._lock:
            pos = self._positions.get(position_id)
            if not pos or pos.status != "open":
                return False
            self._write({
                "type": "close",
                "position_id": position_id,
                "reason": reason,
                "realized_r": realized_r,
            })
            return True

    def invalidate_position(self, position_id: str, reason: str) -> bool:
        with self._lock:
            pos = self._positions.get(position_id)
            if not pos or pos.status != "open":
                return False
            self._write({
                "type": "invalidate",
                "position_id": position_id,
                "reason": reason,
                "realized_r": -1.0,
            })
            return True

    def adjust_sl(self, position_id: str, new_sl: float, reason: str) -> None:
        with self._lock:
            self._write({
                "type": "sl_adjust",
                "position_id": position_id,
                "new_sl": new_sl,
                "reason": reason,
            })

    def adjust_tp(self, position_id: str, new_tp: float, reason: str) -> None:
        with self._lock:
            self._write({
                "type": "tp_adjust",
                "position_id": position_id,
                "new_tp": new_tp,
                "reason": reason,
            })

    def scale_out_position(self, position_id: str, drop_legs: list[int],
                           exit_price: float, realized_r: float,
                           new_sl: float, reason: str) -> bool:
        """Partial close: bank `drop_legs` at exit_price (realized_r added to cycle +
        daily R), move remaining legs' stop to break-even (new_sl). One-shot per cycle.
        No-op if already scaled, closed, or it would drop all legs."""
        with self._lock:
            pos = self._positions.get(position_id)
            if not pos or pos.status != "open" or pos.scaled_out:
                return False
            remaining = [l for l in pos.legs if l.leg not in set(drop_legs)]
            if not drop_legs or not remaining:
                return False
            self._write({
                "type": "scale_out",
                "position_id": position_id,
                "drop_legs": list(drop_legs),
                "exit_price": exit_price,
                "realized_r": realized_r,
                "new_sl": new_sl,
                "reason": reason,
            })
            return True

    def open_positions(self, symbol: str | None = None) -> list[GridPosition]:
        with self._lock:
            return [
                p for p in self._positions.values()
                if p.status == "open"
                and (symbol is None or p.symbol == symbol)
            ]

    def closed_positions(self, symbol: str | None = None, n: int = 50) -> list[GridPosition]:
        """Recent closed/invalidated positions, newest first."""
        with self._lock:
            xs = [
                p for p in self._positions.values()
                if p.status != "open"
                and (symbol is None or p.symbol == symbol)
            ]
        xs.sort(key=lambda p: p.closed_ts or 0, reverse=True)
        return xs[:n]

    def by_broker_ticket(self, ticket: str) -> GridPosition | None:
        if not ticket:
            return None
        with self._lock:
            for p in self._positions.values():
                if p.broker_ticket == ticket and p.status == "open":
                    return p
            return None

    def daily_realized_r(self) -> float:
        """Realized R for the current IST calendar day only (resets at IST midnight)."""
        import time as _time
        today = datetime.fromtimestamp(int(_time.time()), tz=_IST).strftime("%Y-%m-%d")
        return self._daily_r_by_date.get(today, 0.0)


_store: PositionStore | None = None
_store_m2: PositionStore | None = None
_store_lock = Lock()

POSITIONS_LOG_M2 = ROOT / "data" / "positions_m2.jsonl"

# Context-scoped override — set by the strategy manager so that shared execution
# code (cycle_manager, paper executor) transparently reads/writes a *per-strategy*
# PositionStore without any changes to those call sites. Defaults to the global
# singleton when no override is active.
import contextvars as _contextvars
from contextlib import contextmanager as _contextmanager

_override: "_contextvars.ContextVar[PositionStore | None]" = _contextvars.ContextVar(
    "position_store_override", default=None
)


@_contextmanager
def use_store(store: "PositionStore"):
    """Within this block, position_store() returns `store` (per-strategy isolation)."""
    token = _override.set(store)
    try:
        yield store
    finally:
        _override.reset(token)


def position_store() -> PositionStore:
    ov = _override.get()
    if ov is not None:
        return ov
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = PositionStore()
    return _store


def position_store_m2() -> PositionStore:
    """Isolated paper store for Mode 2 A/B — never blocks Mode 1 live positions."""
    global _store_m2
    if _store_m2 is None:
        with _store_lock:
            if _store_m2 is None:
                _store_m2 = PositionStore(log_path=POSITIONS_LOG_M2)
    return _store_m2
