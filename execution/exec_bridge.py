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
_LAST_ACCOUNT_FILE = _ROOT / "data" / "last_account.txt"   # detects an account switch across restarts

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
# squeeze·1h = 770024. Each strategy occupies its own decade (strat·10 .. strat·10+9);
# the tf is recoverable as magic % 10, so the server attributes each EA-reported position
# pool to the TF cycle that owns it (enables parallel per-TF cycles). NOTE: strat_code can
# exceed 9 (hvn_displacement=10 → 7701xx), so the owned upper bound is derived from the max
# strat code below — NOT a hardcoded +100 (that silently dropped displacement cycles).
MAGIC_BASE = 770000
_STRAT_CODE = {
    "hvn_inside_touch": 1, "squeeze": 2, "vp_level_touch": 3, "imbalance": 4,
    "hvn_edge": 5, "anchor": 6, "va": 7, "cvd_div": 8, "hvn_displacement": 10,
    # Setup-level pseudo-kind: the "vp_levels" parallel setup (va OR vp_level_touch) arms
    # under ONE dedicated magic so the trade report reads it as a single setup. The audit
    # still records the real detector (trigger_kind) that fired.
    "vp_levels": 9,
    # BB-expansion touch: post-squeeze band-touch with footprint absorption confirm.
    # Must be registered here so it gets its own isolated magic decade; otherwise unknown
    # trigger_kinds all fall to strat_code=0 and share the same cycle-state slot.
    "bb_expansion_touch": 11,
    "candle_sweep": 12,
    "lvn_edge_touch": 13,
}
_TF_CODE = {"1m": 1, "5m": 2, "15m": 3, "1h": 4}
_CODE_TF = {v: k for k, v in _TF_CODE.items()}
# Upper bound of our owned magic range, derived from the highest strat decade (+9 for tf,
# +1 exclusive). With hvn_displacement=10 this is 770110 — so 7701xx displacement magics
# are recognized instead of being silently treated as "not ours" by a hardcoded +100.
_MAGIC_MAX = MAGIC_BASE + (max(_STRAT_CODE.values()) + 1) * 10


def magic_for(trigger_kind: str, tf: str) -> int:
    """Composite magic identifying (strategy, TF). Unknown kind→0, unknown tf→0."""
    return MAGIC_BASE + _STRAT_CODE.get(trigger_kind, 0) * 10 + _TF_CODE.get(tf, 0)


def tf_from_magic(magic: int) -> str:
    """Recover the TF a magic belongs to (magic % 10). '' if not one of ours."""
    if magic < MAGIC_BASE or magic >= _MAGIC_MAX:
        return ""
    return _CODE_TF.get(int(magic) % 10, "")


_MODIFY_COOLDOWN_CACHE: float | None = None


