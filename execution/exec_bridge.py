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

from execution.arm_state_store import (
    persist_arm, persist_emit, load as _load_arm_state,
)

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


# Placement failures that are TRANSIENT — the leg was rejected because of where price
# happened to be at that instant, not because the leg itself is malformed. All of them
# are worth re-attempting once the quote has moved clear; monitor_cycle's retry gate
# re-checks the live quote before re-sending, so a still-bad leg simply stays queued.
#
#   "inside freeze"  EA client-side pre-check: stop too close to market. Never reaches
#                    the broker, so the EA's own 200ms transient retry never sees it.
#   "invalid price"  MT5 retcode 10015 — e.g. a sell_stop at/above market. Observed as
#                    the SECOND failure on a leg whose freeze-retry fired after price
#                    had moved past it (FB|hvn|1m|s1 @ 4116.1912: freeze, freeze, then
#                    invalid price).
#   "invalid stops"  MT5 retcode 10016 — the attached TP sits inside the broker's
#                    minimum stop distance from entry. Retrying helps when price drifts
#                    away from the TP; it does NOT fix a structurally too-close TP, so
#                    persistent cases want a min-TP-distance guard at plan time instead.
#
# Anything else (bad volume, disabled trading, no money) is a real rejection and is left
# dropped — retrying it would just loop.
_RETRYABLE_PLACE_ERRORS = ("inside freeze", "invalid price", "invalid stops")


def _is_retryable_place_error(err) -> bool:
    e = str(err or "").lower()
    return any(k in e for k in _RETRYABLE_PLACE_ERRORS)


# ── fractal→VP→TP study (LOG-ONLY, no execution effect) ──────────────────────
_FRACTAL_LOG = _ROOT / "data" / "fractal_tp_study.jsonl"


def _emit_fractal_study(row: dict) -> None:
    try:
        _FRACTAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _FRACTAL_LOG.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), **row}) + "\n")
    except Exception:
        pass  # audit must never break execution


def _fractal_tp_study(account: str, broker_symbol: str, cyc: dict, *,
                      tf: str, magic: int, settings: dict | None) -> None:
    """On a newly-confirmed 3-candle fractal, recompute the rolling VP and record what
    this cycle's TP *would* become under the planner's cascade. Modifies nothing.

    Everything is best-effort: any missing piece returns silently rather than raising
    into the exit path. Gated by grid_levels.fractal_study_enabled.
    """
    grid_cfg = (settings.get("grid_levels") or {}) if isinstance(settings, dict) else {}
    if not bool(grid_cfg.get("fractal_study_enabled", False)):
        return
    tf = tf or str(cyc.get("armed_tf") or "")
    if not tf:
        return

    # monitor_cycle is handed the BROKER symbol; bars/VP are keyed by the ANALYSIS symbol.
    symbol_map = ((settings.get("execution") or {}).get("symbol_map") or {}) if isinstance(settings, dict) else {}
    analysis = {v: k for k, v in symbol_map.items()}.get(broker_symbol, broker_symbol)

    from pipeline.state_store import store
    from execution import fractal_tp_study as fts
    from execution.zone_triggers import _VP_WIN

    bars = store().recent(analysis, tf, _VP_WIN.get(tf, 96) + 8)
    if not bars:
        return
    if not fts.should_run(analysis, magic, int(bars[-1].close_ts)):
        return
    sp = fts.newly_confirmed_fractal(analysis, tf, bars)
    if sp is None:
        return

    q = ExecBridge.get_quote(account, broker_symbol) or {}
    row = fts.build_row(
        cycle_id=cycle_id_for(account, broker_symbol, magic, float(cyc.get("ts") or 0.0)),
        magic=magic, tf=tf, cyc=cyc, symbol=analysis, bars=bars, sp=sp,
        venue_mid=float(q.get("mid") or 0.0),
        hvn_reversion_bias=bool(grid_cfg.get("hvn_reversion_bias", True)),
        tp_mult=float(grid_cfg.get("tp_atr_mult", 2.0) or 2.0),
    )
    _emit_fractal_study(row)


# ── durable cycle-outcome log (survives restarts; the analysis ground truth) ──
# exec_emit.jsonl logs arms and exits as SEPARATE rows with no join key, and its
# in-memory cycle state dies on restart — which is why per-cycle P&L could only be
# reconstructed from broker statements. This log writes ONE row per completed cycle
# with the arm context and the exit outcome already joined, keyed by cycle_id.
_CYCLE_LOG = _ROOT / "data" / "cycle_outcomes.jsonl"


