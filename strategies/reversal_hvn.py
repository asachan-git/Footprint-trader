"""ReversalHVN — fade a candle that sweeps a prior High/Low at an HVN extreme and
closes back INSIDE the HVN, gated by a recent (current ± N-bar) CVD divergence.

Setup (per user):
  - An HVN zone whose extreme edge coincides with a prior structural level — a
    swing high/low OR an equal-high/low cluster (both surface as swing pivots).
  - The just-closed candle WICKS past that extreme (sweeps the resting liquidity)
    but its CLOSE comes back inside the HVN band → the breakout/stop-run is
    rejected back into value.
        swept HVN TOP (close back inside)  → fade SHORT
        swept HVN LOW (close back inside)  → fade LONG
  - FILTER (required): a CVD divergence on the CURRENT candle aligned with the
    fade (bear vs a short, bull vs a long) — the live provisional marker from
    scan_divergences(include_live=True). No live div → no trigger.

HVN source: rolling N-bar VP (tracks current price), falling back to cached-daily
HVN if the rolling profile has none (the cached daily can lag price by a session).

Reuses ReversalEqHL (→ Coup): SL just beyond the sweep wick, TP = POC value-magnet
(opposite VA extreme backup) RR-clamped [min_rr, max_rr], hard-SL + CVD-divergence +
POC-trail exits. Only __init__ + decide differ. Own data/strategies/reversal_hvn/.
"""
from __future__ import annotations

import logging

from llm.schema import Decision
from pipeline.types import Bar
from pipeline.state_store import store
from pipeline.features.atr import atr
from pipeline.features.vp_cache import get as vp_get
from pipeline.features.cvd_candlestick import scan_divergences
from pipeline.features import cvd_div_state as cdv

from .coup import _clamp
from .reversal_eqhl import ReversalEqHL

LOG = logging.getLogger(__name__)

_VP_WIN = {"15m": 96, "5m": 288, "1m": 1440}   # ~24h trailing VP window
_TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


