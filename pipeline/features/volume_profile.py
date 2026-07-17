"""Daily + Weekly Volume Profile from accumulated footprint bars.

============================================================================
DATA FLOW
============================================================================
Input:
  bars: list[Bar]  — from state_store.recent(symbol, "1m", N)
                     Each Bar has .ohlc (o/h/l/c), .bid_ladder, .ask_ladder, .delta

Stage 1 — Per-price volume from ladders
  bid_ladder[].price/vol  = aggressor SELLS (taker hit bid)
  ask_ladder[].price/vol  = aggressor BUYS  (taker lifted ask)
  Each (price, vol) is a real transaction at an exact price level.

Stage 2 — Bin construction (ladder-aggregation)
  Tick-aligned grid (institutional convention):
    bin_size = symbol_config.bin_size           (e.g. 0.5pt XAU, 1.0pt BTC)
    p_min    = floor(min(prices) / bin_size) × bin_size
    n_bins   = ceil((max(prices) - p_min) / bin_size) + 1
  For each ladder level on each bar:
    bins[bin_of(level.price)] += level.vol
  Fallback (bar with empty bid+ask ladders, degenerate data only):
    distribute |delta| uniformly across [bar.l, bar.h].
  Legacy mode when bin_size is None: bin_size = (p_max - p_min) / NUM_BINS=68.

Stage 3 — Statistics on the bin array
  POC      = bin with max volume (midpoint of ties)
  VA       = Steidlmayer pair-test expansion from POC until 68% enclosed
  HVN/LVN  = scipy.signal.find_peaks on smoothed bins (uniform_filter1d size=3)
             with prominence ≥ 0.5×avg (HVN) / 0.3×avg (LVN)
             zone boundaries from peak_widths(rel_height=0.5)
  Shape    = P / b / D / B / thin classifier from bin distribution

Output: VolumeProfilePeriod dataclass — POC, VAH, VAL, shape, HVN/LVN zones,
        naked POC, current position, total volume, price range, bar count.

Tick-alignment makes POC/VAH/VAL land on exact bin_size multiples (no
fractional residue like 4539.14 — instead 4539.0 or 4539.5).
============================================================================
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
from scipy.ndimage import gaussian_filter1d, uniform_filter1d
from scipy.signal import find_peaks, peak_widths
from scipy.stats import gaussian_kde

_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1d": 86400, "1w": 604800}

NUM_BINS = 68
VALUE_AREA_PCT = 0.68


@dataclass
class VolumeProfilePeriod:
    period: str                    # "daily" | "weekly" | "cached"
    poc: float | None              # price with highest volume
    vah: float | None              # value area high (68%)
    val: float | None              # value area low (68%)
    shape: str                     # "D" | "P" | "b" | "B" | "thin"
    hvn_zones: list[dict]          # high-volume zones [{low, high}] — strong S/R
    lvn_zones: list[dict]          # low-volume zones [{low, high}] — fast-move
    naked_poc: float | None        # previous period POC not yet revisited
    current_position: str          # "above_vah" | "in_va_above_poc" | "at_poc" | "in_va_below_poc" | "below_val" | "unknown"
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
    else:
        monday = dt - __import__("datetime").timedelta(days=dt.weekday())
        start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
        end = int(start.timestamp()) + 604800
        return int(start.timestamp()), end


def _bar_volume(bar) -> float:
    """Total volume for a bar — sum bid + ask ladder, or fall back to |delta|."""
    total = sum(lvl.vol for lvl in bar.bid_ladder) + sum(lvl.vol for lvl in bar.ask_ladder)
    if total > 0:
        return total
    return abs(bar.delta or 0.0)


def _build_bins(
    bars,
    bin_size: float | None = None,
    num_bins: int = NUM_BINS,
) -> tuple[np.ndarray, np.ndarray, float, float, float] | None:
    """Aggregate ladder volume into a tick-aligned bin grid.

    Primary path: per-bar bid_ladder + ask_ladder give exact transaction
    volume at exact prices. Each level's price is snapped to the tick-aligned
    bin and its volume added — preserves true orderflow resolution.

    Fallback (no ladder data at all on a bar): distribute that bar's
    |delta| uniformly across [bar.l, bar.h]. Only kicks in for bars where
    both ladders are empty.

    Tick-aligned mode (recommended): pass `bin_size` = fixed instrument tick
    multiple (e.g. 0.5 for XAU, 1.0 for BTC). Grid anchored to
    floor(p_min / bin_size) × bin_size so POC/VAH/VAL land on bin_size multiples.

    Legacy mode (bin_size=None): bin_size derived as (p_max - p_min) / num_bins.
    Produces fractional non-tick-aligned values; kept for backwards compat.

    Returns (bins, bin_centers, bin_size, price_min, price_max) — or None if no data.
    """
    if not bars:
        return None
    highs = [b.ohlc.h for b in bars]
    lows = [b.ohlc.l for b in bars]

    raw_min = min(lows)
    p_max = max(highs)
    if p_max <= raw_min:
        return None

    # Widen p_min/p_max so any ladder level is covered too
    for bar in bars:
        for lvl in bar.bid_ladder:
            if lvl.price < raw_min:
                raw_min = lvl.price
            if lvl.price > p_max:
                p_max = lvl.price
        for lvl in bar.ask_ladder:
            if lvl.price < raw_min:
                raw_min = lvl.price
            if lvl.price > p_max:
                p_max = lvl.price

    if bin_size is not None and bin_size > 0:
        p_min = math.floor(raw_min / bin_size) * bin_size
        n = max(1, int(math.ceil((p_max - p_min) / bin_size)) + 1)
    else:
        p_min = raw_min
        bin_size = (p_max - p_min) / num_bins
        n = num_bins

    bins = np.zeros(n, dtype=float)
    bin_centers = np.array([p_min + (i + 0.5) * bin_size for i in range(n)])

    def _bin_of(price: float) -> int:
        return max(0, min(n - 1, int((price - p_min) / bin_size)))

    for bar in bars:
        has_ladder = bool(bar.bid_ladder) or bool(bar.ask_ladder)
        if has_ladder:
            # True orderflow: aggregate exact-price ladder volumes
            for lvl in bar.bid_ladder:
                if lvl.vol > 0:
                    bins[_bin_of(lvl.price)] += lvl.vol
            for lvl in bar.ask_ladder:
                if lvl.vol > 0:
                    bins[_bin_of(lvl.price)] += lvl.vol
        else:
            # Fallback for ladder-less bars (degenerate synthetic data)
            v = abs(bar.delta or 0.0)
            if v <= 0 or bar.ohlc.h <= bar.ohlc.l:
                continue
            b_lo = _bin_of(bar.ohlc.l)
            b_hi = _bin_of(bar.ohlc.h)
            span = max(b_hi - b_lo + 1, 1)
            bins[b_lo:b_hi + 1] += v / span

    return bins, bin_centers, bin_size, p_min, p_max


def _build_price_map(bars) -> dict[float, float]:
    """Backwards-compat helper — aggregate ladder bid+ask volume per price.

    Kept for vp_cache.py / verify_vp.py / vp_history.py which import it.
    """
    vol_map: dict[float, float] = {}
    for bar in bars:
        for lvl in bar.bid_ladder:
            vol_map[lvl.price] = vol_map.get(lvl.price, 0.0) + lvl.vol
        for lvl in bar.ask_ladder:
            vol_map[lvl.price] = vol_map.get(lvl.price, 0.0) + lvl.vol
    return vol_map


def _value_area(bins: np.ndarray, poc_idx: int, va_pct: float) -> tuple[int, int]:
    """Steidlmayer expansion: 2-bin pair-test for DIRECTION, 1-bin step size.

    Uses 2-bin lookahead (classic Steidlmayer) to decide which side to expand
    toward, but always moves 1 bin at a time so we never overshoot the target.
    When both immediate neighbors are zero (sparse tick grid), skip ahead to
    the next non-zero bin on each side rather than stopping.
    """
    n = len(bins)
    total = bins.sum()
    if total == 0:
        return poc_idx, poc_idx
    target = total * va_pct
    lo_idx = hi_idx = poc_idx
    acc = float(bins[poc_idx])
    for _ in range(n * 2):
        if acc >= target:
            break
        at_top = hi_idx + 1 >= n
        at_bot = lo_idx - 1 < 0
        if at_top and at_bot:
            break
        # Skip-zero: advance boundary past empty bins to find next volume
        hi_look = hi_idx + 1
        while hi_look < n and bins[hi_look] == 0:
            hi_look += 1
        lo_look = lo_idx - 1
        while lo_look >= 0 and bins[lo_look] == 0:
            lo_look -= 1
        # 2-bin pair sums for direction decision
        up1 = float(bins[hi_look]) if hi_look < n else 0.0
        up2 = float(bins[hi_look + 1]) if hi_look + 1 < n else 0.0
        dn1 = float(bins[lo_look]) if lo_look >= 0 else 0.0
        dn2 = float(bins[lo_look - 1]) if lo_look - 1 >= 0 else 0.0
        up_pair = up1 + up2
        dn_pair = dn1 + dn2
        if up_pair == 0 and dn_pair == 0:
            break
        # 1-bin step toward the heavier pair
        if not at_top and (at_bot or up_pair >= dn_pair):
            hi_idx = hi_look          # jump to next non-zero
            acc += float(bins[hi_idx])
        elif not at_bot:
            lo_idx = lo_look
            acc += float(bins[lo_idx])
        else:
            break
    return lo_idx, hi_idx


def _classify_shape(
    bins: np.ndarray, poc_idx: int, lo_idx: int, hi_idx: int, num_bins: int
) -> str:
    """Classify VP shape: P (distribution top), b (accumulation bottom), D (balanced), B (bimodal), thin."""
    total = float(bins.sum())
    if total == 0:
        return "thin"

    third = num_bins // 3
    lower = float(bins[:third].sum())
    mid = float(bins[third: 2 * third].sum())
    upper = float(bins[2 * third:].sum())

    lower_pct = lower / total
    mid_pct = mid / total
    upper_pct = upper / total

    if poc_idx >= 2 * third and upper_pct > 0.45:
        return "P"
    if poc_idx < third and lower_pct > 0.45:
        return "b"
    if mid_pct > 0.40 and abs(upper_pct - lower_pct) < 0.15:
        return "D"

    # Bimodal: second peak away from POC with comparable volume
    gap = max(num_bins // 10, 5)
    outside = [bins[i] for i in range(num_bins) if abs(i - poc_idx) > gap]
    if outside:
        second_peak = float(max(outside))
        if second_peak > float(bins.max()) * 0.65:
            return "B"

    avg = total / num_bins
    if avg > 0 and float(bins.max()) / avg < 2.0:
        return "thin"
    return "D"


def _zones_from_peaks(
    arr: np.ndarray,
    peaks: np.ndarray,
    left_ips: np.ndarray,
    right_ips: np.ndarray,
    bin_size: float,
    price_min: float,
    offset: int = 0,
    top_n: int = 4,
    ascending: bool = False,
) -> list[dict]:
    """Convert peak_widths output into sorted zone dicts.

    `offset` shifts indices back into the full array's coordinate space (used
    when LVN detection runs on a sliced region).
    """
    scored: list[tuple[float, float, float]] = []
    for i in range(len(peaks)):
        l = max(0, math.floor(left_ips[i]))
        r = min(len(arr) - 1, math.floor(right_ips[i]))
        seg = arr[l: r + 1]
        vol = float(seg.mean()) if len(seg) > 0 else 0.0
        scored.append((vol, offset + left_ips[i], offset + right_ips[i]))

    scored.sort(key=lambda x: x[0], reverse=not ascending)

    result: list[dict] = []
    for _, lip, rip in scored[:top_n]:
        low = round(price_min + math.floor(lip) * bin_size, 2)
        high = round(price_min + (math.floor(rip) + 1) * bin_size, 2)
        if low < high:
            result.append({"low": low, "high": high})
    return sorted(result, key=lambda z: z["low"])


def _smooth(bins: np.ndarray, n_bins: int, bin_size: float = 1.0, price_min: float = 0.0) -> np.ndarray:
    """Gaussian KDE smoothing on the bin CENTERS weighted by volume, evaluated back onto
    the same bin grid (same length/index space as `bins`) — a drop-in replacement for the
    prior gaussian_filter1d approach, so every downstream caller (find_peaks, peak_widths,
    _zones_from_peaks — all of which index into the result the same way `bins` is indexed)
    needs no changes.

    bw_method=0.05 was swept against live gold data (0.02 → fragments into noise, 0.08+ →
    collapses distinct nodes back into one blob) — same tuning problem as sigma on the
    gaussian_filter1d approach; 0.05 empirically matches that approach's sigma=1.5 result
    on real data (confirmed: same 3-zone HVN structure within ~0.3pt on a live session).
    Falls back to gaussian_filter1d if there's fewer than 2 non-zero bins (KDE needs ≥2
    distinct-enough samples) or all volume sits in one bin (KDE covariance singular).
    """
    active = bins[bins > 0]
    if len(active) < 2:
        return gaussian_filter1d(bins.astype(float), sigma=1.5)
    centers = price_min + (np.arange(n_bins) + 0.5) * bin_size
    try:
        kde = gaussian_kde(centers, weights=bins.astype(float), bw_method=0.05)
    except np.linalg.LinAlgError:
        return gaussian_filter1d(bins.astype(float), sigma=1.5)
    density = kde(centers)
    # Rescale so the smoothed curve's total mass matches the original bins' total volume
    # (KDE density integrates to 1, not to sum(bins)) — keeps `avg = active.mean()` and
    # the prominence/height thresholds in find_peaks meaningful in the SAME units as
    # gaussian_filter1d's output (which preserves the original bins' scale).
    total = float(bins.sum())
    density_sum = float(density.sum())
    if density_sum > 0:
        density = density * (total / density_sum)
    return density


def _merge_zones(zones: list[dict], merge_tol: float) -> list[dict]:
    """Merge adjacent zones whose gap is ≤ merge_tol (Sierra/Bookmap cluster merging)."""
    if not zones:
        return zones
    merged = [dict(zones[0])]
    for z in zones[1:]:
        if z["low"] - merged[-1]["high"] <= merge_tol:
            merged[-1]["high"] = max(merged[-1]["high"], z["high"])
        else:
            merged.append(dict(z))
    return merged


def _merge_peaks_by_trough(
    peaks: np.ndarray, smoothed: np.ndarray,
    merge_gap_bins: int, merge_trough_ratio: float,
) -> list[tuple[int, int]]:
    """Collapse adjacent HVN peaks that are really SHOULDERS of one broad node into a
    single (start_peak, end_peak) span. Two peaks merge when BOTH:
      • gap between them ≤ merge_gap_bins (bins), AND
      • the SHALLOWEST bin in the gap ≥ merge_trough_ratio × the smaller peak height
        (a shallow dip = shoulder; a deep trough = a REAL LVN vacuum → keep split).

    This removes the FAKE interior edge a shallow dip would otherwise carve into one HVN
    (fixes: a wide node split into 2 HVNs, arming on a boundary that sits inside value).
    Returns a list of merged peak-index ranges [(p_start_idx, p_end_idx), ...] into `peaks`.
    """
    if len(peaks) == 0:
        return []
    groups: list[list[int]] = [[0]]   # indices into `peaks`
    for i in range(1, len(peaks)):
        prev_p = peaks[groups[-1][-1]]
        cur_p = peaks[i]
        gap = int(cur_p - prev_p)
        # shallowest bin strictly between the two peaks
        seg = smoothed[prev_p + 1:cur_p]
        trough = float(seg.min()) if len(seg) else 0.0
        smaller_peak = float(min(smoothed[prev_p], smoothed[cur_p]))
        shallow = smaller_peak > 0 and trough >= merge_trough_ratio * smaller_peak
        if gap <= merge_gap_bins and shallow:
            groups[-1].append(i)          # same broad node → merge
        else:
            groups.append([i])            # deep trough or too far → new node
    return [(g[0], g[-1]) for g in groups]


def _find_hvn_zones(
    bins: np.ndarray, bin_size: float, price_min: float, top_n: int = 8,
    merge_gap_bins: int = 20, merge_trough_ratio: float = 0.55,
) -> list[dict]:
    """HVN via Gaussian-smoothed bins, prominence ≥ 0.25×avg, height ≥ 0.5×avg.

    Peaks are then collapsed by _merge_peaks_by_trough: adjacent peaks separated by a
    SHALLOW dip (≥ merge_trough_ratio of the smaller peak, within merge_gap_bins) are one
    broad node — merged so no fake interior edge is created. A DEEP dip = real LVN vacuum →
    kept split. No trailing flat gap-merge: peak_widths(rel_height=0.5) already extends
    each kept zone into its trough, so a plain gap-merge would re-join two peaks the trough
    test deliberately split. _merge_peaks_by_trough is the sole HVN clustering step.

    Thresholds (prominence ≥ 0.25×avg, height ≥ 0.5×avg — was 0.5×avg/1.0×avg) are gated
    against the WHOLE array's average, which on a wide day range can hide real local
    structure below that global bar (e.g. a genuine secondary shelf never becomes a
    find_peaks candidate at all, so it silently gets absorbed into one dominant peak's
    zone instead of being tested by the trough-merge). Lower thresholds let local
    peaks through as CANDIDATES; _merge_peaks_by_trough still does the real
    shoulder-vs-vacuum discrimination — this only widens what's eligible to be tested.
    """
    n = len(bins)
    smoothed = _smooth(bins, n, bin_size, price_min)
    active = bins[bins > 0]
    if len(active) == 0:
        return []
    avg = float(active.mean())

    peaks, _ = find_peaks(smoothed, prominence=avg * 0.25, height=avg * 0.5)
    if len(peaks) == 0:
        return []

    _, _, left_ips, right_ips = peak_widths(smoothed, peaks, rel_height=0.5)
    spans = _merge_peaks_by_trough(peaks, smoothed, merge_gap_bins, merge_trough_ratio)
    m_peaks, m_left, m_right = [], [], []
    for a, b in spans:
        m_peaks.append(peaks[a])          # representative peak (first) — position only
        m_left.append(left_ips[a])         # widen span to cover the merged shoulders
        m_right.append(right_ips[b])
    zones = _zones_from_peaks(bins, np.array(m_peaks), np.array(m_left), np.array(m_right),
                              bin_size, price_min, top_n=top_n * 2)
    return zones[:top_n]


def _edge_lvn_zones(
    bins: np.ndarray, smoothed: np.ndarray, bin_size: float, price_min: float,
    first: int, last: int, avg: float, merge_trough_ratio: float = 0.55,
) -> list[dict]:
    """Catch LVN vacuums sitting at the very start/end of the active price range.

    find_peaks (used by the main valley search below) can never register a valley
    at index 0 or -1 of a region — a valley needs the signal HIGHER on both sides,
    and there's no data outside [first, last]. A real, obviously-thin edge run (e.g.
    a session low that just swept through with almost no resting volume) is
    therefore structurally invisible to the interior valley search. Compare the
    edge segment's minimum against the FIRST/LAST interior peak within the active
    range using the same shallow-vs-deep trough logic as _merge_peaks_by_trough:
    a deep enough edge minimum (≤ merge_trough_ratio of that peak) is a real vacuum.
    """
    region = smoothed[first: last + 1]
    peaks, _ = find_peaks(region, prominence=avg * 0.25, height=avg * 0.5)
    if len(peaks) == 0:
        return []
    # peak_widths' half-height boundary is the SAME boundary _find_hvn_zones uses for
    # this peak's own zone edge — bounding the edge-LVN zone there (not at the peak's
    # center index) guarantees it never overlaps the HVN zone built from this peak.
    _, _, left_ips, right_ips = peak_widths(region, peaks, rel_height=0.5)

    # Match _zones_from_peaks' exact rounding convention (floor(left_ip), floor(right_ip)+1)
    # so an edge-LVN zone's boundary lands bin-for-bin flush against the adjacent HVN
    # zone's boundary — no off-by-half-bin sliver overlap between the two.
    zones: list[dict] = []
    left_bound = int(math.floor(left_ips[0]))
    left_seg = region[:left_bound]
    if len(left_seg) >= 2:
        left_min = float(left_seg.min())
        peak_val = float(region[peaks[0]])
        if peak_val > 0 and left_min <= merge_trough_ratio * peak_val:
            low = round(price_min + first * bin_size, 2)
            high = round(price_min + (first + left_bound) * bin_size, 2)
            if low < high:
                zones.append({"low": low, "high": high})

    right_bound = int(math.floor(right_ips[-1])) + 1
    right_seg = region[right_bound:]
    if len(right_seg) >= 2:
        right_min = float(right_seg.min())
        peak_val = float(region[peaks[-1]])
        if peak_val > 0 and right_min <= merge_trough_ratio * peak_val:
            low = round(price_min + (first + right_bound) * bin_size, 2)
            high = round(price_min + (last + 1) * bin_size, 2)
            if low < high:
                zones.append({"low": low, "high": high})
    return zones


def _find_lvn_zones(
    bins: np.ndarray, bin_size: float, price_min: float, top_n: int = 8
) -> list[dict]:
    """LVN via Gaussian-smoothed inverted signal, restricted to active range.
    Adjacent valleys within 2×bin_size are merged. Also checks the leading/trailing
    edge of the active range for a real vacuum (see _edge_lvn_zones) — the interior
    find_peaks search can't detect a valley with no shoulder on one side.
    """
    n = len(bins)
    smoothed = _smooth(bins, n, bin_size, price_min)
    active_idxs = np.where(bins > 0)[0]
    if len(active_idxs) == 0:
        return []
    avg = float(bins[active_idxs].mean())
    first, last = int(active_idxs[0]), int(active_idxs[-1])
    if last - first < 4:
        return []

    region = smoothed[first: last + 1]
    inverted = -region

    valleys, _ = find_peaks(inverted, prominence=avg * 0.3)
    zones: list[dict] = []
    if len(valleys) > 0:
        _, _, left_ips, right_ips = peak_widths(inverted, valleys, rel_height=0.5)
        zones = _zones_from_peaks(
            bins[first: last + 1], valleys, left_ips, right_ips,
            bin_size, price_min, offset=first, top_n=top_n * 2, ascending=True,
        )

    zones += _edge_lvn_zones(bins, smoothed, bin_size, price_min, first, last, avg)
    if not zones:
        return []
    zones = sorted(zones, key=lambda z: z["low"])
    merged = _merge_zones(zones, merge_tol=bin_size)
    return merged[:top_n]


def _compute_poc_va(
    vol_map: dict[float, float],
    va_pct: float = VALUE_AREA_PCT,
) -> tuple[float, float, float] | None:
    """Backwards-compat helper for callers that pass a sparse price→vol map.

    Bins the sparse map into NUM_BINS and runs the standard POC/VA pipeline.
    """
    if not vol_map:
        return None
    prices = sorted(vol_map.keys())
    p_min, p_max = prices[0], prices[-1]
    if p_max <= p_min:
        return p_min, p_min, p_min

    bin_size = (p_max - p_min) / NUM_BINS
    bins = np.zeros(NUM_BINS, dtype=float)
    for p, v in vol_map.items():
        idx = max(0, min(NUM_BINS - 1, int((p - p_min) / bin_size)))
        bins[idx] += v

    max_v = float(bins.max())
    tied = [i for i in range(NUM_BINS) if bins[i] == max_v]
    poc_idx = tied[len(tied) // 2]
    lo_idx, hi_idx = _value_area(bins, poc_idx, va_pct)

    poc = round(p_min + (poc_idx + 0.5) * bin_size, 2)
    val = round(p_min + lo_idx * bin_size, 2)
    vah = round(p_min + (hi_idx + 1) * bin_size, 2)
    return poc, vah, val


def _detect_shape(vol_map: dict[float, float], poc: float | None) -> str:
    """Backwards-compat shape detection from sparse vol_map."""
    if not vol_map or poc is None:
        return "thin"
    prices = sorted(vol_map.keys())
    p_min, p_max = prices[0], prices[-1]
    if p_max <= p_min:
        return "thin"
    bin_size = (p_max - p_min) / NUM_BINS
    bins = np.zeros(NUM_BINS, dtype=float)
    for p, v in vol_map.items():
        idx = max(0, min(NUM_BINS - 1, int((p - p_min) / bin_size)))
        bins[idx] += v
    poc_idx = max(0, min(NUM_BINS - 1, int((poc - p_min) / bin_size)))
    lo_idx, hi_idx = _value_area(bins, poc_idx, VALUE_AREA_PCT)
    return _classify_shape(bins, poc_idx, lo_idx, hi_idx, NUM_BINS)


def _hvn_lvn(vol_map: dict[float, float]) -> tuple[list[dict], list[dict]]:
    """Backwards-compat HVN/LVN from sparse vol_map (used by verify_vp.py)."""
    if not vol_map:
        return [], []
    prices = sorted(vol_map.keys())
    p_min, p_max = prices[0], prices[-1]
    if p_max <= p_min:
        return [], []
    bin_size = (p_max - p_min) / NUM_BINS
    bins = np.zeros(NUM_BINS, dtype=float)
    for p, v in vol_map.items():
        idx = max(0, min(NUM_BINS - 1, int((p - p_min) / bin_size)))
        bins[idx] += v
    hvn_zones = _find_hvn_zones(bins, bin_size, p_min)
    lvn_zones = _trim_lvn_vs_hvn(hvn_zones, _find_lvn_zones(bins, bin_size, p_min))
    return (hvn_zones, lvn_zones)


def _trim_lvn_vs_hvn(hvn_zones: list[dict], lvn_zones: list[dict]) -> list[dict]:
    """Clip any LVN zone that overlaps an HVN zone back to the HVN's boundary.

    HVN/LVN come from separate find_peaks passes (raw vs inverted signal) using
    slightly different rel_height=0.5 half-width conventions, so their edges can
    land on different bins and overlap by a sliver even though each zone is
    individually correct. A price node can't be both a high-volume acceptance
    area and a low-volume vacuum — HVN wins the shared bin(s).
    """
    trimmed: list[dict] = []
    for lz in lvn_zones:
        low, high = lz["low"], lz["high"]
        for hz in hvn_zones:
            if high <= hz["low"] or low >= hz["high"]:
                continue  # no overlap with this HVN zone
            if low < hz["low"] <= high <= hz["high"]:
                high = hz["low"]
            elif hz["low"] <= low <= hz["high"] < high:
                low = hz["high"]
            elif hz["low"] <= low and high <= hz["high"]:
                low = high  # fully swallowed → drop
        if low < high:
            trimmed.append({"low": low, "high": high})
    return trimmed


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
    *,
    bin_size: float | None = None,
) -> VolumeProfilePeriod:
    """Compute VolumeProfilePeriod from a list of Bar objects for the period.

    bin_size: tick-aligned bin width (e.g. 0.5 for XAU, 1.0 for BTC). When
    provided, POC/VAH/VAL land on exact multiples. When None, falls back to
    legacy range/NUM_BINS=68 binning.
    """
    built = _build_bins(bars, bin_size=bin_size)
    if built is None:
        return VolumeProfilePeriod(
            period=period, poc=None, vah=None, val=None,
            shape="thin", hvn_zones=[], lvn_zones=[],
            naked_poc=None, current_position="unknown",
            total_volume=0.0, price_range=0.0, bar_count=len(bars),
        )

    bins, _bin_centers, used_bin_size, p_min, p_max = built
    n = len(bins)

    max_v = float(bins.max())
    tied = [i for i in range(n) if bins[i] == max_v]
    poc_idx = tied[len(tied) // 2]
    lo_idx, hi_idx = _value_area(bins, poc_idx, VALUE_AREA_PCT)

    # Tick-aligned mode → POC = bin LOWER edge so values land on bin_size grid.
    # Legacy mode → keep center-of-bin convention.
    if bin_size is not None and bin_size > 0:
        poc = round(p_min + poc_idx * used_bin_size, 4)
    else:
        poc = round(p_min + (poc_idx + 0.5) * used_bin_size, 2)
    val = round(p_min + lo_idx * used_bin_size, 4)
    vah = round(p_min + (hi_idx + 1) * used_bin_size, 4)

    hvn = _find_hvn_zones(bins, used_bin_size, p_min)
    lvn = _trim_lvn_vs_hvn(hvn, _find_lvn_zones(bins, used_bin_size, p_min))
    shape = _classify_shape(bins, poc_idx, lo_idx, hi_idx, n)

    # Naked POC: prior-period POC not revisited by this period's bars
    naked: float | None = None
    if prev_poc is not None:
        tol = (p_max - p_min) * 0.001
        revisited = any(abs(b.ohlc.h - prev_poc) <= tol or abs(b.ohlc.l - prev_poc) <= tol
                        or (b.ohlc.l <= prev_poc <= b.ohlc.h) for b in bars)
        if not revisited:
            naked = prev_poc

    pos = _current_position(current_close, poc, vah, val)

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
        total_volume=float(bins.sum()),
        price_range=p_max - p_min,
        bar_count=len(bars),
    )


MIN_BARS_DAILY = 60
MIN_BARS_WEEKLY = 300


# Tick-aligned bin sizes per symbol — institutional convention. Used when
# settings.yaml lacks vp_bin_size or callers go through from_store() directly.
DEFAULT_BIN_SIZE: dict[str, float] = {
    "BTCUSDT": 25.0,    # $25 bins (Sierra/Bookmap convention; ~52 bins per $1300 daily range)
    "XAUTUSDT": 1.0,    # $1 bins
    "BTCUSD": 25.0,     # Vantage CFD
    "XAUUSD+": 1.0,     # Vantage CFD
    "XAUUSD": 1.0,      # generic spot XAU
}


def _resolve_bin_size(symbol: str) -> float | None:
    """Read vp_bin_size[symbol] from config/settings.yaml; fall back to DEFAULT_BIN_SIZE."""
    try:
        import yaml as _yaml
        from pathlib import Path as _Path
        _root = _Path(__file__).resolve().parent.parent.parent
        _cfg = _yaml.safe_load((_root / "config" / "settings.yaml").read_text())
        bs_cfg = (_cfg or {}).get("vp_cache", {}).get("vp_bin_size", {}) or {}
        if symbol in bs_cfg:
            return float(bs_cfg[symbol])
    except Exception:
        pass
    return DEFAULT_BIN_SIZE.get(symbol)


def from_store(symbol: str, primary_tf: str, period: str) -> VolumeProfilePeriod | None:
    """Convenience: compute VP from state_store bars for the given period."""
    from pipeline.state_store import store
    import time

    s = store()
    now_ts = int(time.time())
    start_ts, _ = _period_bounds(now_ts, period)

    all_bars = s.recent(symbol, primary_tf, 10_000)
    period_bars = [b for b in all_bars if b.close_ts >= start_ts]

    min_bars = MIN_BARS_WEEKLY if period == "weekly" else MIN_BARS_DAILY
    if len(period_bars) < min_bars:
        return None

    latest = period_bars[-1]
    current_close = latest.ohlc.c
    bin_size = _resolve_bin_size(symbol)

    # Previous period for naked POC
    prev_period_bars = [b for b in all_bars if b.close_ts < start_ts]
    prev_poc: float | None = None
    if prev_period_bars:
        prev_built = _build_bins(prev_period_bars[-500:], bin_size=bin_size)
        if prev_built is not None:
            prev_bins, _, prev_bin_size, prev_p_min, _ = prev_built
            n = len(prev_bins)
            max_v = float(prev_bins.max())
            tied = [i for i in range(n) if prev_bins[i] == max_v]
            prev_poc_idx = tied[len(tied) // 2]
            if bin_size is not None and bin_size > 0:
                prev_poc = round(prev_p_min + prev_poc_idx * prev_bin_size, 4)
            else:
                prev_poc = round(prev_p_min + (prev_poc_idx + 0.5) * prev_bin_size, 2)

    return compute(period_bars, period, current_close, prev_poc=prev_poc, bin_size=bin_size)
