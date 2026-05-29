"""Anchor Bar detection — high-volume + high-delta institutional footprints.

An anchor bar = single candle where real institutional size transacted.
Price re-testing an anchor's high or low is a high-conviction setup:
  - Return toward bullish anchor low  → continuation long entry (bulls defend)
  - Break through bullish anchor low  → reversal signal (longs trapped)
  - Return toward bearish anchor high → continuation short entry (bears defend)
  - Break through bearish anchor high → reversal signal (shorts trapped)

Detection criteria (all three must pass):
  volume > 2.0 × session_avg_vol(last 60 bars)
  |delta| > 2.0 × session_avg_|delta|(last 60 bars)
  |delta| / volume > 0.30   (directional, not chop)

Storage: per-symbol, max 5 most-recent anchors per session.
Cleared at day rollover (call reset_session on session boundary).
Stale if > 4h old (240 × 1m bars / 16 × 15m bars) without a retest.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Literal

ANCHOR_VOL_Z = 2.0           # volume z-score threshold (× session avg)
ANCHOR_DELTA_Z = 2.0         # |delta| z-score threshold
ANCHOR_DIRECTIONAL_MIN = 0.30 # min |delta|/vol ratio to exclude chop
MAX_ANCHORS_PER_SESSION = 5
ANCHOR_STALE_BARS_15M = 16   # ~4h at 15m TF
ANCHOR_STALE_BARS_1M = 240   # ~4h at 1m TF
RETEST_PROXIMITY_ATR = 0.30  # anchor "active for prompt" when price within 0.3 ATR


@dataclass
class AnchorBar:
    ts: int                    # bar close timestamp
    bar_id: str
    high: float
    low: float
    close: float
    delta_sign: Literal["bull", "bear"]  # net delta direction at formation
    vol_z: float               # volume z-score at formation
    delta_z: float             # |delta| z-score at formation
    age_bars: int = 0          # bars since formation (caller increments)


@dataclass
class RetestResult:
    anchor: AnchorBar
    pattern: Literal["continuation", "reversal", "none"]
    strength: float            # 0.0–1.0
    reason: str


_sessions: dict[str, list[AnchorBar]] = {}
_lock = Lock()


# ── Detection ─────────────────────────────────────────────────────────────────

def detect(bar, recent_bars: list) -> AnchorBar | None:
    """Detect if bar qualifies as an anchor bar relative to recent session context.

    Args:
        bar:         The bar just closed (pipeline.types.Bar).
        recent_bars: Last 60 bars for computing session averages (includes bar).
    """
    if not recent_bars:
        return None

    total_vol = sum(abs(b.delta or 0) + sum(lvl.vol for lvl in b.bid_ladder) +
                    sum(lvl.vol for lvl in b.ask_ladder)
                    for b in recent_bars)
    # Use total traded vol (bid+ask ladders) per bar as "volume"
    bar_vols = [
        sum(lvl.vol for lvl in b.bid_ladder) + sum(lvl.vol for lvl in b.ask_ladder)
        for b in recent_bars
    ]
    bar_deltas = [abs(b.delta or 0) for b in recent_bars]

    n = len(bar_vols)
    avg_vol = sum(bar_vols) / n if n > 0 else 1.0
    avg_delta = sum(bar_deltas) / n if n > 0 else 1.0

    cur_vol = bar_vols[-1] if bar_vols else 0.0
    cur_delta_abs = abs(bar.delta or 0)

    if avg_vol == 0 or avg_delta == 0:
        return None

    vol_z = cur_vol / avg_vol
    delta_z = cur_delta_abs / avg_delta

    if vol_z < ANCHOR_VOL_Z:
        return None
    if delta_z < ANCHOR_DELTA_Z:
        return None
    if cur_vol == 0:
        return None
    directional = cur_delta_abs / cur_vol
    if directional < ANCHOR_DIRECTIONAL_MIN:
        return None

    sign: Literal["bull", "bear"] = "bull" if (bar.delta or 0) > 0 else "bear"
    return AnchorBar(
        ts=bar.close_ts,
        bar_id=bar.bar_id,
        high=bar.ohlc.h,
        low=bar.ohlc.l,
        close=bar.ohlc.c,
        delta_sign=sign,
        vol_z=round(vol_z, 2),
        delta_z=round(delta_z, 2),
    )


# ── Session store ──────────────────────────────────────────────────────────────

def register(symbol: str, anchor: AnchorBar) -> None:
    """Store anchor in session registry (max 5, oldest evicted)."""
    with _lock:
        anchors = _sessions.get(symbol, [])
        anchors.append(anchor)
        if len(anchors) > MAX_ANCHORS_PER_SESSION:
            anchors = anchors[-MAX_ANCHORS_PER_SESSION:]
        _sessions[symbol] = anchors


def tick(symbol: str) -> None:
    """Increment age of all anchors by 1 bar. Evict stale ones."""
    with _lock:
        anchors = _sessions.get(symbol, [])
        for a in anchors:
            a.age_bars += 1
        _sessions[symbol] = [a for a in anchors if a.age_bars <= ANCHOR_STALE_BARS_15M]


def reset_session(symbol: str) -> None:
    """Clear all anchors at session boundary."""
    with _lock:
        _sessions.pop(symbol, None)


def get_all(symbol: str) -> list[AnchorBar]:
    with _lock:
        return list(_sessions.get(symbol, []))


def active_anchors(symbol: str, current_price: float, atr: float) -> list[AnchorBar]:
    """Return anchors where price is within 0.3 ATR of anchor's [low, high] range.

    Only these are relevant for prompt building and zone collection.
    """
    margin = RETEST_PROXIMITY_ATR * atr
    result = []
    with _lock:
        for a in _sessions.get(symbol, []):
            if (a.low - margin) <= current_price <= (a.high + margin):
                result.append(a)
    return result


# ── Retest interpretation ─────────────────────────────────────────────────────

def test_retest(anchor: AnchorBar, current_bar) -> RetestResult:
    """Interpret current bar's interaction with anchor's [low, high] range.

    Returns RetestResult with pattern = "continuation" | "reversal" | "none".
    """
    c = current_bar.ohlc
    # Check if current bar's range overlaps anchor
    overlaps = c.l <= anchor.high and c.h >= anchor.low
    if not overlaps:
        return RetestResult(anchor=anchor, pattern="none", strength=0.0,
                            reason="no overlap with anchor range")

    current_delta = current_bar.delta or 0.0

    if anchor.delta_sign == "bull":
        if c.c < anchor.low:
            return RetestResult(anchor=anchor, pattern="reversal", strength=0.75,
                                reason=f"closed below bullish anchor low {anchor.low:.2f} — longs trapped")
        if c.c > anchor.low and current_delta > 0:
            return RetestResult(anchor=anchor, pattern="continuation", strength=0.80,
                                reason=f"bullish anchor {anchor.low:.2f}–{anchor.high:.2f} defended, delta={current_delta:.0f}")
    else:  # bearish anchor
        if c.c > anchor.high:
            return RetestResult(anchor=anchor, pattern="reversal", strength=0.75,
                                reason=f"closed above bearish anchor high {anchor.high:.2f} — shorts trapped")
        if c.c < anchor.high and current_delta < 0:
            return RetestResult(anchor=anchor, pattern="continuation", strength=0.80,
                                reason=f"bearish anchor {anchor.low:.2f}–{anchor.high:.2f} defended, delta={current_delta:.0f}")

    return RetestResult(anchor=anchor, pattern="none", strength=0.0,
                        reason="overlap but no clear pattern")


# ── Per-bar update (call from ingest) ─────────────────────────────────────────

def update(symbol: str, bar, recent_bars: list) -> AnchorBar | None:
    """Detect + register anchor, advance ages. Call once per bar close.

    Returns the new AnchorBar if one was detected, else None.
    """
    tick(symbol)
    anchor = detect(bar, recent_bars)
    if anchor is not None:
        register(symbol, anchor)
    return anchor
