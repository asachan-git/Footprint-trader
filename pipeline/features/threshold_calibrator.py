"""Dynamic threshold calibrator.

Computes per-symbol, per-TF thresholds from recent live bar history.
Used in _has_setup() to filter noise bars before calling Claude.

MFE analysis (2026-05-29, 103 cycles) showed 86% of cycles peak at < 0.15R
(disaster-floor units). Root cause: firing on noise bars — no real directional
pressure. Fix: raise percentile + add ATR-normalized delta gate.

Thresholds (all three must pass to fire a decision):
  min_abs_delta       — raw |delta| ≥ 65th percentile of recent |delta| distribution
  min_delta_ratio     — |delta|/volume ≥ median of recent ratio distribution
  min_delta_atr_ratio — |delta|/ATR_primary_tf ≥ configurable floor (default 0.08)
                        ATR-normalised: separates real pressure from large-but-routine delta

Why ATR-normalisation matters:
  A delta of 100 on a 50-wide ATR bar = strong signal.
  A delta of 100 on a 2000-wide ATR bar = background noise.
  Absolute delta alone can't tell these apart. ATR ratio can.

Cached in memory, recomputed every RECALC_BARS new bars.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

LOG = logging.getLogger(__name__)

RECALC_BARS      = 50    # recompute after this many new bars
MIN_SAMPLE       = 8     # need at least this many buckets to calibrate
DELTA_PERCENTILE = 0.65  # filter bottom 65% of bars by |delta| (was 0.25 — too permissive)
RATIO_PERCENTILE = 0.50  # |delta|/vol threshold at median (unchanged)

# ATR-normalised delta floor: |delta| must be ≥ this fraction of ATR_primary_tf.
# Filters large-absolute-delta bars on wide-ranging sessions (noise, not signal).
# 0.08 = 8% of 1-bar ATR. Empirically: real sweeps/absorptions sit 0.15-0.40×ATR.
DELTA_ATR_FLOOR  = 0.08


class Thresholds(NamedTuple):
    min_abs_delta: float        # 65th-percentile of |delta| distribution
    min_delta_ratio: float      # median |delta|/volume ratio
    min_delta_atr_ratio: float  # min |delta|/ATR_primary_tf (ATR-normalised)
    sample_size: int


_cache: dict[tuple[str, str], tuple[int, Thresholds]] = {}  # (symbol, tf) → (bar_count, thresholds)


def _bucket_seconds(tf: str) -> int:
    return {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}.get(tf, 900)


def _compute(symbol: str, primary_tf: str, target_tf: str) -> Thresholds:
    from pipeline.state_store import store as _store
    from pipeline.features.atr import atr as _atr
    s = _store()
    live_bars = [b for b in s.recent(symbol, primary_tf, 100_000) if b.source == "live"]
    if len(live_bars) < 10:
        return Thresholds(min_abs_delta=0.0, min_delta_ratio=0.0,
                          min_delta_atr_ratio=0.0, sample_size=0)

    # ATR over recent primary-TF bars for normalisation
    current_atr = _atr(live_bars[-15:])

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
        return Thresholds(min_abs_delta=0.0, min_delta_ratio=0.0,
                          min_delta_atr_ratio=0.0, sample_size=len(deltas))

    deltas_sorted = sorted(deltas)
    ratios_sorted = sorted(d / v for d, v in zip(deltas, vols))

    p_delta = max(0, int(len(deltas_sorted) * DELTA_PERCENTILE))
    p_ratio = len(ratios_sorted) // 2  # median

    # ATR-normalised floor: dynamic but bounded.
    # Compute from history: what delta/ATR ratio corresponds to DELTA_PERCENTILE?
    # Use DELTA_ATR_FLOOR as hard minimum in case ATR is tiny (gap/thin session).
    atr_ratio_thresh = DELTA_ATR_FLOOR
    if current_atr > 0:
        hist_atr_ratios = sorted(d / current_atr for d in deltas)
        atr_ratio_thresh = max(
            DELTA_ATR_FLOOR,
            round(hist_atr_ratios[int(len(hist_atr_ratios) * DELTA_PERCENTILE)], 4),
        )

    thresh = Thresholds(
        min_abs_delta=round(deltas_sorted[p_delta], 2),
        min_delta_ratio=round(ratios_sorted[p_ratio], 4),
        min_delta_atr_ratio=atr_ratio_thresh,
        sample_size=len(deltas),
    )
    LOG.info(
        f"[calibrate] {symbol} {target_tf}: "
        f"min_delta={thresh.min_abs_delta} "
        f"ratio={thresh.min_delta_ratio} "
        f"atr_ratio={thresh.min_delta_atr_ratio} "
        f"(ATR={current_atr:.2f}, n={thresh.sample_size})"
    )
    return thresh


def get(symbol: str, primary_tf: str = "1m", target_tf: str = "15m") -> Thresholds:
    """Return calibrated thresholds. Recomputes if stale (every RECALC_BARS bars)."""
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
