"""Congress — democracy/republic vote signal, entered at the AGGRESSION-side
stacked imbalance with the stop just below it.

Distinct from senate (which stops beyond the *favourable/defensive* wall): congress
enters where the aggression ignited and risks to its base —
  LONG  → the buy stacked-imbalance (aggressive buyers). Entry at the zone START
          (base low) or a low-vol GAP inside it; SL below the zone low.
  SHORT → mirror at the sell stacked-imbalance high.
Tight ignition stop → high R:R if it runs; the inherited republic TP (VP-anchored,
conviction-scaled) supplies the target.

Entry is a LIMIT: decide() arms a pending order at the imbalance level and waits
≤ entry_expiry_bars for a retrace TOUCH (else voids) — so the fill is actually at
the base, not chased at market. Two paper instances A/B the entry level
(imb_start vs imb_lvn). PROVISIONAL — paper.
"""
from __future__ import annotations

import logging

from llm.schema import Decision
from pipeline.types import Bar
from pipeline.state_store import store
from pipeline.footprint import build as build_fp
from pipeline.features.stacked_imbalance import stacked_imbalances
from pipeline.features.atr import atr

from .republic import Republic

LOG = logging.getLogger(__name__)
_TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


class Congress(Republic):
    name = "congress"

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("entry_mode", "imb_start")    # imb_start | imb_lvn
        cfg.setdefault("entry_expiry_bars", 3)
        cfg.setdefault("min_sl_atr_mult", 0.5)
        super().__init__(cfg)
        self._pending: dict[str, dict] = {}

    # ── aggression-side stacked imbalance on the signal bar ─────────────────────
    @staticmethod
    def _agg_zone(bar: Bar, side: str):
        want = "buy" if side == "long" else "sell"
        zones = [z for z in stacked_imbalances(build_fp(bar), min_stack=3, ratio=3.0)
                 if z.side == want]
        return max(zones, key=lambda z: z.count) if zones else None

    def _entry_level(self, bar: Bar, side: str, mode: str):
        z = self._agg_zone(bar, side)
        if z is None:
            return None
        if mode == "imb_lvn":
            fp = build_fp(bar)
            cells = [c for c in fp.cells
                     if c.total > 0 and z.price_low <= c.price <= z.price_high]
            if cells:
                return min(cells, key=lambda c: c.total).price
        # imb_start = base where the aggression ignited
        return z.price_low if side == "long" else z.price_high

    # ── SL: just beyond the aggression-imbalance low/high (+ ATR floor) ─────────
    def _compute_sl(self, plan, bar: Bar, atr15: float, anchor: float) -> tuple[float, str]:
        z = self._agg_zone(bar, plan.side)
        if z is not None:
            floor = max(float(self.config.get("min_sl_atr_mult", 0.5)) * atr15, 1e-9)
            if plan.side == "long":
                return z.price_low - floor, "agg_imb_low"
            return z.price_high + floor, "agg_imb_high"
        return super()._compute_sl(plan, bar, atr15, anchor)   # republic fallback

    # ── entry: deferred LIMIT into the aggression imbalance ─────────────────────
    def decide(self, symbol: str, tf: str, bar: Bar, settings: dict) -> Decision | None:
        decide_tf = str(self.config.get("vote_tf") or "15m")
        bars = store().recent(symbol, decide_tf, 5)
        last = bars[-1] if bars else bar

        pe = self._pending.get(symbol)
        if pe is not None:
            if last.close_ts > pe["expiry_ts"]:
                self._pending.pop(symbol, None)
            elif last.ohlc.l <= pe["level"] <= last.ohlc.h:     # touched → enter
                self._pending.pop(symbol, None)
                d = pe["decision"]
                return d.model_copy(update={"entry": pe["level"]})
            else:
                return None                                     # still waiting

        base = super().decide(symbol, tf, bar, settings)        # vote → side/bias
        if base is None or base.side == "flat":
            return None
        level = self._entry_level(bar, base.side, str(self.config.get("entry_mode", "imb_start")))
        if level is None:
            return None                                          # no aggression imbalance → skip
        # only a real pullback limit (long below / short above the close)
        if (base.side == "long" and level >= bar.ohlc.c) or (base.side == "short" and level <= bar.ohlc.c):
            return None
        tf_sec = _TF_SEC.get(decide_tf, 900)
        self._pending[symbol] = {
            "level": float(level), "decision": base,
            "expiry_ts": last.close_ts + int(self.config.get("entry_expiry_bars", 3)) * tf_sec,
        }
        LOG.info(f"[{self.name}] {symbol} {base.side.upper()} LIMIT armed @{level:.2f} "
                 f"(aggression {self.config.get('entry_mode')}) — wait ≤"
                 f"{self.config.get('entry_expiry_bars',3)} bars for touch")
        return None
