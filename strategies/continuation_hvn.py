"""ContinuationHVN — footprint trend-CONTINUATION off a high-delta ignition bar,
gated by windowed CVD agreement, riding from HVN to HVN.

The CONTINUATION half of the HVN/LVN model (reversal_hvn is the fade half). Thesis
validated offline 2026-06-04 (scripts/validate_continuation.py), re-confirmed on
repaired real-tick footprint 2026-06-16:

  TRIGGER — the just-closed bar is HIGH-VOLUME (total vol ≥ vol_mult × rolling
            median) and HIGH-DELTA (|delta|/vol ≥ delta_ratio): a one-sided,
            order-flow-decisive push. Side = sign of the bar's delta.
  GATE    — windowed CVD AGREES with the bar's delta (Σ delta over the last
            `cvd_window` bars has the same sign). Order flow corroborates the push;
            a high-delta bar against the running CVD is a fade trap, not a trend.
  ENTRY   — MARKET at the bar close. (Validated: market > pullback — runners don't
            retrace; limit/pullback entries adverse-select. See [[project_sl_ab]].)
  TARGET  — the next HVN beyond entry in the trade direction, ≥ min_hvn_dist × ATR
            away (HVN→HVN move). HVN is where price transacts fair value and
            decelerates; LVN is the vacuum it travels THROUGH to get there. No HVN
            target ahead → no trade (the edge is specifically HVN→HVN).
  STOP    — structural, `stop_mode` selects the mechanic (live A/B):
              hvn (default) — just beyond the nearest HVN AGAINST the trade
                              (HVN-support that should hold the continuation).
                              Validated form: +0.318R / 23% WR.
              lvn           — just beyond the nearest LVN against the trade. On
                              repaired data this scored higher (+0.705R / 40% WR);
                              under test vs the documented HVN form.
              atr           — atr_stop_mult × ATR (control).
            ATR-floored either way (min_sl_atr_mult) so a hugging level can't make a
            ~0-risk stop. No structural level for the chosen mode → ATR fallback.

Execution reuses Coup: single tactical leg (adjust_plan drops grid legs), hard-SL
exit on, Claude hedge-eval off, optional CVD-divergence exit. Own per-instance store.
PROVISIONAL — paper A/B first.
"""
from __future__ import annotations

import logging
from statistics import median

from llm.schema import Decision
from pipeline.types import Bar
from pipeline.state_store import store
from pipeline.footprint import build as build_fp
from pipeline.features.atr import atr
from pipeline.features.vp_cache import get as vp_get

from .coup import Coup, _clamp

LOG = logging.getLogger(__name__)

# ~24h trailing VP window per decide-TF (matches reversal_hvn).
_VP_WIN = {"15m": 96, "5m": 288, "1m": 1440}


