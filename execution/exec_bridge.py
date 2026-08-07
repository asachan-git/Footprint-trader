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
import os
import threading
from execution.arm_state_store import (  # noqa: E402
    persist_arm, persist_emit, load as _load_arm_state,
)
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
CLOSE_SIDE = "CLOSE_SIDE"        # close a fraction of ONE side's positions (bias-side booking)

# Per-leg placement retries (2026-08-05, user: "retry till the complete grid is placed …
# only retry for the order which got rejected"). Each rejected leg is re-queued on its own
# with the order TYPE re-derived from the live market; the price never moves. NOT unbounded:
# Vantage flagged this account's EA for HFT abuse at ~1560 order-ops/hr once already, and an
# unbounded loop against a persistently-unfillable price would be exactly that pattern. 10
# attempts spans several minutes of EA polls — long enough for market to move off a freeze
# band, short enough to stay well clear of the rate limit.
_GRID_RETRIES = 10
MOVE_BE = "MOVE_BE"              # move one side's positions' SL to breakeven (risk-free runner)

# ── per-strategy × per-TF magic scheme ───────────────────────────────────────
# magic = MAGIC_BASE + strat_code·10 + tf_code  →  e.g. hvn·15m = 770013,
# squeeze·1h = 770024. The EA owns the whole [MAGIC_BASE, MAGIC_BASE+99] range; the
# tf is recoverable as magic % 10, so the server can attribute each EA-reported
# position pool to the TF cycle that owns it (enables parallel per-TF cycles).
MAGIC_BASE = int(os.environ.get("FB_MAGIC_BASE", "770000"))
# OPERATIONAL-ONLY CHANGE (2026-08-03, feat/jun22-literal) — literal Jun22 (9590331)
# hardcoded 770000, colliding with feat/crude-hvn-rotation's magic range. Env-override
# only, so this can run as a distinct branch without touching any live position under
# 770000. Default unchanged (770000) if FB_MAGIC_BASE isn't set. No strategy/exit/TP/
# sizing logic in this file was touched — see the branch's own commit for the complete,
# minimal diff against 9590331.
_STRAT_CODE = {
    "hvn_inside_touch": 1, "squeeze": 2, "vp_level_touch": 3, "imbalance": 4,
    "hvn_edge": 5, "anchor": 6, "va": 7, "cvd_div": 8,
    # Setup-level pseudo-kind: the "vp_levels" parallel setup (va OR vp_level_touch) arms
    # under ONE dedicated magic so the trade report reads it as a single setup. The audit
    # still records the real detector (trigger_kind) that fired.
    "vp_levels": 9,
    # Ported from feat/jul09-restored-skew 2026-08-06. Code 13 is jul09's, kept identical so
    # magics mean the same thing across branches (base+13*10+tf → 5m=..132, 15m=..133).
    # Requires the +150 owned-range bound in tf_from_magic below — at +100 these are disowned.
    "lvn_edge_touch": 13,
}
_TF_CODE = {"1m": 1, "5m": 2, "15m": 3, "1h": 4}
_CODE_TF = {v: k for k, v in _TF_CODE.items()}


def magic_for(trigger_kind: str, tf: str) -> int:
    """Composite magic identifying (strategy, TF). Unknown kind→0, unknown tf→0."""
    return MAGIC_BASE + _STRAT_CODE.get(trigger_kind, 0) * 10 + _TF_CODE.get(tf, 0)


