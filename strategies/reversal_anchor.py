"""ReversalAnchor — defend a high-delta anchor zone, trade WITH the anchor's delta,
gated to WITH-TREND regime.

The data-validated inversion of the (refuted) sweep-fades: instead of fading, we
join the institutional flow. A high-delta anchor bar (anchor_bar: vol>2σ, |delta|>2σ,
|delta|/vol>0.3) marks a zone. When price RE-ENTERS that zone and DEFENDS it in the
anchor's delta direction (anchor_bar.test_retest=="continuation"), we trade that
direction:
    bullish anchor (price holds the low, +delta) → LONG
    bearish anchor (price holds the high, −delta) → SHORT

REGIME GATE (the big lever): take only WITH-TREND entries — trade direction must
agree with the 20-bar slope (≥ trend_t×ATR), matching direction_engine._trend_regime.
Backtest (15m, BTC+XAUT, 2R bracket): ALL PF 1.69 → with-trend PF 2.91 (+0.73R);
counter-trend loses, range marginal. allow_range adds the (marginal) range bucket.

SL beyond the defended anchor extreme (break-through = thesis dead). TP = rr_target
× risk (2R — matches the validated bracket). Reuses Coup plumbing (single entry,
structural SL clamp, hard-SL exit). PROVISIONAL — paper, IN-SAMPLE only so far.

NOTE: relies on the anchor registry populated per-bar in server/routes/ingest.py.
"""
from __future__ import annotations

import logging

from llm.schema import Decision
from pipeline.types import Bar
from pipeline.state_store import store
from pipeline.features.atr import atr
from pipeline.features import anchor_bar as AB

from .coup import Coup, _clamp

LOG = logging.getLogger(__name__)


class ReversalAnchor(Coup):
    name = "reversal_anchor"

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("decide_tf", "15m")
        cfg.setdefault("require_with_trend", True)   # the PF-2.91 gate
        cfg.setdefault("allow_range", False)         # also take range-regime (marginal)
        cfg.setdefault("regime_n", 20)
        cfg.setdefault("trend_t", 2.0)               # |slope| ≥ this×ATR = trend
        cfg.setdefault("rr_target", 2.0)             # TP = rr_target × risk
        cfg.setdefault("sl_buf_atr", 0.10)
        cfg.setdefault("min_sl_atr_mult", 0.5)
        cfg.setdefault("flip_exit", False)
        cfg.setdefault("cvd_divergence_exit", False)
        super().__init__(cfg)
        self._acted_anchor: dict[str, set] = {}      # symbol → anchor bar_ids acted

    def _regime(self, bars: list[Bar]) -> str:
        cfg = self.config
        n = int(cfg.get("regime_n", 20)); t = float(cfg.get("trend_t", 2.0))
        if len(bars) < n + 1:
            return "range"
        a = atr(bars[-(n + 1):]) or 0.0
        if a <= 0:
            return "range"
        slope = (bars[-1].ohlc.c - bars[-(n + 1)].ohlc.c) / a
        return "trend_up" if slope >= t else ("trend_down" if slope <= -t else "range")

    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        cfg = self.config
        decide_tf = str(cfg.get("decide_tf") or "15m")
        bars = store().recent(symbol, decide_tf, max(60, int(cfg.get("regime_n", 20)) + 25))
        if len(bars) < 25:
            return None
        if self._acted.get(symbol) == bar.close_ts:
            return None
        atr_val = atr(bars) or 0.0
        if atr_val <= 0:
            return None

        regime = self._regime(bars)
        require_wt = bool(cfg.get("require_with_trend", True))
        allow_range = bool(cfg.get("allow_range", False))
        acted = self._acted_anchor.setdefault(symbol, set())

        # strongest active anchor whose zone is being defended in its delta direction
        best = None
        for anchor in AB.active_anchors(symbol, bar.ohlc.c, atr_val):
            if anchor.bar_id in acted:
                continue
            if AB.test_retest(anchor, bar).pattern != "continuation":
                continue
            side = "long" if anchor.delta_sign == "bull" else "short"
            with_trend = (side == "long" and regime == "trend_up") or \
                         (side == "short" and regime == "trend_down")
            counter = (side == "long" and regime == "trend_down") or \
                      (side == "short" and regime == "trend_up")
            # regime gate: with-trend always; range only if allow_range; never counter
            if require_wt and not with_trend and not (allow_range and regime == "range"):
                continue
            if counter:
                continue
            score = anchor.delta_z + (1.0 if with_trend else 0.0)
            if best is None or score > best[0]:
                best = (score, anchor, side, with_trend)
        if best is None:
            return None
        _, anchor, side, with_trend = best

        entry = float(bar.ohlc.c)
        buf = float(cfg.get("sl_buf_atr", 0.10)) * atr_val
        min_dist = max(buf, float(cfg.get("min_sl_atr_mult", 0.5)) * atr_val, 1e-9)
        if side == "short":
            sl = max(anchor.high + buf, entry + min_dist)
            risk = sl - entry
        else:
            sl = min(anchor.low - buf, entry - min_dist)
            risk = entry - sl
        if risk <= 0:
            return None
        rr = float(cfg.get("rr_target", 2.0))
        tp = entry + rr * risk if side == "long" else entry - rr * risk

        acted.add(anchor.bar_id)
        self._acted[symbol] = bar.close_ts
        self._pending_sl[symbol] = sl
        self._pending_tp[symbol] = tp
        LOG.info(f"[reversal_anchor] {symbol} {side.upper()} defend {anchor.delta_sign} anchor "
                 f"[{anchor.low:.2f}-{anchor.high:.2f}] regime={regime} wt={with_trend} "
                 f"entry@{entry:.2f} SL={sl:.2f} TP={tp:.2f}({rr}R)")
        return Decision(
            side=side, entry=entry, stop_loss=sl, take_profit=tp,
            confidence=_clamp(0.5 + 0.1 * (anchor.delta_z - 2.0), 0.4, 0.9),
            bias_strength=4 if with_trend else 3,
            rationale=(
                f"reversal_anchor: {side} — price re-entered a {anchor.delta_sign} high-delta "
                f"anchor zone [{anchor.low:.2f}-{anchor.high:.2f}] (delta_z={anchor.delta_z:.1f}) "
                f"and defended it in the anchor's delta direction; regime={regime} (with-trend). "
                f"SL beyond the anchor {sl:.2f}, {rr}R TP {tp:.2f}."
            ),
            invalidation_note="price closes through the anchor extreme (trapped) → SL",
        )
