"""Wave Phase + CVD Quality + Fibonacci Level Computation.

Identifies where in the impulse-correction-exhaustion cycle the market is,
using price structure and cumulative delta (CVD) together.

CVD is the key differentiator:
  Correction + CVD HOLDING  → healthy pullback, add legs here
  Correction + CVD DECLINING → potential reversal, reduce/skip
  Exhaustion (delta divergence at extreme) → exit signal before price turns

Fibonacci levels are measured from the detected impulse leg and give
precise grid entry tiers (retrace) and dynamic TP tiers (extension).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Fibonacci ratios used for retracement and extension
RETRACE_LEVELS = (0.382, 0.500, 0.618, 0.786)
EXTEND_LEVELS  = (1.000, 1.272, 1.618)

# Minimum bars to form an identifiable impulse leg
MIN_IMPULSE_BARS = 4
# An impulse must move at least this fraction of the recent price range
MIN_IMPULSE_PCT  = 0.002   # 0.2%
# CVD holding tolerance: CVD decline ≤ this fraction of price decline = "holding"
CVD_HOLD_TOLERANCE = 0.30


@dataclass
class WavePhase:
    phase: str              # "impulse" | "correction" | "exhaustion" | "reversal" | "unknown"
    direction: str          # "up" | "down" (direction of the identified impulse)
    confidence: float       # 0.0 – 1.0

    # Impulse leg bounds (None if not identified)
    impulse_low: float | None
    impulse_high: float | None
    impulse_range: float    # high - low (0 if not identified)

    # Retracement state
    retrace_pct: float      # 0.0 – 1.0 (how deep current correction is)
    cvd_quality: str        # "holding" | "declining" | "diverging" | "unknown"

    # Fibonacci price levels (populated when impulse is identified)
    fib_retrace: dict[float, float] = field(default_factory=dict)   # {0.382: price, ...}
    fib_extend:  dict[float, float] = field(default_factory=dict)   # {1.000: price, ...}

    # Invalidation price: close below this = impulse thesis dead
    invalidation: float | None = None

    reason: str = ""


_UNKNOWN = WavePhase(
    phase="unknown", direction="up", confidence=0.0,
    impulse_low=None, impulse_high=None, impulse_range=0.0,
    retrace_pct=0.0, cvd_quality="unknown",
    reason="insufficient data",
)


def detect_swing_points(bars, lookback: int = 5) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (swing_highs, swing_lows) as (index, price) pairs.

    A bar is a swing high if its high is the highest in a [i-2 .. i+2] window.
    A bar is a swing low if its low is the lowest in that window.
    """
    highs: list[tuple[int, float]] = []
    lows:  list[tuple[int, float]] = []
    n = len(bars)
    for i in range(2, n - 2):
        window_h = [bars[j].ohlc.h for j in range(i - 2, i + 3)]
        window_l = [bars[j].ohlc.l for j in range(i - 2, i + 3)]
        if bars[i].ohlc.h == max(window_h):
            highs.append((i, bars[i].ohlc.h))
        if bars[i].ohlc.l == min(window_l):
            lows.append((i, bars[i].ohlc.l))
    return highs, lows


def _cum_delta_at(bars, idx: int) -> float:
    """Cumulative delta from bar 0 up to and including bar idx."""
    return sum(b.delta or 0.0 for b in bars[:idx + 1])