class ReversalHVN(ReversalEqHL):
    name = "reversal_hvn"

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("decide_tf", "15m")
        cfg.setdefault("swing_lookback", 3)        # ± bars for prior swing pivots
        cfg.setdefault("coincide_atr", 0.3)        # HVN edge ≈ prior level tolerance
        cfg.setdefault("cvd_div_strength", 0.0)    # min div strength (0 = any)
        cfg.setdefault("cvd_div_window", 2)        # accept a div from the last N bars
        cfg.setdefault("sl_buf_atr", 0.10)
        cfg.setdefault("min_sl_atr_mult", 0.5)
        cfg.setdefault("max_rr", 4.0)
        cfg.setdefault("min_rr", 1.0)
        cfg.setdefault("cvd_divergence_exit", True)
        super().__init__(cfg)

    def _hvn_zones(self, symbol: str, decide_tf: str, bars: list[Bar]):
        """Rolling-VP HVN zones; fall back to cached-daily HVN. Returns (zones, src)."""
        win = _VP_WIN.get(decide_tf, 96)
        if len(bars) >= win:
            try:
                from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE
                vp = vp_compute(bars[-win:], "daily", bars[-1].ohlc.c,
                                bin_size=DEFAULT_BIN_SIZE.get(symbol))
                if vp.hvn_zones:
                    return vp.hvn_zones, "rolling"
            except Exception as e:
                LOG.debug(f"[reversal_hvn] rolling VP failed: {e}")
        return (vp_get(symbol, "daily") or {}).get("hvn_zones") or [], "daily"

    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        cfg = self.config
        decide_tf = str(cfg.get("decide_tf") or "15m")
        win = _VP_WIN.get(decide_tf, 96)
        bars = store().recent(symbol, decide_tf, win + 30)
        if len(bars) < 30:
            return None
        last = bars[-1]
        if self._acted.get(symbol) == last.close_ts:
            return None

        hvn_zones, hvn_src = self._hvn_zones(symbol, decide_tf, bars)
        if not hvn_zones:
            return None

        atr_val = atr(bars) or 0.0
        if atr_val <= 0:
            return None
        tol = float(cfg.get("coincide_atr", 0.3)) * atr_val
        prox = float(self._p(symbol, "proximity_atr", 2.0)) * atr_val
        min_range = float(self._p(symbol, "min_swing_range_atr", 2.5)) * atr_val
        # Prior swing H/L from the maintained list (significant pivots), not a small
        # lookback — and require a real swing range, same selectivity as reversal_eqhl.
        from pipeline.features.swing import get as swing_get, build as swing_build
        sp = swing_get(symbol) or swing_build(symbol, decide_tf, bars)
        swing_highs = sorted(sp.cvd_swing_highs or [])
        swing_lows = sorted(sp.cvd_swing_lows or [])

        def _sig_near(extreme: float, is_high: bool) -> bool:
            """A prior swing pivot sits within tol of the HVN extreme AND spans a real
            range, AND the extreme is within proximity of current price."""
            if prox > 0 and abs(extreme - last.ohlc.c) > prox:
                return False
            pivots = swing_highs if is_high else swing_lows
            return any(abs(extreme - p) <= tol and
                       self._leg_significant(p, is_high, swing_highs, swing_lows, min_range)
                       for p in pivots)

        # ── find a swept-and-rejected HVN extreme on a significant, near prior level ──
        best = None   # (side, hvn, swept_extreme, wick_beyond)
        for z in hvn_zones:
            hi, lo = float(z["high"]), float(z["low"])
            inside = lo < last.ohlc.c < hi
            if not inside:
                continue
            # SHORT: top extreme on a prior swing high, wicked above, closed inside
            if last.ohlc.h > hi and _sig_near(hi, True):
                beyond = last.ohlc.h - hi
                if best is None or beyond > best[3]:
                    best = ("short", z, hi, beyond)
            # LONG: bottom extreme on a prior swing low, wicked below, closed inside
            if last.ohlc.l < lo and _sig_near(lo, False):
                beyond = lo - last.ohlc.l
                if best is None or beyond > best[3]:
                    best = ("long", z, lo, beyond)
        if best is None:
            return None
        side, z, swept_extreme, _ = best

        # ── FILTER: aligned CVD divergence within the last `cvd_div_window` bars ──
        # Scan refreshes the remembered last-div per symbol (always), then we accept
        # a fresh aligned one (live current candle OR up to N bars back) — not brittle
        # to the divergence landing exactly on the sweep bar.
        want = "bear" if side == "short" else "bull"
        str_gate = float(cfg.get("cvd_div_strength", 0.0))
        cdv.record_from_scan(symbol, scan_divergences(bars[-120:], lookback=3, include_live=True))
        window = int(cfg.get("cvd_div_window", 2))
        tf_sec = _TF_SEC.get(decide_tf, 900)
        live_div = cdv.aligned_within(symbol, side, last.close_ts, window * tf_sec, str_gate)
        if live_div is None:
            return None

        # ── SL beyond the sweep wick, ATR-floored ──
        buf = float(cfg.get("sl_buf_atr", 0.10)) * atr_val
        min_dist = max(buf, float(cfg.get("min_sl_atr_mult", 0.5)) * atr_val, 1e-9)
        entry = float(last.ohlc.c)
        if side == "short":
            sl = max(last.ohlc.h + buf, entry + min_dist)
            risk = sl - entry
        else:
            sl = min(last.ohlc.l - buf, entry - min_dist)
            risk = entry - sl
        if risk <= 0:
            return None

        # ── TP: POC value-magnet (opposite VA extreme backup), RR-clamped; else 2R ──
        max_rr = float(cfg.get("max_rr", 4.0))
        min_rr = float(cfg.get("min_rr", 1.0))
        tp, tp_src = self._vp_fade_tp(symbol, side, entry, min_dist, risk, max_rr)
        if tp is not None and abs(tp - entry) < min_rr * risk:
            tp, tp_src = None, None
        if tp is None:
            tp = entry - 2.0 * risk if side == "short" else entry + 2.0 * risk
            tp_src = "2R_fallback"

        conf = _clamp(0.5 + live_div["strength"], 0.0, 0.9)
        self._acted[symbol] = last.close_ts
        self._pending_sl[symbol] = sl
        self._pending_tp[symbol] = tp

        LOG.info(f"[reversal_hvn] {symbol} {side.upper()} fade swept HVN[{hvn_src}] "
                 f"{'top' if side=='short' else 'low'}@{swept_extreme:.2f} "
                 f"(close inside {z['low']:.2f}-{z['high']:.2f}) +CVD {want} "
                 f"str={live_div['strength']:.2f} entry@{entry:.2f} SL={sl:.2f} TP={tp:.2f}({tp_src})")
        return Decision(
            side=side, entry=entry, stop_loss=sl, take_profit=tp,
            confidence=conf, bias_strength=3,
            rationale=(
                f"reversal_hvn: {side} fade — candle swept a prior {'high' if side=='short' else 'low'} "
                f"at the HVN[{hvn_src}] {'top' if side=='short' else 'low'} extreme "
                f"({swept_extreme:.2f}) and closed back inside the HVN "
                f"({z['low']:.2f}-{z['high']:.2f}); CVD {want} divergence within "
                f"{int(cfg.get('cvd_div_window',2))} bars (str {live_div['strength']:.2f}) "
                f"confirms. TP[{tp_src}] {tp:.2f}, SL {sl:.2f}."
            ),
            invalidation_note="close runs back through the sweep wick extreme (SL), or CVD flips against",
        )
