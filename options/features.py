"""Build options context dict for Claude prompt variable suffix injection."""

from __future__ import annotations

from typing import Any

from options.signal import OptionsSignal
from options.strike_selector import StrikeCandidate


def build_options_context(
    signal: OptionsSignal,
    candidates: list[StrikeCandidate],
    underlying_ltp: float,
    symbol: str,
    expiry: str,
) -> dict[str, Any]:
    """Return a dict injected alongside footprint data in the variable suffix."""
    return {
        "instrument": symbol,
        "underlying_ltp": underlying_ltp,
        "expiry": expiry,
        "options_signal": {
            "pcr_oi": signal.pcr_oi,
            "pcr_volume": signal.pcr_volume,
            "pcr_bias": signal.bias,
            "max_pain": signal.max_pain,
            "atm_strike": signal.atm_strike,
            "atm_ce_iv": signal.atm_ce_iv,
            "atm_pe_iv": signal.atm_pe_iv,
            "atm_ce_oi": signal.atm_ce_oi,
            "atm_pe_oi": signal.atm_pe_oi,
            "atm_ce_oi_change": signal.atm_ce_oi_change,
            "atm_pe_oi_change": signal.atm_pe_oi_change,
            "atm_ce_ltp": signal.atm_ce_ltp,
            "atm_pe_ltp": signal.atm_pe_ltp,
            "total_ce_oi": signal.total_ce_oi,
            "total_pe_oi": signal.total_pe_oi,
            "iv_rank": signal.iv_rank,
            "resistance_strike": signal.max_ce_oi_strike,  # highest CE OI
            "support_strike": signal.max_pe_oi_strike,     # highest PE OI
        },
        "strike_candidates": [
            {
                "label": c.label,
                "type": c.option_type,
                "strike": c.strike,
                "security_id": c.security_id,
                "trading_symbol": c.trading_symbol,
                "ltp": c.ltp,
                "bid": c.bid,
                "ask": c.ask,
                "iv": c.iv,
                "oi": c.oi,
                "oi_change": c.oi_change,
                "delta": c.delta,
            }
            for c in candidates
        ],
    }
