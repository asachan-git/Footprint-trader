"""Compute options-specific signals from option chain data.

PCR, OI change analysis, IV rank, max pain — all derived from the raw chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class OptionsSignal:
    # PCR
    pcr_oi: float           # >1.2 bullish (more put OI = put sellers = support), <0.8 bearish
    pcr_volume: float

    # ATM data
    atm_strike: float
    atm_ce_oi: int
    atm_pe_oi: int
    atm_ce_oi_change: int   # positive = OI buildup (new shorts at strike), negative = unwinding
    atm_pe_oi_change: int
    atm_ce_iv: float
    atm_pe_iv: float
    atm_ce_ltp: float
    atm_pe_ltp: float

    # Aggregate OI
    total_ce_oi: int
    total_pe_oi: int

    # Max pain: underlying price at which writers retain maximum premium
    max_pain: float

    # IV rank 0–100 (current IV vs 52-week range; 50 = median)
    iv_rank: float

    # Derived bias
    bias: str  # "bullish" | "bearish" | "neutral"

    # OI wall levels (strikes with highest CE/PE OI = resistance/support)
    max_ce_oi_strike: float  # highest call OI = resistance
    max_pe_oi_strike: float  # highest put OI = support


def compute(
    chain: list[dict[str, Any]],
    underlying_ltp: float,
    iv_52w_high: float = 0.0,
    iv_52w_low: float = 0.0,
) -> OptionsSignal:
    """Compute signals from parsed option chain."""
    if not chain:
        raise ValueError("Empty chain")

    # ATM: nearest strike to underlying
    atm = min(chain, key=lambda x: abs(x.get("strike", float("inf")) - underlying_ltp))

    # Aggregates
    total_ce_oi = sum(s.get("ce", {}).get("oi", 0) for s in chain)
    total_pe_oi = sum(s.get("pe", {}).get("oi", 0) for s in chain)
    total_ce_vol = sum(s.get("ce", {}).get("volume", 0) for s in chain)
    total_pe_vol = sum(s.get("pe", {}).get("volume", 0) for s in chain)

    pcr_oi = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 1.0
    pcr_volume = round(total_pe_vol / total_ce_vol, 3) if total_ce_vol > 0 else 1.0

    # Max pain: strike that minimises total payout to option buyers
    strikes = [s["strike"] for s in chain]

    def _total_payout(price: float) -> float:
        total = 0.0
        for s in chain:
            k = s["strike"]
            total += max(0.0, price - k) * s.get("ce", {}).get("oi", 0)
            total += max(0.0, k - price) * s.get("pe", {}).get("oi", 0)
        return total

    max_pain = min(strikes, key=_total_payout) if strikes else underlying_ltp

    # OI walls
    max_ce_row = max(chain, key=lambda s: s.get("ce", {}).get("oi", 0), default=atm)
    max_pe_row = max(chain, key=lambda s: s.get("pe", {}).get("oi", 0), default=atm)

    # IV rank
    atm_ce = atm.get("ce", {})
    atm_pe = atm.get("pe", {})
    curr_iv = (atm_ce.get("iv", 0) + atm_pe.get("iv", 0)) / 2
    if iv_52w_high > iv_52w_low > 0:
        iv_rank = round((curr_iv - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100, 1)
        iv_rank = max(0.0, min(100.0, iv_rank))
    else:
        iv_rank = 50.0  # unknown — assume median

    # Bias
    if pcr_oi >= 1.3:
        bias = "bullish"
    elif pcr_oi <= 0.7:
        bias = "bearish"
    else:
        bias = "neutral"

    return OptionsSignal(
        pcr_oi=pcr_oi,
        pcr_volume=pcr_volume,
        atm_strike=atm["strike"],
        atm_ce_oi=atm_ce.get("oi", 0),
        atm_pe_oi=atm_pe.get("oi", 0),
        atm_ce_oi_change=atm_ce.get("oi_change", 0),
        atm_pe_oi_change=atm_pe.get("oi_change", 0),
        atm_ce_iv=atm_ce.get("iv", 0.0),
        atm_pe_iv=atm_pe.get("iv", 0.0),
        atm_ce_ltp=atm_ce.get("ltp", 0.0),
        atm_pe_ltp=atm_pe.get("ltp", 0.0),
        total_ce_oi=total_ce_oi,
        total_pe_oi=total_pe_oi,
        max_pain=max_pain,
        iv_rank=iv_rank,
        bias=bias,
        max_ce_oi_strike=max_ce_row["strike"],
        max_pe_oi_strike=max_pe_row["strike"],
    )
