"""Pending limit-order store — used by paper grid to simulate broker pending orders.

Each pending order belongs to a parent position_id (the cycle's leg-1).
cycle_manager polls open pending orders on every bar close; when price crosses
limit_price, fills the leg via position_store.add_leg() and removes the pending.

Persisted as JSONL at data/pending_orders.jsonl so it survives restarts.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

_ROOT = Path(__file__).resolve().parent.parent
_FILE = _ROOT / "data" / "pending_orders.jsonl"


@dataclass
class PendingOrder:
    pending_id: str
    position_id: str
    symbol: str
    side: Literal["long", "short"]
    limit_price: float
    lots: float
    leg_idx: int
    tp: float
    safety_sl: float | None
    state: str = "open"   # open | filled | cancelled


class PendingStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._orders: dict[str, PendingOrder] = {}
        self._load()

    def _load(self) -> None:
        if not _FILE.exists():
            return
        with _FILE.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    po = PendingOrder(**d)
                    self._orders[po.pending_id] = po
                except Exception:
                    pass

    def _persist(self) -> None:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FILE.with_suffix(".jsonl.tmp")
        with tmp.open("w") as fh:
            for po in self._orders.values():
                fh.write(json.dumps(asdict(po)) + "\n")
        tmp.replace(_FILE)

    def add(
        self, *, position_id: str, symbol: str, side: str,
        limit_price: float, lots: float, leg_idx: int,
        tp: float, safety_sl: float | None,
    ) -> str:
        pid = uuid.uuid4().hex[:12]
        po = PendingOrder(
            pending_id=pid, position_id=position_id, symbol=symbol,
            side=side, limit_price=limit_price, lots=lots, leg_idx=leg_idx,
            tp=tp, safety_sl=safety_sl, state="open",
        )
        with self._lock:
            self._orders[pid] = po
            self._persist()
        return pid

    def open_for(self, symbol: str) -> list[PendingOrder]:
        return [p for p in self._orders.values() if p.symbol == symbol and p.state == "open"]

    def open_for_position(self, position_id: str) -> list[PendingOrder]:
        return [p for p in self._orders.values() if p.position_id == position_id and p.state == "open"]

    def mark_filled(self, pending_id: str) -> None:
        with self._lock:
            if pending_id in self._orders:
                self._orders[pending_id].state = "filled"
                self._persist()

    def cancel(self, pending_id: str) -> None:
        with self._lock:
            if pending_id in self._orders:
                self._orders[pending_id].state = "cancelled"
                self._persist()

    def cancel_for_position(self, position_id: str) -> int:
        n = 0
        with self._lock:
            for p in self._orders.values():
                if p.position_id == position_id and p.state == "open":
                    p.state = "cancelled"
                    n += 1
            if n:
                self._persist()
        return n


_store: PendingStore | None = None
_store_lock = threading.Lock()


def pending_store() -> PendingStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = PendingStore()
        return _store
