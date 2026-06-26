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
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

LOG = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_AUDIT_LOG = _ROOT / "data" / "exec_bridge.jsonl"
_EMIT_LOG = _ROOT / "data" / "exec_emit.jsonl"   # ground-truth arm/exit decisions
_ARM_STATE_FILE = _ROOT / "data" / "arm_state.json"  # persisted across restarts


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
CLOSE_SIDE = "CLOSE_SIDE"        # close a fraction of ONE side's positions (bias-side booking)
MOVE_BE = "MOVE_BE"              # move one side's positions' SL to breakeven (risk-free runner)

# ── per-strategy × per-TF magic scheme ───────────────────────────────────────
# magic = MAGIC_BASE + strat_code·10 + tf_code  →  e.g. hvn·15m = 770013,
# squeeze·1h = 770024. The EA owns the whole [MAGIC_BASE, MAGIC_BASE+99] range; the
# tf is recoverable as magic % 10, so the server can attribute each EA-reported
# position pool to the TF cycle that owns it (enables parallel per-TF cycles).
MAGIC_BASE = 770000
_STRAT_CODE = {
    "hvn_inside_touch": 1, "squeeze": 2, "vp_level_touch": 3, "imbalance": 4,
    "hvn_edge": 5, "anchor": 6, "va": 7, "cvd_div": 8,
    # Setup-level pseudo-kind: the "vp_levels" parallel setup (va OR vp_level_touch) arms
    # under ONE dedicated magic so the trade report reads it as a single setup. The audit
    # still records the real detector (trigger_kind) that fired.
    "vp_levels": 9,
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
    side: str = ""            # "buy"|"sell" for CLOSE_SIDE / MOVE_BE
    frac: float = 0.0         # fraction of that side's positions to close (CLOSE_SIDE)
    buy_tp: float = 0.0       # MODIFY_TP: new TP for buy positions/orders
    sell_tp: float = 0.0      # MODIFY_TP: new TP for sell positions/orders
    locked_pnl: float = 0.0   # MODIFY_SL: dollar P&L to lock in (EA converts to SL price)
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
        elif self.type in (CLOSE_SIDE, MOVE_BE):
            d.update(side=self.side, frac=self.frac, comment=self.comment)
        elif self.type == "MODIFY_TP":
            d.update(buy_tp=self.buy_tp, sell_tp=self.sell_tp)
        elif self.type == "MODIFY_SL":
            d.update(side=self.side, locked_pnl=self.locked_pnl)
        return d


def _load_arm_state() -> dict:
    """Load persisted arm state from disk. Keys are JSON-serialised tuples."""
    try:
        raw = json.loads(_ARM_STATE_FILE.read_text())
        return {tuple(json.loads(k)): v for k, v in raw.items()}
    except Exception:
        return {}


def _save_arm_state(arms: dict) -> None:
    try:
        out = {json.dumps(list(k)): v for k, v in arms.items()}
        _ARM_STATE_FILE.write_text(json.dumps(out))
    except Exception as e:
        LOG.warning(f"[arm_state] save failed: {e}")


class ExecBridge:
    """Process-wide singleton-ish command queue."""

    _lock = threading.Lock()
    _cmds: dict[str, Command] = {}          # id → Command
    _seq: list[str] = []                    # insertion order (FIFO dispatch)
    _quotes: dict[tuple, dict] = {}         # (account, broker_symbol) → {bid,ask,mid,ts}
    _last_emit: dict[tuple, float] = {}     # (account, symbol, tf) → last emitted fulcrum
    _last_arm: dict[tuple, dict] = _load_arm_state()  # persisted across restarts
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
                 tf: str = "", now: float | None = None, magic: int = 0) -> None:
        # Cycle state is keyed by MAGIC (strategy×TF), not TF alone — so multiple setups
        # (hvn / squeeze / vp …) run as INDEPENDENT parallel cycles on the same symbol+TF.
        with cls._lock:
            cls._open[(str(account), symbol, int(magic))] = {
                "positions": int(positions), "pendings": int(pendings), "tf": tf,
                "ts": now if now is not None else time.time(),
            }

    @classmethod
    def get_open(cls, account: str, symbol: str, tf: str = "", magic: int = 0) -> dict:
        with cls._lock:
            return dict(cls._open.get((str(account), symbol, int(magic)), {"positions": 0, "pendings": 0}))

    # ── last-armed grid (ground truth for chart drawing + diagnostics) ─────────
    @classmethod
    def set_last_arm(cls, account: str, broker_symbol: str, tf: str = "", magic: int = 0,
                     **meta) -> None:
        # Keyed by magic. The internal monitor calls re-pass the stored cyc via **cyc, so
        # `magic` arrives either as the named arg or inside meta — accept both.
        key_magic = int(magic or meta.get("magic", 0) or 0)
        with cls._lock:
            cls._last_arm[(str(account), broker_symbol, key_magic)] = dict(meta, tf=tf, magic=key_magic)
            _save_arm_state(cls._last_arm)

    @classmethod
    def get_last_arm(cls, account: str, broker_symbol: str, tf: str = "", magic: int = 0) -> dict | None:
        with cls._lock:
            m = cls._last_arm.get((str(account), broker_symbol, int(magic)))
            return dict(m) if m else None

    @classmethod
    def get_active_arm_for_tf(cls, account: str, broker_symbol: str, tf: str) -> dict | None:
        """Dashboard helper: with cycles keyed by magic, several setups may be armed on
        one TF. Return the most-recently-armed ACTIVE cycle whose stored tf matches — what
        the EA's zone overlay draws (the fulcrum/trigger currently in play on that TF)."""
        with cls._lock:
            best, best_ts = None, -1.0
            for (acc, sym, _mg), m in cls._last_arm.items():
                if acc != str(account) or sym != broker_symbol:
                    continue
                if m.get("tf") != tf or not m.get("active"):
                    continue
                ts = float(m.get("ts", 0.0) or 0.0)
                if ts >= best_ts:
                    best, best_ts = m, ts
            return dict(best) if best else None

    @classmethod
    def active_fulcrums(cls, account: str, broker_symbol: str) -> dict[int, float]:
        """Per-magic venue fulcrum for every ACTIVE cycle on (account, symbol). Lets the
        EA compute hedged loss correctly with N parallel cycles — each cycle's legs
        measured against ITS OWN fulcrum, not one shared value. {magic: fulcrum}."""
        out: dict[int, float] = {}
        with cls._lock:
            for (acc, sym, mg), m in cls._last_arm.items():
                if acc != str(account) or sym != broker_symbol or not m.get("active"):
                    continue
                f = float(m.get("fulcrum", 0.0) or 0.0)
                if mg and f > 0:
                    out[int(mg)] = f
        return out

    @classmethod
    def active_cycles_detail(cls, account: str, broker_symbol: str) -> list[dict]:
        """All active cycles with full arm metadata. Used by /exec/zones to annotate
        which cycle sits in which HVN and to build the dashboard grid_cycles array."""
        out = []
        with cls._lock:
            for (acc, sym, mg), m in cls._last_arm.items():
                if acc != str(account) or sym != broker_symbol or not m.get("active"):
                    continue
                f = float(m.get("fulcrum", 0.0) or 0.0)
                if not (mg and f > 0):
                    continue
                out.append({
                    "magic": int(mg),
                    "tf": str(m.get("tf") or ""),
                    "fulcrum": f,
                    "edge": str(m.get("edge") or ""),
                    "trigger_kind": str(m.get("trigger_kind") or ""),
                    "node_low": float(m.get("node_low") or 0.0),
                    "node_high": float(m.get("node_high") or 0.0),
                    "tp_up":   float(m.get("tp_up") or 0.0),
                    "tp_down": float(m.get("tp_down") or 0.0),
                    "buy_n":   int(m.get("buy_n") or 0),
                    "sell_n":  int(m.get("sell_n") or 0),
                    "squeeze_ok": bool(m.get("squeeze_ok")),
                })
        return out


    # ── cycle monitor (server-side exit brain) ─────────────────────────────────
    @classmethod
    def monitor_cycle(cls, account: str, symbol: str, settings: dict | None, *,
                      pnl: float | None = None, buys: int | None = None,
                      sells: int | None = None, now: float | None = None,
                      tf: str = "", magic: int = 0,
                      buy_pnl: float | None = None, sell_pnl: float | None = None) -> str | None:
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
        cyc = cls.get_last_arm(account, symbol, magic=magic)
        if not cyc or not cyc.get("active"):
            return None
        if not magic:
            magic = int(cyc.get("magic") or 0)

        open_state = cls.get_open(account, symbol, magic=magic)
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
                cls.enqueue(account, CLOSE_ALL, symbol, comment=f"FB|flatten|{tf}|retry", magic=magic, now=t)
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
            elif pendings > 0 and max_seen > 0:
                # Positions closed (TP/SL/manual) but opposite-side pending orders remain.
                # Cancel them so they don't fill into an unprotected new position.
                cls.enqueue(account, CANCEL_PENDINGS, symbol, magic=magic,
                            comment=f"FB|cancel_stale|{tf}", now=t)
                LOG.info(f"[reconcile] {symbol} magic={magic} positions gone, "
                         f"cancelling {pendings} stale pendings")
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

        # 0.5) Cycle-level trailing SL — tracks ONE combined (buy+sell) peak for the
        # whole cycle. When combined P&L retraces from that peak, both sides get a
        # broker-side SL: bias side uses bias_trail_* giveback, hedge side uses
        # hedge_trail_* giveback (can be set tighter or wider independently).
        if (bool(grid_cfg.get("bias_trail_enabled", True))
                and not cyc.get("bias_booked")
                and buy_pnl is not None and sell_pnl is not None):
            buy_pnl_f  = float(buy_pnl  or 0.0)
            sell_pnl_f = float(sell_pnl or 0.0)
            buys_n  = int(buys  or 0)
            sells_n = int(sells or 0)
            combined_pnl = buy_pnl_f + sell_pnl_f
            # Identify which side is the bias (higher positive P&L).
            bias = ""
            if buys_n > 0 and buy_pnl_f > 0 and buy_pnl_f >= sell_pnl_f:
                bias = "buy"
            elif sells_n > 0 and sell_pnl_f > 0:
                bias = "sell"
            if bias:
                old_peak = float(cyc.get("cycle_trail_peak") or 0.0)
                peak = max(old_peak, combined_pnl)
                _act_by_tf = grid_cfg.get("bias_trail_activate_by_tf") or {}
                activate   = float(_act_by_tf.get(tf) or grid_cfg.get("bias_trail_activate_usd", 5.0) or 0.0)
                if peak > old_peak:
                    cyc["cycle_trail_peak"] = peak
                    cls.set_last_arm(account, symbol, **cyc)
                    LOG.info(f"[trail] {symbol} magic={magic} cycle peak "
                             f"{old_peak:.2f}→{peak:.2f} (combined={combined_pnl:.2f})")
                    if activate > 0 and peak >= activate:
                        # Bias side SL
                        _give_bias = grid_cfg.get("bias_trail_giveback_pct_by_tf") or {}
                        gb_bias    = float(_give_bias.get(tf) or grid_cfg.get("bias_trail_giveback_pct", 40.0) or 0.0)
                        if (bias == "buy" and buys_n > 0 and buy_pnl_f > 0) or \
                           (bias == "sell" and sells_n > 0 and sell_pnl_f > 0):
                            side_pnl_bias = buy_pnl_f if bias == "buy" else sell_pnl_f
                            locked_bias   = side_pnl_bias * (1.0 - gb_bias / 100.0)
                            cls.enqueue(account, "MODIFY_SL", symbol, magic=magic, side=bias,
                                        locked_pnl=locked_bias,
                                        comment=f"FB|tsl|{tf}|{bias}|bias", now=t)
                        # Hedge side SL
                        hedge = "sell" if bias == "buy" else "buy"
                        hedge_pnl_f = sell_pnl_f if bias == "buy" else buy_pnl_f
                        hedge_n     = sells_n    if bias == "buy" else buys_n
                        _give_hedge = grid_cfg.get("hedge_trail_giveback_pct_by_tf") or {}
                        gb_hedge    = float(_give_hedge.get(tf) or grid_cfg.get("hedge_trail_giveback_pct", 40.0) or 0.0)
                        if hedge_n > 0 and hedge_pnl_f > 0:
                            locked_hedge = hedge_pnl_f * (1.0 - gb_hedge / 100.0)
                            cls.enqueue(account, "MODIFY_SL", symbol, magic=magic, side=hedge,
                                        locked_pnl=locked_hedge,
                                        comment=f"FB|tsl|{tf}|{hedge}|hedge", now=t)
                        cls.enqueue(account, "CANCEL_PENDINGS", symbol, magic=magic,
                                    comment=f"FB|tsl|{tf}|cancel_pend", now=t)
                        LOG.info(f"[trail] {symbol} magic={magic} tf={tf} cycle SL set "
                                 f"peak={peak:.2f} bias={bias} gb_bias={gb_bias}% gb_hedge={gb_hedge}%")

        reason: str | None = None
        detail: dict = {}

        # 1) flatten-rest — a filled leg closed while the opposite ladder still rests,
        # OR one side of a fully-filled squeeze cycle exited (trail SL / TP / manual)
        # leaving the other side open with no pending orders to cancel.
        _buys_n  = int(buys  or 0)
        _sells_n = int(sells or 0)
        one_sided_remnant = (
            positions > 0 and pendings == 0 and max_seen > 0
            and ((_buys_n == 0) != (_sells_n == 0))  # exactly one side is flat
        )
        if (0 < positions < max_seen and pendings > 0) or one_sided_remnant:
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

        cls.enqueue(account, CLOSE_ALL, symbol, comment=f"FB|flatten|{tf}|{reason}", magic=magic, now=t)
        cls.set_last_arm(account, symbol, **{**cyc, "flatten_ts": t})
        _emit_exit_audit({"account": str(account), "broker_symbol": symbol, "tf": tf, "magic": magic,
                          "exit_reason": reason, "armed_tf": cyc.get("armed_tf", ""),
                          "positions": positions, "pendings": pendings,
                          "buys": buys, "sells": sells, "pnl": pnl, "venue_mid": mid,
                          "tp_up": tp_up, "tp_down": tp_down,
                          "squeeze_ok": cyc.get("squeeze_ok"), "squeeze_rank": cyc.get("squeeze_rank"),
                          **detail})
        return reason

    # ── emit dedup (one grid per HVN-touch episode, not per bar) ───────────────
    @classmethod
    def should_emit(cls, account: str, symbol: str, fulcrum: float, tol: float,
                    magic: int = 0) -> bool:
        """True if this fulcrum is a NEW touched-edge episode (no prior, or moved
        more than `tol` from the last emitted edge). Per-magic so each setup dedups
        independently. Prevents re-arming the same node every bar while price sits in it."""
        last = cls._last_emit.get((str(account), symbol, int(magic)))
        return last is None or abs(fulcrum - last) > tol

    @classmethod
    def mark_emit(cls, account: str, symbol: str, fulcrum: float, magic: int = 0) -> None:
        cls._last_emit[(str(account), symbol, int(magic))] = fulcrum

    @classmethod
    def clear_emit(cls, account: str, symbol: str, magic: int = 0) -> None:
        """Episode ended (no arm this bar) → next arm is a fresh touch."""
        cls._last_emit.pop((str(account), symbol, int(magic)), None)

    # ── venue quote cache (EA reports its live price on each poll) ─────────────
    @classmethod
    def set_quote(cls, account: str, symbol: str, bid: float, ask: float,
                  stops_dist: float = 0.0, now: float | None = None) -> None:
        if bid <= 0 or ask <= 0:
            return
        with cls._lock:
            cls._quotes[(str(account), symbol)] = {
                "bid": float(bid), "ask": float(ask), "mid": (bid + ask) / 2.0,
                "stops_dist": float(stops_dist),   # broker min stop distance ($) for step floor
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
                comment: str = "", magic: int = 0, side: str = "", frac: float = 0.0,
                buy_tp: float = 0.0, sell_tp: float = 0.0,
                locked_pnl: float = 0.0,
                now: float | None = None) -> Command:
        cmd = Command(
            id=uuid.uuid4().hex[:12], account=str(account), type=type, symbol=symbol,
            order_type=order_type, price=round(float(price), 5), lot=round(float(lot), 2),
            sl=round(float(sl), 5), tp=round(float(tp), 5), comment=comment,
            magic=int(magic), side=side, frac=float(frac),
            buy_tp=round(float(buy_tp), 5), sell_tp=round(float(sell_tp), 5),
            locked_pnl=round(float(locked_pnl), 2),
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
                          magic: int = 0, leg_tp: bool = True) -> list[Command]:
        """Translate a rebased neutral GridPlan into PLACE_PENDING commands.
        buy_legs → buy_stop, sell_legs → sell_stop, shared per-side TP, no SL (v1).
        Optionally prepend a clear command: clear_kind="flatten" → CLOSE_ALL (close
        positions + cancel pendings); "cancel" → CANCEL_PENDINGS (cancel stale pendings
        only, never touch a live position — the safe re-arm path).

        leg_tp=False places legs WITHOUT a per-order TP — they never self-close, so the
        winning side can't book while the losing side dangles (a per-leg TP hit ≠ a
        net-positive cycle). The basket net-target exit (monitor_cycle) then owns ALL
        profit-taking and only closes when the whole cycle is net ≥ target."""
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
        buy_tp = getattr(plan, "buy_tp", 0.0) if leg_tp else 0.0
        sell_tp = getattr(plan, "sell_tp", 0.0) if leg_tp else 0.0
        for i, leg in enumerate(getattr(plan, "buy_legs", []) or []):
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol, order_type="buy_stop",
                price=leg.price, lot=leg.lot, sl=0.0, tp=buy_tp,
                comment=f"FB|{tag}|b{i + 1}", magic=magic))
        for i, leg in enumerate(getattr(plan, "sell_legs", []) or []):
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol, order_type="sell_stop",
                price=leg.price, lot=leg.lot, sl=0.0, tp=sell_tp,
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
