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
_EMIT_LOG = _ROOT / "data" / "exec_emit.jsonl"   # ground-truth arm/exit decisions


def _emit_exit_audit(row: dict) -> None:
    """Append one cycle-exit decision — same log the emit route uses for arms."""
    try:
        _EMIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _EMIT_LOG.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "verdict": "exit", **row}) + "\n")
    except Exception:
        pass  # audit must never break execution

# command lifecycle
PENDING = "pending"
IN_FLIGHT = "in_flight"
DONE = "done"
FAILED = "failed"

# command types
PLACE_PENDING = "PLACE_PENDING"
CLOSE_ALL = "CLOSE_ALL"          # close positions + cancel pendings (deliberate flatten)
CANCEL_PENDINGS = "CANCEL_PENDINGS"  # cancel pendings ONLY, leave positions (safe re-arm)

# ── per-strategy × per-TF magic scheme ───────────────────────────────────────
# magic = MAGIC_BASE + strat_code·10 + tf_code  →  e.g. hvn·15m = 770013,
# squeeze·1h = 770024. The EA owns the whole [MAGIC_BASE, MAGIC_BASE+99] range; the
# tf is recoverable as magic % 10, so the server can attribute each EA-reported
# position pool to the TF cycle that owns it (enables parallel per-TF cycles).
MAGIC_BASE = 770000
_STRAT_CODE = {
    "hvn_inside_touch": 1, "squeeze": 2, "vp_level_touch": 3, "imbalance": 4,
    "hvn_edge": 5, "anchor": 6, "va": 7, "cvd_div": 8,
}
_TF_CODE = {"1m": 1, "5m": 2, "15m": 3, "1h": 4}
_CODE_TF = {v: k for k, v in _TF_CODE.items()}


def magic_for(trigger_kind: str, tf: str) -> int:
    """Composite magic identifying (strategy, TF). Unknown kind→0, unknown tf→0."""
    return MAGIC_BASE + _STRAT_CODE.get(trigger_kind, 0) * 10 + _TF_CODE.get(tf, 0)


def tf_from_magic(magic: int) -> str:
    """Recover the TF a magic belongs to (magic % 10). '' if not one of ours."""
    if magic < MAGIC_BASE or magic >= MAGIC_BASE + 100:
        return ""
    return _CODE_TF.get(int(magic) % 10, "")


