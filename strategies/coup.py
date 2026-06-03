"""Coup — a purely footprint absorption→continuation strategy.

Named for the mechanic: a regime change at one decisive candle. The extreme
aggressor of a high-volume / high-delta bar gets *absorbed* (buyers trapped at
the top, or sellers trapped at the bottom). The trapped side is overthrown; we
join the side that did the absorbing — the new winner — and ride it for
continuation. Unlike democracy/republic (a weighted vote panel), coup is a
single decisive event off the footprint, not a poll.

NOTE on the mechanic (validated 2026-06-02): coup was conceived as "absorption→
continuation", but an A/B on the absorption detector showed the edge comes from
the `momentum` selection (the winner side already dominant at the extreme — a
one-sided/CONTINUATION candle), NOT textbook absorption (`canonical`, where the
aggressor pushes the extreme and is rejected — that variant backtests negative).
So coup is really a high-volume CONTINUATION strategy. `absorption_mode` config
selects the variant (default momentum). See config/strategies.yaml.

Thesis (per user):
  1. TRIGGER  — high-vol + high-|delta| 15m candle, extreme aggressor absorbed.
                buyers absorbed at top  → canonical Absorption.side=="sell" → winner SHORT
                sellers absorbed at low → canonical Absorption.side=="buy"  → winner LONG
  2. CONFIRM  — wait (≤ confirm_within bars) for the winner side to aggress *with
                result*: a post bar where winner-direction delta dominates
                (|delta|/vol ≥ confirm_delta_ratio) AND it produced a result —
                a fresh winner-side stacked imbalance or price progress past the
                trigger close. Fallback: a CVD pattern in the winner direction
                with confidence ≥ confirm_cvd_conf.
  3. ENTRY    — single tactical entry at the current candle close (Phase 1).
                Structural SL just beyond the trigger candle's absorbed extreme.
  4. EXIT     — the winner side in turn getting absorbed at an extreme (the mirror
                of the trigger, against us) → cycle_manager._check_absorption_flip,
                enabled by the `coup_flip_exit` flag this strategy turns on. Plus
                the structural hard SL.

Execution policy (settings_override + adjust_plan): single leg (no grid
averaging), hard-SL exit on (so the structural stop closes the cycle), Claude
hedge-eval off, absorption-flip exit on.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from statistics import median

from llm.schema import Decision
from pipeline.types import Bar
from pipeline.state_store import store
from pipeline.footprint import build as build_fp
from pipeline.features.absorption import detect_canonical_absorption
from pipeline.features.stacked_imbalance import stacked_imbalances
from pipeline.features.atr import atr
from pipeline.features.cvd_candlestick import detect as cvd_detect

from .base import Strategy

LOG = logging.getLogger(__name__)

_CVD_LONG = {"breakout_long", "reclaim", "hidden_buying"}
_CVD_SHORT = {"breakout_short", "wick_trap", "hidden_selling"}


class Coup(Strategy):
    name = "coup"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        # de-dup: last trigger close_ts already entered on, per symbol.
        self._acted: dict[str, int] = {}
        # structural SL + 2R TP handed from decide() to adjust_plan(), per symbol.
        self._pending_sl: dict[str, float] = {}
        self._pending_tp: dict[str, float] = {}

    # ── execution policy ─────────────────────────────────────────────────────
    def settings_override(self, settings: dict) -> dict:
        """Hard-SL exit on (structural stop must close the cycle), absorption-flip
        exit on, Claude hedge-eval off (deterministic + free paper A/B)."""
        cyc = {**(settings.get("cycle") or {}),
               "hard_sl_exit": True,
               "hedge_eval_enabled": False,
               "coup_flip_exit": True}
        return {**settings, "cycle": cyc}

    def adjust_plan(self, plan, bar: Bar, settings: dict):
        """Force a single tactical entry (drop grid legs 2-N) and clamp the safety
        SL to the structural level from decide() (just beyond the trigger extreme)."""
        if not plan.legs:
            return plan
        leg1 = plan.legs[0]
        anchor = plan.anchor_price or leg1.price
        offs = plan.leg_offsets_pct[:1] if plan.leg_offsets_pct else ()
        new = replace(
            plan,
            legs=[leg1],
            avg_entry_on_full_fill=leg1.price,
            leg_offsets_pct=offs,
        )
        sl = self._pending_sl.get(bar.symbol)
        if sl is not None and anchor > 0:
            new = replace(new, safety_sl=sl, safety_sl_offset_pct=(sl - anchor) / anchor)
            LOG.info(f"[coup] {bar.symbol} {plan.side} single-leg, structural SL → {sl:.2f}")
        # Force coup's 2R TP — overrides plan_grid's VP-anchored TP so live exits
        # match the backtested 2R target (the cycle TP, not the disaster floor).
        tp = self._pending_tp.get(bar.symbol)
        if tp is not None and anchor > 0:
            new = replace(new, take_profit=tp, tp_source="coup_2R",
                          tp_offset_pct=(tp - anchor) / anchor)
            LOG.info(f"[coup] {bar.symbol} {plan.side} TP → {tp:.2f} (2R)")
        return new

    # ── signal ─────────────────────────────────────────────────────────────────
    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        cfg = self.config
        decide_tf = str(cfg.get("decide_tf") or "15m")
        lookback = int(cfg.get("lookback", 6))
        vol_mult = float(cfg.get("vol_mult", 1.8))
        delta_ratio = float(cfg.get("delta_ratio", 0.35))
        confirm_within = int(cfg.get("confirm_within", 3))
        sl_mode = str(cfg.get("sl_mode", "candle"))          # candle | swing | imbalance
        sl_buffer_pct = float(cfg.get("sl_buffer_pct", 0.10))  # × trigger range
        swing_lookback = int(cfg.get("swing_lookback", 3))
        entry_mode = str(cfg.get("entry_mode", "close"))     # close | imbalance | lvn | range
        range_pct = float(cfg.get("range_pct", 0.5))         # entry_mode=range: pullback into candle

        vol_lookback = int(cfg.get("vol_lookback", 20))   # candles for the vol median
        bars = store().recent(symbol, decide_tf, max(25, vol_lookback + 5))
        if len(bars) < min(8, vol_lookback):
            return None

        # rolling median of total bar volume over the last `vol_lookback` bars.
        totals = [t for t in (self._total_vol(b) for b in bars[-vol_lookback:]) if t > 0]
        med = median(totals) if totals else 0.0
        if med <= 0:
            return None

        # ── 1. TRIGGER — most recent high-vol/high-delta absorption in window ──
        absorption_mode = str(cfg.get("absorption_mode", "momentum"))
        # Climax guards (default no-op): skip vol spikes ≥ vol_mult_max×median and
        # rejection wicks > rej_max (reversal obs: vol≥3× and extWick≥0.5 continue
        # LESS — exhaustion). Only coup_reversal sets these.
        vol_ceiling = float(cfg.get("vol_mult_max", 1e9)) * med
        rej_max = float(cfg.get("rej_max", 1.0))
        trigger = self._find_trigger(bars, lookback, vol_mult * med, delta_ratio,
                                     absorption_mode, vol_ceiling, rej_max)
        if trigger is None:
            return None
        t_idx, winner, t_bar = trigger

        if self._acted.get(symbol) == t_bar.close_ts:
            return None  # already entered on this trigger

        # ── 2. CONFIRM — winner aggression with result, AFTER the trigger ──
        # Require ≥1 post bar: the thesis is the opposite side becoming aggressor
        # *after* the trap, so never confirm on the trigger bar itself.
        post = bars[t_idx + 1:]
        if not post or len(post) > confirm_within:
            return None  # not yet, or trigger went stale
        if not self._confirm(winner, t_bar, post, bars):
            return None

        # ── 3. ENTRY — single tactical entry; price per entry_mode ──
        # close = market at bar close; imbalance/lvn/range = limit into the trigger
        # candle (leg1 fills on price touch via cycle_manager). Falls back to close.
        entry = self._entry_price(winner, t_bar, entry_mode, range_pct, bar.ohlc.c)
        buf = max(t_bar.ohlc.h - t_bar.ohlc.l, 1e-9) * sl_buffer_pct
        sl = self._compute_sl(winner, t_bar, bars, entry, sl_mode, buf, swing_lookback)
        # Guard: an SL on/near the wrong side of entry → ~0 risk → instant stop-out,
        # blown R math (seen live: entry 67790 / SL 67788 = 2-tick stop → −1R). The
        # old clamp only enforced `buf`, which on a small trigger candle is tiny. Floor
        # the risk to a real distance = max(buf, min_sl_atr_mult × ATR) beyond entry.
        atr_val = atr(bars) or 0.0
        min_dist = max(buf, float(cfg.get("min_sl_atr_mult", 0.5)) * atr_val)
        if winner == "long":
            sl = min(sl, entry - min_dist)
            risk = entry - sl
            tp = entry + 2.0 * risk
        else:
            sl = max(sl, entry + min_dist)
            risk = sl - entry
            tp = entry - 2.0 * risk

        dr = abs(t_bar.delta or 0) / max(self._total_vol(t_bar), 1e-9)
        bias = 3 + round(2 * _clamp((dr - delta_ratio) / max(1 - delta_ratio, 1e-9), 0.0, 1.0))
        bias = int(_clamp(bias, 1, 5))

        self._acted[symbol] = t_bar.close_ts
        self._pending_sl[symbol] = sl
        self._pending_tp[symbol] = tp

        LOG.info(f"[coup] {symbol} {winner.upper()} trigger@{t_bar.ohlc.c:.2f} "
                 f"delta_ratio={dr:.2f} confirmed bias={bias} SL={sl:.2f} ({sl_mode})")
        return Decision(
            side=winner,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            confidence=_clamp(0.5 + (dr - delta_ratio), 0.0, 1.0),
            bias_strength=bias,
            rationale=(
                f"coup: {winner} continuation — trigger candle absorbed "
                f"({'buyers@top' if winner == 'short' else 'sellers@low'}) "
                f"vol≥{vol_mult}×median, |delta|/vol={dr:.2f}; winner aggression confirmed. "
                f"SL[{sl_mode}] @ {sl:.2f}."
            ),
            invalidation_note="winner side absorbed at extreme (flip), or structural SL hit",
        )

    # ── helpers ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _total_vol(b: Bar) -> float:
        fp = build_fp(b)
        return fp.total_bid + fp.total_ask

    def _find_trigger(self, bars: list[Bar], lookback: int, vol_floor: float,
                      delta_ratio: float, absorption_mode: str = "momentum",
                      vol_ceiling: float = float("inf"), rej_max: float = 1.0):
        """Return (idx, winner_side, trigger_bar) for the most recent qualifying
        trigger in the lookback window, else None."""
        scan = bars[-lookback:]
        base = len(bars) - len(scan)
        found = None
        # reversal candles are two-sided (wick + fight at the extreme) → low NET
        # |delta|; the |delta| gate (a momentum/one-sided test) would exclude them.
        # The reversal detector enforces direction itself, so skip the gate there.
        skip_delta_gate = absorption_mode == "reversal"
        for off, b in enumerate(scan):
            total = self._total_vol(b)
            if total < vol_floor or total > vol_ceiling:   # too quiet OR climax
                continue
            if not skip_delta_gate and (b.delta is None or abs(b.delta) / max(total, 1e-9) < delta_ratio):
                continue
            for a in detect_canonical_absorption(b, build_fp(b), mode=absorption_mode):
                winner = "short" if a.side == "sell" else "long"
                if rej_max < 1.0:   # skip exhaustion: rejection wick too large
                    rng = b.ohlc.h - b.ohlc.l
                    if rng > 0:
                        wick = ((min(b.ohlc.o, b.ohlc.c) - b.ohlc.l) if winner == "long"
                                else (b.ohlc.h - max(b.ohlc.o, b.ohlc.c))) / rng
                        if wick > rej_max:
                            continue
                found = (base + off, winner, b)  # keep the most recent
        return found

    def _confirm(self, winner: str, t_bar: Bar, post: list[Bar], bars: list[Bar]) -> bool:
        """Winner side aggresses *with result* after the trigger.

        Stronger than mere presence of a same-side stacked imbalance (which a
        balanced bar can carry): require a post bar where the winner side is the
        dominant aggressor (signed delta in the winner direction ≥ a fraction of
        that bar's volume) AND that aggression produced a result — a fresh
        winner-side stacked imbalance, or price progress past the trigger close.
        CVD fallback is gated on real confidence, not bare pattern membership.
        """
        cfg = self.config
        dr_min = float(cfg.get("confirm_delta_ratio", 0.25))
        cvd_conf_min = float(cfg.get("confirm_cvd_conf", 0.60))
        need_progress = bool(cfg.get("confirm_require_progress", True))
        long = winner == "long"
        target = "buy" if long else "sell"
        ref = t_bar.ohlc.c  # continuation reference

        for b in post:
            total = self._total_vol(b)
            d = b.delta or 0.0
            # aggression: winner-direction delta dominates this bar
            aggressive = (d > 0 if long else d < 0) and abs(d) / max(total, 1e-9) >= dr_min
            if not aggressive:
                continue
            # result: fresh winner-side stacked imbalance, or price progress
            has_zone = any(z.side == target for z in stacked_imbalances(build_fp(b), min_stack=3))
            progressed = (b.ohlc.c > ref) if long else (b.ohlc.c < ref)
            if has_zone or (not need_progress) or progressed:
                return True

        # CVD fallback — require real confidence, matching side
        sig = cvd_detect(bars[-20:])
        if sig.confidence >= cvd_conf_min:
            if long and sig.pattern in _CVD_LONG:
                return True
            if not long and sig.pattern in _CVD_SHORT:
                return True
        return False

    # ── entry variants (A/B which fill is best) ──────────────────────────────────
    def _entry_price(self, winner: str, t_bar: Bar, mode: str, range_pct: float,
                     fallback: float) -> float:
        """Entry level on the trigger candle. Falls back to `close` (market).
          close     — current bar close (market)
          imbalance — winner-side stacked-imbalance zone edge (limit into the zone)
          lvn       — low-volume node between the absorbed extreme and POC (vacuum)
          range     — range_pct retrace into the trigger candle (0.5 = midpoint)
        """
        if mode == "imbalance":
            lvl = self._imbalance_edge(winner, t_bar, "near")
            return lvl if lvl is not None else fallback
        if mode == "lvn":
            lvl = self._lvn_level(winner, build_fp(t_bar))
            return lvl if lvl is not None else fallback
        if mode == "range":
            lo, hi = t_bar.ohlc.l, t_bar.ohlc.h
            rng = max(hi - lo, 1e-9)
            return (lo + range_pct * rng) if winner == "long" else (hi - range_pct * rng)
        return fallback  # close

    @staticmethod
    def _lvn_level(winner: str, fp):
        """Low-volume node between the absorbed extreme and POC — the vacuum price
        tends to retrace into."""
        if not fp.cells:
            return None
        poc = fp.poc_price
        if poc is None:
            return None
        region = [c for c in fp.cells if (c.price <= poc if winner == "long" else c.price >= poc)]
        region = [c for c in region if c.total > 0]
        if len(region) < 2:
            return None
        return min(region, key=lambda c: c.total).price

    # ── SL variants (A/B which protects best) ───────────────────────────────────
    def _compute_sl(self, winner: str, t_bar: Bar, bars: list[Bar], entry: float,
                    mode: str, buf: float, swing_lb: int) -> float:
        """Structural stop. Variants fall back to `candle` if their level is absent.
          candle    — beyond the trigger candle's absorbed extreme
          swing     — beyond the nearest swing low/high (wave.detect_swing_points)
          imbalance — beyond the far edge of the trigger's winner-side imbalance zone
        """
        lvl = None
        if mode == "swing":
            lvl = self._swing_level(winner, bars, entry, swing_lb)
        elif mode == "imbalance":
            lvl = self._imbalance_edge(winner, t_bar, "far")
        if lvl is None:                       # candle (default + fallback)
            lvl = t_bar.ohlc.l if winner == "long" else t_bar.ohlc.h
        return (lvl - buf) if winner == "long" else (lvl + buf)

    @staticmethod
    def _swing_level(winner: str, bars: list[Bar], entry: float, lookback: int):
        from pipeline.features.wave import detect_swing_points
        highs, lows = detect_swing_points(bars, lookback=lookback)
        if winner == "long":
            below = [p for _, p in lows if p < entry]
            return max(below) if below else None    # nearest swing low below entry
        above = [p for _, p in highs if p > entry]
        return min(above) if above else None        # nearest swing high above entry

    @staticmethod
    def _imbalance_edge(winner: str, t_bar: Bar, edge: str):
        """Edge of the trigger candle's strongest winner-side stacked-imbalance zone.
          near — the edge facing price (limit ENTRY: buy the top of a buy-zone on a
                 pullback / sell the bottom of a sell-zone).
          far  — the edge beyond the zone (structural SL).
        """
        target = "buy" if winner == "long" else "sell"
        zones = [z for z in stacked_imbalances(build_fp(t_bar), min_stack=3)
                 if z.side == target]
        if not zones:
            return None
        z = max(zones, key=lambda z: z.count)   # strongest stack
        if winner == "long":
            return z.price_high if edge == "near" else z.price_low
        return z.price_low if edge == "near" else z.price_high


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
