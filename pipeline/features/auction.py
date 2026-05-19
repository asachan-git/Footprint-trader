"""LVN Transit / Auction Failure Detection.

Auction theory: price visiting a price level is an "auction" — the market tests
whether participants want to transact there. Two outcomes:

  FAILURE (rejection): price enters a zone, finds no acceptance, returns quickly.
    - Low volume at zone (< 40% session avg) — no one trading there
    - Bar closes back outside the zone in direction it came from
    - Signal: market REJECTED this level → reversal likely
    - Grid add fires AFTER rejection, not at first touch

  ACCEPTANCE: price enters a zone and stays.
    - High volume, wide range bars, closes deep inside zone
    - Signal: market accepting these prices → DO NOT add against move

LVN zones are the primary targets because thin volume = fast moves = clear
signal when rejected (no one wants to trade there).
"""

from __future__ import annotations

from dataclasses import dataclass

# Volume relative to session average below this = "thin" (LVN-like behaviour)
LVN_VOL_RATIO = 0.40
# Minimum bars to establish a session volume average
MIN_BARS_FOR_AVG = 10
# How many consecutive bars to track for zone dwell
DWELL_WINDOW = 3


@dataclass
class AuctionSignal:
    type: str           # "failure_long" | "failure_short" | "acceptance_long" | "acceptance_short" | "none"
    zone_low: float
    zone_high: float
    confidence: float   # 0.0 – 1.0
    bars_in_zone: int   # how many bars price spent inside the zone
    avg_vol_ratio: float  # bar volume relative to session average (< 1 = thin)
    reason: str


_NONE = AuctionSignal(
    type="none", zone_low=0.0, zone_high=0.0,
    confidence=0.0, bars_in_zone=0, avg_vol_ratio=0.0, reason="",
)


def _bar_total_volume(bar) -> float:
    """Sum bid + ask volume across all ladder levels."""
    vol = sum(lvl.vol for lvl in bar.bid_ladder) + sum(lvl.vol for lvl in bar.ask_ladder)
    # Fallback: use delta magnitude as proxy if ladder is empty
    if vol == 0 and bar.delta is not None:
        vol = abs(bar.delta)
    return vol


def _session_avg_volume(bars) -> float:
    """Rolling average bar volume across recent bars."""
    vols = [_bar_total_volume(b) for b in bars]
    vols = [v for v in vols if v > 0]
    return sum(vols) / len(vols) if vols else 0.0


def _bar_in_zone(bar, zone_low: float, zone_high: float) -> bool:
    """True if the bar's range overlaps with the zone."""
    return bar.ohlc.l <= zone_high and bar.ohlc.h >= zone_low


def _close_pct(bar) -> float:
    """Closing position within bar range: 0.0 = at low, 1.0 = at high."""
    rng = bar.ohlc.h - bar.ohlc.l
    if rng <= 0:
        return 0.5
    return (bar.ohlc.c - bar.ohlc.l) / rng


def detect(
    recent_bars,
    lvn_zones: list[dict],
    session_avg_vol: float | None = None,
) -> AuctionSignal:
    """Detect auction failure or acceptance in any LVN zone.

    Examines the last DWELL_WINDOW bars. Returns the most significant signal.

    Args:
        recent_bars:      Recent bars (ascending by close_ts). At least 3 needed.
        lvn_zones:        LVN zones from daily VP [{low, high}, ...].
        session_avg_vol:  Pre-computed session average volume (optional).
    """
    if len(recent_bars) < DWELL_WINDOW or not lvn_zones:
        return _NONE

    bars = recent_bars[-DWELL_WINDOW:]
    avg_vol = session_avg_vol or _session_avg_volume(
        recent_bars[-max(MIN_BARS_FOR_AVG, len(recent_bars)):]
    )
    if avg_vol <= 0:
        return _NONE

    best: AuctionSignal = _NONE

    for zone in lvn_zones:
        zl, zh = zone["low"], zone["high"]

        # Count how many of the DWELL bars are inside the zone
        in_zone = [b for b in bars if _bar_in_zone(b, zl, zh)]
        if not in_zone:
            continue

        zone_vols = [_bar_total_volume(b) for b in in_zone]
        avg_zone_vol = sum(zone_vols) / len(zone_vols) if zone_vols else 0
        vol_ratio = avg_zone_vol / avg_vol if avg_vol > 0 else 1.0

        signal = _classify_zone_visit(bars, in_zone, zl, zh, vol_ratio)
        if signal and signal.confidence > best.confidence:
            best = signal

    return best


