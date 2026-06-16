"""Execution bridge — a command queue Python fills and a thin MQL5 EA drains.

MQL5 EAs are outbound-only (no inbound listener), so "Python sends orders to MT5"
is realised as: Python ENQUEUES order commands here; the FBExecBridge EA POLLs the
queue over HTTP on a short timer, executes each via CTrade, and ACKs the results.
Python is authoritative — it decides every order; the EA only does as told.

v1 scope (place-only minimal):
  PLACE_PENDING  — a buy_stop / sell_stop at price, lot, optional sl/tp
  CLOSE_ALL      — close all positions + cancel all pendings for the symbol

Commands carry the BROKER symbol (the EA places verbatim). Idempotency: each
command has a unique id; poll() moves PENDING→IN_FLIGHT so it isn't re-sent, and
ack() finalises it. An IN_FLIGHT command that is never ack'd (EA crash/miss) is
re-armed to PENDING after `reclaim_after_s`, so a poll can pick it up again.

In-memory state + an append-only jsonl audit log. State is per-process (a restart
drops the queue — order intents are ephemeral); the audit log is durable.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_AUDIT_LOG = _ROOT / "data" / "exec_bridge.jsonl"

# command lifecycle
PENDING = "pending"
IN_FLIGHT = "in_flight"
DONE = "done"
FAILED = "failed"

# command types
PLACE_PENDING = "PLACE_PENDING"
CLOSE_ALL = "CLOSE_ALL"

_RECLAIM_AFTER_S = 10.0   # IN_FLIGHT with no ack this long → back to PENDING


@dataclass
class Command:
    id: str
    account: str
    type: str                 # PLACE_PENDING | CLOSE_ALL
    symbol: str               # BROKER symbol (EA places verbatim)
    order_type: str = ""      # buy_stop | sell_stop  (PLACE_PENDING only)
    price: float = 0.0
    lot: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    comment: str = ""
    status: str = PENDING
    ts_created: float = 0.0
    ts_sent: float = 0.0
    result: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        """Flat dict the EA's JSON parser consumes (only execution fields)."""
        d = {"id": self.id, "type": self.type, "symbol": self.symbol}
        if self.type == PLACE_PENDING:
            d.update(order_type=self.order_type, price=self.price, lot=self.lot,
                     sl=self.sl, tp=self.tp, comment=self.comment)
        return d


class ExecBridge:
    """Process-wide singleton-ish command queue."""

    _lock = threading.Lock()
    _cmds: dict[str, Command] = {}          # id → Command
    _seq: list[str] = []                    # insertion order (FIFO dispatch)
    _quotes: dict[tuple, dict] = {}         # (account, broker_symbol) → {bid,ask,mid,ts}

    # ── venue quote cache (EA reports its live price on each poll) ─────────────
    @classmethod
    def set_quote(cls, account: str, symbol: str, bid: float, ask: float,
                  now: float | None = None) -> None:
        if bid <= 0 or ask <= 0:
            return
        with cls._lock:
            cls._quotes[(str(account), symbol)] = {
                "bid": float(bid), "ask": float(ask), "mid": (bid + ask) / 2.0,
                "ts": now if now is not None else time.time(),
            }

    @classmethod
    def get_quote(cls, account: str, symbol: str) -> dict | None:
        with cls._lock:
            q = cls._quotes.get((str(account), symbol))
            return dict(q) if q else None

    # ── audit ────────────────────────────────────────────────────────────────
    @classmethod
    def _audit(cls, event: str, cmd: Command) -> None:
        try:
            _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _AUDIT_LOG.open("a") as fh:
                fh.write(json.dumps({"event": event, "ts": cmd.ts_created, **asdict(cmd)}) + "\n")
        except Exception:
            pass  # audit must never break execution

    # ── enqueue ────────────────────────────────────────────────────────────────
    @classmethod
    def enqueue(cls, account: str, type: str, symbol: str, *, order_type: str = "",
                price: float = 0.0, lot: float = 0.0, sl: float = 0.0, tp: float = 0.0,
                comment: str = "", now: float | None = None) -> Command:
        cmd = Command(
            id=uuid.uuid4().hex[:12], account=str(account), type=type, symbol=symbol,
            order_type=order_type, price=round(float(price), 5), lot=round(float(lot), 2),
            sl=round(float(sl), 5), tp=round(float(tp), 5), comment=comment,
            ts_created=now if now is not None else time.time(),
        )
        with cls._lock:
            cls._cmds[cmd.id] = cmd
            cls._seq.append(cmd.id)
        cls._audit("enqueue", cmd)
        return cmd

    @classmethod
    def enqueue_grid_plan(cls, account: str, broker_symbol: str, plan, *,
                          close_first: bool = True) -> list[Command]:
        """Translate a rebased neutral GridPlan into PLACE_PENDING commands.
        buy_legs → buy_stop, sell_legs → sell_stop, shared per-side TP, no SL (v1).
        Optionally prepend a CLOSE_ALL to clear any prior cycle on the symbol."""
        out: list[Command] = []
        if close_first:
            out.append(cls.enqueue(account, CLOSE_ALL, broker_symbol))
        for i, leg in enumerate(getattr(plan, "buy_legs", []) or []):
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol, order_type="buy_stop",
                price=leg.price, lot=leg.lot, sl=0.0, tp=getattr(plan, "buy_tp", 0.0),
                comment=f"FB|hvn|b{i + 1}"))
        for i, leg in enumerate(getattr(plan, "sell_legs", []) or []):
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol, order_type="sell_stop",
                price=leg.price, lot=leg.lot, sl=0.0, tp=getattr(plan, "sell_tp", 0.0),
                comment=f"FB|hvn|s{i + 1}"))
        return out

    # ── poll (EA pulls) ──────────────────────────────────────────────────────
    @classmethod
    def poll(cls, account: str, now: float | None = None) -> list[dict]:
        """Return wire-form commands for `account`: every PENDING, plus any
        IN_FLIGHT stale beyond reclaim_after_s (re-armed). Marks them IN_FLIGHT."""
        t = now if now is not None else time.time()
        account = str(account)
        out: list[dict] = []
        with cls._lock:
            for cid in cls._seq:
                c = cls._cmds.get(cid)
                if c is None or c.account != account:
                    continue
                if c.status == PENDING or (
                        c.status == IN_FLIGHT and (t - c.ts_sent) > _RECLAIM_AFTER_S):
                    c.status = IN_FLIGHT
                    c.ts_sent = t
                    out.append(c.to_wire())
        return out

    # ── ack (EA reports results) ───────────────────────────────────────────────
    @classmethod
    def ack(cls, results: list[dict]) -> dict:
        """Finalise commands from EA results: [{id, ok, ticket, retcode, error}]."""
        done = failed = unknown = 0
        with cls._lock:
            for r in results or []:
                cid = str(r.get("id") or "")
                c = cls._cmds.get(cid)
                if c is None:
                    unknown += 1
                    continue
                c.result = dict(r)
                if r.get("ok"):
                    c.status = DONE
                    done += 1
                else:
                    c.status = FAILED
                    failed += 1
                cls._audit("ack", c)
        return {"done": done, "failed": failed, "unknown": unknown}

    # ── introspection (tests / dashboard) ──────────────────────────────────────
    @classmethod
    def snapshot(cls, account: str | None = None) -> list[dict]:
        with cls._lock:
            return [asdict(c) for c in cls._cmds.values()
                    if account is None or c.account == str(account)]

    @classmethod
    def reset(cls) -> None:
        """Clear all state (tests only)."""
        with cls._lock:
            cls._cmds.clear()
            cls._seq.clear()
            cls._quotes.clear()