def _cvd_quality(
    bars,
    impulse_start_idx: int,
    impulse_end_idx: int,
    current_idx: int,
    direction: str,
) -> str:
    """Assess CVD behaviour during the correction.

    Compares:
    - CVD at impulse end (peak/trough of the impulse)
    - CVD now (during the correction)

    direction: "up" impulse → correction = price falling, check if CVD holding up
    direction: "down" impulse → correction = price rising, check if CVD holding down
    """
    cvd_impulse_end = _cum_delta_at(bars, impulse_end_idx)
    cvd_now = _cum_delta_at(bars, current_idx)

    price_impulse_end = bars[impulse_end_idx].ohlc.c
    price_now = bars[current_idx].ohlc.c

    if price_impulse_end == 0:
        return "unknown"

    price_retrace_chg = abs(price_now - price_impulse_end) / abs(price_impulse_end)

    if direction == "up":
        # Upward impulse → correction is price falling
        # CVD holding = CVD not falling as fast as price
        cvd_chg = cvd_now - cvd_impulse_end   # negative = CVD declining
        if cvd_chg >= 0:
            return "holding"  # CVD actually rising during price pullback (strong)
        # Normalize: how much did CVD fall relative to price fall?
        cvd_decline_ratio = abs(cvd_chg) / (abs(cvd_impulse_end) + 1)
        if cvd_decline_ratio <= CVD_HOLD_TOLERANCE * price_retrace_chg:
            return "holding"
        if cvd_decline_ratio <= 0.5:
            return "declining"
        return "diverging"  # CVD falling hard → reversal risk

    else:  # direction == "down"
        cvd_chg = cvd_now - cvd_impulse_end   # positive = CVD rising (correction buying)
        if cvd_chg <= 0:
            return "holding"  # CVD still falling / flat
        cvd_rise_ratio = abs(cvd_chg) / (abs(cvd_impulse_end) + 1)
        if cvd_rise_ratio <= CVD_HOLD_TOLERANCE * price_retrace_chg:
            return "holding"
        if cvd_rise_ratio <= 0.5:
            return "declining"
        return "diverging"


def _fib_levels(
    low: float, high: float, direction: str
) -> tuple[dict[float, float], dict[float, float]]:
    """Compute Fibonacci retrace and extension levels from an impulse leg.

    For an upward impulse (low → high):
      Retracements are prices below the high.
      Extensions are prices above the high.

    For a downward impulse (high → low):
      Retracements are prices above the low.
      Extensions are prices below the low.
    """
    rng = high - low
    if rng <= 0:
        return {}, {}

    retrace: dict[float, float] = {}
    extend:  dict[float, float] = {}

    if direction == "up":
        for r in RETRACE_LEVELS:
            retrace[r] = round(high - rng * r, 6)
        for e in EXTEND_LEVELS:
            extend[e] = round(low + rng * e, 6)
    else:  # down
        for r in RETRACE_LEVELS:
            retrace[r] = round(low + rng * r, 6)
        for e in EXTEND_LEVELS:
            extend[e] = round(high - rng * e, 6)

    return retrace, extend


def classify(bars, lookback: int = 20) -> WavePhase:
    """Identify the current wave phase from recent bars.

    Args:
        bars:     Recent bars ascending by close_ts. At least 8 needed.
        lookback: How many of the recent bars to analyse for swing structure.
    """
    window = bars[-lookback:] if len(bars) >= lookback else bars
    if len(window) < MIN_IMPULSE_BARS + 2:
        return _UNKNOWN

    swing_highs, swing_lows = detect_swing_points(window)
    if not swing_highs or not swing_lows:
        return _UNKNOWN

    n = len(window)
    current_price = window[-1].ohlc.c
    current_idx = n - 1

    # --- Identify most recent impulse leg ---
    # Try upward impulse: last significant swing_low → last significant swing_high
    # Try downward impulse: last significant swing_high → last significant swing_low
    # Use whichever leg is more recent and meaningful

    best_phase: WavePhase | None = None

    # Upward impulse candidate
    if swing_lows and swing_highs:
        last_low_idx,  last_low_price  = swing_lows[-1]
        last_high_idx, last_high_price = swing_highs[-1]

        # Upward impulse: low comes before high
        if last_low_idx < last_high_idx:
            impulse_range = last_high_price - last_low_price
            min_range = last_low_price * MIN_IMPULSE_PCT
            if impulse_range >= min_range:
                retrace_now = (last_high_price - current_price) / impulse_range
                retrace_now = max(0.0, min(1.0, retrace_now))
                cvd_q = _cvd_quality(window, last_low_idx, last_high_idx, current_idx, "up")
                retrace, extend = _fib_levels(last_low_price, last_high_price, "up")

                phase, conf = _assign_phase(retrace_now, cvd_q, "up")
                best_phase = WavePhase(
                    phase=phase, direction="up", confidence=conf,
                    impulse_low=last_low_price, impulse_high=last_high_price,
                    impulse_range=round(impulse_range, 6),
                    retrace_pct=round(retrace_now, 3),
                    cvd_quality=cvd_q,
                    fib_retrace=retrace, fib_extend=extend,
                    invalidation=round(retrace.get(0.786, last_low_price), 6),
                    reason=f"upward impulse {last_low_price:.2f}→{last_high_price:.2f}, retrace {retrace_now*100:.1f}%, CVD {cvd_q}",
                )

        # Downward impulse: high comes before low
        elif last_high_idx < last_low_idx:
            impulse_range = last_high_price - last_low_price
            min_range = last_high_price * MIN_IMPULSE_PCT
            if impulse_range >= min_range:
                retrace_now = (current_price - last_low_price) / impulse_range
                retrace_now = max(0.0, min(1.0, retrace_now))
                cvd_q = _cvd_quality(window, last_high_idx, last_low_idx, current_idx, "down")
                retrace, extend = _fib_levels(last_low_price, last_high_price, "down")

                phase, conf = _assign_phase(retrace_now, cvd_q, "down")
                best_phase = WavePhase(
                    phase=phase, direction="down", confidence=conf,
                    impulse_low=last_low_price, impulse_high=last_high_price,
                    impulse_range=round(impulse_range, 6),
                    retrace_pct=round(retrace_now, 3),
                    cvd_quality=cvd_q,
                    fib_retrace=retrace, fib_extend=extend,
                    invalidation=round(retrace.get(0.786, last_high_price), 6),
                    reason=f"downward impulse {last_high_price:.2f}→{last_low_price:.2f}, retrace {retrace_now*100:.1f}%, CVD {cvd_q}",
                )

    return best_phase or _UNKNOWN