def _classify_zone_visit(
    bars,
    in_zone,
    zl: float,
    zh: float,
    vol_ratio: float,
) -> AuctionSignal | None:
    """Classify a zone visit as failure or acceptance based on bar behaviour."""
    last_bar = bars[-1]
    first_in = in_zone[0]

    # Determine approach direction: where did price come FROM before entering zone?
    approach_from_above = first_in.ohlc.o > zh  # came down into zone
    approach_from_below = first_in.ohlc.o < zl  # came up into zone

    # --- FAILURE signatures ---

    # Low volume AND price exited the zone (close outside zone)
    last_close = last_bar.ohlc.c
    exited_up = last_close > zh
    exited_down = last_close < zl

    if vol_ratio < LVN_VOL_RATIO and (exited_up or exited_down):
        # Failure long: approached from below, low vol in zone, close above zone
        if exited_up:
            confidence = _failure_confidence(vol_ratio, len(in_zone), _close_pct(last_bar))
            return AuctionSignal(
                type="failure_long",
                zone_low=zl, zone_high=zh,
                confidence=confidence,
                bars_in_zone=len(in_zone),
                avg_vol_ratio=round(vol_ratio, 3),
                reason=f"low vol ({vol_ratio:.2f}× avg) in LVN, price exited above {zh:.2f}",
            )
        # Failure short: approached from above, low vol, close below zone
        if exited_down:
            confidence = _failure_confidence(vol_ratio, len(in_zone), 1.0 - _close_pct(last_bar))
            return AuctionSignal(
                type="failure_short",
                zone_low=zl, zone_high=zh,
                confidence=confidence,
                bars_in_zone=len(in_zone),
                avg_vol_ratio=round(vol_ratio, 3),
                reason=f"low vol ({vol_ratio:.2f}× avg) in LVN, price exited below {zl:.2f}",
            )

    # --- ACCEPTANCE signatures ---

    # High volume AND price closing deep inside or beyond zone
    if vol_ratio >= 1.2 and len(in_zone) >= 2:
        # Acceptance long: price above zone midpoint and staying/pushing higher
        zone_mid = (zl + zh) / 2
        if last_close > zone_mid and not exited_down:
            return AuctionSignal(
                type="acceptance_long",
                zone_low=zl, zone_high=zh,
                confidence=min(0.75, 0.40 + (vol_ratio - 1.2) * 0.15),
                bars_in_zone=len(in_zone),
                avg_vol_ratio=round(vol_ratio, 3),
                reason=f"high vol ({vol_ratio:.2f}× avg) in LVN, price accepting above midpoint",
            )
        # Acceptance short: price below zone midpoint and staying/pushing lower
        if last_close < zone_mid and not exited_up:
            return AuctionSignal(
                type="acceptance_short",
                zone_low=zl, zone_high=zh,
                confidence=min(0.75, 0.40 + (vol_ratio - 1.2) * 0.15),
                bars_in_zone=len(in_zone),
                avg_vol_ratio=round(vol_ratio, 3),
                reason=f"high vol ({vol_ratio:.2f}× avg) in LVN, price accepting below midpoint",
            )

    return None


def _failure_confidence(vol_ratio: float, bars_in_zone: int, close_strength: float) -> float:
    """Compute failure confidence.

    Higher when: very low volume, spent only 1 bar in zone, strong close away.
    """
    vol_score = max(0.0, (LVN_VOL_RATIO - vol_ratio) / LVN_VOL_RATIO)  # 0-1
    dwell_score = 1.0 / bars_in_zone                                     # 1 bar = 1.0, 3 bars = 0.33
    close_score = min(1.0, close_strength)                               # 0-1
    raw = 0.4 * vol_score + 0.35 * dwell_score + 0.25 * close_score
    return round(min(0.90, max(0.35, raw + 0.3)), 2)


def from_store(symbol: str, primary_tf: str) -> AuctionSignal:
    """Convenience: detect auction signal from latest bars in state_store."""
    from pipeline.state_store import store
    from pipeline.features.vp_cache import get as vp_get
    from types import SimpleNamespace

    s = store()
    recent = s.recent(symbol, primary_tf, 30)
    if not recent:
        return _NONE

    lvn_zones: list[dict] = []
    try:
        vp = vp_get(symbol, "daily")
        if vp:
            lvn_zones = vp.get("lvn_zones", [])
    except Exception:
        pass

    return detect(recent, lvn_zones)