def tf_from_magic(magic: int) -> str:
    """Recover the TF a magic belongs to (magic % 10). '' if not one of ours."""
    # +150, not +100 (2026-08-06). Strat codes can exceed 9 — lvn_edge_touch is 13, so its
    # magics run to base+134. The EA already owns 150 (InpMagicRange, whose own comment says
    # "covers strat decades 0-13"); the server side was never widened. With the old +100
    # bound tf_from_magic returned "" for those magics, the per-magic poll loop skipped them,
    # and monitor_cycle never ran → live positions with no net_target/trail/flatten at all.
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
    retries_left: int = 0     # PLACE_PENDING only (2026-08-05, user): auto-requeue on a
                               # failed ack, nudging price further from live quote each
                               # attempt (helps "inside freeze" specifically — a same-price
                               # retry won't clear a freeze-band violation, an outward nudge
                               # can). 0 = no retry (default for non-grid commands).
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
    # combined net-PnL target (2026-08-05, user) — account-wide floating P&L summed
    # across every open cycle, ported from feat/jul09's identical mechanism. Own
    # baseline, no daily_target_pct equivalent exists on this branch to share one with.
    _combined_target_hit: dict[str, bool] = {}       # account → True once combined-$ target reached
    _combined_start_balance: dict[str, float] = {}   # account → balance the target is measured from

    @classmethod
    def check_combined_target(cls, account: str, balance: float, equity: float,
                              target_usd: float, is_flat: bool) -> dict:
        """Combined-PnL account-wide target (2026-08-05, user), ported from feat/jul09's
        identical mechanism. Account-wide $ check across every open cycle's floating
        P&L summed together — separate from grid_levels.cycle_net_target_usd, which
        still lets one cycle book early on its own; this is an overlay on top.

        RESET RULE: NOT sticky-for-the-day. Once hit=True has blocked new arms and the
        account then goes fully flat (is_flat, caller-computed from positions+pendings
        across every magic), the hit clears AND the baseline re-bases to the CURRENT
        balance — so the next $target is measured as fresh profit from this flat point
        forward, not re-triggering instantly off the old baseline (which would still
        read >= target since the gain is now realized into balance, not lost)."""
        account = str(account)
        with cls._lock:
            if account not in cls._combined_start_balance:
                cls._combined_start_balance[account] = balance

            already_hit = cls._combined_target_hit.get(account, False)
            if already_hit and is_flat:
                cls._combined_target_hit[account] = False
                cls._combined_start_balance[account] = balance
                already_hit = False

            start = cls._combined_start_balance.get(account, balance)
            pnl_usd = equity - start
            hit = already_hit or (target_usd > 0 and pnl_usd >= target_usd)
            if hit and not already_hit:
                cls._combined_target_hit[account] = True
        return {"hit": hit, "pnl_usd": round(pnl_usd, 2), "start_balance": round(start, 2)}

    @classmethod
    def combined_target_hit(cls, account: str) -> bool:
        return cls._combined_target_hit.get(str(account), False)

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
        state = dict(meta, tf=tf, magic=key_magic)
        with cls._lock:
            cls._last_arm[(str(account), broker_symbol, key_magic)] = state
        try:
            persist_arm(account, broker_symbol, key_magic, state)
        except Exception:
            pass  # persistence failure must never block execution

    # ── intrabar touch-arm tick-reversal state (2026-08-05, user) ────────────
    _touch_state: dict[tuple, dict] = {}   # (account, broker_symbol, tf, kind) → tap record

    @classmethod
    def touch_arm_check(cls, account: str, broker_symbol: str, tf: str,
                        live_price: float, edge: float, side: str,
                        confirm_ticks: float, now: float | None = None,
                        kind: str = "hvn_inside_touch") -> bool:
        """Tick-reversal state machine for intrabar touch-arming. Call each poll with the
        live price and the edge it is tapping. Returns True ONCE, when price has tapped the
        edge AND then reverted back INSIDE the node by `confirm_ticks` ($) — the intrabar
        twin of "the candle closed back inside".

        A breakout (price keeps going THROUGH the edge) never reverts, so it never confirms
        and never arms — that is the point: the tap alone is not the signal, the rejection
        is. State is keyed per (account, symbol, tf, kind) so future touch triggers stay
        isolated. Consumed on confirm (one arm per tap)."""
        t = now if now is not None else time.time()
        key = (str(account), broker_symbol, tf, kind)
        with cls._lock:
            st = cls._touch_state.get(key)
            if st is not None and st.get("edge") == edge and st.get("side") == side:
                # reverted INSIDE = away from the edge, toward the node interior
                reverted = (live_price <= edge - confirm_ticks) if side == "top" \
                    else (live_price >= edge + confirm_ticks)
                if reverted:
                    cls._touch_state.pop(key, None)   # consume — one confirm per tap
                    return True
                return False
            if confirm_ticks <= 0:
                return True          # no reversal wait → arm on the raw first tap
            cls._touch_state[key] = {"edge": edge, "side": side,
                                     "tapped_px": live_price, "ts": t}
            return False

    @classmethod
    def clear_touch_state(cls, account: str, broker_symbol: str, tf: str,
                          kind: str = "hvn_inside_touch") -> None:
        """Drop a pending tap (price left the buffer entirely → tap abandoned)."""
        with cls._lock:
            cls._touch_state.pop((str(account), broker_symbol, tf, kind), None)

    @classmethod
    def get_last_arm(cls, account: str, broker_symbol: str, tf: str = "", magic: int = 0) -> dict | None:
        with cls._lock:
            m = cls._last_arm.get((str(account), broker_symbol, int(magic)))
            return dict(m) if m else None

    @classmethod
    def active_magics(cls, account: str, broker_symbol: str) -> list[int]:
        """Magics with an ACTIVE arm record for (account, broker_symbol).

        Needed by the absent-magic reap: the EA only reports magics that currently hold a
        position or pending, so a cycle that went flat simply stops appearing and
        monitor_cycle — which owns the retire path — is never called for it again. Without
        this enumeration there is no way to notice those.
        """
        with cls._lock:
            return [mg for (acct, sym, mg), m in cls._last_arm.items()
                    if acct == str(account) and sym == broker_symbol and m.get("active")]

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

        # 0.5) bias-side trailing book — JUN22 MECHANISM, restored 2026-08-06 (user).
        # Reverted from the net-P&L full-cycle-exit variant written 2026-08-05/06 back to
        # what feat/composed-strategy@4f387b7 actually ran on 2026-06-22:
        #   • measures the BIAS SIDE's own P&L, not the cycle net
        #   • that side must be FULLY filled (the move committed your way)
        #   • books book_frac of it and moves the remainder to BREAKEVEN (risk-free runner)
        #   • ONE-SHOT per cycle via bias_booked — the cycle CONTINUES, no flatten
        # activate is the lot-scaled Jun22 value: 5.0 at Jun22's base_lot 0.01 == 125 at the
        # current 0.25 (25x). giveback 40% and book_frac 0.5 are Jun22-literal.
        #
        # DELIBERATE DEVIATION FROM JUN22 — the `side_pnl > 0` floor below. Jun22 had no
        # such check, so its giveback test (side_pnl <= peak*0.6) was satisfied by ANY
        # negative number and the "profit trail" could book a deeply losing side at market.
        # Observed live on this branch 2026-08-05: a fire at side_pnl -2678.5. Keeping the
        # floor; everything else is Jun22-literal.
        # ONE-SHOT FLAG — gates on bias_trail_done, NOT bias_booked. Jun22 gated on
        # bias_booked and that was safe there because Jun22's flatten-rest never cleared it.
        # THIS branch does clear it (race fix added 2026-08-05: the partial close a book
        # causes must not be read as "a leg closed unexpectedly"), so gating the trail on
        # bias_booked would let that consume re-open the gate — observed as 5 fires in 10s
        # on magic 775011. bias_trail_done is never cleared → one-shot is Jun22-equivalent.
        if (bool(grid_cfg.get("bias_trail_enabled", True))
                and not cyc.get("bias_trail_done")
                and buy_pnl is not None and sell_pnl is not None):
            buy_n = int(cyc.get("buy_n") or 0)
            sell_n = int(cyc.get("sell_n") or 0)
            bias = ""
            if buy_n > 0 and int(buys or 0) >= buy_n:
                bias = "buy"
            elif sell_n > 0 and int(sells or 0) >= sell_n:
                bias = "sell"
            if bias:
                side_pnl = float(buy_pnl if bias == "buy" else sell_pnl)
                peak = max(float(cyc.get("bias_peak") or 0.0), side_pnl)
                if peak != float(cyc.get("bias_peak") or 0.0):
                    cyc["bias_peak"] = peak
                    cls.set_last_arm(account, symbol, magic=magic,
                                     **{k: v for k, v in cyc.items() if k != "magic"})
                activate = float(grid_cfg.get("bias_trail_activate_usd", 5.0) or 0.0)
                giveback = float(grid_cfg.get("bias_trail_giveback_pct", 40.0) or 0.0)
                book_frac = float(grid_cfg.get("bias_book_frac", 0.5) or 0.5)
                if (activate > 0 and peak >= activate and side_pnl > 0
                        and side_pnl <= peak * (1.0 - giveback / 100.0)):
                    cls.enqueue(account, CLOSE_SIDE, symbol, magic=magic, side=bias,
                                frac=book_frac, comment=f"FB|book|{bias}", now=t)
                    cls.enqueue(account, MOVE_BE, symbol, magic=magic, side=bias,
                                comment=f"FB|be|{bias}", now=t)
                    # bias_trail_done = the Jun22 one-shot (never cleared for this cycle).
                    # bias_booked = flatten-rest suppressor, consumed a poll later once the
                    # broker-side partial close actually lands.
                    cls.set_last_arm(account, symbol, magic=magic,
                                     **{**{k: v for k, v in cyc.items() if k != "magic"},
                                        "bias_booked": True, "bias_trail_done": True,
                                        f"bias_trail_done_{bias}": True})
                    _emit_exit_audit({"account": str(account), "broker_symbol": symbol,
                                      "tf": tf, "magic": magic,
                                      "exit_reason": "bias_book_trail",
                                      "bias": bias, "peak": round(peak, 2),
                                      "side_pnl": round(side_pnl, 2),
                                      "book_frac": book_frac,
                                      "squeeze_ok": cyc.get("squeeze_ok"),
                                      "squeeze_rank": cyc.get("squeeze_rank")})
                    return "bias_book_trail"   # cycle continues (runner + hedge); no flatten

        reason: str | None = None
        detail: dict = {}

        # 1) flatten-rest — a filled leg closed while the opposite ladder still rests.
        # Skip if bias_trail just booked a fraction — that partial close is EXPECTED, not a
        # signal. Ported from feat/jul09-restored-skew 2026-08-05 after this fired live on
        # magic 775013: 18:03:43 bias_book_trail booked the sell side at peak 322.75 /
        # side_pnl 191.50, and 18:03:44 — one second later — flatten-rest saw positions drop
        # 3→2 (caused BY that book), fired leg_closed_other, and market-dumped the rest at
        # pnl −206.75. Unguarded, every successful trail fire triggers a full-cycle dump.
        #
        # RACE: the skip must be consumed on the poll where positions has ACTUALLY dropped
        # (broker close landed), not the poll right after booking — CLOSE_SIDE takes ~2s, so
        # on the next 1s poll `positions` is still the pre-close count. Consuming
        # unconditionally would clear bias_booked against a STALE (still-high) count, and the
        # real drop 1-2 polls later would then fire unguarded anyway.
        bias_booked = bool(cyc.get("bias_booked", False))
        if bias_booked and 0 < positions < max_seen:
            # NOTE: key is max_pos_seen (what line ~298 reads back). jul09's copy of this
            # fix writes "max_seen" — a key nothing reads — so its re-baseline is a no-op
            # there. Deliberately not replicating that typo.
            cls.set_last_arm(account, symbol, magic=magic,
                             **{**{k: v for k, v in cyc.items() if k != "magic"},
                                "max_pos_seen": positions, "bias_booked": False})
            max_seen = positions
        if reason is None and 0 < positions < max_seen and pendings > 0 and not bias_booked:
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

    @classmethod
    def reconcile_from_poll(cls, account: str, broker_symbol: str,
                            magics: list[dict]) -> list[int]:
        """Adopt any magic the EA reports with LIVE positions that this server has no arm
        record for (2026-08-05, ported from jul09).

        The safety net behind load_persisted_state: persistence only helps if the cycle was
        written to disk in the first place. A cycle armed by a build that predates
        persistence — or lost to any gap — would otherwise stay orphaned forever, with
        monitor_cycle silently skipping it while the positions sit open at the broker. Here
        the EA's own per-magic report is treated as ground truth and a minimal stub is
        created so net_target / full_hedge / flatten can manage it again.

        Also corrects buy_n/sell_n drift on already-tracked cycles (partial manual close,
        missed reap) so exits aren't computed against stale counts.

        Returns the magics that were stubbed or reactivated.
        """
        import logging
        log = logging.getLogger(__name__)
        stubbed: list[int] = []
        for m in magics or []:
            try:
                mg = int(m.get("magic", 0))
                if not mg:
                    continue
                ea_buys, ea_sells = int(m.get("buys", 0)), int(m.get("sells", 0))
                positions = ea_buys + ea_sells
                if positions <= 0:
                    continue
                existing = cls.get_last_arm(account, broker_symbol, magic=mg)
                if existing and existing.get("active"):
                    if (int(existing.get("buy_n") or 0) != ea_buys
                            or int(existing.get("sell_n") or 0) != ea_sells):
                        body = {k: v for k, v in existing.items() if k != "magic"}
                        body["buy_n"], body["sell_n"] = ea_buys, ea_sells
                        body["max_pos_seen"] = max(int(existing.get("max_pos_seen") or 0), positions)
                        cls.set_last_arm(account, broker_symbol, magic=mg, **body)
                        log.info(f"[exec_bridge] reconcile: corrected stale counts magic {mg} "
                                 f"buy_n/sell_n → {ea_buys}/{ea_sells}")
                    continue
                if existing and not existing.get("active"):
                    body = {k: v for k, v in existing.items() if k != "magic"}
                    body["active"] = True
                    body["max_pos_seen"] = max(int(existing.get("max_pos_seen") or 0), positions)
                    cls.set_last_arm(account, broker_symbol, magic=mg, **body)
                    stubbed.append(mg)
                    log.warning(f"[exec_bridge] reconcile: reactivated retired magic {mg} "
                                f"({positions} positions still open)")
                    continue
                tf_stub = tf_from_magic(mg) or "15m"
                stub = {
                    "active": True, "armed_tf": tf_stub, "tf": tf_stub,
                    "fulcrum": 0.0, "trigger_kind": "recovered",
                    "tp_up": 0.0, "tp_down": 0.0, "n_per_side": 0, "step": 0.0,
                    "buy_n": ea_buys, "sell_n": ea_sells,
                    "bias_peak": 0.0, "bias_booked": False,
                    "max_pos_seen": positions, "pend_seen": 0,
                    "flatten_ts": 0.0, "squeeze_ok": False, "squeeze_rank": 1.0,
                    "ts": time.time(), "recovered": True,
                }
                cls.set_last_arm(account, broker_symbol, magic=mg, **stub)
                stubbed.append(mg)
                log.warning(f"[exec_bridge] reconcile: stubbed orphaned magic {mg} "
                            f"({positions} positions) for {account}/{broker_symbol}")
            except Exception:
                log.exception("[exec_bridge] reconcile error for one magic")
        return stubbed

    @classmethod
    def load_persisted_state(cls) -> dict:
        """Restore _last_arm/_last_emit from disk after a restart (2026-08-05, user).

        Before this, arm state on this branch was purely in-memory: every restart orphaned
        any live cycle — monitor_cycle found no cyc, so net_target/trail/flatten all went
        silent while the positions stayed open at the broker. Call once at startup before
        the first poll."""
        import logging
        log = logging.getLogger(__name__)
        try:
            arms, emits = _load_arm_state()
        except Exception as e:
            log.warning(f"[exec_bridge] load_persisted_state failed: {e}")
            return {"arms": 0, "emits": 0, "error": str(e)}
        with cls._lock:
            for key, state in arms.items():
                cls._last_arm[key] = state
            for key, fulcrum in emits.items():
                if fulcrum is not None:
                    cls._last_emit[key] = fulcrum
        active = sum(1 for s in arms.values() if s.get("active"))
        log.info(f"[exec_bridge] restored {len(arms)} arm records ({active} active), "
                 f"{len(emits)} emit records from disk")
        return {"arms": len(arms), "emits": len(emits), "active": active}

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

    @classmethod
    def pending_order_type(cls, side: str, price: float, account: str = "",
                           broker_symbol: str = "") -> str:
        """STOP vs LIMIT for a leg, decided purely by where `price` sits vs the live market
        (2026-08-05, user). Leg PRICES are fulcrum-relative and never moved — buys sit above
        the fulcrum, sells below — but whether a given leg is a stop or a limit depends on
        which side of the CURRENT market it lands on:

            buy  above ask → buy_stop      buy  below ask → buy_limit
            sell below bid → sell_stop     sell above bid → sell_limit

        Previously every buy leg was hardcoded buy_stop and every sell leg sell_stop. When
        the fulcrum sat away from market, the near legs landed on the wrong side and MT5
        rejected them ("buy_stop inside freeze") — the grid went in one-sided. Falls back to
        the old stop-only behaviour if no quote is cached.
        """
        q = cls.get_quote(account, broker_symbol) if account and broker_symbol else None
        if not q or not q.get("bid") or not q.get("ask"):
            return "buy_stop" if side == "buy" else "sell_stop"
        bid, ask = float(q["bid"]), float(q["ask"])
        if side == "buy":
            return "buy_stop" if price >= ask else "buy_limit"
        return "sell_stop" if price <= bid else "sell_limit"

    # ── enqueue ────────────────────────────────────────────────────────────────
    @classmethod
    def enqueue(cls, account: str, type: str, symbol: str, *, order_type: str = "",
                price: float = 0.0, lot: float = 0.0, sl: float = 0.0, tp: float = 0.0,
                comment: str = "", magic: int = 0, side: str = "", frac: float = 0.0,
                retries_left: int = 0, now: float | None = None) -> Command:
        cmd = Command(
            id=uuid.uuid4().hex[:12], account=str(account), type=type, symbol=symbol,
            order_type=order_type, price=round(float(price), 5), lot=round(float(lot), 2),
            sl=round(float(sl), 5), tp=round(float(tp), 5), comment=comment,
            magic=int(magic), side=side, frac=float(frac), retries_left=int(retries_left),
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
                          magic: int = 0, leg_tp: bool = True, tf: str = "") -> list[Command]:
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
        # MT5 comment: FB|poc|15m|b1, FB|vah|5m|s2, FB|hvn|1m|b3 … (vp_level_touch → its
        # level_type; hvn_inside_touch → "hvn"; else the trigger kind). TF added
        # 2026-08-05 (user) — the original Jun22 comment format omitted it entirely,
        # which made later per-TF attribution from broker history impossible (had to be
        # reconstructed from magic numbers instead). All legs of one grid share the
        # level+TF; b/s + index distinguish legs. (MT5 comment cap ~31 chars — fits.)
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
        tf_tag = tf or "?"

        out: list[Command] = []
        if close_first:
            clear_cmd = CLOSE_ALL if clear_kind == "flatten" else CANCEL_PENDINGS
            # scope the clear to THIS cycle's magic so a re-arm only cancels its own
            # TF/strategy pendings, never a sibling TF cycle's live orders.
            out.append(cls.enqueue(account, clear_cmd, broker_symbol, magic=magic))
        buy_tp = getattr(plan, "buy_tp", 0.0) if leg_tp else 0.0
        sell_tp = getattr(plan, "sell_tp", 0.0) if leg_tp else 0.0
        # Leg prices are FULCRUM-relative (buys above it, sells below) and are never moved.
        # Only the order TYPE adapts to where each leg sits vs the live market.
        _rt = _GRID_RETRIES
        for i, leg in enumerate(getattr(plan, "buy_legs", []) or []):
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol,
                order_type=cls.pending_order_type("buy", leg.price, account, broker_symbol),
                price=leg.price, lot=leg.lot, sl=0.0, tp=buy_tp,
                comment=f"FB|{tag}|{tf_tag}|b{i + 1}", magic=magic, retries_left=_rt))
        for i, leg in enumerate(getattr(plan, "sell_legs", []) or []):
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol,
                order_type=cls.pending_order_type("sell", leg.price, account, broker_symbol),
                price=leg.price, lot=leg.lot, sl=0.0, tp=sell_tp,
                comment=f"FB|{tag}|{tf_tag}|s{i + 1}", magic=magic, retries_left=_rt))
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
        """Finalise commands from EA results: [{id, ok, ticket, retcode, error}].

        2026-08-05 (user, "add 3 retries to place a grid"): a failed PLACE_PENDING with
        retries_left > 0 is requeued as a fresh command (retries_left - 1). If the error
        mentions "freeze" (a stop-distance rejection — the price landed inside the
        broker's freeze band around live market), the retry price is nudged further out
        using the live cached quote + reported minStop, since resending the SAME price
        would just fail again identically. Other failures (transient reject the EA
        already retried once, or a genuine reject like market-closed) are resent
        unchanged — a later poll may land in a better market. Retries are consumed
        across up to 3 total attempts (retries_left starts at 3 in enqueue_grid_plan)."""
        done = failed = unknown = 0
        retried: list[tuple] = []   # (account, symbol, order_type, price, lot, sl, tp, comment, magic, retries_left)
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
                    if c.type == PLACE_PENDING and c.retries_left > 0:
                        # Re-queue ONLY this rejected leg — siblings that filled are
                        # untouched. The PRICE is preserved (it is fulcrum-relative and
                        # must not drift); only the STOP/LIMIT type is re-derived against
                        # the market as it stands now. That is what actually clears an
                        # "inside freeze": the leg was on the wrong side of market, so the
                        # correct fix is the other order type, not a moved price (an
                        # earlier version nudged the price outward — it broke fulcrum
                        # geometry and could still fail).
                        _side = "buy" if c.order_type.startswith("buy") else "sell"
                        retried.append((c.account, c.symbol, _side, c.price, c.lot,
                                        c.sl, c.tp, c.comment, c.magic, c.retries_left - 1))
                cls._audit("ack", c)
        for (acct, sym, side, price, lot, sl, tp, comment, magic, retries_left) in retried:
            cls.enqueue(acct, PLACE_PENDING, sym,
                        order_type=cls.pending_order_type(side, price, acct, sym),
                        price=price, lot=lot, sl=sl, tp=tp, comment=comment,
                        magic=magic, retries_left=retries_left)
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