def _assign_phase(retrace_pct: float, cvd_quality: str, direction: str) -> tuple[str, float]:
    """Assign phase name and confidence from retracement depth and CVD quality."""
    # Not retraced yet — still in impulse or just at top/bottom
    if retrace_pct < 0.10:
        if cvd_quality == "diverging":
            return "exhaustion", 0.70
        return "impulse", 0.65

    # Healthy correction zone (38.2% – 61.8%)
    if 0.30 <= retrace_pct <= 0.65:
        if cvd_quality == "holding":
            return "correction", 0.80
        if cvd_quality == "declining":
            return "correction", 0.55
        return "correction", 0.45  # diverging CVD in correction = watch carefully

    # Deep correction (61.8% – 78.6%) — still valid but weakening
    if 0.65 < retrace_pct <= 0.82:
        if cvd_quality == "holding":
            return "correction", 0.60
        return "reversal", 0.55

    # Beyond 78.6% — invalidation zone
    if retrace_pct > 0.82:
        return "reversal", 0.75

    # Shallow retrace (10% – 30%) — early correction
    return "correction", 0.50


def target_tier(wave: WavePhase, current_price: float) -> tuple[str, float | None]:
    """Suggest current target tier based on wave extension levels.

    Returns (tier_name, price): "T1", "T2", or "T3" with the price target.
    """
    if not wave.fib_extend or wave.phase not in ("impulse", "correction"):
        return "none", None

    ext = wave.fib_extend
    t1 = ext.get(1.000)
    t2 = ext.get(1.272)
    t3 = ext.get(1.618)

    if wave.direction == "up":
        if t1 and current_price < t1:
            return "T1", t1
        if t2 and current_price < t2:
            return "T2", t2
        if t3:
            return "T3", t3
    else:
        if t1 and current_price > t1:
            return "T1", t1
        if t2 and current_price > t2:
            return "T2", t2
        if t3:
            return "T3", t3

    return "none", None


def nearest_retrace_level(wave: WavePhase, current_price: float) -> tuple[float | None, float | None]:
    """Return (fib_ratio, price) of the nearest Fibonacci retrace level to current price."""
    if not wave.fib_retrace:
        return None, None
    closest = min(wave.fib_retrace.items(), key=lambda kv: abs(kv[1] - current_price))
    return closest


def from_store(symbol: str, primary_tf: str, lookback: int = 20) -> WavePhase:
    """Convenience: classify wave phase from state_store recent bars."""
    from pipeline.state_store import store
    s = store()
    bars = s.recent(symbol, primary_tf, lookback + 5)
    return classify(bars, lookback=lookback)