_RECLAIM_AFTER_S = 10.0   # IN_FLIGHT with no ack this long → back to PENDING
# Cycle-flatten idempotency: once a CLOSE_ALL is enqueued, suppress further exit
# evaluation until it confirms (positions→0) or this grace lapses, then re-issue.
# Must exceed _RECLAIM_AFTER_S so the queue's own re-send isn't double-stacked.
_FLATTEN_GRACE_S = 12.0


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
    magic: int = 0            # per-strategy magic (0 → EA uses its default InpMagic)
    status: str = PENDING
    ts_created: float = 0.0
    ts_sent: float = 0.0
    result: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        """Flat dict the EA's JSON parser consumes (only execution fields)."""
        d = {"id": self.id, "type": self.type, "symbol": self.symbol, "magic": self.magic}
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
    _last_emit: dict[tuple, float] = {}     # (account, symbol, tf) → last emitted fulcrum
    _last_arm: dict[tuple, dict] = {}       # (account, broker_symbol) → last armed grid metadata
    _open: dict[tuple, dict] = {}           # (account, broker_symbol) → {positions, pendings, ts}
    _ict_overlay: dict[str, dict] = {}      # analysis_symbol → ict_fvg setup (analysis frame)

    # ── ict_fvg chart overlay (paper strategy publishes its active setup; the EA
    #    draws it rebased onto the venue) ───────────────────────────────────────
    @classmethod
    def set_ict_overlay(cls, symbol: str, payload: dict) -> None:
        with cls._lock:
            cls._ict_overlay[str(symbol)] = dict(payload)

    @classmethod
    def get_ict_overlay(cls, symbol: str) -> dict | None:
        with cls._lock:
            p = cls._ict_overlay.get(str(symbol))
            return dict(p) if p else None

    # ── live open-state (EA reports its position/order counts on each poll) ────
    @classmethod
    def set_open(cls, account: str, symbol: str, positions: int, pendings: int,
                 tf: str = "", now: float | None = None) -> None:
        with cls._lock:
            cls._open[(str(account), symbol, tf)] = {
                "positions": int(positions), "pendings": int(pendings),
                "ts": now if now is not None else time.time(),
            }

    @classmethod
    def get_open(cls, account: str, symbol: str, tf: str = "") -> dict:
        with cls._lock:
            return dict(cls._open.get((str(account), symbol, tf), {"positions": 0, "pendings": 0}))

    # ── last-armed grid (ground truth for chart drawing + diagnostics) ─────────
    @classmethod
    def set_last_arm(cls, account: str, broker_symbol: str, tf: str = "", **meta) -> None:
        with cls._lock:
            cls._last_arm[(str(account), broker_symbol, tf)] = dict(meta, tf=tf)

    @classmethod
    def get_last_arm(cls, account: str, broker_symbol: str, tf: str = "") -> dict | None:
        with cls._lock:
            m = cls._last_arm.get((str(account), broker_symbol, tf))
            return dict(m) if m else None

    # ── cycle monitor (server-side exit brain) ─────────────────────────────────
    @classmethod
    def monitor_cycle(cls, account: str, symbol: str, settings: dict | None, *,
                      pnl: float | None = None, buys: int | None = None,
                      sells: int | None = None, now: float | None = None,
                      tf: str = "", magic: int = 0) -> str | None:
        """Evaluate the active grid cycle on `(account, symbol, tf)` and, if any exit
        trigger fires, enqueue ONE CLOSE_ALL scoped to `magic` (flatten ONLY this TF's
        position pool — sibling TF cycles on the same symbol are untouched). Returns the
        exit reason, or None. LOCK-FREE: composes the locked classmethods only —
        never holds cls._lock while enqueuing (cls._lock is non-reentrant).

        Called once per EA poll (the only ~1s cadence). Exit order, first wins:
          1. flatten-rest — a leg closed mid-cycle while the opposite ladder rests
          2. net-$ target — basket floating ≥ effective (hedge-decayed) target
          3. full-hedge   — delta-neutral basket cut to free margin (realizes loss)
        """
        t = now if now is not None else time.time()
        cyc = cls.get_last_arm(account, symbol, tf)
        if not cyc or not cyc.get("active"):
            return None
        if not magic:
            magic = int(cyc.get("magic") or 0)

        open_state = cls.get_open(account, symbol, tf)
        pendings = int(open_state.get("pendings", 0) or 0)
        # Prefer the per-side sum the new EA sends; fall back to the legacy count.
        if buys is not None and sells is not None:
            positions = int(buys) + int(sells)
        else:
            positions = int(open_state.get("positions", 0) or 0)

        # 0) flatten-pending guard — checked FIRST so we never stack CLOSE_ALLs.
        fts = float(cyc.get("flatten_ts") or 0.0)
        if fts > 0:
            if positions == 0:
                cls.set_last_arm(account, symbol, **{**cyc, "active": False, "flatten_ts": 0.0})
            elif (t - fts) > _FLATTEN_GRACE_S:
                # close demonstrably didn't land (past the queue's reclaim window) → re-issue once
                cls.enqueue(account, CLOSE_ALL, symbol, comment="FB|flatten|retry", magic=magic, now=t)
                cls.set_last_arm(account, symbol, **{**cyc, "flatten_ts": t})
            return None

        # track high-water of open positions (basis for flatten-rest) + resting pendings
        # (so a never-filled cycle can be retired); both reset per arm.
        max_seen = int(cyc.get("max_pos_seen") or 0)
        pend_seen = int(cyc.get("pend_seen") or 0)
        if positions > max_seen or pendings > pend_seen:
            max_seen = max(max_seen, positions)
            pend_seen = max(pend_seen, pendings)
            cyc["max_pos_seen"] = max_seen
            cyc["pend_seen"] = pend_seen
            cls.set_last_arm(account, symbol, **cyc)

        if positions <= 0:
            # flat. Retire the cycle once it had something live (positions filled OR
            # pendings rested) and now has nothing open AND nothing resting — frees the
            # symbol for a new arm by either tf. The (max/pend)_seen high-water avoids
            # the placement-window race (active set before the EA reports pendings).
            if (max_seen > 0 or pend_seen > 0) and pendings == 0:
                cls.set_last_arm(account, symbol, **{**cyc, "active": False})
            return None

        n = int(cyc.get("n_per_side") or 0)
        tp_up = float(cyc.get("tp_up") or 0.0)
        tp_down = float(cyc.get("tp_down") or 0.0)
        q = cls.get_quote(account, symbol) or {}
        mid = float(q.get("mid") or 0.0)

        grid_cfg = (settings.get("grid_levels") or {}) if isinstance(settings, dict) else {}
        base_target = float(grid_cfg.get("cycle_net_target_usd", 0.0) or 0.0)
        decay_pct = float(grid_cfg.get("cycle_hedge_decay_pct", 33.0) or 0.0)
        min_target = float(grid_cfg.get("cycle_min_target_usd", 0.20) or 0.0)
        close_on_full_hedge = bool(grid_cfg.get("cycle_close_on_full_hedge", True))

        reason: str | None = None
        detail: dict = {}

        # 1) flatten-rest — a filled leg closed while the opposite ladder still rests.
        if 0 < positions < max_seen and pendings > 0:
            tol = max(mid * 1e-4, 1e-6) if mid > 0 else 1e-6
            confirms = (tp_up > 0 and mid >= tp_up - tol) or (tp_down > 0 and 0 < mid <= tp_down + tol)
            reason = "leg_tp" if confirms else "leg_closed_other"

        # 2) net-$ target (hedge-decayed)
        if reason is None and base_target > 0 and pnl is not None and n > 0:
            hedged = min(int(buys or 0), int(sells or 0))
            decay = min(1.0, max(0.0, (hedged / n) * (decay_pct / 100.0)))
            eff = max(min_target, base_target * (1.0 - decay))
            if float(pnl) >= eff:
                reason = "net_target"
                detail = {"effective_target": round(eff, 2), "decay": round(decay, 3)}

        # 3) full-hedge backstop (delta-neutral → cut to free margin; realizes a loss)
        if reason is None and close_on_full_hedge and n > 0 \
                and buys is not None and sells is not None:
            if min(int(buys), int(sells)) >= n and (int(buys) + int(sells)) >= 2 * n:
                reason = "full_hedge"

        if reason is None:
            return None

        cls.enqueue(account, CLOSE_ALL, symbol, comment=f"FB|flatten|{reason}", magic=magic, now=t)
        cls.set_last_arm(account, symbol, **{**cyc, "flatten_ts": t})
        _emit_exit_audit({"account": str(account), "broker_symbol": symbol, "tf": tf, "magic": magic,
                          "exit_reason": reason, "armed_tf": cyc.get("armed_tf", ""),
                          "positions": positions, "pendings": pendings,
                          "buys": buys, "sells": sells, "pnl": pnl, "venue_mid": mid,
                          "tp_up": tp_up, "tp_down": tp_down, **detail})
        return reason

    # ── emit dedup (one grid per HVN-touch episode, not per bar) ───────────────
    @classmethod
    def should_emit(cls, account: str, symbol: str, tf: str, fulcrum: float, tol: float) -> bool:
        """True if this fulcrum is a NEW touched-edge episode (no prior, or moved
        more than `tol` from the last emitted edge). Prevents re-arming the same
        node every bar while price sits in it."""
        last = cls._last_emit.get((str(account), symbol, tf))
        return last is None or abs(fulcrum - last) > tol

    @classmethod
    def mark_emit(cls, account: str, symbol: str, tf: str, fulcrum: float) -> None:
        cls._last_emit[(str(account), symbol, tf)] = fulcrum

    @classmethod
    def clear_emit(cls, account: str, symbol: str, tf: str) -> None:
        """Episode ended (no arm this bar) → next arm is a fresh touch."""
        cls._last_emit.pop((str(account), symbol, tf), None)

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
                comment: str = "", magic: int = 0, now: float | None = None) -> Command:
        cmd = Command(
            id=uuid.uuid4().hex[:12], account=str(account), type=type, symbol=symbol,
            order_type=order_type, price=round(float(price), 5), lot=round(float(lot), 2),
            sl=round(float(sl), 5), tp=round(float(tp), 5), comment=comment,
            magic=int(magic),
            ts_created=now if now is not None else time.time(),
        )
        with cls._lock:
            cls._cmds[cmd.id] = cmd
            cls._seq.append(cmd.id)
        cls._audit("enqueue", cmd)
        return cmd

    @classmethod
    def enqueue_grid_plan(cls, account: str, broker_symbol: str, plan, *,
                          close_first: bool = True, clear_kind: str = "flatten",
                          magic: int = 0) -> list[Command]:
        """Translate a rebased neutral GridPlan into PLACE_PENDING commands.
        buy_legs → buy_stop, sell_legs → sell_stop, shared per-side TP, no SL (v1).
        Optionally prepend a clear command: clear_kind="flatten" → CLOSE_ALL (close
        positions + cancel pendings); "cancel" → CANCEL_PENDINGS (cancel stale pendings
        only, never touch a live position — the safe re-arm path)."""
        # Per-order tag = the source level, so each grid's legs are identifiable in the
        # MT5 comment: FB|poc|b1, FB|vah|s2, FB|hvn|b3 … (vp_level_touch → its level_type;
        # hvn_inside_touch → "hvn"; else the trigger kind). All legs of one grid share
        # the level; b/s + index distinguish legs. (MT5 comment cap ~31 chars — fits.)
        ctx = getattr(plan, "trigger_context", {}) or {}
        kind = getattr(plan, "trigger_kind", "") or ""
        if kind == "vp_level_touch":
            tag = str(ctx.get("level_type") or "vp")
        elif kind == "hvn_inside_touch":
            tag = "hvn"
        elif kind == "squeeze":
            tag = "sqz"
        else:
            tag = (kind[:8] or "grid")

        out: list[Command] = []
        if close_first:
            clear_cmd = CLOSE_ALL if clear_kind == "flatten" else CANCEL_PENDINGS
            # scope the clear to THIS cycle's magic so a re-arm only cancels its own
            # TF/strategy pendings, never a sibling TF cycle's live orders.
            out.append(cls.enqueue(account, clear_cmd, broker_symbol, magic=magic))
        for i, leg in enumerate(getattr(plan, "buy_legs", []) or []):
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol, order_type="buy_stop",
                price=leg.price, lot=leg.lot, sl=0.0, tp=getattr(plan, "buy_tp", 0.0),
                comment=f"FB|{tag}|b{i + 1}", magic=magic))
        for i, leg in enumerate(getattr(plan, "sell_legs", []) or []):
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol, order_type="sell_stop",
                price=leg.price, lot=leg.lot, sl=0.0, tp=getattr(plan, "sell_tp", 0.0),
                comment=f"FB|{tag}|s{i + 1}", magic=magic))
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
            cls._last_emit.clear()
            cls._last_arm.clear()
            cls._open.clear()
            cls._ict_overlay.clear()
