"""Multi-cycle PnL tracker for the recovery grid system.

A "cycle" is one directional grid attempt: open → (legs added) → close/hedge/recover.
When footprint invalidation fires, instead of closing we hedge and start a new cycle
in the opposite direction whose profits offset the hedged cycle's drawdown.

Persisted to data/cycles.jsonl — one event per state change.

Cycle states:
  active    → legs being added, position open
  hedged    → footprint invalidation fired, hedge opened, cycle frozen
  recovered → hedge removed, cycle back to active (price reversed in our favour)
  closed    → cycle closed with realized PnL
  circuit   → circuit breaker triggered, force-closed

Each cycle links to a GridPosition in position_store via position_id.
Recovery cycles link to the cycle they offset via parent_cycle_id.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock

_IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
CYCLES_LOG = ROOT / "data" / "cycles.jsonl"


def _ts_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_IST).strftime("%Y-%m-%d %H:%M:%S IST")


@dataclass
class TradeCycle:
    cycle_id: str
    symbol: str
    tf: str
    cycle_num: int               # 1 = first, 2 = recovery, 3 = second recovery…
    direction: str               # "long" | "short"
    position_id: str             # GridPosition in position_store
    parent_cycle_id: str | None  # cycle this is recovering

    status: str = "active"       # active | hedged | recovered | closed | circuit
    hedge_id: str | None = None  # HedgePosition id if hedged
    opened_ts: int = 0
    closed_ts: int = 0
    realized_pnl: float = 0.0   # in R units (relative to risk per leg)
    close_reason: str = ""
    legs_added: int = 1


class CycleStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._cycles: dict[str, TradeCycle] = {}
        CYCLES_LOG.parent.mkdir(parents=True, exist_ok=True)
        self._replay()

    # ── persistence ──────────────────────────────────────────────────────────

    def _replay(self) -> None:
        if not CYCLES_LOG.exists():
            return
        for line in CYCLES_LOG.read_text().splitlines():
            try:
                self._apply(json.loads(line))
            except Exception:
                pass

    def _apply(self, event: dict) -> None:
        etype = event["type"]
        cid = event["cycle_id"]
        if etype == "open":
            self._cycles[cid] = TradeCycle(
                cycle_id=cid,
                symbol=event["symbol"],
                tf=event["tf"],
                cycle_num=event["cycle_num"],
                direction=event["direction"],
                position_id=event["position_id"],
                parent_cycle_id=event.get("parent_cycle_id"),
                opened_ts=event["ts"],
            )
        elif cid in self._cycles:
            c = self._cycles[cid]
            if etype == "hedge":
                c.status = "hedged"
                c.hedge_id = event["hedge_id"]
            elif etype == "recover":
                c.status = "recovered"
                c.hedge_id = None
            elif etype == "leg_added":
                c.legs_added += 1
            elif etype in ("close", "circuit"):
                c.status = "circuit" if etype == "circuit" else "closed"
                c.closed_ts = event["ts"]
                c.realized_pnl = event.get("realized_pnl", 0.0)
                c.close_reason = event.get("reason", "")

    def _write(self, event: dict) -> None:
        now = int(time.time())
        event["ts"] = now
        event["ts_ist"] = _ts_ist(now)
        with CYCLES_LOG.open("a") as fh:
            fh.write(json.dumps(event) + "\n")
        self._apply(event)

    # ── public API ────────────────────────────────────────────────────────────

    def open_cycle(
        self,
        symbol: str,
        tf: str,
        direction: str,
        position_id: str,
        parent_cycle_id: str | None = None,
    ) -> TradeCycle:
        cid = uuid.uuid4().hex[:12]
        # Determine cycle number
        existing = [c for c in self._cycles.values()
                    if c.symbol == symbol and c.status in ("active", "hedged", "recovered")]
        cycle_num = len(existing) + 1
        with self._lock:
            self._write({
                "type": "open",
                "cycle_id": cid,
                "symbol": symbol,
                "tf": tf,
                "cycle_num": cycle_num,
                "direction": direction,
                "position_id": position_id,
                "parent_cycle_id": parent_cycle_id,
            })
        return self._cycles[cid]

    def hedge_cycle(self, cycle_id: str, hedge_id: str) -> None:
        with self._lock:
            self._write({"type": "hedge", "cycle_id": cycle_id, "hedge_id": hedge_id})

    def recover_cycle(self, cycle_id: str) -> None:
        with self._lock:
            self._write({"type": "recover", "cycle_id": cycle_id})

    def record_leg(self, cycle_id: str) -> None:
        with self._lock:
            self._write({"type": "leg_added", "cycle_id": cycle_id})

    def close_cycle(self, cycle_id: str, realized_pnl: float, reason: str) -> bool:
        """No-op (returns False) if cycle already closed/circuit — prevents
        double-close from bar-check + reconciler racing on the same cycle."""
        with self._lock:
            c = self._cycles.get(cycle_id)
            if not c or c.status in ("closed", "circuit"):
                return False
            self._write({
                "type": "close",
                "cycle_id": cycle_id,
                "realized_pnl": realized_pnl,
                "reason": reason,
            })
            return True

    def circuit_break(self, cycle_id: str, reason: str) -> None:
        with self._lock:
            self._write({
                "type": "circuit",
                "cycle_id": cycle_id,
                "realized_pnl": 0.0,
                "reason": reason,
            })

    # ── queries ───────────────────────────────────────────────────────────────

    def active_cycles(self, symbol: str | None = None) -> list[TradeCycle]:
        return [c for c in self._cycles.values()
                if c.status in ("active", "hedged", "recovered")
                and (symbol is None or c.symbol == symbol)]

    def by_position_id(self, position_id: str) -> TradeCycle | None:
        """Find the active cycle (if any) linked to a position_id."""
        for c in self._cycles.values():
            if c.position_id == position_id and c.status in ("active", "hedged", "recovered"):
                return c
        return None

    def total_open_exposure(self, symbol: str) -> int:
        """Count total open legs across all active cycles for exposure check."""
        return sum(c.legs_added for c in self.active_cycles(symbol))

    def net_pnl(self, symbol: str) -> float:
        """Sum of realized PnL across all closed cycles for symbol."""
        return sum(
            c.realized_pnl for c in self._cycles.values()
            if c.symbol == symbol and c.status in ("closed", "circuit")
        )

    def summary(self, symbol: str) -> dict:
        active = self.active_cycles(symbol)
        return {
            "active_count": len(active),
            "total_legs": sum(c.legs_added for c in active),
            "directions": [c.direction for c in active],
            "hedged": [c.cycle_id for c in active if c.status == "hedged"],
            "net_realized_pnl": round(self.net_pnl(symbol), 4),
        }


# ── singleton ─────────────────────────────────────────────────────────────────

_store: CycleStore | None = None
_store_lock = Lock()


def cycle_store() -> CycleStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = CycleStore()
    return _store
