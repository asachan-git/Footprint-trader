"""Reversal — the VP-gated climax-pivot → next-bar-delta-flip setup, paper only.

Derived in scripts/reversal_study.py + reversal_pattern_backtest.py. The raw
pattern (climax pivot, vol≥2×median, then a next candle that flips reversal-aligned
delta and closes the reversal way) is ~breakeven on its own. The ONE filter that
turned it PF-positive in backtest was VOLUME-PROFILE context: trade only reversals
that FADE a value-area extreme (long ≤ VAL / short ≥ VAH) and are NOT nearest a HVN
magnet → that subset + an imbalance/swing structural SL = PF ~1.50 (vs 0.95). The
edge skewed short over a down-week, so this is PROVISIONAL — it runs in PAPER to
accumulate cross-regime, out-of-sample samples (the only real de-confounder).

Reuses Coup's execution plumbing (single tactical entry, structural SL clamp, forced
2R TP, flip + hard-SL exit) via subclassing; only `decide` differs — it reads the
detector (pipeline/features/reversal_pattern.detect) for a fresh flip on the just-
closed decide_tf bar. Distinct `name` → its own data/strategies/reversal/ store.
"""
from __future__ import annotations

import logging

from llm.schema import Decision
from pipeline.types import Bar
from pipeline.state_store import store
from pipeline.features.reversal_pattern import detect as detect_reversals, VP_WIN

from .coup import Coup, _clamp

LOG = logging.getLogger(__name__)


class Reversal(Coup):
    name = "reversal"

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("decide_tf", "15m")
        cfg.setdefault("vol_mult", 2.0)
        cfg.setdefault("delta_swing", 50.0)
        cfg.setdefault("vp_filter", True)
        super().__init__(cfg)

    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        cfg = self.config
        decide_tf = str(cfg.get("decide_tf") or "15m")
        vol_mult = float(cfg.get("vol_mult", 2.0))
        delta_swing = float(cfg.get("delta_swing", 50.0))
        vp_filter = bool(cfg.get("vp_filter", True))

        # Enough history for the VP window (~24h) + the pivot/flip lookback.
        need = (VP_WIN.get(decide_tf, 96) if vp_filter else 30) + 30
        bars = store().recent(symbol, decide_tf, need)
        if len(bars) < 30:
            return None

        markers = detect_reversals(bars, vol_mult=vol_mult, delta_swing=delta_swing,
                                   symbol=symbol, tf=decide_tf, vp_filter=vp_filter)
        if not markers:
            return None
        m = markers[-1]
        # only act on a flip that printed on the just-closed decide_tf bar
        if m["ts"] != bars[-1].close_ts:
            return None
        if self._acted.get(symbol) == m["ts"]:
            return None

        side, entry, sl, tp = m["side"], float(m["entry"]), float(m["sl"]), float(m["tp"])
        risk = abs(entry - sl)
        if risk <= 0:
            return None

        self._acted[symbol] = m["ts"]
        self._pending_sl[symbol] = sl
        self._pending_tp[symbol] = tp

        conf = _clamp(0.45 + (m["vol_ratio"] - vol_mult) * 0.05, 0.0, 0.9)
        LOG.info(f"[reversal] {symbol} {side.upper()} fade @{entry:.2f} "
                 f"near={m['near_level']} pos={m['vp_pos']} vol×{m['vol_ratio']} "
                 f"Δsw{m['delta_swing']} SL={sl:.2f}({m['sl_basis']}) TP={tp:.2f}")
        return Decision(
            side=side,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            confidence=conf,
            bias_strength=3,
            rationale=(
                f"reversal: {side} fade of a value-area extreme — climax pivot "
                f"(vol×{m['vol_ratio']}) at {m['near_level']} ({m['vp_pos']}), next bar "
                f"flipped delta (Δswing {m['delta_swing']}) + closed the reversal way. "
                f"SL[{m['sl_basis']}] @ {sl:.2f}, 2R TP @ {tp:.2f}."
            ),
            invalidation_note="structural SL (trapped-side imbalance / swing) hit, or winner-side flip",
        )