def cycle_id_for(account: str, symbol: str, magic: int, armed_ts: float) -> str:
    """Stable id for one arm→exit lifecycle. armed_ts disambiguates re-arms on the
    same magic, so consecutive cycles never collide."""
    return f"{account}:{symbol}:{int(magic)}:{int(armed_ts)}"


def _emit_cycle_outcome(cyc: dict, *, account: str, symbol: str, magic: int,
                        tf: str, exit_reason: str, **outcome) -> None:
    """One durable row per completed cycle: arm context + exit outcome, joined.

    Pulls the arm-side fields straight off the persisted cycle dict so the row is
    self-contained — no post-hoc join against exec_emit.jsonl is needed to answer
    "which setup, on which TF, from which fulcrum, exited how, for how much".
    """
    try:
        armed_ts = float(cyc.get("ts") or 0.0)
        row = {
            "cycle_id": cycle_id_for(account, symbol, magic, armed_ts),
            "account": str(account), "broker_symbol": symbol,
            "magic": int(magic), "tf": tf or cyc.get("armed_tf", ""),
            # arm context
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
            # lifecycle high-water
            "max_pos_seen": cyc.get("max_pos_seen"), "pend_seen": cyc.get("pend_seen"),
            "bias_booked": bool(cyc.get("bias_booked")),
            "bias_peak": cyc.get("bias_peak"),
            # exit outcome
            "exit_reason": exit_reason,
            **outcome,
        }
        _CYCLE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _CYCLE_LOG.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
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
OPEN_MARKET = "OPEN_MARKET"      # open a market position immediately (feed-outage hedge only)

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


# Reserved OUT-OF-BAND magic for the feed-outage protective hedge — deliberately ABOVE the
# strat range (MAGIC_BASE+100) so it never collides with a strat×TF cycle and is NOT decoded
# as one (tf_from_magic(HEDGE_MAGIC) returns ""). CLOSE_ALL magic=HEDGE_MAGIC scopes cleanly
# to just the hedge. See pipeline/feed_hedge.py.
HEDGE_MAGIC = MAGIC_BASE + 990   # 770990 — "not a cycle"