class ContinuationHVN(Coup):
    name = "continuation_hvn"

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("symbols", ["BTCUSDT", "XAUTUSDT"])
        cfg.setdefault("decide_tf", "15m")
        cfg.setdefault("vol_lookback", 20)        # bars for vol median + CVD window
        cfg.setdefault("cvd_window", 20)          # bars summed for the CVD gate
        cfg.setdefault("vol_mult", 1.5)           # vol ≥ vol_mult × median = high-vol
        cfg.setdefault("delta_ratio", 0.35)       # |delta|/vol ≥ this = high-delta
        cfg.setdefault("min_hvn_dist", 1.0)       # HVN target ≥ this × ATR beyond entry
        cfg.setdefault("stop_mode", "hvn")        # hvn | lvn | atr
        cfg.setdefault("sl_buf_atr", 0.10)        # buffer beyond the structural level
        cfg.setdefault("min_sl_atr_mult", 0.5)    # risk floor
        cfg.setdefault("atr_stop_mult", 1.5)      # stop_mode=atr / fallback
        cfg.setdefault("flip_exit", False)        # not an absorption strategy
        cfg.setdefault("cvd_divergence_exit", False)  # continuation rides; OFF (see wave_fib)
        super().__init__(cfg)

    # ── HVN / LVN level source ───────────────────────────────────────────────────
    def _zones(self, symbol: str, decide_tf: str, bars: list[Bar]):
        """Rolling-VP (hvn_zones, lvn_zones); fall back to cached-daily HVN. The
        cached daily has no LVN bands in this path, so lvn falls back to []."""
        win = _VP_WIN.get(decide_tf, 96)
        if len(bars) >= win:
            try:
                from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE
                vp = vp_compute(bars[-win:], "daily", bars[-1].ohlc.c,
                                bin_size=DEFAULT_BIN_SIZE.get(symbol))
                if vp.hvn_zones:
                    return vp.hvn_zones, (vp.lvn_zones or []), "rolling"
            except Exception as e:
                LOG.debug(f"[{self.name}] rolling VP failed: {e}")
        daily = (vp_get(symbol, "daily") or {}).get("hvn_zones") or []
        return daily, [], "daily"

    @staticmethod
    def _mids(zones) -> list[float]:
        return sorted((float(z["low"]) + float(z["high"])) / 2 for z in zones)

    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        cfg = self.config
        decide_tf = str(cfg.get("decide_tf") or "15m")
        vol_lb = int(cfg.get("vol_lookback", 20))
        cvd_w = int(cfg.get("cvd_window", 20))
        win = _VP_WIN.get(decide_tf, 96)

        need = max(win + 30, vol_lb + 5, cvd_w + 5)
        bars = store().recent(symbol, decide_tf, need)
        if len(bars) < max(20, vol_lb + 2):
            return None
        last = bars[-1]
        if self._acted.get(symbol) == last.close_ts:
            return None

        # ── 1. TRIGGER — the just-closed bar is high-vol AND high-delta ──
        totals = [t for t in (self._total_vol(b) for b in bars[-vol_lb - 1:-1]) if t > 0]
        med = median(totals) if totals else 0.0
        if med <= 0:
            return None
        v = self._total_vol(last)
        d = last.delta or 0.0
        if v < float(cfg.get("vol_mult", 1.5)) * med:
            return None
        delta_ratio = float(cfg.get("delta_ratio", 0.35))
        if abs(d) / max(v, 1e-9) < delta_ratio:
            return None
        side = "long" if d > 0 else "short"

        # ── 2. GATE — windowed CVD agrees with the bar's delta ──
        cvd = sum((b.delta or 0.0) for b in bars[-cvd_w:])
        if (cvd > 0) != (d > 0):
            return None

        atr_val = atr(bars) or 0.0
        if atr_val <= 0:
            return None
        entry = float(last.ohlc.c)

        # ── 3. TARGET — next HVN beyond entry in the trade direction (HVN→HVN) ──
        hvn_zones, lvn_zones, src = self._zones(symbol, decide_tf, bars)
        hvn = self._mids(hvn_zones)
        min_dist_hvn = float(cfg.get("min_hvn_dist", 1.0)) * atr_val
        if side == "long":
            tcand = [h for h in hvn if h >= entry + min_dist_hvn]
            target = min(tcand) if tcand else None
        else:
            tcand = [h for h in hvn if h <= entry - min_dist_hvn]
            target = max(tcand) if tcand else None
        if target is None:
            return None   # the edge is specifically HVN→HVN — no target, no trade

        # ── 4. STOP — structural per stop_mode, ATR-floored ──
        buf = float(cfg.get("sl_buf_atr", 0.10)) * atr_val
        min_dist = max(buf, float(cfg.get("min_sl_atr_mult", 0.5)) * atr_val, 1e-9)
        stop_mode = str(cfg.get("stop_mode", "hvn"))
        struct = None
        if stop_mode == "hvn":
            cand = [h for h in hvn if (h < entry if side == "long" else h > entry)]
            struct = (max(cand) if side == "long" else min(cand)) if cand else None
        elif stop_mode == "lvn":
            lmids = self._mids(lvn_zones)
            cand = [m for m in lmids if (m < entry if side == "long" else m > entry)]
            struct = (max(cand) if side == "long" else min(cand)) if cand else None
        # stop_mode == "atr" → struct stays None → ATR fallback below

        if struct is not None:
            sl = (struct - buf) if side == "long" else (struct + buf)
            sl_basis = stop_mode
        else:
            mult = float(cfg.get("atr_stop_mult", 1.5))
            sl = entry - mult * atr_val if side == "long" else entry + mult * atr_val
            sl_basis = "atr" if stop_mode == "atr" else f"{stop_mode}->atr"

        # ATR floor — never let a hugging level make a ~0-risk stop.
        if side == "long":
            sl = min(sl, entry - min_dist)
            risk = entry - sl
        else:
            sl = max(sl, entry + min_dist)
            risk = sl - entry
        if risk <= 0:
            return None

        if not (sl < entry < target if side == "long" else target < entry < sl):
            return None   # geometry sanity: SL and TP on the correct sides

        tp = float(target)
        rr = abs(tp - entry) / risk
        dr = abs(d) / max(v, 1e-9)
        bias = int(_clamp(3 + round(2 * _clamp((dr - delta_ratio) / max(1 - delta_ratio, 1e-9), 0.0, 1.0)), 1, 5))

        self._acted[symbol] = last.close_ts
        self._pending_sl[symbol] = sl
        self._pending_tp[symbol] = tp

        LOG.info(f"[{self.name}] {symbol} {side.upper()} HVN→HVN @{entry:.2f} "
                 f"vol×{v / med:.1f} Δ/v={dr:.2f} CVD={cvd:+.0f} TP={tp:.2f}[{src}] "
                 f"SL={sl:.2f}[{sl_basis}] RR={rr:.2f}")
        return Decision(
            side=side, entry=entry, stop_loss=sl, take_profit=tp,
            confidence=_clamp(0.5 + (dr - delta_ratio), 0.0, 0.9),
            bias_strength=bias,
            rationale=(
                f"continuation_hvn: {side} continuation — high-vol (×{v / med:.1f} median) "
                f"high-delta (|Δ|/v {dr:.2f}) ignition bar, windowed CVD agrees "
                f"({cvd:+.0f}); MARKET entry {entry:.2f}, target the next HVN[{src}] "
                f"{tp:.2f} (HVN→HVN), SL[{sl_basis}] {sl:.2f} (RR {rr:.2f})."
            ),
            invalidation_note=(
                f"price breaks the {sl_basis} structural stop → continuation thesis dead"
            ),
        )
