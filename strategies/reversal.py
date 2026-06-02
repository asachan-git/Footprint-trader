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

    _TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("decide_tf", "15m")
        cfg.setdefault("vol_mult", 2.0)
        cfg.setdefault("delta_swing", 50.0)
        cfg.setdefault("vp_filter", True)
        cfg.setdefault("entry_mode", "lvn")        # market | lvn | imbalance
        cfg.setdefault("entry_expiry_bars", 3)     # bars a limit waits for the touch
        super().__init__(cfg)
        # symbol → pending limit signal awaiting a retrace touch
        self._pending_entry: dict[str, dict] = {}

    def settings_override(self, settings: dict) -> dict:
        """Coup's exits (hard_sl, absorption_flip, no Claude hedge) + the
        CVD-divergence-against exit. POC-trail runs unconditionally in cycle_manager."""
        s = super().settings_override(settings)
        cyc = {**(s.get("cycle") or {}),
               "cvd_divergence_exit": bool(self.config.get("cvd_divergence_exit", True)),
               "cvd_exit_conf": float(self.config.get("cvd_exit_conf", 0.65))}
        return {**s, "cycle": cyc}

    def _build_decision(self, symbol: str, m: dict, entry: float) -> Decision:
        sl, tp = float(m["sl"]), float(m["tp"])
        self._acted[symbol] = m["ts"]
        self._pending_sl[symbol] = sl
        self._pending_tp[symbol] = tp
        conf = _clamp(0.45 + (m["vol_ratio"] - float(self.config.get("vol_mult", 2.0))) * 0.05, 0.0, 0.9)
        LOG.info(f"[reversal] {symbol} {m['side'].upper()} fade @{entry:.2f} "
                 f"({m['entry_kind']}) near={m['near_level']} pos={m['vp_pos']} "
                 f"vol×{m['vol_ratio']} Δsw{m['delta_swing']} SL={sl:.2f}({m['sl_basis']}) TP={tp:.2f}")
        return Decision(
            side=m["side"], entry=entry, stop_loss=sl, take_profit=tp,
            confidence=conf, bias_strength=3,
            rationale=(
                f"reversal: {m['side']} fade of a value-area extreme — climax pivot "
                f"(vol×{m['vol_ratio']}) at {m['near_level']} ({m['vp_pos']}), next bar "
                f"flipped delta (Δswing {m['delta_swing']}) + closed the reversal way. "
                f"entry[{m['entry_kind']}] {entry:.2f}, SL[{m['sl_basis']}] {sl:.2f}, 2R TP {tp:.2f}."
            ),
            invalidation_note="structural SL (trapped-side imbalance / swing) hit, or winner-side flip",
        )

    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        cfg = self.config
        decide_tf = str(cfg.get("decide_tf") or "15m")
        vol_mult = float(cfg.get("vol_mult", 2.0))
        delta_swing = float(cfg.get("delta_swing", 50.0))
        vp_filter = bool(cfg.get("vp_filter", True))
        entry_mode = str(cfg.get("entry_mode", "lvn"))

        need = (VP_WIN.get(decide_tf, 96) if vp_filter else 30) + 30
        bars = store().recent(symbol, decide_tf, need)
        if len(bars) < 30:
            return None
        last = bars[-1]

        # ── 1. resolve a pending LIMIT signal: fill on a retrace touch, else expire ──
        pe = self._pending_entry.get(symbol)
        if pe is not None:
            if last.close_ts > pe["expiry_ts"] or self._acted.get(symbol) == pe["m"]["ts"]:
                self._pending_entry.pop(symbol, None)          # voided / already acted
            elif last.ohlc.l <= pe["level"] <= last.ohlc.h:    # touched → enter
                self._pending_entry.pop(symbol, None)
                return self._build_decision(symbol, pe["m"], pe["level"])
            else:
                return None                                    # still waiting for the touch

        # ── 2. detect a fresh flip on the just-closed bar ──
        markers = detect_reversals(bars, vol_mult=vol_mult, delta_swing=delta_swing,
                                   symbol=symbol, tf=decide_tf, vp_filter=vp_filter,
                                   entry_mode=entry_mode)
        if not markers:
            return None
        m = markers[-1]
        if m["ts"] != last.close_ts or self._acted.get(symbol) == m["ts"]:
            return None
        if abs(float(m["entry"]) - float(m["sl"])) <= 0:
            return None

        if not m.get("is_limit"):
            return self._build_decision(symbol, m, float(m["entry"]))   # market: enter now
        # LIMIT entry: wait for price to retrace into the footprint level.
        tf_sec = self._TF_SEC.get(decide_tf, 900)
        self._pending_entry[symbol] = {
            "m": m, "level": float(m["entry"]), "side": m["side"],
            "expiry_ts": m["ts"] + int(cfg.get("entry_expiry_bars", 3)) * tf_sec,
        }
        LOG.info(f"[reversal] {symbol} {m['side'].upper()} LIMIT armed @{m['entry']:.2f} "
                 f"({m['entry_kind']}) — waiting ≤{cfg.get('entry_expiry_bars',3)} bars for touch")
        return None
