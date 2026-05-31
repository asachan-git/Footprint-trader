"""TP resolver — picks the nearest realistic profit target for a grid cycle.

Aggregates candidate levels from:
  - VP daily/weekly: poc, vah, val, naked_poc
  - SwingPoints: session high/low, prior day high/low, CVD swing pivots
  - Unfilled FVGs on the higher TF (default 15m)

Returns the nearest level beyond the anchor in the trade direction
with a minimum-distance gate (avoids placing TP inside noise).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.types import Bar


@dataclass(frozen=True)
class TPCandidate:
    price: float
    source: str  # "vp_poc_daily" | "vp_vah_weekly" | "fvg_bull_15m" | "swing_high" | ...


def _naked_poc_levels(symbol: str, anchor: float) -> list[TPCandidate]:
    """Untested prior-session POCs — high-probability draw targets."""
    out: list[TPCandidate] = []
    try:
        from pipeline.features.naked_poc import get_naked_pocs
        for n in get_naked_pocs(symbol, current_price=anchor):
            out.append(TPCandidate(price=n.price, source=f"naked_poc_{n.session}_age{n.age_sessions}"))
    except Exception:
        pass
    return out


def _vp_levels(symbol: str) -> list[TPCandidate]:
    out: list[TPCandidate] = []
    try:
        from pipeline.features.vp_cache import get as vp_get
    except Exception:
        return out
    for period in ("daily", "weekly"):
        vp = vp_get(symbol, period)
        if not vp:
            continue
        for key in ("poc", "vah", "val", "naked_poc"):
            v = vp.get(key)
            if isinstance(v, (int, float)) and v > 0:
                out.append(TPCandidate(price=float(v), source=f"vp_{key}_{period}"))
    return out


def _swing_levels(symbol: str) -> list[TPCandidate]:
    out: list[TPCandidate] = []
    try:
        from pipeline.features.swing import get as swing_get
        sp = swing_get(symbol)
        if sp is None:
            return out
        named = [
            ("session_high", sp.session_high),
            ("session_low", sp.session_low),
            ("prior_day_high", sp.prior_day_high),
            ("prior_day_low", sp.prior_day_low),
            ("vah", sp.vah),
            ("val", sp.val),
        ]
        for label, v in named:
            if isinstance(v, (int, float)):
                out.append(TPCandidate(price=float(v), source=f"swing_{label}"))
        for p in sp.cvd_swing_highs or []:
            out.append(TPCandidate(price=float(p), source="cvd_swing_high"))
        for p in sp.cvd_swing_lows or []:
            out.append(TPCandidate(price=float(p), source="cvd_swing_low"))
    except Exception:
        pass
    return out


def _fvg_levels(htf_bars: list[Bar]) -> list[TPCandidate]:
    out: list[TPCandidate] = []
    try:
        from pipeline.features.fvg import detect_fvgs
        for f in detect_fvgs(htf_bars, max_age_bars=200):
            # Long-side targets: bullish FVG zone low (entry of zone from below)
            # Short-side targets: bearish FVG zone high (entry of zone from above)
            if f.side == "bull":
                out.append(TPCandidate(price=float(f.low), source="fvg_bull_15m"))
            else:
                out.append(TPCandidate(price=float(f.high), source="fvg_bear_15m"))
    except Exception:
        pass
    return out


def resolve_tp(
    symbol: str,
    direction: Literal["long", "short"],
    anchor: float,
    htf_bars: list[Bar] | None = None,
    min_distance: float = 0.0,
) -> TPCandidate | None:
    """Find nearest TP level in the trade direction.

    `anchor` = avg_entry of the cycle (or planned entry if pre-fill).
    `min_distance` = required distance from anchor (e.g. 1.5 × ATR_15m).
    Returns None if no valid candidate.
    """
    candidates: list[TPCandidate] = []
    candidates.extend(_naked_poc_levels(symbol, anchor))
    candidates.extend(_vp_levels(symbol))
    candidates.extend(_swing_levels(symbol))
    if htf_bars:
        candidates.extend(_fvg_levels(htf_bars))

    if direction == "long":
        filt = [c for c in candidates if c.price >= anchor + min_distance]
        return min(filt, key=lambda c: c.price) if filt else None
    else:
        filt = [c for c in candidates if c.price <= anchor - min_distance]
        return max(filt, key=lambda c: c.price) if filt else None
