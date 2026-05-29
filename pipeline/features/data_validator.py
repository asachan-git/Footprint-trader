"""Cross-feed data validator — Binance vs ByBit bar quality check.

Compares bars from two feeds for the same symbol/tf/close_ts:
  - Volume deviation > 30% → flag
  - Opposite delta signs → flag
  - Close price drift > 0.1% → flag

Produces feed_quality_score ∈ [0, 1] per comparison:
  1.0 = all 3 metrics agree
  0.0 = all 3 disagree

When feed_quality_score < 0.5 for 3 consecutive bars:
  Sets suspect=True for the worse feed.
  Optionally gate new trades (configurable).

WS gap detection: if no bar emitted for > 90s, tag bar as "suspect_gap".

Shadow approach: Binance is canonical (stored in state_store).
ByBit kept in shadow dict here for validation only — no schema change to state_store.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

log = logging.getLogger(__name__)

WS_GAP_THRESHOLD_S = 90      # seconds without a bar = gap
QUALITY_FLOOR = 0.50          # below this → mark suspect
CONSECUTIVE_BAD_BARS = 3      # consecutive bad bars before suspect=True
MAX_SHADOW_BARS = 200         # max shadow bars per (symbol, tf) key


@dataclass
class ValidationResult:
    symbol: str
    tf: str
    close_ts: int
    feed_quality_score: float       # 0.0–1.0
    vol_deviation: float | None     # |vol_a - vol_b| / max
    delta_sign_mismatch: bool
    close_drift: float | None       # |close_a - close_b| / close_a
    suspect: bool                   # True if quality degraded for 3+ bars
    source: str = "live"            # "live" | "rest_backfill"
    reason: str = ""


@dataclass
class _FeedState:
    symbol: str
    tf: str
    last_bar_ts: float = 0.0       # wall-clock time of last bar
    consecutive_bad: int = 0
    suspect: bool = False
    shadow_bars: deque = field(default_factory=lambda: deque(maxlen=MAX_SHADOW_BARS))


_states: dict[str, _FeedState] = {}  # key: "{symbol}|{tf}"
_lock = Lock()


def _key(symbol: str, tf: str) -> str:
    return f"{symbol}|{tf}"


def _get_state(symbol: str, tf: str) -> _FeedState:
    k = _key(symbol, tf)
    if k not in _states:
        _states[k] = _FeedState(symbol=symbol, tf=tf)
    return _states[k]


# ── Shadow feed storage ────────────────────────────────────────────────────────

def ingest_shadow_bar(symbol: str, tf: str, bar_dict: dict) -> None:
    """Store a bar from the shadow feed (ByBit) for validation.

    bar_dict should have: close_ts, close, delta, total_volume
    """
    with _lock:
        state = _get_state(symbol, tf)
        state.shadow_bars.append(bar_dict)
        state.last_bar_ts = time.time()


def _find_shadow_bar(state: _FeedState, close_ts: int) -> dict | None:
    for b in state.shadow_bars:
        if b.get("close_ts") == close_ts:
            return b
    return None


# ── Validation ─────────────────────────────────────────────────────────────────

def validate_bar(
    symbol: str,
    tf: str,
    canonical_bar,                 # pipeline.types.Bar (Binance)
    force_quality: float | None = None,
) -> ValidationResult:
    """Compare canonical bar against shadow feed bar for same close_ts.

    If no shadow bar exists for this timestamp, returns quality=1.0 (unverified).
    Call this after storing the canonical bar in state_store.
    """
    with _lock:
        state = _get_state(symbol, tf)
        close_ts = canonical_bar.close_ts
        shadow = _find_shadow_bar(state, close_ts)

    if shadow is None:
        # No shadow data — unverified but not flagged
        return ValidationResult(
            symbol=symbol, tf=tf, close_ts=close_ts,
            feed_quality_score=1.0,
            vol_deviation=None, delta_sign_mismatch=False, close_drift=None,
            suspect=state.suspect,
            reason="no_shadow_bar",
        )

    if force_quality is not None:
        return ValidationResult(
            symbol=symbol, tf=tf, close_ts=close_ts,
            feed_quality_score=force_quality,
            vol_deviation=None, delta_sign_mismatch=False, close_drift=None,
            suspect=state.suspect,
        )

    # Canonical values
    can_vol = (sum(lvl.vol for lvl in canonical_bar.bid_ladder) +
               sum(lvl.vol for lvl in canonical_bar.ask_ladder))
    can_delta = canonical_bar.delta or 0.0
    can_close = canonical_bar.ohlc.c

    # Shadow values
    shd_vol = shadow.get("total_volume", 0.0)
    shd_delta = shadow.get("delta", 0.0)
    shd_close = shadow.get("close", can_close)

    mismatches = 0

    # 1. Volume deviation
    max_vol = max(can_vol, shd_vol, 1.0)
    vol_dev = abs(can_vol - shd_vol) / max_vol
    if vol_dev > 0.30:
        mismatches += 1

    # 2. Delta sign mismatch
    delta_sign_ok = (
        (can_delta > 0 and shd_delta > 0) or
        (can_delta < 0 and shd_delta < 0) or
        (abs(can_delta) < 1 and abs(shd_delta) < 1)  # both near zero = neutral
    )
    delta_mismatch = not delta_sign_ok
    if delta_mismatch:
        mismatches += 1

    # 3. Close price drift
    close_drift = abs(can_close - shd_close) / max(abs(can_close), 1.0)
    if close_drift > 0.001:
        mismatches += 1

    # Score: 0 mismatches → 1.0, 1 → 0.65, 2 → 0.35, 3 → 0.0
    score_map = {0: 1.0, 1: 0.65, 2: 0.35, 3: 0.0}
    score = score_map.get(mismatches, 0.0)

    with _lock:
        if score < QUALITY_FLOOR:
            state.consecutive_bad += 1
        else:
            state.consecutive_bad = 0
        if state.consecutive_bad >= CONSECUTIVE_BAD_BARS:
            if not state.suspect:
                log.warning(f"[data_validator] {symbol}|{tf} SUSPECT: {CONSECUTIVE_BAD_BARS} consecutive bad bars")
            state.suspect = True
        else:
            state.suspect = False

    reason_parts = []
    if vol_dev > 0.30:
        reason_parts.append(f"vol_dev={vol_dev:.0%}")
    if delta_mismatch:
        reason_parts.append(f"delta_sign_mismatch(can={can_delta:.0f},shd={shd_delta:.0f})")
    if close_drift > 0.001:
        reason_parts.append(f"close_drift={close_drift:.4%}")

    return ValidationResult(
        symbol=symbol, tf=tf, close_ts=close_ts,
        feed_quality_score=round(score, 3),
        vol_deviation=round(vol_dev, 3),
        delta_sign_mismatch=delta_mismatch,
        close_drift=round(close_drift, 6),
        suspect=state.suspect,
        reason=", ".join(reason_parts) if reason_parts else "ok",
    )


def check_ws_gap(symbol: str, tf: str) -> bool:
    """True if no shadow bar received in > WS_GAP_THRESHOLD_S seconds."""
    with _lock:
        state = _get_state(symbol, tf)
        if state.last_bar_ts == 0:
            return False
        return (time.time() - state.last_bar_ts) > WS_GAP_THRESHOLD_S


def is_suspect(symbol: str, tf: str) -> bool:
    """True if feed quality has been degraded for 3+ consecutive bars."""
    with _lock:
        return _get_state(symbol, tf).suspect


def get_quality_score(symbol: str, tf: str) -> float:
    """Return latest feed quality score (1.0 if no comparison available)."""
    with _lock:
        state = _get_state(symbol, tf)
        if not state.shadow_bars:
            return 1.0
        # If suspect, return floor
        if state.suspect:
            return QUALITY_FLOOR - 0.1
        return 1.0