_RECLAIM_AFTER_S = 10.0   # IN_FLIGHT with no ack this long → back to PENDING
# Cycle-flatten idempotency: once a CLOSE_ALL is enqueued, suppress further exit
# evaluation until it confirms (positions→0) or this grace lapses, then re-issue.
# Must exceed _RECLAIM_AFTER_S so the queue's own re-send isn't double-stacked.
_FLATTEN_GRACE_S = 12.0
# Max re-placement attempts for a transiently-rejected leg. Freeze rejects usually clear
# on the first or second retry once price drifts; "invalid stops" (TP inside the broker's
# min distance) may never clear, so the bound stops a doomed leg re-sending every poll.
_MAX_LEG_RETRIES = 3


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
    retry_n: int = 0          # placement attempts already spent on this leg (bounded
                              # re-placement of transient rejects; not sent to the EA)

    def to_wire(self) -> dict:
        """Flat dict the EA's JSON parser consumes (only execution fields)."""
        d = {"id": self.id, "type": self.type, "symbol": self.symbol, "magic": self.magic}
        if self.type == PLACE_PENDING:
            d.update(order_type=self.order_type, price=self.price, lot=self.lot,
                     sl=self.sl, tp=self.tp, comment=self.comment)
        elif self.type in (CLOSE_SIDE, MOVE_BE):
            d.update(side=self.side, frac=self.frac, comment=self.comment)
        elif self.type == OPEN_MARKET:
            d.update(side=self.side, lot=self.lot, comment=self.comment)
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
    _venue_bars: dict[tuple, list] = {}     # (account, broker_symbol, tf) → [Bar,...] oldest-first, EA CopyRates
    # Last poll's per-magic breakdown + the (account, broker_symbol) that reported it — stashed
    # each poll so the out-of-band feed-hedge (driven by the feed_monitor thread, which only
    # knows analysis symbols) can read net exposure + resolve the account during a Binance
    # outage. Keyed by analysis_symbol so feed_hedge can look it up directly.
    _last_magics: dict[str, dict] = {}      # analysis_symbol → {account, broker_symbol, magics:[...], ts}

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
        # Crash-safe: mirror to disk so a Flask restart re-adopts this cycle instead of
        # orphaning it (no arm persistence was the reason a restart lost all live cycles).
        # A persistence failure must never block execution.
        try:
            persist_arm(account, broker_symbol, key_magic, state)
        except Exception:
            pass

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
    def active_magics(cls, account: str, broker_symbol: str) -> list[int]:
        """Magics with an ACTIVE arm record on (account, symbol). Used by the
        absent-magic reap: any of these the EA didn't report this poll is flat in MT5."""
        with cls._lock:
            return [mg for (acc, sym, mg), m in cls._last_arm.items()
                    if acc == str(account) and sym == broker_symbol and m.get("active")]

    @classmethod
    def _save_cyc(cls, account: str, symbol: str, magic: int, cyc: dict, **updates) -> None:
        """set_last_arm for a reloaded cycle dict. Strips cyc's own 'magic' before the
        spread and passes magic= explicitly — mandatory once arm state persists to disk:
        a disk-restored cyc has magic stripped from its body, so a bare **cyc spread
        would collapse the write to magic=0 and corrupt a DIFFERENT cycle's slot. And if
        cyc DOES still carry magic, passing magic= too raises TypeError on every poll
        (the monitor then never exits). This helper makes both impossible."""
        body = {k: v for k, v in cyc.items() if k != "magic"}
        body.update(updates)
        cls.set_last_arm(account, symbol, magic=int(magic), **body)

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

        # LOG-ONLY fractal→VP→TP study. Runs before any exit logic and swallows
        # everything, so it can never change execution. Self-throttled to once per
        # closed bar (this method is the ~1s poll path).
        try:
            _fractal_tp_study(account, symbol, cyc, tf=tf, magic=magic, settings=settings)
        except Exception:
            pass  # audit must never break execution

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
                cls._save_cyc(account, symbol, magic, cyc, active=False, flatten_ts=0.0)
            elif (t - fts) > _FLATTEN_GRACE_S:
                # close demonstrably didn't land (past the queue's reclaim window) → re-issue once
                cls.enqueue(account, CLOSE_ALL, symbol,
                           comment=f"FB|flatten|{cyc.get('armed_tf') or tf}|retry", magic=magic, now=t)
                cls._save_cyc(account, symbol, magic, cyc, flatten_ts=t)
            return None

        # 0.1) retry legs that failed placement with "inside freeze" (ack() stashed them —
        # a client-side EA pre-check that rejects a stop too close to market; it never
        # reaches the broker so the EA's own transient-reject retry never sees it, since
        # freeze zones don't clear in 200ms). Checked BEFORE the positions<=0 flat-return
        # below — a freshly-armed cycle with 0 fills yet (only resting pendings) is exactly
        # when freeze-rejected legs exist and most need retrying, so this can't sit after
        # that guard. Re-attempt once per poll tick, only once the CURRENT quote actually
        # clears the freeze distance — buy_n/sell_n get bumped back up on success so the
        # leg counts (and bias_trail_fill_frac's gate) reflect the full intended ladder
        # again, not the reduced one from the failure.
        retry_legs = list(cyc.get("pending_retry") or [])
        if retry_legs:
            q0 = cls.get_quote(account, symbol) or {}
            bid0 = float(q0.get("bid") or 0.0)
            ask0 = float(q0.get("ask") or 0.0)
            if bid0 > 0 and ask0 > 0:
                # Match the EA's OWN freeze check exactly (ExecPlacePending: buy vs ask,
                # sell vs bid — not mid, which sits between the two and would under-count
                # the true freeze distance, causing the EA to re-reject the retry anyway.
                min_stop = float(q0.get("stops_dist") or 0.0)   # broker min-stop distance ($)
                still_frozen: list[dict] = []
                bumped = {"buy": 0, "sell": 0}
                for leg in retry_legs:
                    price = float(leg.get("price") or 0.0)
                    order_type = leg.get("order_type")
                    clear = (order_type == "buy_stop" and price >= ask0 + min_stop) or \
                            (order_type == "sell_stop" and price <= bid0 - min_stop) or \
                            min_stop <= 0   # no freeze-distance info from quote → don't block retry forever
                    if not clear:
                        still_frozen.append(leg)
                        continue
                    # Bounded attempts. The stash now also holds "invalid stops" legs,
                    # whose TP can be structurally inside the broker's min distance — that
                    # never clears on its own, so an unbounded retry would re-send the same
                    # doomed leg every poll for the life of the cycle. Drop after
                    # _MAX_LEG_RETRIES and leave buy_n/sell_n decremented, which is the
                    # honest state: that leg is not coming back.
                    tries = int(leg.get("tries") or 0)
                    if tries >= _MAX_LEG_RETRIES:
                        continue
                    leg["tries"] = tries + 1
                    cls.enqueue(account, PLACE_PENDING, symbol, order_type=order_type,
                               price=price, lot=float(leg.get("lot") or 0.0),
                               tp=float(leg.get("tp") or 0.0), comment=leg.get("comment", ""),
                               magic=magic, now=t, retry_n=tries + 1)
                    bumped["buy" if order_type == "buy_stop" else "sell"] += 1
                if len(still_frozen) != len(retry_legs):
                    updated = dict(cyc, pending_retry=still_frozen)
                    for side in ("buy", "sell"):
                        if bumped[side]:
                            ckey = f"{side}_n"
                            updated[ckey] = int(updated.get(ckey) or 0) + bumped[side]
                    cls._save_cyc(account, symbol, magic, updated)
                    cyc = updated

        # track high-water of open positions (basis for flatten-rest) + resting pendings
        # (so a never-filled cycle can be retired); both reset per arm.
        max_seen = int(cyc.get("max_pos_seen") or 0)
        pend_seen = int(cyc.get("pend_seen") or 0)
        if positions > max_seen or pendings > pend_seen:
            max_seen = max(max_seen, positions)
            pend_seen = max(pend_seen, pendings)
            cyc["max_pos_seen"] = max_seen
            cyc["pend_seen"] = pend_seen
            cls._save_cyc(account, symbol, magic, cyc)

        if positions <= 0:
            # flat. Retire the cycle once it had something live (positions filled OR
            # pendings rested) and now has nothing open AND nothing resting — frees the
            # symbol for a new arm by either tf. The (max/pend)_seen high-water avoids
            # the placement-window race (active set before the EA reports pendings).
            if (max_seen > 0 or pend_seen > 0) and pendings == 0:
                cls._save_cyc(account, symbol, magic, cyc, active=False)
            return None

        # n for the exit gates = the LONGEST side actually laddered, not the pre-skew
        # n_per_side. _build_legs gives the favoured side n+1 legs, so a skewed cycle
        # has buy_n != sell_n and n_per_side understates the real ladder.
        #
        # 2026-07-22: this was reading n_per_side, which made full_hedge fire one leg
        # early on every skewed cycle — observed live on magic 770052 (n_per_side 2,
        # buy_n 2, sell_n 3): min(buys=2, sells=2) >= 2 and 4 >= 4 tripped the gate at
        # 4 of 5 legs, declaring "delta-neutral" while the sell ladder was still a leg
        # short. full_hedge has NO profit floor, so it cut the cycle at -521.
        # The same n is the decay divisor for net_target, where too small an n
        # overstates decay and lowers the effective target.
        n = max(int(cyc.get("buy_n") or 0), int(cyc.get("sell_n") or 0),
                int(cyc.get("n_per_side") or 0))
        tp_up = float(cyc.get("tp_up") or 0.0)
        tp_down = float(cyc.get("tp_down") or 0.0)
        q = cls.get_quote(account, symbol) or {}
        mid = float(q.get("mid") or 0.0)

        grid_cfg = (settings.get("grid_levels") or {}) if isinstance(settings, dict) else {}
        base_target = float(grid_cfg.get("cycle_net_target_usd", 0.0) or 0.0)
        decay_pct = float(grid_cfg.get("cycle_hedge_decay_pct", 33.0) or 0.0)
        min_target = float(grid_cfg.get("cycle_min_target_usd", 0.20) or 0.0)
        close_on_full_hedge = bool(grid_cfg.get("cycle_close_on_full_hedge", True))

        # 0.5) bias-side trailing book — directional profit capture, independent of the
        # net-basket exit (which a hedge leg can mask). Gate: a side has ALL its legs
        # filled (the move committed your way). Track that side's peak floating P&L; when
        # it gives back >= giveback% from the peak, BOOK half that side and move the rest
        # to breakeven (risk-free runner). Fires once per cycle (bias_booked guard).
        #
        # 2026-07-22: reverted to Jun22's PER-SIDE form (4f387b7). 8766961 had rewritten
        # this to a cycle-wide net-P&L trail; that version's own comment documented the
        # hole it introduced — if net peaks while one side wins and the OTHER reverses
        # hard after booking, no per-side trail remains to catch it. Per-side tracks the
        # committed side's own P&L, so the trail follows the directional move that
        # actually earned the profit. Restored verbatim except: (a) set_last_arm keeps the
        # explicit magic= kwarg (the cycle-wide version's fix for the magic-key collapse
        # bug — Jun22's bare **cyc would re-key the arm to magic 0), and (b) the
        # _emit_cycle_outcome audit call is retained.
        if (bool(grid_cfg.get("bias_trail_enabled", True))
                and not cyc.get("bias_booked")
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
                    # Route through _save_cyc — strips cyc's own magic and applies updates
                    # via dict.update, so neither the explicit magic= NOR any update key
                    # (e.g. bias_booked already in the restored body) can collide. Both
                    # collisions raise TypeError on every poll and stall all exits
                    # (magic-collision was the c5ed565 crash; bias_booked-collision was the
                    # same shape the persistence port re-exposed on 2026-07-24).
                    cls._save_cyc(account, symbol, magic, cyc)
                activate = float(grid_cfg.get("bias_trail_activate_usd", 5.0) or 0.0)
                giveback = float(grid_cfg.get("bias_trail_giveback_pct", 40.0) or 0.0)
                if (activate > 0 and peak >= activate
                        and side_pnl <= peak * (1.0 - giveback / 100.0)):
                    # Trail hit → COLLAPSE THE ENTIRE CYCLE (both sides + pendings +
                    # any hedge), then wait for a fresh trigger. No half-book / BE-runner:
                    # one exit trigger closes everything, matching the net_target path.
                    comment_tf = cyc.get("armed_tf") or tf
                    cls.enqueue(account, CLOSE_ALL, symbol,
                                comment=f"FB|flatten|{comment_tf}|bias_trail"[:31],
                                magic=magic, now=t)
                    cls._save_cyc(account, symbol, magic, cyc,
                                  bias_booked=True, flatten_ts=t)
                    _emit_exit_audit({"account": str(account), "broker_symbol": symbol,
                                      "tf": tf, "magic": magic, "exit_reason": "bias_book_trail",
                                      "bias": bias, "peak": round(peak, 2),
                                      "side_pnl": round(side_pnl, 2),
                                      "positions": positions, "pendings": pendings,
                                      "squeeze_ok": cyc.get("squeeze_ok"),
                                      "squeeze_rank": cyc.get("squeeze_rank")})
                    # durable per-cycle record (full collapse — cycle ends here)
                    _emit_cycle_outcome(cyc, account=str(account), symbol=symbol,
                                        magic=magic, tf=tf,
                                        exit_reason="bias_book_trail",
                                        bias=bias, peak=round(peak, 2),
                                        pnl_at_exit=round(side_pnl, 2),
                                        buys=buys, sells=sells)
                    return "bias_book_trail"   # full CLOSE_ALL; cycle collapses

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

        # MT5 order comment cap ~31 chars — "FB|flatten|15m|leg_closed_other" sits AT
        # that boundary with zero margin, so clip defensively. The full `reason` string
        # is still what goes to the audit log/exit-reason return value; only the
        # broker-visible comment is shortened.
        cls.enqueue(account, CLOSE_ALL, symbol,
                   comment=f"FB|flatten|{cyc.get('armed_tf') or tf}|{reason}"[:31], magic=magic, now=t)
        cls._save_cyc(account, symbol, magic, cyc, flatten_ts=t)
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
            persist_emit(account, symbol, int(magic), None)   # None = cleared
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

    # ── venue-native bar cache (EA CopyRates on each poll — lets a feed comparator or
    # detector check the SAME OHLC Vantage will fill against, no analysis-feed rebase
    # needed) ────────────────────────────────────────────────────────────────
    @classmethod
    def set_venue_bars(cls, account: str, symbol: str, tf: str, bars: list) -> None:
        with cls._lock:
            cls._venue_bars[(str(account), symbol, tf)] = bars

    @classmethod
    def get_venue_bars(cls, account: str, symbol: str, tf: str) -> list:
        with cls._lock:
            return list(cls._venue_bars.get((str(account), symbol, tf), []))

    @classmethod
    def set_last_magics(cls, analysis_symbol: str, account: str, broker_symbol: str,
                        magics: list, now: float | None = None) -> None:
        """Stash the latest poll's per-magic breakdown + originating account/broker, keyed by
        analysis symbol. Read by pipeline/feed_hedge.py (which runs on the feed_monitor thread
        and only knows analysis symbols) to size the hedge + resolve the account during a
        Binance-feed outage — the EA keeps polling Vantage even when Binance is down, so this
        stays fresh."""
        with cls._lock:
            cls._last_magics[analysis_symbol] = {
                "account": str(account), "broker_symbol": broker_symbol,
                "magics": list(magics or []),
                "ts": now if now is not None else time.time(),
            }

    @classmethod
    def get_last_magics(cls, analysis_symbol: str) -> dict | None:
        with cls._lock:
            v = cls._last_magics.get(analysis_symbol)
            return dict(v) if v else None

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
                now: float | None = None, retry_n: int = 0) -> Command:
        cmd = Command(
            id=uuid.uuid4().hex[:12], account=str(account), type=type, symbol=symbol,
            order_type=order_type, price=round(float(price), 5), lot=round(float(lot), 2),
            sl=round(float(sl), 5), tp=round(float(tp), 5), comment=comment,
            magic=int(magic), side=side, frac=float(frac), retry_n=int(retry_n),
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
        # MT5 comment: FB|poc|5m|b1, FB|vah|15m|s2, FB|hvn|1m|b3 … (vp_level_touch → its
        # level_type; hvn_inside_touch → "hvn"; else the trigger kind). TF disambiguates
        # magic in the MT5 history view without cross-referencing the magic number — a
        # symbol can run several TF cycles of the same setup in parallel (magic already
        # encodes strat×TF, but the comment is what's visible in the terminal at a glance).
        # All legs of one grid share the level+tf; b/s + index distinguish legs. (MT5
        # comment cap ~31 chars — longest case FB|vp_level|15m|b7 is 18, comfortably fits.)
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
        tf_part = f"|{tf}" if tf else ""

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
                comment=f"FB|{tag}{tf_part}|b{i + 1}", magic=magic))
        for i, leg in enumerate(getattr(plan, "sell_legs", []) or []):
            out.append(cls.enqueue(
                account, PLACE_PENDING, broker_symbol, order_type="sell_stop",
                price=leg.price, lot=leg.lot, sl=0.0, tp=sell_tp,
                comment=f"FB|{tag}{tf_part}|s{i + 1}", magic=magic))
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
        # A failed PLACE_PENDING leg (e.g. "inside freeze") never becomes a fillable
        # position, but buy_n/sell_n (set at arm time from the PLANNED leg count, before
        # any broker result is known) still counts it — so bias_trail_fill_frac's
        # filled_n >= fill_frac*total_n gate stays out of reach if enough near-market
        # legs get rejected (common: they're closest to price, most likely to freeze-
        # reject, and also the ones most likely to fill first). Decrement the arm
        # record's count for that side so the gate reflects legs that could ACTUALLY fill.
        #
        # "inside freeze" is a CLIENT-SIDE pre-check in the EA (price too close to
        # market at that instant) — it never reaches the broker, so the EA's own 200ms
        # transient-reject retry never sees it (freeze zones don't clear that fast).
        # Stash the leg's price/lot/tp/comment/order_type as a pending_retry entry so
        # monitor_cycle can re-attempt it on a later poll, once price has moved clear.
        arm_decrements: dict[tuple, dict[str, int]] = {}   # (account, symbol, magic) → {"buy":n, "sell":n}
        arm_retries: dict[tuple, list[dict]] = {}          # (account, symbol, magic) → [leg dict, ...]
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
                    if c.type == PLACE_PENDING and c.order_type in ("buy_stop", "sell_stop"):
                        side = "buy" if c.order_type == "buy_stop" else "sell"
                        key = (c.account, c.symbol, c.magic)
                        arm_decrements.setdefault(key, {"buy": 0, "sell": 0})[side] += 1
                        if _is_retryable_place_error(r.get("error")):
                            # `tries` is carried on the command so a leg that fails again
                            # after a retry keeps its count — otherwise re-stashing here
                            # would reset it to 0 every round and the bound never binds.
                            arm_retries.setdefault(key, []).append({
                                "order_type": c.order_type, "price": c.price, "lot": c.lot,
                                "tp": c.tp, "comment": c.comment,
                                "tries": int(getattr(c, "retry_n", 0) or 0),
                            })
                cls._audit("ack", c)
        for key in set(arm_decrements) | set(arm_retries):
            acct, sym, magic = key
            cyc = cls.get_last_arm(acct, sym, magic=magic)
            if not cyc:
                continue
            updated = dict(cyc)
            dec = arm_decrements.get(key, {"buy": 0, "sell": 0})
            for side in ("buy", "sell"):
                if dec[side]:
                    ckey = f"{side}_n"
                    updated[ckey] = max(0, int(updated.get(ckey) or 0) - dec[side])
            if key in arm_retries:
                updated["pending_retry"] = list(updated.get("pending_retry") or []) + arm_retries[key]
            cls._save_cyc(acct, sym, magic, updated)
        return {"done": done, "failed": failed, "unknown": unknown}

    # ── crash-safe persistence (restore live cycles after a Flask restart) ──────
    @classmethod
    def load_persisted_state(cls) -> dict:
        """Restore _last_arm and _last_emit from disk after a Flask restart. Call once
        at app startup (before the first poll) so live cycles aren't orphaned. Returns
        counts for logging. Records are loaded verbatim — active:false entries are kept
        (reconcile_from_poll reactivates any that still have live MT5 positions)."""
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
        """On each EA poll, re-adopt any magic with live positions whose _last_arm entry
        is missing or flagged inactive (orphaned/reaped across a restart), and correct
        stale buy_n/sell_n from the EA's ground-truth counts. Returns magics touched."""
        import logging
        log = logging.getLogger(__name__)
        touched: list[int] = []
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
                    # tracked — but counts can drift (partial manual close, missed reap).
                    if int(existing.get("buy_n") or 0) != ea_buys or int(existing.get("sell_n") or 0) != ea_sells:
                        cls._save_cyc(account, broker_symbol, mg, existing,
                                      buy_n=ea_buys, sell_n=ea_sells,
                                      max_pos_seen=max(int(existing.get("max_pos_seen") or 0), positions))
                        log.info(f"[exec_bridge] reconcile: corrected counts for magic {mg} "
                                 f"→ {ea_buys}/{ea_sells} on {account}/{broker_symbol}")
                    continue
                if existing and not existing.get("active"):
                    # reaped/inactive but MT5 still holds positions → reactivate with its
                    # original arm metadata so net_target/bias_trail manage it.
                    cls._save_cyc(account, broker_symbol, mg, existing, active=True,
                                  max_pos_seen=max(int(existing.get("max_pos_seen") or 0), positions))
                    touched.append(mg)
                    log.warning(f"[exec_bridge] reconcile: reactivated magic {mg} "
                                f"({positions} pos) on {account}/{broker_symbol}")
                    continue
                # no entry at all (disk record lost / never persisted) → minimal stub so
                # monitor_cycle tracks P&L and position_open gates correctly. fulcrum/tp
                # unknown, so it can only exit via net_target/full_hedge, not tp-refresh.
                tf_stub = tf_from_magic(mg) or "1m"
                stub = {
                    "active": True, "armed_tf": tf_stub,
                    "fulcrum": 0.0, "trigger_kind": "recovered",
                    "tp_up": 0.0, "tp_down": 0.0, "net_target_usd": 0.0,
                    "n_per_side": 0, "step": 0.0,
                    "buy_n": ea_buys, "sell_n": ea_sells,
                    "bias_peak": 0.0, "bias_booked": False,
                    "max_pos_seen": positions, "pend_seen": 0, "flatten_ts": 0.0,
                    "node_low": 0.0, "node_high": 0.0,
                    "squeeze_ok": False, "squeeze_rank": 1.0,
                    "ts": time.time(), "recovered": True,
                }
                cls.set_last_arm(account, broker_symbol, magic=mg, **stub)
                touched.append(mg)
                log.warning(f"[exec_bridge] reconcile: stubbed orphaned magic {mg} "
                            f"({positions} pos) on {account}/{broker_symbol}")
            except Exception:
                pass
        return touched

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
