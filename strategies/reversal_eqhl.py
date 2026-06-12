"""ReversalEqHL — fade a swept equal-high/low liquidity cluster back to value.

Thesis: an EqH/EqL stop cluster (two-or-more pivots within 0.15%, found by
swing.py) is resting liquidity. When price sweeps it and the grab is delta-
confirmed, the breakout/stop-run traders above (EqH) / below (EqL) are offside —
fade them back toward the value area.

  sweep of an equal_high  → liquidity grabbed above → fade SHORT
  sweep of an equal_low   → liquidity grabbed below → fade LONG

CVD divergence is OPTIONAL "sauce": a confirming divergence (bearish vs a short,
bullish vs a long) raises confidence + bias, but is not required to trigger.

TP = the OPPOSITE value-area extreme (cached daily VP): short → VAL (POC magnet
fallback), long → VAH. SL = just beyond the sweep wick extreme, ATR-floored.
Reuses Coup's plumbing (single tactical entry, structural SL clamp, hard-SL +
CVD-divergence exits) — only `decide` differs. Own data/strategies/reversal_eqhl/.

NOTE: depends on the sweep registry's EqH/EqL classification, which only fires
correctly after the 2026-06-05 detect/tick reclaim fix (age_bars=-1).
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

from .coup import Coup, _clamp

_TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

LOG = logging.getLogger(__name__)

_EQ_LABELS = {"equal_high", "equal_low"}
_TRAP_CLASS = {"reversal", "liquidity_grab", ""}   # "" = just-fired, still pending


class ReversalEqHL(Coup):
    name = "reversal_eqhl"

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("decide_tf", "15m")
        cfg.setdefault("max_sweep_age", 3)        # bars since the grab — must be fresh
        cfg.setdefault("require_reclaim", False)  # if True, only fade swept+reclaimed
        cfg.setdefault("sl_buf_atr", 0.10)
        cfg.setdefault("min_sl_atr_mult", 0.5)
        cfg.setdefault("max_rr", 4.0)             # cap TP distance (stale/wide VA guard)
        cfg.setdefault("cvd_div_window", 2)       # accept a remembered div from last N bars
        cfg.setdefault("cvd_div_strength", 0.0)   # min div strength for the sauce (0 = any)
        cfg.setdefault("cvd_divergence_exit", True)
        super().__init__(cfg)
        self._acted_key: dict[str, str] = {}      # symbol → last swept-cluster acted

    @staticmethod
    def _leg_significant(level: float, is_high: bool, sw_highs: list[float],
                         sw_lows: list[float], min_range: float) -> bool:
        """A swing level is significant if its leg to the nearest opposite pivot
        ≥ min_range (filters tiny bumps). No opposite pivot on record → pass.
        Shared by reversal_eqhl + reversal_hvn."""
        if min_range <= 0:
            return True
        if is_high:
            below = [p for p in sw_lows if p < level]
            return (level - max(below)) >= min_range if below else True
        above = [p for p in sw_highs if p > level]
        return (min(above) - level) >= min_range if above else True

    def _p(self, symbol: str, key: str, default):
        """Per-symbol config override: config.per_symbol[symbol][key] → config[key] →
        default. Lets XAUT run tighter gates than BTC on the same strategy."""
        cfg = self.config
        ps = (cfg.get("per_symbol") or {}).get(symbol) or {}
        return ps.get(key, cfg.get(key, default))

    # ── value-area fade TP (cached daily VP) ────────────────────────────────────
    def _vp_fade_tp(self, symbol: str, side: str, entry: float, risk_floor: float,
                    risk: float, max_rr: float):
        """Fade target back to value. POC (the value magnet) is preferred over the
        opposite VA extreme, which off the cached-daily VP is often stale/very far
        (the VP-fragmentation problem) → 10R+ targets that never fill. Falls back to
        the extreme only if POC isn't beyond entry. Capped at max_rr × risk so a
        stale/wide VA can't manufacture a starvation target. Returns (price, source)
        or (None, None)."""
        vp = vp_get(symbol, "daily") or {}
        poc, vah, val = vp.get("poc"), vp.get("vah"), vp.get("val")
        extreme = val if side == "short" else vah
        cap = (entry - max_rr * risk) if side == "short" else (entry + max_rr * risk)
        for src, lvl in (("poc", poc), ("va_extreme", extreme)):   # magnet first, extreme as backup
            if lvl is None:
                continue
            beyond = (lvl < entry - risk_floor) if side == "short" else (lvl > entry + risk_floor)
            if not beyond:
                continue
            # clamp to the RR cap (keeps the level's direction, bounds the distance)
            tp = max(lvl, cap) if side == "short" else min(lvl, cap)
            capped = "_capped" if tp != lvl else ""
            return float(tp), src + capped
        return None, None

    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        from pipeline.features.sweep import active_sweeps
        cfg = self.config
        decide_tf = str(cfg.get("decide_tf") or "15m")
        bars = store().recent(symbol, decide_tf, 60)
        if len(bars) < 20:
            return None

        # ── 1. fresh, delta-confirmed EqH/EqL sweep at a SIGNIFICANT, NEAR-price level ──
        # The sweep registry fires on every minor eq-cluster (swing.py: small lookback
        # + 0.15% tol), so without gating the strategy triggers on almost every candle.
        # Use the swing H/L list we already maintain (swing.get → cvd_swing_highs/lows)
        # and keep only swept levels that (a) have a real SWING RANGE — the leg to the
        # nearest opposite pivot ≥ min_swing_range_atr×ATR (filters tiny bumps) — and
        # (b) are CLOSE to current price.
        from pipeline.features.swing import get as swing_get, build as swing_build
        max_age = int(cfg.get("max_sweep_age", 3))
        require_reclaim = bool(cfg.get("require_reclaim", False))
        atr_val = atr(bars) or 0.0
        prox = float(self._p(symbol, "proximity_atr", 2.0)) * atr_val
        min_range = float(self._p(symbol, "min_swing_range_atr", 2.5)) * atr_val
        sp = swing_get(symbol) or swing_build(symbol, decide_tf, bars)
        sw_highs = sorted(sp.cvd_swing_highs or [])
        sw_lows = sorted(sp.cvd_swing_lows or [])

        def _has_range(level: float, is_high: bool) -> bool:
            return self._leg_significant(level, is_high, sw_highs, sw_lows, min_range)

        cands = []
        for sw in active_sweeps(symbol):
            if sw.level_label not in _EQ_LABELS or sw.stale or not sw.delta_confirms:
                continue
            if sw.age_bars > max_age:
                continue
            if sw.classification not in _TRAP_CLASS:
                continue
            if require_reclaim and sw.pattern != "sweep_reclaim":
                continue
            # proximity: only fade levels near current price (skip stale/far clusters)
            if atr_val > 0 and prox > 0 and abs(sw.swept_level - bar.ohlc.c) > prox:
                continue
            # significance: the swept swing level must span a real range
            if not _has_range(sw.swept_level, sw.sweep_type == "sweep_high"):
                continue
            cands.append(sw)
        if not cands:
            return None
        sw = max(cands, key=lambda s: s.confidence)   # strongest cluster grab

        side = "short" if sw.sweep_type == "sweep_high" else "long"
        key = f"{sw.sweep_type}|{round(sw.swept_level, 2)}"
        if self._acted_key.get(symbol) == key:
            return None   # already faded this cluster

        # ── 2. SL beyond the sweep wick extreme, ATR-floored ──
        buf = float(cfg.get("sl_buf_atr", 0.10)) * atr_val
        min_dist = max(buf, float(cfg.get("min_sl_atr_mult", 0.5)) * atr_val, 1e-9)
        entry = float(bar.ohlc.c)
        if side == "short":
            sl = max(sw.wick_extreme + buf, entry + min_dist)
            risk = sl - entry
        else:
            sl = min(sw.wick_extreme - buf, entry - min_dist)
            risk = entry - sl
        if risk <= 0:
            return None

        # ── 3. TP = POC value-magnet (opposite VA extreme backup), RR-clamped; else 2R ──
        # Clamp to [min_rr, max_rr]×risk: max guards a stale/wide VA (10R+ starvation),
        # min guards a magnet sitting right on entry (sub-1R negative-EV scalp).
        max_rr = float(cfg.get("max_rr", 4.0))
        min_rr = float(cfg.get("min_rr", 1.0))
        tp, tp_src = self._vp_fade_tp(symbol, side, entry, min_dist, risk, max_rr)
        if tp is not None and abs(tp - entry) < min_rr * risk:
            tp, tp_src = None, None      # magnet too close → use the 2R fallback below
        if tp is None:
            tp = entry - 2.0 * risk if side == "short" else entry + 2.0 * risk
            tp_src = "2R_fallback"

        # ── 4. CVD divergence — optional confidence/bias boost ──
        # CVD divergence sauce — shared remembered-div cache (same source as
        # reversal_hvn): an aligned div within cvd_div_window bars boosts conf/bias.
        conf = _clamp(0.40 + (sw.confidence - 0.6), 0.0, 0.85)
        bias = 3
        cdv.record_from_scan(symbol, scan_divergences(bars[-120:], lookback=3, include_live=True))
        tf_sec = _TF_SEC.get(decide_tf, 900)
        rec = cdv.aligned_within(symbol, side, bar.close_ts,
                                 int(cfg.get("cvd_div_window", 2)) * tf_sec,
                                 float(cfg.get("cvd_div_strength", 0.0)))
        want = "bull" if side == "long" else "bear"
        cvd_sauce = rec is not None
        if cvd_sauce:
            conf = _clamp(conf + 0.15, 0.0, 0.95)
            bias = 4

        self._acted_key[symbol] = key
        self._acted[symbol] = bar.close_ts
        self._pending_sl[symbol] = sl
        self._pending_tp[symbol] = tp

        LOG.info(f"[reversal_eqhl] {symbol} {side.upper()} fade swept {sw.level_label}"
                 f"@{sw.swept_level:.2f} (age={sw.age_bars} {sw.classification or 'pending'}"
                 f"{'/reclaim' if sw.pattern=='sweep_reclaim' else ''}) entry@{entry:.2f} "
                 f"SL={sl:.2f} TP={tp:.2f}({tp_src}) cvd_sauce={cvd_sauce} bias={bias}")
        return Decision(
            side=side, entry=entry, stop_loss=sl, take_profit=tp,
            confidence=conf, bias_strength=bias,
            rationale=(
                f"reversal_eqhl: {side} fade of a swept {sw.level_label} liquidity "
                f"cluster @{sw.swept_level:.2f} (grab age={sw.age_bars}, "
                f"{sw.classification or 'pending'}); trapped breakout/stop-run traders "
                f"faded back to value. TP[{tp_src}] {tp:.2f} (opposite VA extreme), "
                f"SL beyond sweep wick {sl:.2f}."
                + (f" +CVD {want} confirms (sauce)." if cvd_sauce else "")
            ),
            invalidation_note="price runs back through the sweep wick extreme (SL), or CVD flips against",
        )
