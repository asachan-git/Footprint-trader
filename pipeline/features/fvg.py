"""FVG (Fair Value Gap) detection.

3-candle pattern:
  Bullish FVG: candle[i-2].high < candle[i].low  (gap up)
    → unfilled zone = (candle[i-2].high, candle[i].low)
  Bearish FVG: candle[i-2].low > candle[i].high  (gap down)
    → unfilled zone = (candle[i].high, candle[i-2].low)

An FVG is "filled" once price has traded fully through the zone.
Returned list contains only UNFILLED FVGs as of the latest bar.

Used by:
- grid_placer: TP target = nearest unfilled bullish FVG above (long) / bearish below (short)
- cycle_manager: hedge trigger uses FVG against current cycle direction as bear signal
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.types import Bar


@dataclass(frozen=True)
class FVG:
    side: Literal["bull", "bear"]
    low: float
    high: float
    formed_at_ts: int
    formed_at_bar_id: str

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    def is_filled(self, bars_after: list[Bar]) -> bool:
        """True if any subsequent bar fully traverses the FVG zone."""
        if self.side == "bull":
            return any(b.ohlc.l <= self.low for b in bars_after)
        return any(b.ohlc.h >= self.high for b in bars_after)


def detect_fvgs(bars: list[Bar], max_age_bars: int = 100) -> list[FVG]:
    """Scan bars for unfilled FVGs. `max_age_bars` caps lookback for performance.

    Returns FVGs sorted by formed_at_ts ascending. Only UNFILLED ones returned.
    """
    if len(bars) < 3:
        return []
    start = max(2, len(bars) - max_age_bars)
    found: list[FVG] = []
    for i in range(start, len(bars)):
        c0 = bars[i - 2].ohlc
        c2 = bars[i].ohlc
        # Bullish FVG: gap up between bar[i-2].h and bar[i].l
        if c0.h < c2.l:
            fvg = FVG(side="bull", low=c0.h, high=c2.l,
                      formed_at_ts=bars[i].close_ts, formed_at_bar_id=bars[i].bar_id)
            if not fvg.is_filled(bars[i + 1:]):
                found.append(fvg)
        # Bearish FVG: gap down between bar[i].h and bar[i-2].l
        elif c0.l > c2.h:
            fvg = FVG(side="bear", low=c2.h, high=c0.l,
                      formed_at_ts=bars[i].close_ts, formed_at_bar_id=bars[i].bar_id)
            if not fvg.is_filled(bars[i + 1:]):
                found.append(fvg)
    return found


def nearest_fvg_above(fvgs: list[FVG], price: float, side: Literal["bull", "bear"] | None = None) -> FVG | None:
    """Find lowest FVG with low > price (next zone above)."""
    cand = [f for f in fvgs if f.low > price and (side is None or f.side == side)]
    return min(cand, key=lambda f: f.low) if cand else None


def nearest_fvg_below(fvgs: list[FVG], price: float, side: Literal["bull", "bear"] | None = None) -> FVG | None:
    """Find highest FVG with high < price (next zone below)."""
    cand = [f for f in fvgs if f.high < price and (side is None or f.side == side)]
    return max(cand, key=lambda f: f.high) if cand else None


# ── VP Context Annotation ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class AnnotatedFVG:
    """FVG enriched with VP structural context.

    inside_lvn: FVG zone overlaps an LVN (thin liquidity) — momentum kick likely.
    above_hvn:  Nearest HVN is below the FVG — HVN acts as floor/support.
    below_hvn:  Nearest HVN is above the FVG — HVN acts as ceiling/resistance.
    hvn_zone:   The HVN zone dict if above_hvn or below_hvn.
    """
    fvg: FVG
    inside_lvn: bool
    above_hvn: dict | None      # HVN zone below this FVG (floor)
    below_hvn: dict | None      # HVN zone above this FVG (ceiling)

    @property
    def side(self) -> str:
        return self.fvg.side

    @property
    def low(self) -> float:
        return self.fvg.low

    @property
    def high(self) -> float:
        return self.fvg.high

    @property
    def mid(self) -> float:
        return self.fvg.mid

    @property
    def momentum_boost(self) -> bool:
        """True if FVG inside LVN — expects fast price movement through zone."""
        return self.inside_lvn

    @property
    def confluence_with_hvn(self) -> bool:
        """True if FVG is near an HVN that acts as structural support/resistance."""
        return self.above_hvn is not None or self.below_hvn is not None


def annotate_with_vp_context(
    fvg: FVG,
    hvn_zones: list[dict],
    lvn_zones: list[dict],
) -> AnnotatedFVG:
    """Enrich an FVG with its relationship to HVN and LVN zones.

    Args:
        fvg:       The FVG to annotate.
        hvn_zones: HVN zone list from vp_cache (each: {low, high, volume}).
        lvn_zones: LVN zone list from vp_cache (each: {low, high, volume}).
    """
    # Check overlap with any LVN
    inside_lvn = any(
        z["low"] <= fvg.mid <= z["high"]
        for z in lvn_zones
        if isinstance(z, dict) and "low" in z and "high" in z
    )

    # Find nearest HVN below FVG (floor for bullish FVG)
    above_hvn: dict | None = None
    hvns_below = [z for z in hvn_zones
                  if isinstance(z, dict) and "high" in z and z["high"] < fvg.low]
    if hvns_below:
        above_hvn = max(hvns_below, key=lambda z: z["high"])

    # Find nearest HVN above FVG (ceiling for bearish FVG)
    below_hvn: dict | None = None
    hvns_above = [z for z in hvn_zones
                  if isinstance(z, dict) and "low" in z and z["low"] > fvg.high]
    if hvns_above:
        below_hvn = min(hvns_above, key=lambda z: z["low"])

    return AnnotatedFVG(
        fvg=fvg,
        inside_lvn=inside_lvn,
        above_hvn=above_hvn,
        below_hvn=below_hvn,
    )


def annotate_all(
    fvgs: list[FVG],
    hvn_zones: list[dict],
    lvn_zones: list[dict],
) -> list[AnnotatedFVG]:
    """Annotate a list of FVGs with VP context. Returns AnnotatedFVG list."""
    return [annotate_with_vp_context(f, hvn_zones, lvn_zones) for f in fvgs]
