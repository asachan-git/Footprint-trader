"""Daily + Weekly Volume Profile from accumulated footprint bars.

Aggregates bid+ask volume across all bars in a period → per-price volume map.
Computes: POC, VAH, VAL (70%), shape (D/P/b/double/elongated), HVN, LVN zones, naked POC.

No new data source needed — uses bars already in state_store.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1d": 86400, "1w": 604800}


@dataclass
class VolumeProfilePeriod:
    period: str                    # "daily" | "weekly"
    poc: float | None              # price with highest volume
    vah: float | None              # value area high (70%)
    val: float | None              # value area low (70%)
    shape: str                     # "D" | "P" | "b" | "double" | "elongated" | "thin"
    hvn_zones: list[dict]          # high-volume zones [{low, high}] — strong S/R areas
    lvn_zones: list[dict]          # low-volume zones [{low, high}] — fast-move areas
    naked_poc: float | None        # previous period POC not yet revisited by current bars
    current_position: str          # "above_vah" | "in_va_above_poc" | "at_poc" | "in_va_below_poc" | "below_val"
    total_volume: float
    price_range: float             # high - low of period
    bar_count: int


def _period_bounds(now_ts: int, period: str) -> tuple[int, int]:
    """Return (start_ts, end_ts) for 'daily' or 'weekly' period containing now_ts."""
    dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    if period == "daily":
        start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        end = int(start.timestamp()) + 86400
        return int(start.timestamp()), end
    else:  # weekly — Monday 00:00 UTC
        monday = dt - __import__("datetime").timedelta(days=dt.weekday())
        start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
        end = int(start.timestamp()) + 604800
        return int(start.timestamp()), end


def _build_price_map(bars) -> dict[float, float]:
    """Aggregate bid + ask volume per price level across all bars."""
    vol_map: dict[float, float] = {}
    for bar in bars:
        for lvl in bar.bid_ladder:
            vol_map[lvl.price] = vol_map.get(lvl.price, 0.0) + lvl.vol
        for lvl in bar.ask_ladder:
            vol_map[lvl.price] = vol_map.get(lvl.price, 0.0) + lvl.vol
    return vol_map


def _compute_poc_va(vol_map: dict[float, float], va_pct: float = 0.70) -> tuple[float, float, float] | None:
    """Returns (poc, vah, val). None if no data."""
    if not vol_map:
        return None
    prices = sorted(vol_map.keys())
    total = sum(vol_map.values())
    poc = max(prices, key=lambda p: vol_map[p])
    poc_idx = prices.index(poc)
    target = total * va_pct
    lo = hi = poc_idx
    acc = vol_map[poc]
    while acc < target and (lo > 0 or hi < len(prices) - 1):
        left = vol_map[prices[lo - 1]] if lo > 0 else -1
        right = vol_map[prices[hi + 1]] if hi < len(prices) - 1 else -1
        if right >= left and hi < len(prices) - 1:
            hi += 1
            acc += vol_map[prices[hi]]
        elif lo > 0:
            lo -= 1
            acc += vol_map[prices[lo]]
        else:
            break
    return poc, prices[hi], prices[lo]


def _smooth(vol_map: dict[float, float], window: int = 2) -> dict[float, float]:
    """5-level rolling average to reduce noise before peak detection."""
    prices = sorted(vol_map.keys())
    vols = [vol_map[p] for p in prices]
    n = len(prices)
    smoothed: dict[float, float] = {}
    for i, p in enumerate(prices):
        lo = max(0, i - window)
        hi = min(n, i + window + 1)
        smoothed[p] = sum(vols[lo:hi]) / (hi - lo)
    return smoothed


def _find_peaks_valleys(smoothed: dict[float, float], n: int = 2) -> tuple[list[float], list[float]]:
    """Local maxima (peaks) and local minima (valleys) on smoothed curve.

    A price is a peak if its smoothed vol is the max of its [i-n .. i+n] window.
    """
    prices = sorted(smoothed.keys())
    vols = [smoothed[p] for p in prices]
    peaks: list[float] = []
    valleys: list[float] = []
    for i in range(n, len(prices) - n):
        window = vols[i - n: i + n + 1]
        if vols[i] == max(window):
            peaks.append(prices[i])
        if vols[i] == min(window):
            valleys.append(prices[i])
    return peaks, valleys


def _detect_shape(vol_map: dict[float, float], poc: float) -> str:
    """Classify VP shape using centroid + peak/valley detection.

    Shape logic (institutional approach):
    1. Centroid-based: weighted average price relative to range → P / D / b
    2. Double: two statistically significant peaks with a significant valley between
    3. Elongated: neither concentrated nor bimodal
    """
    if len(vol_map) < 5:
        return "thin"
    prices = sorted(vol_map.keys())
    total = sum(vol_map.values())
    if total == 0 or prices[-1] == prices[0]:
        return "thin"

    price_range = prices[-1] - prices[0]

    # Step 1: volume centroid (weighted average price)
    centroid = sum(p * v for p, v in vol_map.items()) / total
    rel_pos = (centroid - prices[0]) / price_range   # 0=bottom, 1=top

    # Step 2: peak/valley detection for double detection
    smoothed = _smooth(vol_map)
    peaks, valleys = _find_peaks_valleys(smoothed)

    # Double: 2+ significant peaks with deep valley between, separated by >30% range
    import statistics as _stats
    avg = total / len(vol_map)
    std = _stats.pstdev(vol_map.values()) if len(vol_map) > 1 else 0.0
    sig_peaks = [p for p in peaks if smoothed[p] > avg + 0.8 * std]
    if len(sig_peaks) >= 2:
        peak_span = max(sig_peaks) - min(sig_peaks)
        if peak_span > price_range * 0.30:
            # verify there's a meaningful valley between the two main peaks
            between_valleys = [v for v in valleys if min(sig_peaks) < v < max(sig_peaks)]
            if between_valleys:
                deepest_valley = min(smoothed[v] for v in between_valleys)
                peak_avg_vol = (smoothed[sig_peaks[0]] + smoothed[sig_peaks[-1]]) / 2
                if deepest_valley < peak_avg_vol * 0.65:  # valley is ≥35% below peak average
                    return "double"

    # Step 3: centroid-based shape
    if rel_pos > 0.60:
        return "P"    # volume concentrated in upper part = distribution
    if rel_pos < 0.40:
        return "b"    # volume concentrated in lower part = accumulation
    return "D"        # balanced, centered


def _group_levels_into_zones(
    prices: list[float],
    price_step: float,
    price_range: float = 0.0,
) -> list[dict]:
    """Merge price levels into meaningful zones.

    Merge gap: max(price_step × 5, price_range × 1%) — prevents tiny isolated zones.
    Min width: max(price_step × 2, price_range × 0.5%) — filters single-point zones.
    """
    if not prices:
        return []
    prices = sorted(prices)
    effective_range = price_range or (prices[-1] - prices[0]) or price_step
    merge_gap = max(price_step * 5, effective_range * 0.01)
    min_width  = max(price_step * 2, effective_range * 0.005)

    zones: list[dict] = []
    zone_start = zone_end = prices[0]
    for p in prices[1:]:
        if p - zone_end <= merge_gap:
            zone_end = p
        else:
            if zone_end - zone_start >= min_width:
                zones.append({"low": zone_start, "high": zone_end})
            zone_start = zone_end = p
    if zone_end - zone_start >= min_width:
        zones.append({"low": zone_start, "high": zone_end})
    return zones


def _hvn_lvn(vol_map: dict[float, float]) -> tuple[list[dict], list[dict]]:
    """Identify HVN and LVN zones using peak/valley detection on smoothed histogram.

    HVN: zones surrounding statistically significant peaks (vol > mean + 1σ)
    LVN: zones surrounding statistically significant valleys (vol < mean - 0.5σ)
    """
    if not vol_map or len(vol_map) < 3:
        return [], []

    prices = sorted(vol_map.keys())
    price_range = prices[-1] - prices[0] if len(prices) > 1 else 1.0
    price_step = min(prices[i + 1] - prices[i] for i in range(len(prices) - 1)) or 1.0

    import statistics as _stats
    vols = list(vol_map.values())
    avg = sum(vols) / len(vols)
    std = _stats.pstdev(vols) if len(vols) > 1 else 0.0

    # Smooth and detect peaks/valleys
    smoothed = _smooth(vol_map)
    peaks, valleys = _find_peaks_valleys(smoothed)

    # Significant peaks → HVN
    hvn_prices = [p for p in peaks if vol_map[p] >= avg + 1.0 * std]
    # Significant valleys → LVN
    lvn_prices = [p for p in valleys if vol_map[p] <= avg - 0.5 * std]

    # Fallback: use relative threshold if peak detection yields nothing
    # For synthetic/flat data (low std/mean ratio), use top/bottom 20% by volume
    cv = std / avg if avg > 0 else 0  # coefficient of variation
    if not hvn_prices:
        if cv >= 0.3:
            hvn_prices = [p for p, v in vol_map.items() if v >= avg + 0.5 * std]
        else:
            # Flat distribution (synthetic data) — take top 20% by volume
            vol_threshold = sorted(vol_map.values())[int(len(vol_map) * 0.80)]
            hvn_prices = [p for p, v in vol_map.items() if v >= vol_threshold]
    if not lvn_prices:
        if cv >= 0.3:
            lvn_prices = [p for p, v in vol_map.items() if 0 < v <= avg - 0.5 * std]
        else:
            # Flat distribution — take bottom 20% by volume
            vol_threshold = sorted(vol_map.values())[int(len(vol_map) * 0.20)]
            lvn_prices = [p for p, v in vol_map.items() if 0 < v <= vol_threshold]

    return (
        _group_levels_into_zones(hvn_prices, price_step, price_range),
        _group_levels_into_zones(lvn_prices, price_step, price_range),
    )


def _current_position(close: float, poc: float, vah: float, val: float) -> str:
    if close > vah:
        return "above_vah"
    if close < val:
        return "below_val"
    if abs(close - poc) <= (vah - val) * 0.05:
        return "at_poc"
    if close > poc:
        return "in_va_above_poc"
    return "in_va_below_poc"


def compute(
    bars,
    period: str,
    current_close: float,
    prev_poc: float | None = None,
) -> VolumeProfilePeriod:
    """Compute VolumeProfilePeriod from a list of Bar objects for the period."""
    vol_map = _build_price_map(bars)
    if not vol_map:
        return VolumeProfilePeriod(
            period=period, poc=None, vah=None, val=None,
            shape="thin", hvn_zones=[], lvn_zones=[],
            naked_poc=None, current_position="unknown",
            total_volume=0.0, price_range=0.0, bar_count=len(bars),
        )

    prices = list(vol_map.keys())
    result = _compute_poc_va(vol_map)
    if result is None:
        poc = vah = val = None
    else:
        poc, vah, val = result

    shape = _detect_shape(vol_map, poc) if poc else "thin"
    hvn, lvn = _hvn_lvn(vol_map)
    price_range = max(prices) - min(prices) if prices else 0.0

    # Naked POC: previous period POC not yet touched by current period bars
    naked = None
    if prev_poc is not None and poc is not None:
        touched = any(abs(p - prev_poc) <= price_range * 0.001 for p in prices)
        if not touched:
            naked = prev_poc

    pos = _current_position(current_close, poc, vah, val) if poc and vah and val else "unknown"

    return VolumeProfilePeriod(
        period=period,
        poc=poc,
        vah=vah,
        val=val,
        shape=shape,
        hvn_zones=hvn,
        lvn_zones=lvn,
        naked_poc=naked,
        current_position=pos,
        total_volume=sum(vol_map.values()),
        price_range=price_range,
        bar_count=len(bars),
    )


MIN_BARS_DAILY = 60     # need at least 60 x 1m bars (~1hr) for meaningful daily VP
MIN_BARS_WEEKLY = 300   # need at least 300 x 1m bars (~5hr) for meaningful weekly VP


def from_store(symbol: str, primary_tf: str, period: str) -> VolumeProfilePeriod | None:
    """Convenience: compute VP from state_store bars for the given period.
    Returns None if insufficient bars for meaningful profile.
    """
    from pipeline.state_store import store
    import time

    s = store()
    now_ts = int(time.time())
    start_ts, _ = _period_bounds(now_ts, period)

    all_bars = s.recent(symbol, primary_tf, 10_000)
    period_bars = [b for b in all_bars if b.close_ts >= start_ts]

    min_bars = MIN_BARS_WEEKLY if period == "weekly" else MIN_BARS_DAILY
    if len(period_bars) < min_bars:
        return None   # too few bars for meaningful VP — avoid noisy profile

    latest = period_bars[-1]
    current_close = latest.ohlc.c

    # Previous period for naked POC
    prev_period_bars = [b for b in all_bars if b.close_ts < start_ts]
    prev_result = None
    if prev_period_bars:
        prev_vol_map = _build_price_map(prev_period_bars[-500:])
        if prev_vol_map:
            r = _compute_poc_va(prev_vol_map)
            if r:
                prev_result = r[0]

    return compute(period_bars, period, current_close, prev_poc=prev_result)