def _modify_cooldown_s() -> float:
    """Min seconds between MODIFY enqueues per (cycle, side, kind) — broker order-rate cap.
    Read once from grid_levels.modify_cooldown_s (default 0 = no throttle)."""
    global _MODIFY_COOLDOWN_CACHE
    if _MODIFY_COOLDOWN_CACHE is None:
        v = 0.0
        try:
            import yaml as _yaml
            import pathlib as _pl
            _cfg = _yaml.safe_load(
                (_pl.Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text()
            ) or {}
            v = float((_cfg.get("grid_levels") or {}).get("modify_cooldown_s", 0.0) or 0.0)
        except Exception:
            v = 0.0
        _MODIFY_COOLDOWN_CACHE = max(0.0, v)
    return _MODIFY_COOLDOWN_CACHE


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
            # sl=0 → EA keeps existing; sl>0 → EA sets it. tp=0 → EA keeps existing.
            d.update(sl=self.sl, tp=self.tp, side=self.side, comment=self.comment)
        return d


class ExecBridge:
    """Process-wide singleton-ish command queue."""

    _lock = threading.Lock()
    _cmds: dict[str, Command] = {}          # id → Command
    _seq: list[str] = []                    # insertion order (FIFO dispatch)
    _quotes: dict[tuple, dict] = {}         # (account, broker_symbol) → {bid,ask,mid,ts}
    _last_emit: dict[tuple, float] = {}     # (account, symbol, tf) → last emitted fulcrum
    _last_arm: dict[tuple, dict] = {}       # (account, broker_symbol) → last armed grid metadata
    _last_modify_ts: dict[tuple, float] = {}  # (acct, sym, magic, side, kind) → last MODIFY enqueue ts (broker-rate throttle)
    _open: dict[tuple, dict] = {}           # (account, broker_symbol) → {positions, pendings, ts}
    _ict_overlay: dict[str, dict] = {}      # analysis_symbol → ict_fvg setup (analysis frame)
    # daily P&L target tracking — keyed by account
    _daily_start_balance: dict[str, float] = {}   # account → balance at first poll of the day
    _daily_start_date: dict[str, str] = {}         # account → YYYY-MM-DD of that first poll
    _daily_target_hit: dict[str, bool] = {}        # account → True once daily target reached
    # intrabar touch-arm tick-reversal state — keyed by (account, broker_symbol, tf)
    # tracks an edge tap awaiting a reversal-back-inside confirm (touch-arm path)
    _touch_state: dict[tuple, dict] = {}           # → {edge, side, tapped_px, ts}
    _last_seen_account: str | None = None   # cached from _LAST_ACCOUNT_FILE on first poll

    # ── account-switch cleanup ──────────────────────────────────────────────────
    @classmethod
    def check_account_switch(cls, account: str) -> int:
        """Detect the EA polling a DIFFERENT account than last seen (persisted across
        restarts in _LAST_ACCOUNT_FILE) and retire every stale arm left over from the
        old account — otherwise those ghost cycles keep re-arming/reaping forever
        (they can never fill: the EA only executes for the account it's logged into).

        Returns the number of arms retired (0 on no switch / first-ever poll)."""
        account = str(account)
        if cls._last_seen_account is None:
            try:
                cls._last_seen_account = _LAST_ACCOUNT_FILE.read_text().strip() or None
            except Exception:
                cls._last_seen_account = None
        retired = 0
        if cls._last_seen_account and cls._last_seen_account != account:
            # sweep EVERY other account, not just the immediately-previous one —
            # arms can accumulate across several past account switches.
            retired = cls._retire_other_accounts(account)
            import logging
            logging.getLogger(__name__).info(
                f"[exec] account switch {cls._last_seen_account} → {account}: "
                f"retired {retired} stale arm(s) across other accounts")
        if cls._last_seen_account != account:
            cls._last_seen_account = account
            try:
                _LAST_ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
                _LAST_ACCOUNT_FILE.write_text(account)
            except Exception:
                pass
        return retired

    @classmethod
    def _retire_other_accounts(cls, current_account: str) -> int:
        """Mark every active arm NOT belonging to `current_account` inactive and clear
        its emit cooldown, so it stops being polled/reaped. Persists via the normal
        set_last_arm/clear_emit paths (each takes cls._lock itself — collect keys
        under the lock, then release before calling them, since _lock is non-reentrant)."""
        with cls._lock:
            keys = [k for k, m in cls._last_arm.items()
                    if k[0] != current_account and m.get("active")]
        for (acc, sym, magic) in keys:
            cyc = cls.get_last_arm(acc, sym, magic=magic) or {}
            # magic= must come from the OUTER key, passed explicitly — a stale/legacy
            # arm body can carry its own wrong/absent "magic" field (seen: 0 on old
            # "recovered" stubs), and spreading that over the explicit kwarg would
            # collide anyway. Strip it from the spread so the outer key always wins
            # (see project_arm_magic_key_bug memory).
            cyc.pop("magic", None)
            cls.set_last_arm(acc, sym, magic=magic, **{**cyc, "active": False})
            cls.clear_emit(acc, sym, magic=magic)
        return len(keys)

    # ── daily P&L target ────────────────────────────────────────────────────────
    @classmethod
    def update_account_balance(cls, account: str, balance: float, equity: float,
                               target_pct: float) -> dict:
        """Track daily equity and return {hit, pnl_pct, start_balance} each poll.

        Resets at midnight (UTC date change). Once hit=True it stays True for the
        day — caller should enqueue CLOSE_ALL and block new arms.
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
                return {"hit": False, "pnl_pct": 0.0, "start_balance": start}

            pnl_pct = (equity - start) / start * 100.0

            already_hit = cls._daily_target_hit.get(account, False)
            hit = already_hit or (target_pct > 0 and pnl_pct >= target_pct)
            if hit and not already_hit:
                cls._daily_target_hit[account] = True

        return {"hit": hit, "pnl_pct": round(pnl_pct, 4),
                "start_balance": round(start, 2), "equity": round(equity, 2)}

    @classmethod
    def daily_target_hit(cls, account: str) -> bool:
        return cls._daily_target_hit.get(str(account), False)

    # ── intrabar touch-arm + tick-reversal confirm ───────────────────────────────
    @classmethod
    def touch_arm_check(cls, account: str, broker_symbol: str, tf: str,
                        live_price: float, edge: float, side: str,
                        confirm_ticks: float, now: float | None = None,
                        kind: str = "hvn_inside_touch") -> bool:
        """Tick-reversal state machine for intrabar touch-arming. Call each poll with
        the live price and the edge it's tapping (from touch_arm_trigger). Returns True
        ONCE when price has tapped the edge AND then reverted back INSIDE the node by
        `confirm_ticks` (a mini-rejection) — the intrabar twin of "close back inside".

        State per (account, symbol, tf, kind): record the tap, then on a later poll
        confirm if price moved back inside. `kind` keeps HVN and LVN (or any future
        touch-arm trigger) tap-state isolated even when both run on the same TF —
        without it a tap on one would corrupt/consume the other's pending confirm.
        A breakout (price keeps going through the edge) never reverts → never confirms
        → no arm. State clears on confirm or when price leaves the buffer entirely
        (tap abandoned)."""
        t = now if now is not None else time.time()
        key = (str(account), broker_symbol, tf, kind)
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
                                     "tapped_px": live_price, "ts": t, "trig": None}
            return False

    @classmethod
    def clear_touch_state(cls, account: str, broker_symbol: str, tf: str,
                          kind: str = "hvn_inside_touch") -> None:
        """Drop a pending tap (price left the buffer without confirming)."""
        with cls._lock:
            cls._touch_state.pop((str(account), broker_symbol, tf, kind), None)

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
                 buys: int = 0, sells: int = 0,
                 buy_pendings: int = 0, sell_pendings: int = 0) -> None:
        # Cycle state is keyed by MAGIC (strategy×TF), not TF alone — so multiple setups
        # (hvn / squeeze / vp …) run as INDEPENDENT parallel cycles on the same symbol+TF.
        # buys/sells retained for the cycle_live(2B,3S,…) skip-reason breakdown.
        # buy_pendings/sell_pendings (per-side split, EA-reported) let a side-only
        # re-arm detect that ONE side is fully flat (no positions AND no resting
        # pendings on that side) while the other side is still live.
        with cls._lock:
            cls._open[(str(account), symbol, int(magic))] = {
                "positions": int(positions), "pendings": int(pendings), "tf": tf,
                "buys": int(buys), "sells": int(sells),
                "buy_pendings": int(buy_pendings), "sell_pendings": int(sell_pendings),
                "ts": now if now is not None else time.time(),
            }

    @classmethod
    def get_open(cls, account: str, symbol: str, tf: str = "", magic: int = 0) -> dict:
        with cls._lock:
            return dict(cls._open.get((str(account), symbol, int(magic)),
                                      {"positions": 0, "pendings": 0, "buys": 0, "sells": 0,
                                       "buy_pendings": 0, "sell_pendings": 0}))

    # ── last-armed grid (ground truth for chart drawing + diagnostics) ─────────
    @classmethod
    def set_last_arm(cls, account: str, broker_symbol: str, tf: str = "", magic: int = 0,
                     **meta) -> None:
        # Keyed by magic. The internal monitor calls re-pass the stored cyc via **cyc, so
        # `magic` arrives either as the named arg or inside meta — accept both.
        key_magic = int(magic or meta.get("magic", 0) or 0)
        state = dict(meta, tf=tf, magic=key_magic)
        with cls._lock:
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
                      buy_pnl: float | None = None, sell_pnl: float | None = None,
                      buy_lots: float | None = None, sell_lots: float | None = None) -> str | None:
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
        # A cycle restored from disk (load_persisted_state) has NO embedded "magic"
        # field (arm_state_store.load strips it — it's redundant with the outer key).
        # Every set_last_arm(**cyc) call below MUST pass magic=magic explicitly and
        # never trust cyc's own (possibly-absent) copy — spreading a magic-less cyc
        # without doing so silently collapses the write to magic=0, corrupting a
        # DIFFERENT cycle's slot instead of updating this one (bias_peak, deferred
        # SL, flatten state, etc. all silently vanish). Strip it here once so every
        # downstream **cyc spread is safe by construction.
        cyc.pop("magic", None)

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
                cls.set_last_arm(account, symbol, magic=magic, **{**cyc, "active": False, "flatten_ts": 0.0})
                cls.clear_emit(account, symbol, magic=magic)
            elif (t - fts) > _FLATTEN_GRACE_S:
                # close demonstrably didn't land (past the queue's reclaim window) → re-issue once
                cls.enqueue(account, CLOSE_ALL, symbol, comment="FB|flatten|retry", magic=magic, now=t)
                cls.set_last_arm(account, symbol, magic=magic, **{**cyc, "flatten_ts": t})
            return None

        # track high-water of open positions (basis for flatten-rest) + resting pendings
        # (so a never-filled cycle can be retired); both reset per arm.
        max_seen = int(cyc.get("max_pos_seen") or 0)
        pend_seen = int(cyc.get("pend_seen") or 0)
        _cyc_hw_dirty = False
        if positions > max_seen or pendings > pend_seen:
            max_seen = max(max_seen, positions)
            pend_seen = max(pend_seen, pendings)
            cyc["max_pos_seen"] = max_seen
            cyc["pend_seen"] = pend_seen
            _cyc_hw_dirty = True
        # per-side high-water: needed to detect one-side TP (side drops from >0 to 0)
        if buys is not None and sells is not None:
            _mb = int(cyc.get("max_buys_seen") or 0)
            _ms = int(cyc.get("max_sells_seen") or 0)
            if int(buys) > _mb:
                cyc["max_buys_seen"] = int(buys)
                _cyc_hw_dirty = True
            if int(sells) > _ms:
                cyc["max_sells_seen"] = int(sells)
                _cyc_hw_dirty = True
        if _cyc_hw_dirty:
            cls.set_last_arm(account, symbol, magic=magic, **cyc)

        # FREEZE the VP/HVN structure on FIRST leg fill. Until a position opens the cycle
        # tracks live VP (cheap re-anchor of resting pendings); once committed, we manage
        # against the structure that JUSTIFIED the entry — not a morphing live VP (which
        # caused false fade-flattens). Stores session HVN zones (analysis frame) + step into
        # the arm under `frozen_zones`; the refresh paths use these instead of live VP, and
        # fade-flatten is skipped entirely for a frozen cycle. Fires once (vp_frozen guard).
        if positions > 0 and not cyc.get("vp_frozen"):
            try:
                from pipeline.state_store import store as _st
                _atf = cyc.get("armed_tf") or cyc.get("tf") or ""
                # analysis symbol: monitor gets broker symbol; map back via settings
                _smap = (settings.get("execution") or {}).get("symbol_map", {}) if isinstance(settings, dict) else {}
                _b2a = {v: k for k, v in _smap.items()}
                _asym = _b2a.get(symbol, symbol)
                _zbars = _st().recent(_asym, _atf, 120) if _atf else []
                if cyc.get("trigger_kind") == "lvn_edge_touch":
                    from execution.zone_triggers import _session_lvn_zones
                    _fz, _ = _session_lvn_zones(_asym, _atf, _zbars) if _atf else ([], "")
                else:
                    from execution.zone_triggers import _session_hvn_zones
                    _fz, _ = _session_hvn_zones(_asym, _atf, _zbars) if _atf else ([], "")
                if _fz:
                    cyc["frozen_zones"] = [[round(lo, 5), round(hi, 5)] for lo, hi in _fz]
                cyc["vp_frozen"] = True
                cls.set_last_arm(account, symbol, magic=magic, **cyc)
            except Exception:
                pass  # never break the monitor on a snapshot failure

        # Deferred structural SL toggle (config): when False, a side that commits >50%
        # is NOT given a fixed node-edge/fulcrum SL — net_target/full_hedge own the exit
        # (grid-recovery thesis). Profit-trailing SL (candle_sweep/hvn_edge cont) unaffected.
        _defer_sl = bool(((settings.get("grid_levels") or {}) if isinstance(settings, dict)
                          else {}).get("defer_sl_on_half_fill", True))

        # Deferred SL arming for hvn_inside_touch / lvn_edge_touch: once >50% of a side's
        # legs fill, set the structural SL (node_low for buys, node_high for sells) on all
        # open positions on that side. Guard with sl_armed_{side} so it fires exactly once
        # per cycle arm. tp guard: EA requires non-zero tp on MODIFY_POSITION; skip if no
        # TP is known yet. Generic risk management, not thesis-specific — both kinds
        # populate node_low/node_high identically.
        if (_defer_sl and positions > 0
                and cyc.get("trigger_kind") in ("hvn_inside_touch", "lvn_edge_touch")
                and buys is not None and sells is not None):
            _node_lo = float(cyc.get("node_low") or 0.0)
            _node_hi = float(cyc.get("node_high") or 0.0)
            _buy_n   = int(cyc.get("buy_n") or 0)
            _sell_n  = int(cyc.get("sell_n") or 0)
            _cyc_dirty = False
            _tp_up   = float(cyc.get("tp_up")   or 0.0)
            _tp_down = float(cyc.get("tp_down") or 0.0)
            if (_node_lo > 0 and _buy_n > 0 and _tp_up > 0
                    and int(buys) > _buy_n / 2
                    and not cyc.get("sl_armed_buy")):
                cls.enqueue_modify_sl(account, symbol, magic, _node_lo,
                                      side="buy", comment="FB|sl_arm|buy", tp=_tp_up)
                cyc["sl_armed_buy"] = True
                cyc["sl_buy"] = _node_lo   # persist so tp_refresh preserves the SL
                _cyc_dirty = True
            if (_node_hi > 0 and _sell_n > 0 and _tp_down > 0
                    and int(sells) > _sell_n / 2
                    and not cyc.get("sl_armed_sell")):
                cls.enqueue_modify_sl(account, symbol, magic, _node_hi,
                                      side="sell", comment="FB|sl_arm|sell", tp=_tp_down)
                cyc["sl_armed_sell"] = True
                cyc["sl_sell"] = _node_hi  # persist so tp_refresh preserves the SL
                _cyc_dirty = True
            if _cyc_dirty:
                cls.set_last_arm(account, symbol, magic=magic, **cyc)

        # Deferred SL arming for hvn_edge reversion side: once >50% of the reversion side
        # fills, lock SL at the fulcrum (the HVN edge the breakout tapped). Runs every poll
        # so it doesn't depend on candle close. One-shot per cycle (be_done_{side} guard).
        # tp guard: EA requires non-zero tp on MODIFY_POSITION.
        if (_defer_sl and positions > 0
                and cyc.get("trigger_kind") == "hvn_edge"
                and buys is not None and sells is not None):
            _he_bias    = str(cyc.get("breakout_bias") or "")
            _he_fulcrum = float(cyc.get("fulcrum") or 0.0)
            _he_buy_n   = int(cyc.get("buy_n")  or 0)
            _he_sell_n  = int(cyc.get("sell_n") or 0)
            _he_tp_up   = float(cyc.get("tp_up")   or 0.0)
            _he_tp_down = float(cyc.get("tp_down") or 0.0)
            _cyc_dirty  = False
            # Bull break → reversion = sells (inside HVN) → SL at fulcrum hi edge
            if (_he_bias == "buy" and int(sells) > 0 and _he_sell_n > 0
                    and int(sells) > _he_sell_n / 2
                    and _he_fulcrum > 0 and _he_tp_down > 0
                    and not cyc.get("be_done_sell")):
                cls.enqueue_modify_sl(account, symbol, magic, _he_fulcrum,
                                      side="sell", comment="FB|hvn_edge_rev_sl|sell",
                                      tp=_he_tp_down)
                cyc["be_done_sell"] = True
                cyc["sl_sell"] = _he_fulcrum
                _cyc_dirty = True
            # Bear break → reversion = buys (inside HVN) → SL at fulcrum lo edge
            if (_he_bias == "sell" and int(buys) > 0 and _he_buy_n > 0
                    and int(buys) > _he_buy_n / 2
                    and _he_fulcrum > 0 and _he_tp_up > 0
                    and not cyc.get("be_done_buy")):
                cls.enqueue_modify_sl(account, symbol, magic, _he_fulcrum,
                                      side="buy", comment="FB|hvn_edge_rev_sl|buy",
                                      tp=_he_tp_up)
                cyc["be_done_buy"] = True
                cyc["sl_buy"] = _he_fulcrum
                _cyc_dirty = True
            if _cyc_dirty:
                cls.set_last_arm(account, symbol, magic=magic, **cyc)

        # candle_sweep VWAP-BE: once the basket P&L ≥ the pre-computed be_distance_usd
        # (≈ candle_hl × lot × contract_size), move all filled positions' SL to the
        # session VWAP (stored as vwap in arm context). Fires once per arm (vwap_be_armed).
        if (positions > 0
                and cyc.get("trigger_kind") == "candle_sweep"
                and not cyc.get("vwap_be_armed")
                and pnl is not None):
            _be_thresh = float(cyc.get("sweep_be_usd") or 0.0)
            _vwap_sl   = float(cyc.get("sweep_vwap")  or 0.0)
            if _be_thresh > 0 and _vwap_sl > 0 and float(pnl) >= _be_thresh:
                cls.enqueue_modify_sl(account, symbol, magic, _vwap_sl,
                                      side="", comment="FB|sweep_vwap_be")
                cyc["vwap_be_armed"] = True
                cls.set_last_arm(account, symbol, magic=magic, **cyc)

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
                cls.set_last_arm(account, symbol, magic=magic, **{**cyc, "active": False})
                cls.clear_emit(account, symbol, magic=magic)
            elif unfroze:
                # still active with resting pendings — persist the unfreeze so live-VP
                # refresh resumes on the resting legs / next fill.
                cls.set_last_arm(account, symbol, magic=magic, **cyc)
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
        # Fires once per side (be_done_{side} guard, CAS under lock to prevent spam).
        if bool(grid_cfg.get("fullfill_be_enabled", True)):
            _cancel_opp = bool(grid_cfg.get("fullfill_cancel_opposite", True))
            _bn = int(cyc.get("buy_n") or 0)
            _sn = int(cyc.get("sell_n") or 0)
            for _side, _need, _have in (("buy", _bn, int(buys or 0)),
                                        ("sell", _sn, int(sells or 0))):
                if _need > 0 and _have >= _need and not cyc.get(f"be_done_{_side}"):
                    # CAS under lock: re-check flag so concurrent threads don't both fire.
                    key = (str(account), symbol, int(magic))
                    with cls._lock:
                        live = cls._last_arm.get(key)
                        if live is None or live.get(f"be_done_{_side}"):
                            continue   # another thread already set it
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
                                      "cancel_opp": _cancel_opp,
                                      "squeeze_ok": cyc.get("squeeze_ok"),
                                      "squeeze_rank": cyc.get("squeeze_rank")})

        # net-basket exit (which a hedge leg can mask). Gate: a side has a LOT-WEIGHTED
        # MAJORITY of its planned exposure filled (>50% of buy_lots_total/sell_lots_total,
        # not >50% of leg COUNT — the ladder's far legs carry more lot size than near ones,
        # so a count-based gate under/over-states real commitment). Track that side's peak
        # floating P&L; when it gives back ≥ giveback% from the peak, BOOK half that side
        # and move the rest to breakeven (risk-free runner).
        # Fires once per cycle — guarded by bias_trail_done, a PRIVATE one-shot flag
        # (NOT bias_booked, which the flatten-rest suppression below resets one poll
        # after booking; gating on bias_booked let the trail double/triple-fire within
        # seconds of the first book, each time at a worse price).
        if (bool(grid_cfg.get("bias_trail_enabled", True))
                and not cyc.get("bias_trail_done")
                and buy_pnl is not None and sell_pnl is not None):
            buy_lots_total  = float(cyc.get("buy_lots_total")  or 0.0)
            sell_lots_total = float(cyc.get("sell_lots_total") or 0.0)
            bias = ""
            _bias_peak_set = float(cyc.get("bias_peak") or 0.0) > 0.0
            if buy_lots_total > 0 and float(buy_lots or 0.0) > 0 \
                    and (float(buy_lots or 0.0) > buy_lots_total / 2 or _bias_peak_set):
                bias = "buy"
            elif sell_lots_total > 0 and float(sell_lots or 0.0) > 0 \
                    and (float(sell_lots or 0.0) > sell_lots_total / 2 or _bias_peak_set):
                bias = "sell"
            if bias:
                side_pnl = float(buy_pnl if bias == "buy" else sell_pnl)
                peak = max(float(cyc.get("bias_peak") or 0.0), side_pnl)
                if peak != float(cyc.get("bias_peak") or 0.0) or cyc.get("bias_side") != bias:
                    cyc["bias_peak"] = peak
                    cyc["bias_side"] = bias   # persisted so downstream (dashboard) doesn't re-derive
                    cls.set_last_arm(account, symbol, magic=magic, **cyc)
                # Per-TF activate (mirrors cycle_net_target_by_tf): scale the trail-arm
                # threshold to the TF so a big-target cycle isn't BE-locked on a tiny wiggle.
                _act_by_tf = grid_cfg.get("bias_trail_activate_by_tf") or {}
                _act_base = grid_cfg.get("bias_trail_activate_usd", 5.0)
                activate = float((_act_by_tf.get(tf, _act_base) if isinstance(_act_by_tf, dict)
                                  else _act_base) or 0.0)
                if cyc.get("squeeze_ok"):
                    activate *= float(grid_cfg.get("squeeze_trail_mult", 1.0) or 1.0)
                giveback = float(grid_cfg.get("bias_trail_giveback_pct", 40.0) or 0.0)
                book_frac = float(grid_cfg.get("bias_book_frac", 0.5) or 0.5)
                # side_pnl > 0 floor: giveback%-off-peak alone has no floor at zero, so a
                # side that ran deep negative after touching peak would still satisfy
                # "retraced ≥ giveback% from peak" and book a LOSS as if it were a
                # profit-lock (observed: booked at side_pnl -1895 off a peak of 1500).
                # This is meant to lock in profit on a pullback, not realize a reversal.
                if (activate > 0 and peak >= activate and side_pnl > 0
                        and side_pnl <= peak * (1.0 - giveback / 100.0)):
                    cls.enqueue(account, CLOSE_SIDE, symbol, magic=magic, side=bias,
                                frac=book_frac, comment=f"FB|book|{bias}", now=t)
                    cls.enqueue(account, MOVE_BE, symbol, magic=magic, side=bias,
                                comment=f"FB|be|{bias}", now=t)
                    # bias_trail_done = private one-shot (never reset while cycle lives);
                    # bias_booked = flatten-rest suppression (reset by that block below).
                    cls.set_last_arm(account, symbol, magic=magic, **{**cyc, "bias_booked": True,
                                                         "bias_trail_done": True})
                    _emit_exit_audit({"account": str(account), "broker_symbol": symbol,
                                      "tf": tf, "magic": magic, "exit_reason": "bias_book_trail",
                                      "bias": bias, "peak": round(peak, 2),
                                      "side_pnl": round(side_pnl, 2), "book_frac": book_frac,
                                      "squeeze_ok": cyc.get("squeeze_ok"),
                                      "squeeze_rank": cyc.get("squeeze_rank")})
                    return "bias_book_trail"   # cycle continues (runner + hedge); no flatten

        reason: str | None = None
        detail: dict = {}

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
        if cyc.get("squeeze_ok"):
            sq_mult = float(grid_cfg.get("squeeze_target_mult", 1.0) or 1.0)
            base_target *= sq_mult
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
        # Skip if bias_trail just booked a fraction — partial close is expected, not a
        # signal. RACE FIX: the skip must be consumed on the poll where positions has
        # ACTUALLY dropped (the broker close landed), not on the poll right after
        # booking — CLOSE_SIDE takes ~2s to execute, so on the very next 1s poll
        # positions is still the pre-close count. Consuming here unconditionally used
        # to clear bias_booked + reset max_seen to that STALE (still-high) count before
        # the real drop was even visible, so when positions finally did drop 1-2 polls
        # later, bias_booked was already False and flatten-rest fired unguarded —
        # flattening the whole cycle at whatever price it was at that instant, seconds
        # after the trail had already booked a much better one (observed repeatedly:
        # trail books green/small-red, flatten fires red/deep-red 2-4s later).
        bias_booked = bool(cyc.get("bias_booked", False))
        if bias_booked and 0 < positions < max_seen:
            cls.set_last_arm(account, symbol, magic=magic, **{**cyc,
                             "max_seen": positions, "bias_booked": False})
        if reason is None and 0 < positions < max_seen and pendings > 0 and not bias_booked:
            tol = max(mid * 1e-4, 1e-6) if mid > 0 else 1e-6
            confirms = (tp_up > 0 and mid >= tp_up - tol) or (tp_down > 0 and 0 < mid <= tp_down + tol)
            reason = "leg_tp" if confirms else "leg_closed_other"

        # 3) full-hedge backstop (delta-neutral → cut to free margin; realizes a loss)
        if reason is None and close_on_full_hedge and n > 0 \
                and buys is not None and sells is not None:
            if min(int(buys), int(sells)) >= n and (int(buys) + int(sells)) >= 2 * n:
                reason = "full_hedge"

        if reason is None:
            return None

        cls.enqueue(account, CLOSE_ALL, symbol, comment=f"FB|flatten|{reason}", magic=magic, now=t)
        cls.set_last_arm(account, symbol, magic=magic, **{**cyc, "flatten_ts": t})
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

    @classmethod
    def enqueue_grid_plan(cls, account: str, broker_symbol: str, plan, *,
                          close_first: bool = True, clear_kind: str = "flatten",
                          magic: int = 0, leg_tp: bool = True,
                          sides: set[str] | None = None) -> list[Command]:
        """Translate a rebased neutral GridPlan into PLACE_PENDING commands.
        buy_legs → buy_stop, sell_legs → sell_stop, shared per-side TP, no SL (v1).
        Optionally prepend a clear command: clear_kind="flatten" → CLOSE_ALL (close
        positions + cancel pendings); "cancel" → CANCEL_PENDINGS (cancel stale pendings
        only, never touch a live position — the safe re-arm path).

        sides: restrict to {"buy"} or {"sell"} for a SIDE-ONLY re-arm — used when one
        side of an active cycle exited flat while the other is still live. Only that
        side's legs are enqueued, and close_first is scoped to that side only (never
        touches the still-live side's resting orders/positions). None/both = normal
        full straddle.

        leg_tp=False places legs WITHOUT a per-order TP — they never self-close, so the
        winning side can't book while the losing side dangles (a per-leg TP hit ≠ a
        net-positive cycle). The basket net-target exit (monitor_cycle) then owns ALL
        profit-taking and only closes when the whole cycle is net ≥ target."""
        sides = sides or {"buy", "sell"}
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

        out: list[Command] = []
        if close_first:
            # A side-only re-arm (sides has exactly one element) MUST use clear_kind
            # "cancel" — CLOSE_ALL's EA handler (ExecCloseAll) does not honor a side=
            # filter and would close BOTH sides' positions, defeating the whole point
            # of leaving the still-live side untouched. CANCEL_PENDINGS's EA handler
            # (ExecCancelPendings) DOES respect side=.
            if len(sides) == 1:
                assert clear_kind == "cancel", \
                    "side-only enqueue_grid_plan requires clear_kind='cancel'"
                out.append(cls.enqueue(account, CANCEL_PENDINGS, broker_symbol, magic=magic,
                                       side=next(iter(sides))))
            else:
                clear_cmd = CLOSE_ALL if clear_kind == "flatten" else CANCEL_PENDINGS
                # scope the clear to THIS cycle's magic so a re-arm only cancels its own
                # TF/strategy pendings, never a sibling TF cycle's live orders.
                out.append(cls.enqueue(account, clear_cmd, broker_symbol, magic=magic))
        buy_tp  = getattr(plan, "buy_tp",  0.0) if leg_tp else 0.0
        sell_tp = getattr(plan, "sell_tp", 0.0) if leg_tp else 0.0
        _trigger_kind = getattr(plan, "trigger_kind", "") or ""
        # hvn_inside_touch: SL is deferred — armed by monitor_cycle once >50% of a side
        # fills. Place orders without SL so early fills don't get stopped prematurely.
        if _trigger_kind == "hvn_inside_touch":
            buy_sl = sell_sl = 0.0
        else:
            buy_sl  = getattr(plan, "buy_sl",  0.0)
            sell_sl = getattr(plan, "sell_sl", 0.0)
        # Order type per leg based on current market price:
        #   buy leg above ask  → buy_stop  (price must rise to trigger)
        #   buy leg below ask  → buy_limit (price already above level → limit entry on pullback)
        #   sell leg below bid → sell_stop (price must fall to trigger)
        #   sell leg above bid → sell_limit (price already below level → limit entry on bounce)
        # When no live quote is cached, default all to stops (EA freeze guard is backstop).
        _q = cls.get_quote(account, broker_symbol) or {}
        _ask = float(_q.get("ask") or 0.0)
        _bid = float(_q.get("bid") or 0.0)
        for i, leg in enumerate(getattr(plan, "buy_legs", []) or [] if "buy" in sides else []):
            if _ask > 0 and leg.price < _ask:
                _otype = "buy_limit"
            else:
                _otype = "buy_stop"
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol, order_type=_otype,
                price=leg.price, lot=leg.lot, sl=buy_sl, tp=buy_tp,
                comment=f"FB|{tag}|{tf_tag}|b{i + 1}", magic=magic))
        for i, leg in enumerate(getattr(plan, "sell_legs", []) or [] if "sell" in sides else []):
            if _bid > 0 and leg.price > _bid:
                _otype = "sell_limit"
            else:
                _otype = "sell_stop"
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol, order_type=_otype,
                price=leg.price, lot=leg.lot, sl=sell_sl, tp=sell_tp,
                comment=f"FB|{tag}|{tf_tag}|s{i + 1}", magic=magic))
        return out

    @classmethod
    def _modify_throttled(cls, account: str, broker_symbol: str, magic: int,
                          side: str, kind: str) -> bool:
        """Broker-rate throttle: at most one MODIFY per (cycle, side, kind) every
        grid_levels.modify_cooldown_s seconds. A full refresh batch (buy/sell/fulcrum =
        distinct side/kind keys) passes the first time, then is suppressed until the
        cooldown elapses — keeps the per-account order-modification rate well under the
        levels that trip broker HFT-abuse limits. 0/absent cooldown = no throttle."""
        cd = _modify_cooldown_s()
        if cd <= 0:
            return False
        k = (str(account), broker_symbol, int(magic), side or "", kind)
        now = time.time()
        with cls._lock:
            if now - cls._last_modify_ts.get(k, 0.0) < cd:
                return True
            cls._last_modify_ts[k] = now
        return False

    @classmethod
    def enqueue_modify_pending(cls, account: str, broker_symbol: str, magic: int,
                               price_delta: float, new_tp: float = 0.0,
                               side: str = "") -> "Command | None":
        """Shift all pending stop orders for `magic` by `price_delta` and update TP.

        price_delta: signed pts to add to each pending's current price (+ = up, - = down).
        new_tp: replacement TP for all legs on that side; 0 = leave unchanged.
        side: "buy", "sell", or "" (both). Returns None when throttled (rate cap)."""
        # kind distinguishes a fulcrum SHIFT (price_delta) from a TP-only refresh so the two
        # don't share a cooldown slot (each is independently rate-limited).
        _kind = "pend_shift" if abs(price_delta) > 0 else "pend_tp"
        if cls._modify_throttled(account, broker_symbol, magic, side, _kind):
            return None
        return cls.enqueue(account, MODIFY_PENDING, broker_symbol,
                           magic=magic, price=price_delta, tp=new_tp, side=side)

    @classmethod
    def enqueue_modify_position(cls, account: str, broker_symbol: str, magic: int,
                                new_tp: float, side: str = "", comment: str = "",
                                sl: float = 0.0) -> "Command | None":
        """Refresh TP on FILLED positions for `magic` on `side`.

        Pass `sl` = the arm's stored sl_buy/sl_sell to preserve a previously armed SL.
        sl=0 → EA keeps posInfo.StopLoss() (existing SL, safe default).
        side: "buy", "sell", or "" (both). Returns None when throttled."""
        if cls._modify_throttled(account, broker_symbol, magic, side, "pos_tp"):
            return None
        return cls.enqueue(account, MODIFY_POSITION, broker_symbol,
                           magic=magic, tp=new_tp, sl=sl, side=side, comment=comment)

    @classmethod
    def enqueue_modify_sl(cls, account: str, broker_symbol: str, magic: int,
                          new_sl: float, side: str = "", comment: str = "",
                          tp: float = 0.0) -> "Command | None":
        """Set SL on FILLED positions for `magic` on `side`.

        Pass `tp` = the arm's stored TP for that side so the EA gets a valid
        (SL, TP) pair — MT5 rejects a modify where both are zero or TP is absent.
        Bypasses the modify throttle — SL arming is a one-shot structural event."""
        return cls.enqueue(account, MODIFY_POSITION, broker_symbol,
                           magic=magic, sl=new_sl, tp=tp, side=side, comment=comment)

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
        with cls._lock:
            for (account, broker_symbol, magic), state in arms.items():
                cls._last_arm[(account, broker_symbol, magic)] = state
            for (account, symbol, magic), fulcrum in emits.items():
                if fulcrum is not None:
                    cls._last_emit[(account, symbol, magic)] = fulcrum
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
                    # already tracked — but buy_n/sell_n can still drift from the
                    # broker (partial manual close, missed reap window, etc). EA's
                    # poll-reported buys/sells is ground truth; correct in place so
                    # tp_refresh/net_target/bias_trail don't manage against stale counts.
                    ea_buys, ea_sells = int(m.get("buys", 0)), int(m.get("sells", 0))
                    if int(existing.get("buy_n") or 0) != ea_buys or int(existing.get("sell_n") or 0) != ea_sells:
                        existing.pop("magic", None)
                        existing["buy_n"], existing["sell_n"] = ea_buys, ea_sells
                        existing["max_pos_seen"] = max(int(existing.get("max_pos_seen") or 0), positions)
                        cls.set_last_arm(account, broker_symbol, magic=mg, **existing)
                        import logging
                        logging.getLogger(__name__).info(
                            f"[exec_bridge] reconcile: corrected stale counts for magic {mg} "
                            f"buy_n/sell_n → {ea_buys}/{ea_sells} for {account}/{broker_symbol}")
                    continue  # already tracked
                if existing and not existing.get("active"):
                    # Reaped (active=False) but positions still open in MT5 — reaper fired
                    # before the EA reported orders within the placement grace window.
                    # Reactivate using original arm metadata so net_target/bias_trail manage them.
                    reactivated = {k: v for k, v in existing.items() if k != "magic"}
                    reactivated["active"] = True
                    reactivated["max_pos_seen"] = max(int(existing.get("max_pos_seen") or 0), positions)
                    cls.set_last_arm(account, broker_symbol, magic=mg, **reactivated)
                    stubbed.append(mg)
                    import logging
                    logging.getLogger(__name__).warning(
                        f"[exec_bridge] reconcile: reactivated reaped magic {mg} "
                        f"({positions} positions) for {account}/{broker_symbol}")
                    continue
                # No entry at all — create a minimal stub so monitor_cycle can
                # track P&L and the position_open gate fires correctly.
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
