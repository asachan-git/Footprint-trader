"""Grid mode dispatcher — two execution modes picked by regime.

MeanReversionGrid (range regime OR counter-trend exhaustion):
  Entries at VAH/VAL/HVN/POC boundaries, fade toward opposite boundary.
  Lot ladder: Fib [1,1,2,3,5] × base — adds aggressively if proven wrong.
  Max legs: 5 (range) / 3 (counter-trend exhaustion).
  TP: nearest opposite zone (POC for VAH-entry short, etc.).
  SL: 5×ATR (range) / 3×ATR (counter-trend).

TrendContinuationGrid (trend_up/trend_down regime + continuation pattern):
  Entries at Fib pullback levels (0.382, 0.5, 0.618) + HVN confluence.
  Lot ladder: [3,2,1] × base — front-loaded (biggest at shallow pullback).
  Max legs: 3.
  TP: Fib extension 1.272 (primary) / 1.618 (if participation strong).
  SL: below wave invalidation level.
  Step multiplier: 0.35×ATR (shallower than mean-rev).

Both return a GridModeResult that feeds into grid_placer.plan_grid()
(which handles leg placement, lot sizing, broker dispatch).

Usage:
    from execution.grid_modes import plan_grid
    result = plan_grid(side, regime, wave, daily_vp, bars, settings)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)

# Lot ladder constants
MEAN_REV_LOT_LADDER = (1, 1, 2, 3, 5)
TREND_LOT_LADDER    = (3, 2, 1)

# Step multipliers
MEAN_REV_STEP_MULT = 0.50
TREND_STEP_MULT    = 0.35


@dataclass
class EntryZone:
    price: float
    source: str          # "vah" | "val" | "hvn" | "fib_382" | "fib_500" | "fib_618" | "atr_step"
    strength: float      # 0.0–1.0
    lot_mult: int        # ladder multiplier for this leg


@dataclass
class GridModeResult:
    grid_mode: str               # "mean_reversion" | "trend_continuation" | "cautious"
    side: Literal["long", "short"]
    entry_zones: list[EntryZone]
    take_profit: float
    tp_source: str
    safety_sl: float
    max_legs: int
    step_mult: float
    bias_strength: int           # 1–5 passed through to grid_placer
    lot_ladder: tuple[int, ...]
    counter_trend_exhaustion: bool = False
    note: str = ""


def _no_plan(side: str, reason: str) -> GridModeResult:
    return GridModeResult(
        grid_mode="cautious", side=side,  # type: ignore[arg-type]
        entry_zones=[], take_profit=0.0, tp_source="none",
        safety_sl=0.0, max_legs=1, step_mult=MEAN_REV_STEP_MULT,
        bias_strength=1, lot_ladder=(1,), note=reason,
    )


# ── Mean Reversion ─────────────────────────────────────────────────────────────

class MeanReversionGrid:
    """Entries at VA boundaries / HVN, TP at POC / opposite boundary."""

    def plan(
        self,
        side: Literal["long", "short"],
        daily_vp: dict | None,
        atr_val: float,
        current_price: float,
        bias_strength: int = 3,
        counter_trend_exhaustion: bool = False,
    ) -> GridModeResult:
        max_legs = 3 if counter_trend_exhaustion else 5
        sl_atr_mult = 3.0 if counter_trend_exhaustion else 5.0
        bs = min(bias_strength, 2) if counter_trend_exhaustion else bias_strength

        poc   = (daily_vp or {}).get("poc")
        vah   = (daily_vp or {}).get("vah")
        val   = (daily_vp or {}).get("val")
        hvns  = (daily_vp or {}).get("hvn_zones", [])

        entry_zones: list[EntryZone] = []
        tp = current_price
        tp_source = "atr_fallback"

        if side == "long":
            # Entries: VAL (primary) → HVN near VAL → POC
            if val:
                entry_zones.append(EntryZone(val, "val", 0.90, 1))
            for hvn in sorted(hvns, key=lambda z: z.get("low", 0)):
                mid = (hvn.get("low", 0) + hvn.get("high", 0)) / 2
                if val and mid > val and (not poc or mid < poc):
                    entry_zones.append(EntryZone(mid, "hvn", 0.75, 1))
                    break
            if len(entry_zones) < max_legs and current_price - atr_val > 0:
                step = MEAN_REV_STEP_MULT * atr_val
                for i in range(1, max_legs - len(entry_zones) + 1):
                    entry_zones.append(EntryZone(
                        round(current_price - i * step, 4), f"atr_step_{i}", 0.50, 2
                    ))
            # TP: POC (primary), then VAH
            if poc:
                tp, tp_source = poc, "poc"
            elif vah:
                tp, tp_source = vah, "vah"
            else:
                tp, tp_source = current_price + atr_val * 1.5, "atr_1.5"
            sl = current_price - atr_val * sl_atr_mult

        else:  # short
            if vah:
                entry_zones.append(EntryZone(vah, "vah", 0.90, 1))
            for hvn in sorted(hvns, key=lambda z: z.get("high", 0), reverse=True):
                mid = (hvn.get("low", 0) + hvn.get("high", 0)) / 2
                if vah and mid < vah and (not poc or mid > poc):
                    entry_zones.append(EntryZone(mid, "hvn", 0.75, 1))
                    break
            if len(entry_zones) < max_legs:
                step = MEAN_REV_STEP_MULT * atr_val
                for i in range(1, max_legs - len(entry_zones) + 1):
                    entry_zones.append(EntryZone(
                        round(current_price + i * step, 4), f"atr_step_{i}", 0.50, 2
                    ))
            if poc:
                tp, tp_source = poc, "poc"
            elif val:
                tp, tp_source = val, "val"
            else:
                tp, tp_source = current_price - atr_val * 1.5, "atr_1.5"
            sl = current_price + atr_val * sl_atr_mult

        entry_zones = entry_zones[:max_legs]
        return GridModeResult(
            grid_mode="mean_reversion",
            side=side,
            entry_zones=entry_zones,
            take_profit=round(tp, 4),
            tp_source=tp_source,
            safety_sl=round(sl, 4),
            max_legs=max_legs,
            step_mult=MEAN_REV_STEP_MULT,
            bias_strength=bs,
            lot_ladder=MEAN_REV_LOT_LADDER[:max_legs],
            counter_trend_exhaustion=counter_trend_exhaustion,
            note=f"mean_rev {side} poc={poc} bs={bs} max_legs={max_legs}",
        )


# ── Trend Continuation ─────────────────────────────────────────────────────────

class TrendContinuationGrid:
    """Entries on pullbacks at Fib retracements. Front-loaded lot ladder."""

    def plan(
        self,
        side: Literal["long", "short"],
        wave,                      # WavePhase from pipeline.features.wave
        daily_vp: dict | None,
        atr_val: float,
        current_price: float,
        bias_strength: int = 3,
    ) -> GridModeResult:
        entry_zones: list[EntryZone] = []
        tp = current_price
        tp_source = "atr_fallback"
        sl = current_price

        hvns = (daily_vp or {}).get("hvn_zones", [])

        # Fib retracement entries from wave
        has_fib = (wave is not None and wave.fib_retrace and
                   wave.phase != "unknown" and wave.direction is not None)

        if has_fib and wave is not None:
            for ratio, label in ((0.382, "fib_382"), (0.500, "fib_500"), (0.618, "fib_618")):
                fib_price = wave.fib_retrace.get(ratio)
                if fib_price is None:
                    continue
                # Check if an HVN is near this Fib level (confluence boost)
                strength = 0.85
                for hvn in hvns:
                    mid = (hvn.get("low", 0) + hvn.get("high", 0)) / 2
                    if abs(mid - fib_price) < atr_val * 0.3:
                        strength = 0.95  # HVN + Fib confluence
                        break
                entry_zones.append(EntryZone(round(fib_price, 4), label, strength, TREND_LOT_LADDER[len(entry_zones)]))

            # TP: Fib extension
            fib_ext_272 = wave.fib_extend.get(1.272)
            fib_ext_618 = wave.fib_extend.get(1.618)
            if fib_ext_272:
                tp, tp_source = round(fib_ext_272, 4), "fib_1.272"
            elif side == "long":
                tp, tp_source = current_price + atr_val * 2.0, "atr_2.0"
            else:
                tp, tp_source = current_price - atr_val * 2.0, "atr_2.0"

            # SL: below/above wave invalidation
            sl = round(wave.invalidation, 4) if wave.invalidation else (
                current_price - atr_val * 3.0 if side == "long" else current_price + atr_val * 3.0
            )
        else:
            # No wave data — ATR-step fallback entries
            step = TREND_STEP_MULT * atr_val
            for i in range(3):
                offset = (i + 1) * step
                p = current_price - offset if side == "long" else current_price + offset
                entry_zones.append(EntryZone(round(p, 4), f"atr_step_{i+1}", 0.60, TREND_LOT_LADDER[i]))
            if side == "long":
                tp = current_price + atr_val * 2.0
                sl = current_price - atr_val * 3.0
            else:
                tp = current_price - atr_val * 2.0
                sl = current_price + atr_val * 3.0
            tp_source = "atr_2.0"

        # Ensure TP has minimum distance (≥ 1.5 ATR from first entry)
        if entry_zones:
            first_entry = entry_zones[0].price
            min_tp_dist = atr_val * 1.5
            if side == "long" and tp < first_entry + min_tp_dist:
                tp = round(first_entry + min_tp_dist, 4)
                tp_source = "min_dist_floor"
            elif side == "short" and tp > first_entry - min_tp_dist:
                tp = round(first_entry - min_tp_dist, 4)
                tp_source = "min_dist_floor"

        return GridModeResult(
            grid_mode="trend_continuation",
            side=side,
            entry_zones=entry_zones[:3],
            take_profit=tp,
            tp_source=tp_source,
            safety_sl=sl,
            max_legs=3,
            step_mult=TREND_STEP_MULT,
            bias_strength=bias_strength,
            lot_ladder=TREND_LOT_LADDER,
            note=f"trend_cont {side} fib={has_fib} bs={bias_strength}",
        )


# ── Dispatcher ────────────────────────────────────────────────────────────────

_mean_rev = MeanReversionGrid()
_trend_cont = TrendContinuationGrid()


def plan_grid(
    side: Literal["long", "short"],
    regime,                        # DayType from pipeline.features.day_type
    wave=None,                     # WavePhase (optional)
    daily_vp: dict | None = None,
    bars: list | None = None,
    settings: dict | None = None,
    bias_strength: int = 3,
    counter_trend_exhaustion: bool = False,
) -> GridModeResult:
    """Dispatch to the appropriate grid mode based on regime + exhaustion flag.

    Trend regime + no exhaustion → TrendContinuationGrid.
    Range regime OR counter_trend_exhaustion → MeanReversionGrid.
    Uncertain → MeanReversionGrid (cautious, bias_strength capped at 2).
    """
    if side not in ("long", "short"):
        return _no_plan(side, f"invalid side: {side}")

    try:
        from pipeline.features.atr import atr
        atr_val = atr(bars) if bars else 10.0
    except Exception:
        atr_val = 10.0

    current_price = bars[-1].ohlc.c if bars else 0.0
    regime_type = getattr(regime, "type", "uncertain") if regime else "uncertain"

    if counter_trend_exhaustion:
        return _mean_rev.plan(
            side, daily_vp, atr_val, current_price,
            bias_strength=min(bias_strength, 2),
            counter_trend_exhaustion=True,
        )

    if regime_type in ("trend_up", "trend_down"):
        regime_conf = getattr(regime, "confidence", 0.0) if regime else 0.0
        if regime_conf >= 0.60:
            return _trend_cont.plan(
                side, wave, daily_vp, atr_val, current_price,
                bias_strength=bias_strength,
            )
        # Low-confidence trend → mean-rev with reduced legs
        return _mean_rev.plan(
            side, daily_vp, atr_val, current_price,
            bias_strength=min(bias_strength, 3),
        )

    if regime_type == "range":
        return _mean_rev.plan(
            side, daily_vp, atr_val, current_price,
            bias_strength=bias_strength,
        )

    # uncertain
    return _mean_rev.plan(
        side, daily_vp, atr_val, current_price,
        bias_strength=min(bias_strength, 2),
    )
