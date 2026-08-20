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

State is backed by arm_state_store (JSONL on disk). Call load_persisted_state()
at Flask startup to restore _last_arm / _last_emit after a restart so live
cycles are not orphaned.
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

from execution.arm_state_store import persist_arm, persist_emit, load as _load_arm_state  # noqa: E402


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
MODIFY_PENDING = "MODIFY_PENDING"    # shift pending stop prices by delta + update TP
MOVE_BE = "MOVE_BE"              # move one side's positions' SL to breakeven (risk-free runner)
MODIFY_POSITION = "MODIFY_POSITION"  # refresh TP on one side's filled positions (keep SL)

# ── per-strategy × per-TF magic scheme ───────────────────────────────────────
# magic = MAGIC_BASE + strat_code·10 + tf_code  →  e.g. hvn·15m = 770013,
# squeeze·1h = 770024. The EA owns the whole [MAGIC_BASE, MAGIC_BASE+99] range; the
# tf is recoverable as magic % 10, so the server can attribute each EA-reported
# position pool to the TF cycle that owns it (enables parallel per-TF cycles).
MAGIC_BASE = 770000
_STRAT_CODE = {
    "hvn_inside_touch": 1, "squeeze": 2, "vp_level_touch": 3, "imbalance": 4,
    "hvn_edge": 5, "anchor": 6, "va": 7, "cvd_div": 8, "hvn_displacement": 10,
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
    """Recover the TF a magic belongs to (magic % 10). '' if not one of ours.

    Range must cover the highest strat_code in _STRAT_CODE (hvn_displacement=10 → magics
    770101-770104), not just the first 10 strategies. Was bounded at MAGIC_BASE+100, which
    silently excluded every hvn_displacement magic — tf_from_magic("") then made
    _refresh_cycle_tps/retry_failed_pendings skip those cycles outright. Match the EA's own
    magic_hi=770150 poll window (ea_guard.py) so this can't drift out of sync again."""
    if magic < MAGIC_BASE or magic >= MAGIC_BASE + 150:
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
        elif self.type == MODIFY_PENDING:
            # price field carries price_delta; tp = new TP (0 = leave unchanged)
            d.update(price_delta=self.price, tp=self.tp, side=self.side)
        elif self.type == MODIFY_POSITION:
            # tp = new TP; sl = new SL (0 = leave unchanged) for filled positions on `side`
            d.update(tp=self.tp, sl=self.sl, side=self.side, comment=self.comment)
        return d



# ── durable cycle-outcome log (survives restarts; the analysis ground truth) ──
# exec_emit.jsonl logs arms and exits as SEPARATE rows with no join key, and its
# in-memory cycle state dies on restart — which is why per-cycle P&L on this branch
# could only ever be reconstructed from broker statements. This writes ONE row per
# completed cycle with the arm context and the exit outcome already joined, keyed by
# cycle_id and partitioned by trading date so a day's file is self-contained.
#
# It also carries squeeze_ok/squeeze_rank, which is what makes the squeeze A/B
# answerable: the planner labels EVERY arm whether or not the gate is enforcing,
# so with require_squeeze_gate false both cohorts arm and both get stamped.
_CYCLE_LOG_DIR = _ROOT / "data" / "cycles"


def _cycle_log_path() -> "Path":
    return _CYCLE_LOG_DIR / f"cycle_outcomes_{time.strftime('%Y-%m-%d')}.jsonl"


def cycle_id_for(account: str, symbol: str, magic: int, armed_ts: float) -> str:
    """Stable id for one arm->exit lifecycle. armed_ts disambiguates re-arms on the
    same magic, so consecutive cycles never collide."""
    return f"{account}:{symbol}:{int(magic)}:{int(armed_ts)}"


def _emit_cycle_outcome(cyc: dict, *, account: str, symbol: str, magic: int,
                        tf: str, exit_reason: str, **outcome) -> None:
    """One durable row per completed cycle: arm context + exit outcome, joined.

    Pulls the arm-side fields off the persisted cycle dict so the row is
    self-contained — no post-hoc join against exec_emit.jsonl is needed to answer
    "which setup, on which TF, from which fulcrum, with or without a coil, exited
    how, for how much"."""
    try:
        armed_ts = float(cyc.get("ts") or 0.0)
        row = {
            "cycle_id": cycle_id_for(account, symbol, magic, armed_ts),
            "date": time.strftime("%Y-%m-%d"),
            "account": str(account), "broker_symbol": symbol,
            "magic": int(magic), "tf": tf or cyc.get("armed_tf", ""),
            "armed_ts": armed_ts, "exit_ts": time.time(),
            "held_s": round(time.time() - armed_ts, 1) if armed_ts else None,
            "trigger_kind": cyc.get("trigger_kind", ""), "edge": cyc.get("edge", ""),
            "fulcrum": cyc.get("fulcrum"), "step": cyc.get("step"),
            "n_per_side": cyc.get("n_per_side"),
            "buy_n": cyc.get("buy_n"), "sell_n": cyc.get("sell_n"),
            "node_low": cyc.get("node_low"), "node_high": cyc.get("node_high"),
            "tp_up": cyc.get("tp_up"), "tp_down": cyc.get("tp_down"),
            "squeeze_ok": cyc.get("squeeze_ok"), "squeeze_rank": cyc.get("squeeze_rank"),
            "skew": cyc.get("skew"), "net_target_usd": cyc.get("net_target_usd"),
            "max_pos_seen": cyc.get("max_pos_seen"), "pend_seen": cyc.get("pend_seen"),
            "bias_booked": bool(cyc.get("bias_booked")),
            "bias_peak": cyc.get("bias_peak"), "trough": cyc.get("trough"),
            "exit_reason": exit_reason,
            **outcome,
        }
        _CYCLE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _cycle_log_path().open("a") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass  # audit must never break execution


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
    _retried_cmd_ids: set[str] = set()      # PLACE_PENDING cmd ids already resubmitted once
    _retry_counts: dict[tuple, int] = {}    # (account,symbol,magic,comment) → attempts so far
    _last_retry_at: dict[tuple, float] = {} # (account,symbol,magic) → ts of last retry pass
    _last_be_attempt: dict[tuple, float] = {}  # (account,symbol,magic,side) → ts of last MOVE_BE
    _be_attempt_count: dict[tuple, int] = {}   # (account,symbol,magic,side) → attempts so far
    _last_close_retry_at: dict[tuple, float] = {}  # (account,symbol,magic) → ts of last close retry
    _close_retry_counts: dict[tuple, int] = {}     # (account,symbol,magic,type,side) → attempts
    _pending_book_cmd: dict[tuple, str] = {}    # (account,symbol,magic,side) → in-flight CLOSE_SIDE cmd id
    _last_book_attempt: dict[tuple, float] = {} # (account,symbol,magic,side) → ts of last book attempt
    _last_candle_sl: dict[tuple, float] = {}    # (account,symbol,magic,side) → last SL we ratcheted to
    # daily P&L target tracking — keyed by account
    _daily_start_balance: dict[str, float] = {}   # account → balance at first poll of the day
    _daily_start_date: dict[str, str] = {}         # account → YYYY-MM-DD of that first poll
    _daily_target_hit: dict[str, bool] = {}        # account → True once daily target reached
    # intrabar touch-arm tick-reversal state — keyed by (account, broker_symbol, tf)
    # tracks an edge tap awaiting a reversal-back-inside confirm (touch-arm path)
    _touch_state: dict[tuple, dict] = {}           # → {edge, side, tapped_px, ts}

    # ── daily P&L target ────────────────────────────────────────────────────────
    @classmethod
    def update_account_balance(cls, account: str, balance: float, equity: float,
                               target_pct: float, target_usd: float = 0.0) -> dict:
        """Track daily equity and return {hit, pnl_pct, pnl_usd, start_balance} each poll.

        Hits on EITHER gate, whichever is configured >0 — an absolute $ combined-PnL
        target (target_usd) alongside/instead of the % target. Resets at midnight (UTC
        date change). Once hit=True it stays True for the day — caller enqueues CLOSE_ALL
        for every active magic on each poll from here on. Does NOT block new arms; a
        cycle that arms after the hit is flattened on its next poll, not refused upfront.
        """
        import datetime
        account = str(account)
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        with cls._lock:
            # reset on new day
            if cls._daily_start_date.get(account) != today:
                cls._daily_start_date[account]   = today
                cls._daily_start_balance[account] = balance
                cls._daily_target_hit[account]    = False

            start = cls._daily_start_balance.get(account, balance)
            if start <= 0:
                return {"hit": False, "pnl_pct": 0.0, "pnl_usd": 0.0, "start_balance": start}

            pnl_usd = equity - start
            pnl_pct = pnl_usd / start * 100.0

            already_hit = cls._daily_target_hit.get(account, False)
            hit = already_hit or (target_pct > 0 and pnl_pct >= target_pct) \
                              or (target_usd > 0 and pnl_usd >= target_usd)
            if hit and not already_hit:
                cls._daily_target_hit[account] = True

        return {"hit": hit, "pnl_pct": round(pnl_pct, 4), "pnl_usd": round(pnl_usd, 2),
                "start_balance": round(start, 2), "equity": round(equity, 2)}

    @classmethod
    def daily_target_hit(cls, account: str) -> bool:
        return cls._daily_target_hit.get(str(account), False)

    # ── intrabar touch-arm + tick-reversal confirm ───────────────────────────────
    @classmethod
    def touch_arm_check(cls, account: str, broker_symbol: str, tf: str,
                        live_price: float, edge: float, side: str,
                        confirm_ticks: float, now: float | None = None) -> bool:
        """Tick-reversal state machine for intrabar touch-arming. Call each poll with
        the live price and the edge it's tapping (from touch_arm_trigger). Returns True
        ONCE when price has tapped the edge AND then reverted back INSIDE the node by
        `confirm_ticks` (a mini-rejection) — the intrabar twin of "close back inside".

        State per (account, symbol, tf): record the tap, then on a later poll confirm
        if price moved back inside. A breakout (price keeps going through the edge)
        never reverts → never confirms → no arm. State clears on confirm or when price
        leaves the buffer entirely (tap abandoned)."""
        t = now if now is not None else time.time()
        key = (str(account), broker_symbol, tf)
        with cls._lock:
            st = cls._touch_state.get(key)
            # reverted INSIDE = moved away from the edge toward node interior:
            #   top edge → price dropped below (edge - confirm_ticks)
            #   bottom edge → price rose above (edge + confirm_ticks)
            if st is not None and st.get("edge") == edge and st.get("side") == side:
                reverted = (live_price <= edge - confirm_ticks) if side == "top" \
                    else (live_price >= edge + confirm_ticks)
                if reverted:
                    cls._touch_state.pop(key, None)   # consume — one confirm per tap
                    return True
                return False
            # new tap on this edge → record, await reversal
            cls._touch_state[key] = {"edge": edge, "side": side,
                                     "tapped_px": live_price, "ts": t}
            return False

    @classmethod
    def clear_touch_state(cls, account: str, broker_symbol: str, tf: str) -> None:
        """Drop a pending tap (price left the buffer without confirming)."""
        with cls._lock:
            cls._touch_state.pop((str(account), broker_symbol, tf), None)

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
                 tf: str = "", now: float | None = None, magic: int = 0,
                 buys: int = 0, sells: int = 0) -> None:
        # Cycle state is keyed by MAGIC (strategy×TF), not TF alone — so multiple setups
        # (hvn / squeeze / vp …) run as INDEPENDENT parallel cycles on the same symbol+TF.
        # buys/sells retained for the cycle_live(2B,3S,…) skip-reason breakdown.
        with cls._lock:
            cls._open[(str(account), symbol, int(magic))] = {
                "positions": int(positions), "pendings": int(pendings), "tf": tf,
                "buys": int(buys), "sells": int(sells),
                "ts": now if now is not None else time.time(),
            }

    @classmethod
    def get_open(cls, account: str, symbol: str, tf: str = "", magic: int = 0) -> dict:
        with cls._lock:
            return dict(cls._open.get((str(account), symbol, int(magic)),
                                      {"positions": 0, "pendings": 0, "buys": 0, "sells": 0}))

    # ── last-armed grid (ground truth for chart drawing + diagnostics) ─────────
    @classmethod
    def set_last_arm(cls, account: str, broker_symbol: str, tf: str = "", magic: int = 0,
                     **meta) -> None:
        # Keyed by magic. The internal monitor calls re-pass the stored cyc via **cyc, so
        # `magic` arrives either as the named arg or inside meta — accept both.
        key_magic = int(magic or meta.get("magic", 0) or 0)
        if key_magic == 0:
            # A cycle key of 0 is never legitimate — it means a `**cyc` spread lost the
            # magic (a restored/persisted cyc carries it only in the KEY, not the body).
            # Writing anyway silently forks the cycle: the real magic keeps a stale record
            # while every subsequent update lands under 0. Observed 2026-08-20 — magic
            # 770023's bias_peak froze at 61.0 on disk while the live peak climbed past
            # 374 under key 0, so the trail was tracking and blind at the same time.
            import logging
            logging.getLogger("exec_bridge").error(
                "[set_last_arm] refusing magic=0 write for %s/%s — caller lost the magic "
                "(tf=%s, keys=%s)", account, broker_symbol, tf, sorted(meta)[:8])
            return
        state = dict(meta, tf=tf, magic=key_magic)
        # Carry the trail's accumulated view across a RE-ANCHOR. A fulcrum shift writes a
        # whole fresh arm record every few minutes, and it was resetting bias_peak to 0.0
        # while the FILLED legs from the previous fulcrum were still open — so the trail's
        # memory of "how good did this get" was erased on a timer. Observed live
        # 2026-08-20 on magic 770052: hvn_edge 5m re-anchored at 17:10 and 17:30, the
        # position reached 800+ USC against a 125 threshold, and nothing ever booked
        # because each re-arm zeroed the peak before it could be given back.
        #
        # Reset is only correct when the cycle is genuinely FLAT and starting over. While
        # it holds positions, the peak, the one-shot book guard and the high-water position
        # count all belong to the inventory, not to the fulcrum.
        with cls._lock:
            prev = cls._last_arm.get((str(account), broker_symbol, key_magic)) or {}
            # `prev.get("active")` matters here: a fully-closed cycle always gets
            # active=False written at close (every flatten path does this), so a NEW
            # cycle later reusing the same magic sees prev.active=False and correctly
            # skips carry-over even though prev.max_pos_seen is stale-nonzero from the
            # old, unrelated episode. Only a still-open cycle being re-anchored qualifies.
            if prev and prev.get("active") and int(prev.get("max_pos_seen") or 0) > 0 \
                    and not state.get("_flat"):
                # `k not in meta` used to gate this — but the emit_grid re-arm path (the
                # ONLY path that matters here) always explicitly passes bias_peak=0.0,
                # bias_booked=False, max_pos_seen=0 as its "fresh arm" defaults, re-anchor
                # or not. Those keys are therefore always present in meta, so the old
                # `k not in meta` guard never actually fired on a real re-anchor — verified
                # live 2026-08-20 on magic 770052: prev_bias_peak=140.25/max_pos_seen=2 right
                # before a re-anchor, then the new record showed bias_peak=0.0/max_pos_seen=0
                # anyway. Fixed with max()/OR instead of a blind overwrite, so a genuine
                # higher update from monitor_cycle (which only ever raises these) still wins
                # over a stale carry-over, while a re-arm's blind zero-default never wins
                # over a real prior peak.
                for k in ("bias_peak", "max_pos_seen"):
                    if k in prev:
                        state[k] = max(float(state.get(k, 0) or 0), float(prev.get(k, 0) or 0))
                for k in ("bias_booked", "bias_trail_done", "be_done_buy", "be_done_sell"):
                    if k in prev:
                        state[k] = bool(state.get(k, False)) or bool(prev.get(k, False))
                for k in ("vp_frozen", "frozen_zones"):
                    if k in prev and k not in meta:
                        state[k] = prev[k]
            state.pop("_flat", None)
            cls._last_arm[(str(account), broker_symbol, key_magic)] = state
        try:
            persist_arm(account, broker_symbol, key_magic, state)
        except Exception:
            pass  # persistence failure must never block execution

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
        """All active cycles with full arm metadata: magic, tf, fulcrum, edge, trigger_kind,
        node_low, node_high. Used by /exec/zones to annotate which cycle sits in which HVN."""
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
          1. net-$ target — basket floating ≥ effective (hedge-decayed) per-TF target
          2. flatten-rest — a leg closed mid-cycle while the opposite ladder rests
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
                cls.set_last_arm(account, symbol, **{**cyc, "magic": magic, "active": False, "flatten_ts": 0.0})
                cls.clear_emit(account, symbol, magic=magic)
            elif (t - fts) > _FLATTEN_GRACE_S:
                # close demonstrably didn't land (past the queue's reclaim window) → re-issue once
                cls.enqueue(account, CLOSE_ALL, symbol, comment="FB|flatten|retry", magic=magic, now=t)
                cls.set_last_arm(account, symbol, **{**cyc, "magic": magic, "flatten_ts": t})
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
            cls.set_last_arm(account, symbol, **{**cyc, "magic": magic})

        # FREEZE the VP/HVN structure on FIRST leg fill. Until a position opens the cycle
        # tracks live VP (cheap re-anchor of resting pendings); once committed, we manage
        # against the structure that JUSTIFIED the entry — not a morphing live VP (which
        # caused false fade-flattens). Stores session HVN zones (analysis frame) + step into
        # the arm under `frozen_zones`; the refresh paths use these instead of live VP, and
        # fade-flatten is skipped entirely for a frozen cycle. Fires once (vp_frozen guard).
        if positions > 0 and not cyc.get("vp_frozen"):
            try:
                from execution.zone_triggers import _session_hvn_zones
                from pipeline.state_store import store as _st
                _atf = cyc.get("armed_tf") or cyc.get("tf") or ""
                # analysis symbol: monitor gets broker symbol; map back via settings
                _smap = (settings.get("execution") or {}).get("symbol_map", {}) if isinstance(settings, dict) else {}
                _b2a = {v: k for k, v in _smap.items()}
                _asym = _b2a.get(symbol, symbol)
                _zbars = _st().recent(_asym, _atf, 120) if _atf else []
                _fz, _ = _session_hvn_zones(_asym, _atf, _zbars) if _atf else ([], "")
                if _fz:
                    cyc["frozen_zones"] = [[round(lo, 5), round(hi, 5)] for lo, hi in _fz]
                cyc["vp_frozen"] = True
                cls.set_last_arm(account, symbol, **{**cyc, "magic": magic})
            except Exception:
                pass  # never break the monitor on a snapshot failure

        if positions <= 0:
            # UNFREEZE on full close. The VP freeze only exists to stop a morphing live
            # VP from false-fading an OPEN position; once the cycle is flat that risk is
            # gone. Clearing vp_frozen/frozen_zones lets refresh/fade/TP-refresh — and any
            # re-fill of resting pendings — track LIVE VP/HVN/LVN again (back to the
            # pre-first-fill pending-only behavior). Re-fires the snapshot on the next fill.
            unfroze = False
            if cyc.get("vp_frozen") and (max_seen > 0 or pend_seen > 0):
                cyc.pop("vp_frozen", None)
                cyc.pop("frozen_zones", None)
                unfroze = True
            # flat. Retire the cycle once it had something live (positions filled OR
            # pendings rested) and now has nothing open AND nothing resting — frees the
            # symbol for a new arm by either tf. The (max/pend)_seen high-water avoids
            # the placement-window race (active set before the EA reports pendings).
            if (max_seen > 0 or pend_seen > 0) and pendings == 0:
                cls.set_last_arm(account, symbol, **{**cyc, "magic": magic, "active": False})
                cls.clear_emit(account, symbol, magic=magic)
            elif unfroze:
                # still active with resting pendings — persist the unfreeze so live-VP
                # refresh resumes on the resting legs / next fill.
                cls.set_last_arm(account, symbol, **{**cyc, "magic": magic})
            return None

        n = int(cyc.get("n_per_side") or 0)
        tp_up = float(cyc.get("tp_up") or 0.0)
        tp_down = float(cyc.get("tp_down") or 0.0)
        q = cls.get_quote(account, symbol) or {}
        mid = float(q.get("mid") or 0.0)

        grid_cfg = (settings.get("grid_levels") or {}) if isinstance(settings, dict) else {}
        close_on_full_hedge = bool(grid_cfg.get("cycle_close_on_full_hedge", True))

        # 0.4) full-fill action — one side filled ALL its legs; price committed that way.
        # cancel_opp=True (default): cancel the opposite side's pendings immediately so
        # they can't fill on a retrace and create a new unhedged position, then move the
        # filled side to BE so it can no longer lose. The cycle stays active so net_target
        # / bias_trail can still exit the filled side profitably.
        # cancel_opp=False (legacy): leave opposite pendings, only move BE (old behaviour).
        #
        # Retried, not one-shot: the EA's MOVE_BE loops every open ticket on the side, but
        # a ticket whose own breakeven price sits inside the broker's freeze band AT THAT
        # INSTANT is silently skipped (not an error) — its SL never gets set. The old code
        # set be_done_{side}=True unconditionally BEFORE the EA even attempted the move, so
        # a skipped ticket never got a second chance and stayed fully exposed forever.
        # Observed live 2026-08-20 on magic 770052: 2 buy tickets full-filled, MOVE_BE fired
        # once, EA moved only 1 (`moved:1`) — the other's BE fell inside the freeze band —
        # and the survivor sat unprotected until it was -272.50. ExecMoveBE is idempotent
        # (its own `improve` check no-ops a ticket already at BE), so it's safe to keep
        # resending: throttled to fullfill_be_retry_interval_s per side, capped at
        # fullfill_be_max_attempts so a structurally-stuck side doesn't spam forever.
        if bool(grid_cfg.get("fullfill_be_enabled", True)):
            _cancel_opp = bool(grid_cfg.get("fullfill_cancel_opposite", True))
            _be_interval = float(grid_cfg.get("fullfill_be_retry_interval_s", 3.0) or 3.0)
            _be_max_attempts = int(grid_cfg.get("fullfill_be_max_attempts", 20) or 20)
            _bn = int(cyc.get("buy_n") or 0)
            _sn = int(cyc.get("sell_n") or 0)
            for _side, _need, _have in (("buy", _bn, int(buys or 0)),
                                        ("sell", _sn, int(sells or 0))):
                if _need <= 0 or _have < _need or _have <= 0:
                    continue
                be_key = (str(account), symbol, int(magic), _side)
                n_attempts = cls._be_attempt_count.get(be_key, 0)
                if n_attempts >= _be_max_attempts:
                    continue
                if t - cls._last_be_attempt.get(be_key, 0.0) < _be_interval:
                    continue
                cls._last_be_attempt[be_key] = t
                cls._be_attempt_count[be_key] = n_attempts + 1
                if not cyc.get(f"be_done_{_side}"):
                    key = (str(account), symbol, int(magic))
                    with cls._lock:
                        live = cls._last_arm.get(key)
                        if live is not None and not live.get(f"be_done_{_side}"):
                            live[f"be_done_{_side}"] = True
                            cyc[f"be_done_{_side}"] = True
                            try:
                                persist_arm(account, symbol, int(magic), live)
                            except Exception:
                                pass
                _opp = "sell" if _side == "buy" else "buy"
                if _cancel_opp:
                    # cancel opposite pendings first so they can't fill on retrace
                    cls.enqueue(account, CANCEL_PENDINGS, symbol, magic=magic,
                                side=_opp, comment=f"FB|fullfill_cancel_opp|{_opp}", now=t)
                cls.enqueue(account, MOVE_BE, symbol, magic=magic, side=_side,
                            comment=f"FB|fullfill_be|{_side}", now=t)
                _emit_exit_audit({"account": str(account), "broker_symbol": symbol,
                                  "tf": tf, "magic": magic, "exit_reason": "fullfill_be",
                                  "side": _side, "filled": _have, "need": _need,
                                  "cancel_opp": _cancel_opp, "attempt": n_attempts + 1,
                                  "squeeze_ok": cyc.get("squeeze_ok"),
                                  "squeeze_rank": cyc.get("squeeze_rank")})

        # net-basket exit (which a hedge leg can mask). Gate: a side has ALL its legs
        # filled (the move committed your way). Track that side's peak floating P&L; when
        # it gives back ≥ giveback% from the peak, BOOK half that side and move the rest
        # to breakeven (risk-free runner). Fires once per cycle — guarded by
        # bias_trail_done, a PRIVATE one-shot flag distinct from bias_booked. bias_booked
        # is shared with the flatten-rest suppression block below, which RESETS it after
        # a partial close — gating the trail on bias_booked (as this branch used to) let
        # it re-fire 2-4s after its own book, on the SAME stored peak, at whatever price
        # the market had moved to by then: double-fire, ~75% booked then a 2nd book at
        # momentum price. Fixed upstream 2026-07-06 (deb8062) after it booked a LOSS on
        # the 2nd fire (Jun-26 side_pnl -1356 after +954, 3 occurrences in 57 fires) —
        # never ported to base-v2 (forked 2026-06-24, two weeks before this fix existed).
        # Confirmed live 2026-08-20: every "trail fired twice/three-times within seconds
        # on the same magic" reported this session was this exact bug, not deliberate
        # re-arming as earlier assumed. set_last_arm replaces the record wholesale, so a
        # fresh arm still starts clean (no explicit reset needed for bias_trail_done).
        if (bool(grid_cfg.get("bias_trail_enabled", True))
                and not cyc.get("bias_trail_done")
                and buy_pnl is not None and sell_pnl is not None):
            buy_n = int(cyc.get("buy_n") or 0)
            sell_n = int(cyc.get("sell_n") or 0)
            bias = ""
            # Gate on "this side is committed", NOT on "all its legs are open right
            # now". Legs close — a leg hitting its TP ceiling drops the count below
            # buy_n permanently, and the original `>= buy_n` test then reads false
            # forever, leaving the survivors with no exit but a distant TP. Once a
            # peak has been recorded the side is committed, so keep monitoring at
            # any leg count. The `> 0` guard stops a side with no positions at all
            # being selected as the bias.
            _peak_set = float(cyc.get("bias_peak") or 0.0) > 0.0
            # bias_trail_track_partial: also track a side that is only PARTIALLY filled
            # but already in profit. The original gate (d32794c) required ALL legs of a
            # side open — "the move committed that way" — and 3ad9740's _peak_set escape
            # only keeps a trail alive AFTER a peak exists; it cannot start one. So a side
            # that ran profitable without filling its last leg was invisible: no peak was
            # ever recorded, so nothing could ever arm.
            #
            # Observed live 2026-08-20 on magic 770052: buy_n=2, buys=1, and the buy side
            # reached +12.6 to +31.3 USC at the session high against a 5.0 activate
            # threshold. bias_peak stayed 0.0 the whole time and the profit was given back.
            # At base_lot 0.01 with 2-3 legs a side, partial fills are the NORMAL case, so
            # this blind spot is far more consequential here than at the June sizing.
            #
            # Gated because it cuts both ways: tracking partials books sooner, which may
            # clip runners. Off = historical behaviour, on = track any profitable side.
            _partial_ok = bool(grid_cfg.get("bias_trail_track_partial", False))
            _buy_live = int(buys or 0) > 0 and (int(buys or 0) >= buy_n or _peak_set
                                                or (_partial_ok and float(buy_pnl or 0.0) > 0))
            _sell_live = int(sells or 0) > 0 and (int(sells or 0) >= sell_n or _peak_set
                                                  or (_partial_ok and float(sell_pnl or 0.0) > 0))
            # bias_peak is a single shared scalar (not per-side) — once EITHER side has
            # ever peaked, _peak_set is True and makes BOTH _buy_live and _sell_live
            # eligible simultaneously. A hard buy-first order then locks bias to buy for
            # the rest of the cycle regardless of which side is actually committed now.
            # Observed live 2026-08-21 on magic 770012: buy peaked once early ($17.25),
            # then sat 1/5 filled at -$158.50 while sell ran to 4/5 filled at +$223 — sell
            # never got a peak recorded or a trail chance because buy always won the
            # elif. When both sides are simultaneously eligible, pick whichever is MORE
            # COMMITTED (more legs filled); tie-break on the better current P&L.
            _buy_fill = int(buys or 0)
            _sell_fill = int(sells or 0)
            if buy_n > 0 and _buy_live and sell_n > 0 and _sell_live:
                if _sell_fill > _buy_fill:
                    bias = "sell"
                elif _buy_fill > _sell_fill:
                    bias = "buy"
                else:
                    bias = "buy" if float(buy_pnl or 0.0) >= float(sell_pnl or 0.0) else "sell"
            elif buy_n > 0 and _buy_live:
                bias = "buy"
            elif sell_n > 0 and _sell_live:
                bias = "sell"
            if bias:
                side_pnl = float(buy_pnl if bias == "buy" else sell_pnl)
                peak = max(float(cyc.get("bias_peak") or 0.0), side_pnl)
                if peak != float(cyc.get("bias_peak") or 0.0):
                    cyc["bias_peak"] = peak
                    cls.set_last_arm(account, symbol, **{**cyc, "magic": magic})
                activate = float(grid_cfg.get("bias_trail_activate_usd", 5.0) or 0.0)
                giveback = float(grid_cfg.get("bias_trail_giveback_pct", 40.0) or 0.0)
                book_frac = float(grid_cfg.get("bias_book_frac", 0.5) or 0.5)
                # side_pnl > 0 floor: give-back-off-peak has no floor at zero on its
                # own, so a side that peaked and then ran deep negative still
                # satisfies "retraced >= giveback% from peak" and books a LOSS as if
                # it were a profit-lock — observed at side_pnl -1895 off a peak of
                # 1500. Worse than the bad book: the same branch sets bias_booked,
                # retiring the trail for the rest of the cycle. This is meant to lock
                # in profit on a pullback, not to realize a reversal.
                if (activate > 0 and peak >= activate and side_pnl > 0
                        and side_pnl <= peak * (1.0 - giveback / 100.0)):
                    # Verify before committing: don't set bias_booked / write cycle_outcomes
                    # until the CLOSE_SIDE actually closed something. Observed live
                    # 2026-08-20 on magic 770104: CLOSE_SIDE|buy came back
                    # {"ok": false, "closed": 0, "error": "close fail #529338888"} — the
                    # close never happened — but the old code committed anyway (fire-and-
                    # forget), permanently latching bias_booked=True and writing a
                    # cycle_outcomes row claiming +$7.5 booked that was never realized on
                    # the broker. bias_booked being wrongly True also blocked any further
                    # attempt on that side for the rest of the cycle.
                    book_key = (str(account), symbol, magic, bias)
                    pending_id = cls._pending_book_cmd.get(book_key)
                    if pending_id:
                        with cls._lock:
                            pcmd = cls._cmds.get(pending_id)
                        if pcmd is not None and pcmd.status == DONE \
                                and int((pcmd.result or {}).get("closed", 0)) > 0:
                            # CONFIRMED — commit now, not at enqueue time. bias_trail_done
                            # is the private one-shot (never reset while the cycle lives);
                            # bias_booked keeps its separate flatten-rest-suppression role.
                            cls._pending_book_cmd.pop(book_key, None)
                            cls.set_last_arm(account, symbol,
                                            **{**cyc, "magic": magic, "bias_booked": True,
                                               "bias_trail_done": True})
                            _emit_exit_audit({"account": str(account), "broker_symbol": symbol,
                                              "tf": tf, "magic": magic,
                                              "exit_reason": "bias_book_trail",
                                              "bias": bias, "peak": round(peak, 2),
                                              "side_pnl": round(side_pnl, 2),
                                              "book_frac": book_frac,
                                              "squeeze_ok": cyc.get("squeeze_ok"),
                                              "squeeze_rank": cyc.get("squeeze_rank")})
                            _emit_cycle_outcome(cyc, account=str(account), symbol=symbol,
                                                magic=magic, tf=tf,
                                                exit_reason="bias_book_trail", partial=True,
                                                peak=round(peak, 2),
                                                pnl_at_exit=round(float(pnl or 0.0), 2),
                                                book_frac=book_frac, buys=buys, sells=sells)
                            return "bias_book_trail"
                        elif pcmd is not None and pcmd.status == FAILED:
                            cls._pending_book_cmd.pop(book_key, None)  # clear, retry below
                        elif pcmd is not None:
                            return "bias_book_trail"  # still in flight — don't duplicate
                        else:
                            cls._pending_book_cmd.pop(book_key, None)  # lost track, retry below
                    # No confirmed or in-flight attempt — (re)try, throttled to 3s so a
                    # persistent rejection doesn't spam a fresh CLOSE_SIDE every poll.
                    if t - cls._last_book_attempt.get(book_key, 0.0) >= 3.0:
                        cls._last_book_attempt[book_key] = t
                        close_cmd = cls.enqueue(account, CLOSE_SIDE, symbol, magic=magic,
                                                side=bias, frac=book_frac,
                                                comment=f"FB|book|{bias}", now=t)
                        cls.enqueue(account, MOVE_BE, symbol, magic=magic, side=bias,
                                    comment=f"FB|be|{bias}", now=t)
                        cls._pending_book_cmd[book_key] = close_cmd.id
                    return "bias_book_trail"   # cycle continues (runner + hedge); no flatten

        reason: str | None = None
        detail: dict = {}

        # 0.9) per-cycle loss cap — the ONLY exit here that fires on a loss.
        # 8175f57 proved no per-leg TP can be profitable on a straddle and removed
        # every per-leg profit exit; it never replaced them with a loss exit, and
        # nothing on this branch ever did. Every other branch below fires on profit,
        # neutrality or a structural event, so without this a cycle that goes against
        # the ladder has no bounded outcome — it stays open until price returns or the
        # account cannot carry it. Checked FIRST so a losing cycle can never be held
        # open by a profit branch evaluating later. 0 disables.
        max_loss = float(grid_cfg.get("cycle_max_loss_usd", 0.0) or 0.0)
        if reason is None and max_loss > 0 and pnl is not None and float(pnl) <= -abs(max_loss):
            reason = "max_loss"
            detail = {"cap": round(-abs(max_loss), 2), "pnl": round(float(pnl), 2)}

        # 1) net-$ target — the whole basket (this magic = this TF cycle) is floating
        # ≥ its effective target. Base target is per-TF (cycle_net_target_by_tf, keyed by
        # the TF recovered from the magic) with cycle_net_target_usd as fallback. The
        # target DECAYS as the basket hedges toward delta-neutral: a hedged basket can't
        # realistically reach the directional target, so cycle_hedge_decay_pct shrinks it
        # (floored at cycle_min_target_usd, kept > 0 so a hedged cycle still has a green
        # exit). This is the primary profit-taker — bias_trail (above) rides clean
        # directional winners; full_hedge (below) cuts symmetric whipsaw at a loss.
        base_target = float(grid_cfg.get("cycle_net_target_usd", 0.0) or 0.0)
        by_tf = grid_cfg.get("cycle_net_target_by_tf") or {}
        if isinstance(by_tf, dict) and tf and tf in by_tf:
            base_target = float(by_tf.get(tf) or base_target)
        if reason is None and base_target > 0 and pnl is not None \
                and buys is not None and sells is not None:
            decay = float(grid_cfg.get("cycle_hedge_decay_pct", 0.0) or 0.0) / 100.0
            min_target = float(grid_cfg.get("cycle_min_target_usd", 0.0) or 0.0)
            hi = max(int(buys), int(sells))
            hedge_ratio = (min(int(buys), int(sells)) / hi) if hi > 0 else 0.0
            eff_target = max(min_target, base_target * (1.0 - decay * hedge_ratio))
            if float(pnl) >= eff_target:
                reason = "net_target"
                detail = {"eff_target": round(eff_target, 2), "base_target": base_target,
                          "hedge_ratio": round(hedge_ratio, 3)}

        # 2) flatten-rest — a filled leg closed while the opposite ladder still rests.
        # Skip if bias_trail just booked a fraction — partial close is expected, not a signal.
        # Once skipped once, reset max_seen to current positions and clear bias_booked so
        # any further unexpected drop still triggers the flatten.
        bias_booked = bool(cyc.get("bias_booked", False))
        if bias_booked and positions > 0:
            cls.set_last_arm(account, symbol, **{**cyc,
                             "max_seen": positions, "bias_booked": False, "magic": magic})
        if reason is None and 0 < positions < max_seen and pendings > 0 and not bias_booked:
            tol = max(mid * 1e-4, 1e-6) if mid > 0 else 1e-6
            confirms = (tp_up > 0 and mid >= tp_up - tol) or (tp_down > 0 and 0 < mid <= tp_down + tol)
            # leg_tp closes the WHOLE cycle (both sides + pendings) when a TP-side leg
            # books — but ONLY if the basket is net-POSITIVE. A TP tag while net-negative
            # (deep hedge on the other side) is NOT a win; don't realize that loss here —
            # let net_target / bias_trail / full_hedge own those exits.
            net_ok = (pnl is None) or (float(pnl) > 0)
            if confirms and net_ok:
                reason = "leg_tp"
            elif not confirms:
                reason = "leg_closed_other"

        # 3) full-hedge backstop (delta-neutral → cut to free margin; realizes a loss)
        if reason is None and close_on_full_hedge and n > 0 \
                and buys is not None and sells is not None:
            if min(int(buys), int(sells)) >= n and (int(buys) + int(sells)) >= 2 * n:
                reason = "full_hedge"

        if reason is None:
            return None

        cls.enqueue(account, CLOSE_ALL, symbol, comment=f"FB|flatten|{reason}", magic=magic, now=t)
        cls.set_last_arm(account, symbol, **{**cyc, "magic": magic, "flatten_ts": t})
        _emit_exit_audit({"account": str(account), "broker_symbol": symbol, "tf": tf, "magic": magic,
                          "exit_reason": reason, "armed_tf": cyc.get("armed_tf", ""),
                          "positions": positions, "pendings": pendings,
                          "buys": buys, "sells": sells, "pnl": pnl, "venue_mid": mid,
                          "tp_up": tp_up, "tp_down": tp_down,
                          "squeeze_ok": cyc.get("squeeze_ok"), "squeeze_rank": cyc.get("squeeze_rank"),
                          **detail})
        # durable, restart-proof per-cycle record (arm context + outcome joined)
        _emit_cycle_outcome(cyc, account=str(account), symbol=symbol, magic=magic, tf=tf,
                            exit_reason=reason, pnl_at_exit=pnl, venue_mid=mid,
                            positions=positions, pendings=pendings,
                            buys=buys, sells=sells, **detail)
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
        try:
            persist_emit(account, symbol, int(magic), fulcrum)
        except Exception:
            pass

    @classmethod
    def clear_emit(cls, account: str, symbol: str, magic: int = 0) -> None:
        """Episode ended (no arm this bar) → next arm is a fresh touch."""
        cls._last_emit.pop((str(account), symbol, int(magic)), None)
        try:
            persist_emit(account, symbol, int(magic), None)
        except Exception:
            pass

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
                now: float | None = None) -> Command:
        cmd = Command(
            id=uuid.uuid4().hex[:12], account=str(account), type=type, symbol=symbol,
            order_type=order_type, price=round(float(price), 5), lot=round(float(lot), 2),
            sl=round(float(sl), 5), tp=round(float(tp), 5), comment=comment,
            magic=int(magic), side=side, frac=float(frac),
            ts_created=now if now is not None else time.time(),
        )
        with cls._lock:
            cls._cmds[cmd.id] = cmd
            cls._seq.append(cmd.id)
        cls._audit("enqueue", cmd)
        return cmd

    @staticmethod
    def _pick_order_type(side: str, price: float, quote: dict | None) -> str:
        """stop vs limit, decided off the live quote so a leg that landed on the wrong
        side of the market (or inside the broker's freeze band as a stop) goes out as a
        limit instead. margin = broker stops-level distance (set_quote's stops_dist);
        falls back to plain stop/geometry when no quote is cached yet."""
        q = quote or {}
        margin = float(q.get("stops_dist") or 0.0)
        if side == "buy":
            ask = float(q.get("ask") or 0.0)
            if ask <= 0:
                return "buy_stop"
            return "buy_stop" if price > ask + margin else "buy_limit"
        else:
            bid = float(q.get("bid") or 0.0)
            if bid <= 0:
                return "sell_stop"
            return "sell_stop" if price < bid - margin else "sell_limit"

    @classmethod
    def retry_failed_pendings(cls, account: str, broker_symbol: str, magic: int,
                              max_attempts: int = 5, min_interval_s: float = 3.0) -> int:
        """Re-submit PLACE_PENDING legs that failed (broker freeze-band reject, wrong-side
        geometry from a stale plan price, transient reject exhausted on the EA side) for an
        ACTIVE cycle. Re-picks stop vs limit off the current quote each attempt — a leg the
        market has since crossed goes out as the opposite order kind instead of retrying the
        same rejected geometry. Bounded per leg (by magic+comment) so a leg that's genuinely
        un-placeable (e.g. still inside the freeze band on both sides) doesn't retry forever.

        Called from the poll path (every EA poll, ~1s) so a stuck leg is caught fast, but
        throttled to min_interval_s per (account,symbol,magic) — retrying every single poll
        would spam the same rejected geometry on quotes that haven't moved. Also called once
        per refresh-tps pass (≈1/min) as a backstop for a poll-path gap."""
        key_t = (str(account), broker_symbol, magic)
        now = time.time()
        last = cls._last_retry_at.get(key_t, 0.0)
        if now - last < min_interval_s:
            return 0
        arm = cls.get_last_arm(account, broker_symbol, magic=magic)
        if not arm or not arm.get("active"):
            return 0
        quote = cls.get_quote(account, broker_symbol)
        if not quote:
            return 0
        cls._last_retry_at[key_t] = now
        with cls._lock:
            candidates = [c for c in cls._cmds.values()
                          if c.account == str(account) and c.symbol == broker_symbol
                          and c.magic == magic and c.type == PLACE_PENDING
                          and c.status == FAILED and c.id not in cls._retried_cmd_ids]
        retried = 0
        for c in candidates:
            key = (account, broker_symbol, magic, c.comment)
            n = cls._retry_counts.get(key, 0)
            with cls._lock:
                cls._retried_cmd_ids.add(c.id)
            if n >= max_attempts:
                continue
            cls._retry_counts[key] = n + 1
            side = "buy" if c.order_type.startswith("buy") else "sell"
            new_type = cls._pick_order_type(side, c.price, quote)
            cls.enqueue(account, PLACE_PENDING, broker_symbol, order_type=new_type,
                       price=c.price, lot=c.lot, sl=c.sl, tp=c.tp,
                       comment=c.comment, magic=magic)
            retried += 1
        return retried

    @classmethod
    def retry_failed_closes(cls, account: str, broker_symbol: str, magic: int,
                            max_attempts: int = 10, min_interval_s: float = 3.0) -> int:
        """Re-submit CLOSE_ALL/CLOSE_SIDE commands that failed to close every ticket they
        targeted. Observed live 2026-08-20 on magic 770012: a flatten-rest CLOSE_ALL
        returned {"closed": 1, "error": "close fail #529568081"} — one ticket closed, one
        didn't — and nothing ever re-attempted it. The cycle was already marked inactive by
        that point, so (unlike retry_failed_pendings/MOVE_BE) this deliberately does NOT
        gate on arm.active — closing out a position must finish even after the cycle's
        bookkeeping considers it done. Re-sends the identical command (no price/type to
        recompute for a close); the EA's close path is idempotent for a ticket that's
        already gone (nothing left to close ≠ a failure), so a retry against a
        since-self-resolved position is harmless."""
        key_t = (str(account), broker_symbol, magic)
        now = time.time()
        if now - cls._last_close_retry_at.get(key_t, 0.0) < min_interval_s:
            return 0
        cls._last_close_retry_at[key_t] = now
        with cls._lock:
            candidates = [c for c in cls._cmds.values()
                          if c.account == str(account) and c.symbol == broker_symbol
                          and c.magic == magic and c.type in (CLOSE_ALL, CLOSE_SIDE)
                          and c.status == FAILED and c.id not in cls._retried_cmd_ids]
        retried = 0
        for c in candidates:
            key = (account, broker_symbol, magic, c.type, c.side)
            n = cls._close_retry_counts.get(key, 0)
            with cls._lock:
                cls._retried_cmd_ids.add(c.id)
            if n >= max_attempts:
                continue
            cls._close_retry_counts[key] = n + 1
            cls.enqueue(account, c.type, broker_symbol, side=c.side, frac=c.frac,
                       comment=c.comment, magic=magic)
            retried += 1
        return retried

    @staticmethod
    def _leg_sl(entry: float, side: str, disaster_usd: float) -> float:
        """Disaster stop for ONE leg, measured from that leg's OWN entry.

        Not a shared per-side level: a shared level puts the outermost leg a
        full ladder-span from its stop while the innermost sits on top of it.
        Per-leg keeps the risk of every leg identical, which is what makes the
        number tunable from the `trough` field later.

        This is a DISASTER stop, not a strategy stop — it sits wider than every
        server-side exit and exists for the case where the server, the bridge or
        the terminal is gone. 0 disables it.
        """
        if disaster_usd <= 0 or entry <= 0:
            return 0.0
        sl = entry - disaster_usd if side == "buy" else entry + disaster_usd
        return round(sl, 4) if sl > 0 else 0.0

    @classmethod
    def enqueue_grid_plan(cls, account: str, broker_symbol: str, plan, *,
                          close_first: bool = True, clear_kind: str = "flatten",
                          magic: int = 0, leg_tp: bool = True,
                          disaster_sl_usd: float = 0.0) -> list[Command]:
        """Translate a rebased neutral GridPlan into PLACE_PENDING commands.
        buy_legs → buy_stop, sell_legs → sell_stop, shared per-side TP, no SL (v1).
        Optionally prepend a clear command: clear_kind="flatten" → CLOSE_ALL (close
        positions + cancel pendings); "cancel" → CANCEL_PENDINGS (cancel stale pendings
        only, never touch a live position — the safe re-arm path).

        leg_tp=False places legs WITHOUT a per-order TP — they never self-close, so the
        winning side can't book while the losing side dangles (a per-leg TP hit ≠ a
        net-positive cycle). The basket net-target exit (monitor_cycle) then owns ALL
        profit-taking and only closes when the whole cycle is net ≥ target."""
        # Per-order tag = the source level + the cycle's TF, so each grid's legs are
        # identifiable in the MT5 comment: FB|poc|15m|b1, FB|vah|5m|s2, FB|hvn|1m|b3 …
        # (vp_level_touch → its level_type; hvn_inside_touch → "hvn"; else the trigger
        # kind). TF is recovered from the magic (magic % 10). All legs of one grid share
        # tag+TF; b/s + index distinguish legs. (MT5 comment cap ~31 chars — fits.)
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
        tf_tag = tf_from_magic(magic) or "?"
        # squeeze_gate always checks the cycle's OWN tf (grid_planner._resolve builds the
        # plan for the magic's tf, and squeeze_gate(symbol, tf, ...) is called with that
        # same tf) — so "which TF was squeeze checked on" is always tf_tag. Tag it in the
        # comment only when this arm actually passed the gate (plan.squeeze_ok), so the
        # order itself carries proof of the coil it armed on.
        sq_tag = f"|sq{tf_tag}" if bool(getattr(plan, "squeeze_ok", False)) else ""

        out: list[Command] = []
        if close_first:
            clear_cmd = CLOSE_ALL if clear_kind == "flatten" else CANCEL_PENDINGS
            # scope the clear to THIS cycle's magic so a re-arm only cancels its own
            # TF/strategy pendings, never a sibling TF cycle's live orders.
            out.append(cls.enqueue(account, clear_cmd, broker_symbol, magic=magic))
        buy_tp  = getattr(plan, "buy_tp",  0.0) if leg_tp else 0.0
        sell_tp = getattr(plan, "sell_tp", 0.0) if leg_tp else 0.0
        # A structural SL from the planner (displacement's candle extreme) always
        # wins where one exists. Everything else — hvn_inside_touch, hvn_edge —
        # has always gone out with sl=0.0, which is the whole reason a cycle that
        # goes against the ladder has no bounded outcome. Fall back to a per-leg
        # disaster stop so every leg carries one.
        buy_sl  = getattr(plan, "buy_sl",  0.0)
        sell_sl = getattr(plan, "sell_sl", 0.0)
        disaster = float(disaster_sl_usd or 0.0)
        quote = cls.get_quote(account, broker_symbol)
        for i, leg in enumerate(getattr(plan, "buy_legs", []) or []):
            leg_sl = buy_sl or cls._leg_sl(leg.price, "buy", disaster)
            otype = cls._pick_order_type("buy", leg.price, quote)
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol, order_type=otype,
                price=leg.price, lot=leg.lot, sl=leg_sl, tp=buy_tp,
                comment=f"FB|{tag}|{tf_tag}{sq_tag}|b{i + 1}", magic=magic))
        for i, leg in enumerate(getattr(plan, "sell_legs", []) or []):
            leg_sl = sell_sl or cls._leg_sl(leg.price, "sell", disaster)
            otype = cls._pick_order_type("sell", leg.price, quote)
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol, order_type=otype,
                price=leg.price, lot=leg.lot, sl=leg_sl, tp=sell_tp,
                comment=f"FB|{tag}|{tf_tag}{sq_tag}|s{i + 1}", magic=magic))
        return out

    @classmethod
    def enqueue_modify_pending(cls, account: str, broker_symbol: str, magic: int,
                               price_delta: float, new_tp: float = 0.0,
                               side: str = "") -> "Command":
        """Shift all pending stop orders for `magic` by `price_delta` and update TP.

        price_delta: signed pts to add to each pending's current price (+ = up, - = down).
        new_tp: replacement TP for all legs on that side; 0 = leave unchanged.
        side: "buy", "sell", or "" (both).
        """
        return cls.enqueue(account, MODIFY_PENDING, broker_symbol,
                           magic=magic, price=price_delta, tp=new_tp, side=side)

    @classmethod
    def enqueue_modify_position(cls, account: str, broker_symbol: str, magic: int,
                                new_tp: float = 0.0, new_sl: float = 0.0, side: str = "",
                                comment: str = "") -> "Command":
        """Refresh TP and/or SL on FILLED positions for `magic` on `side`. 0 = leave
        that field unchanged (the EA keeps the existing value when it sees 0).

        Complements enqueue_modify_pending: pending legs track the HVN via MODIFY_PENDING,
        filled legs track it via this. side: "buy", "sell", or "" (both).
        """
        return cls.enqueue(account, MODIFY_POSITION, broker_symbol,
                           magic=magic, tp=new_tp, sl=new_sl, side=side, comment=comment)

    @classmethod
    def ratchet_candle_sl(cls, account: str, broker_symbol: str, magic: int,
                          side: str, candidate_sl: float) -> bool:
        """Ratchet-only check for the expansion candle-close SL trail: buy SL only ever
        moves UP (candidate must exceed the last one we set), sell SL only ever moves
        DOWN. Returns True (and records candidate_sl as the new high-water mark) iff the
        caller should actually enqueue the modify; False means candidate_sl is no
        improvement (or invalid) and nothing should be sent. Distinct from MOVE_BE/other
        SL paths — this is its own key so it doesn't fight the breakeven/disaster-stop
        mechanisms, all of which the EA resolves by simply taking whatever SL arrives last."""
        if candidate_sl <= 0:
            return False
        key = (str(account), broker_symbol, magic, side)
        prev = cls._last_candle_sl.get(key)
        if prev is not None:
            if side == "buy" and candidate_sl <= prev:
                return False
            if side == "sell" and candidate_sl >= prev:
                return False
        cls._last_candle_sl[key] = candidate_sl
        return True

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
    def load_persisted_state(cls) -> dict:
        """Restore _last_arm and _last_emit from disk after a Flask restart.

        Call once at app startup (before the first poll) so live cycles are
        not orphaned. Returns counts of records restored for logging.
        """
        import logging
        log = logging.getLogger(__name__)
        try:
            arms, emits = _load_arm_state()
        except Exception as e:
            log.warning(f"[exec_bridge] load_persisted_state failed: {e}")
            return {"arms": 0, "emits": 0, "error": str(e)}
        # Refuse to re-adopt a record that cannot describe a real cycle. A restart
        # re-evaluates everything it adopts, so a dead record is replayed through
        # monitor_cycle and writes a FRESH cycle_outcomes row every time — on
        # 2026-08-20 that made 64 of 67 rows (96%) phantom repeats of five stale
        # signatures, one written 24 times across seven restarts, and every
        # conclusion drawn from that log was wrong.
        #
        # A live cycle always has a magic, an arm timestamp and the trigger that
        # armed it. Missing any of those means the record predates a schema, lost
        # its key to a magic-0 collapse, or was never a cycle at all.
        skipped = []
        with cls._lock:
            for (account, broker_symbol, magic), state in arms.items():
                if not magic or not float(state.get("ts") or 0) or not state.get("trigger_kind"):
                    if state.get("active"):
                        skipped.append((magic, state.get("ts"), state.get("trigger_kind")))
                    continue
                cls._last_arm[(account, broker_symbol, magic)] = state
            for (account, symbol, magic), fulcrum in emits.items():
                if fulcrum is not None:
                    cls._last_emit[(account, symbol, magic)] = fulcrum
        if skipped:
            log.warning("[exec_bridge] skipped %d unusable arm record(s) — no magic/ts/"
                        "trigger_kind: %s", len(skipped), skipped[:5])
        active = sum(1 for s in arms.values() if s.get("active"))
        log.info(f"[exec_bridge] restored {len(arms)} arm records ({active} active), "
                 f"{len(emits)} emit records from disk")
        return {"arms": len(arms), "emits": len(emits), "active": active}

    @classmethod
    def reconcile_from_poll(cls, account: str, broker_symbol: str,
                            magics: list[dict]) -> list[int]:
        """Called on each EA poll. For any magic with live positions that has no
        _last_arm entry (orphaned after a Flask restart), create a minimal stub so
        monitor_cycle can track it and position_open gate fires correctly.

        Returns list of magics that were stub-created.
        """
        stubbed = []
        for m in magics or []:
            try:
                mg = int(m.get("magic", 0))
                if not mg:
                    continue
                positions = int(m.get("buys", 0)) + int(m.get("sells", 0))
                if positions <= 0:
                    continue
                existing = cls.get_last_arm(account, broker_symbol, magic=mg)
                if existing and existing.get("active"):
                    continue
                if existing:
                    # Real arm data, just marked inactive — reactivate in place rather than
                    # blank-stub over good geometry (fulcrum/trigger_kind/TPs). This is the
                    # reap-race leftover: a magic reaped in the same tick it was armed (see
                    # the absent-magic reap grace fix) has a genuine record but active=False,
                    # and previously `if existing: continue` left it permanently orphaned even
                    # with live, profitable positions open — magic 770013 sat untracked with
                    # +$597.75 floating from 20:00:08 until this was found, 2026-08-20.
                    existing = dict(existing)
                    existing.pop("magic", None)
                    existing["active"] = True
                    cls.set_last_arm(account, broker_symbol, magic=mg, **existing)
                    stubbed.append(mg)
                    import logging
                    logging.getLogger(__name__).warning(
                        f"[exec_bridge] reconcile: reactivated inactive-but-live magic {mg} "
                        f"(positions={positions}) for {account}/{broker_symbol}")
                    continue
                # Orphaned live position — create a stub that marks cycle as active
                # so monitor_cycle tracks P&L and position_open gate fires.
                tf_stub = tf_from_magic(mg) or "1m"
                stub = {
                    "active": True, "armed_tf": tf_stub, "tf": tf_stub,
                    "fulcrum": 0.0, "trigger_kind": "recovered",
                    "tp_up": 0.0, "tp_down": 0.0,
                    "net_target_usd": 0.0, "n_per_side": 0, "step": 0.0,
                    "buy_n": int(m.get("buys", 0)), "sell_n": int(m.get("sells", 0)),
                    "bias_peak": 0.0, "bias_booked": False,
                    # recovered cycle: suppress full-fill BE (we don't know its real
                    # buy_n/sell_n target, and we don't want to spam MOVE_BE on a stub).
                    "be_done_buy": True, "be_done_sell": True,
                    "max_pos_seen": positions, "pend_seen": 0,
                    "flatten_ts": 0.0, "node_low": 0.0, "node_high": 0.0,
                    "squeeze_ok": False, "squeeze_rank": 1.0,
                    "ts": __import__("time").time(), "recovered": True,
                }
                cls.set_last_arm(account, broker_symbol, magic=mg, **stub)
                stubbed.append(mg)
                import logging
                logging.getLogger(__name__).warning(
                    f"[exec_bridge] reconcile: stubbed orphaned magic {mg} "
                    f"({positions} positions) for {account}/{broker_symbol}")
            except Exception:
                pass
        return stubbed

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
