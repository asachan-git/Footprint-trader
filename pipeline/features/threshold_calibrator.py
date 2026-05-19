"""Dynamic threshold calibrator.

Computes per-symbol, per-TF thresholds from recent live bar history.
Used in _has_setup() to filter weak delta bars before calling Claude.

Thresholds:
  min_abs_delta  — absolute delta must exceed this (25th percentile of historical |delta|)
  min_delta_ratio — |delta|/volume must exceed this (signal strength, not just size)

Cached in memory, recomputed every RECALC_BARS new bars.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

LOG = logging.getLogger(__name__)

RECALC_BARS = 50        # recompute after this many new bars
MIN_SAMPLE  = 8         # need at least this many 15m buckets to calibrate
PERCENTILE  = 0.25      # threshold = 25th percentile of |delta| (filter bottom 25%)


class Thresholds(NamedTuple):
    min_abs_delta: float
    min_delta_ratio: float
    sample_size: int


_cache: dict[tuple[str, str], tuple[int, Thresholds]] = {}  # (symbol, tf) → (bar_count, thresholds)


def _bucket_seconds(tf: str) -> int:
    return {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}.get(tf, 900)


def _compute(symbol: str, primary_tf: str, target_tf: str) -> Thresholds:
    from pipeline.state_store import store as _store
    s = _store()
    live_bars = [b for b in s.recent(symbol, primary_tf, 100_000) if b.source == "live"]
    if len(live_bars) < 10:
        return Thresholds(min_abs_delta=0.0, min_delta_ratio=0.0, sample_size=0)

    bucket_s = _bucket_seconds(target_tf)
    buckets: dict[int, list] = {}
    for b in live_bars:
        key = (b.close_ts // bucket_s) * bucket_s + bucket_s
        buckets.setdefault(key, []).append(b)

    deltas, vols = [], []
    for bb in buckets.values():
        bid = sum(lvl.vol for b in bb for lvl in b.bid_ladder)
        ask = sum(lvl.vol for b in bb for lvl in b.ask_ladder)
        vol = bid + ask
        if vol > 0:
            deltas.append(abs(ask - bid))
            vols.append(vol)

    if len(deltas) < MIN_SAMPLE:
        return Thresholds(min_abs_delta=0.0, min_delta_ratio=0.0, sample_size=len(deltas))

    deltas_sorted = sorted(deltas)
    ratios_sorted = sorted(d / v for d, v in zip(deltas, vols))
    p_idx = max(0, int(len(deltas_sorted) * PERCENTILE))

    thresh = Thresholds(
        min_abs_delta=round(deltas_sorted[p_idx], 2),
        min_delta_ratio=round(ratios_sorted[len(ratios_sorted) // 2], 4),  # median ratio
        sample_size=len(deltas),
    )
    LOG.info(f"[calibrate] {symbol} {target_tf}: min_delta={thresh.min_abs_delta} "
             f"ratio={thresh.min_delta_ratio} (n={thresh.sample_size})")
    return thresh


def get(symbol: str, primary_tf: str = "1m", target_tf: str = "15m") -> Thresholds:
    """Return calibrated thresholds. Recomputes if stale."""
    from pipeline.state_store import store as _store
    key = (symbol, target_tf)
    s = _store()
    current_count = len(s.recent(symbol, primary_tf, 100_000))
    cached_count, cached = _cache.get(key, (0, None))  # type: ignore[assignment]

    if cached is None or (current_count - cached_count) >= RECALC_BARS:
        fresh = _compute(symbol, primary_tf, target_tf)
        _cache[key] = (current_count, fresh)
        return fresh
    return cached  # type: ignore[return-value]
