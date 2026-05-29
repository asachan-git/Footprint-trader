"""Wave Phase + CVD Quality + Fibonacci Level Computation.

Identifies current phase (impulse vs correction) from price structure + CVD.
Fibonacci levels from impulse leg drive grid entry tiers (retrace) and TP tiers (extend).

Elliott Wave counting runs on 15m bars (100-bar lookback) to identify which
wave we're in: w1/w2/w3/w4/w5 or corrective wA/wB/wC.
Wave 3 = strongest impulse (best trend-continuation entry, wider TP).
Wave 5 = final leg (weaker volume, CVD divergence common — watch exhaustion).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Fibonacci ratios
RETRACE_LEVELS = (0.382, 0.500, 0.618, 0.786)
EXTEND_LEVELS  = (1.000, 1.272, 1.618)

MIN_IMPULSE_BARS = 4
MIN_IMPULSE_PCT  = 0.002   # 0.2% minimum impulse size
CVD_HOLD_TOLERANCE = 0.30

# Wave counting constants
WAVE_COUNT_LOOKBACK = 100   # 15m bars ≈ 25 hours of structure
WAVE_SWING_HALF = 3         # ±3 bars = ±45 min at 15m for swing detection


@dataclass
class WavePhase:
    phase: str              # "impulse" | "correction" | "unknown"
    direction: str          # "up" | "down"
    confidence: float       # 0.0 – 1.0

    impulse_low: float | None
    impulse_high: float | None
    impulse_range: float

    retrace_pct: float
    cvd_quality: str        # "holding" | "declining" | "diverging" | "unknown"

    fib_retrace: dict[float, float] = field(default_factory=dict)
    fib_extend:  dict[float, float] = field(default_factory=dict)

    invalidation: float | None = None
    reason: str = ""

    # Elliott Wave counting fields (populated when 15m lookback available)
    impulse_count: int = 0       # segment number from anchor (1=w1, 2=w2, … 5=w5, 6+=corrective)
    wave_label: str = "unknown"  # "w1"|"w2"|"w3"|"w4"|"w5"|"wA"|"wB"|"wC"|"unknown"
    is_wave3: bool = False       # wave 3: length ≥ wave 1 + confirmed by structure
    is_wave5: bool = False       # wave 5: shorter than wave 3, CVD diverging


_UNKNOWN = WavePhase(
    phase="unknown", direction="up", confidence=0.0,
    impulse_low=None, impulse_high=None, impulse_range=0.0,
    retrace_pct=0.0, cvd_quality="unknown",
    reason="insufficient data",
)

_WAVE_LABELS: dict[int, str] = {
    1: "w1", 2: "w2", 3: "w3", 4: "w4", 5: "w5",
    6: "wA", 7: "wB", 8: "wC",
}


def detect_swing_points(
    bars, lookback: int = 2
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (swing_highs, swing_lows) as (index, price) pairs.

    lookback: half-window size. Bar i is a swing high if its high is the highest
    in [i-lookback .. i+lookback]. Default 2 = ±2 bars (10 min at 1m, 30 min at 15m).
    Use lookback=3 for 15m wave counting (±45 min).
    """
    half = max(1, lookback)
    highs: list[tuple[int, float]] = []
    lows:  list[tuple[int, float]] = []
    n = len(bars)
    for i in range(half, n - half):
        window_h = [bars[j].ohlc.h for j in range(i - half, i + half + 1)]
        window_l = [bars[j].ohlc.l for j in range(i - half, i + half + 1)]
        if bars[i].ohlc.h == max(window_h):
            highs.append((i, bars[i].ohlc.h))
        if bars[i].ohlc.l == min(window_l):
            lows.append((i, bars[i].ohlc.l))
    return highs, lows


def _cum_delta_at(bars, idx: int) -> float:
    return sum(b.delta or 0.0 for b in bars[:idx + 1])


def _cvd_quality(bars, impulse_start_idx: int, impulse_end_idx: int,
                 current_idx: int, direction: str) -> str:
    cvd_impulse_end = _cum_delta_at(bars, impulse_end_idx)
    cvd_now = _cum_delta_at(bars, current_idx)
    price_impulse_end = bars[impulse_end_idx].ohlc.c
    price_now = bars[current_idx].ohlc.c

    if price_impulse_end == 0:
        return "unknown"

    price_retrace_chg = abs(price_now - price_impulse_end) / abs(price_impulse_end)

    if direction == "up":
        cvd_chg = cvd_now - cvd_impulse_end
        if cvd_chg >= 0:
            return "holding"
        cvd_decline_ratio = abs(cvd_chg) / (abs(cvd_impulse_end) + 1)
        if cvd_decline_ratio <= CVD_HOLD_TOLERANCE * price_retrace_chg:
            return "holding"
        if cvd_decline_ratio <= 0.5:
            return "declining"
        return "diverging"
    else:
        cvd_chg = cvd_now - cvd_impulse_end
        if cvd_chg <= 0:
            return "holding"
        cvd_rise_ratio = abs(cvd_chg) / (abs(cvd_impulse_end) + 1)
        if cvd_rise_ratio <= CVD_HOLD_TOLERANCE * price_retrace_chg:
            return "holding"
        if cvd_rise_ratio <= 0.5:
            return "declining"
        return "diverging"


def _fib_levels(low: float, high: float, direction: str) -> tuple[dict[float, float], dict[float, float]]:
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
    else:
        for r in RETRACE_LEVELS:
            retrace[r] = round(low + rng * r, 6)
        for e in EXTEND_LEVELS:
            extend[e] = round(high - rng * e, 6)
    return retrace, extend


# ── Elliott Wave counting ──────────────────────────────────────────────────────

def _build_alternating_swings(
    highs: list[tuple[int, float]], lows: list[tuple[int, float]]
) -> list[tuple[int, float, str]]:
    """Merge highs + lows into alternating sequence (H L H L …).

    Consecutive same-type swings are collapsed to the most extreme.
    Returns list of (bar_idx, price, "H"|"L").
    """
    combined = [(idx, price, "H") for idx, price in highs] + \
               [(idx, price, "L") for idx, price in lows]
    combined.sort(key=lambda x: x[0])

    filtered: list[tuple[int, float, str]] = []
    for swing in combined:
        if not filtered or filtered[-1][2] != swing[2]:
            filtered.append(swing)
        else:
            # Same type — keep more extreme
            prev = filtered[-1]
            if swing[2] == "H" and swing[1] > prev[1]:
                filtered[-1] = swing
            elif swing[2] == "L" and swing[1] < prev[1]:
                filtered[-1] = swing
    return filtered


def _count_waves(bars, lookback: int = WAVE_COUNT_LOOKBACK) -> tuple[int, str, bool, bool]:
    """Count Elliott Wave position from alternating swing sequence.

    Returns (impulse_count, wave_label, is_wave3, is_wave5).

    Algorithm:
    1. Find swings with ±3 bar window (suitable for 15m structure)
    2. Build alternating H/L sequence
    3. Find macro anchor: lowest low (bullish) or highest high (bearish)
       whichever comes first in the sequence
    4. Count segments from anchor → map to wave label
    5. Confirm wave 3 (length ≥ wave 1) and wave 5 (shorter than wave 3)
    """
    window = bars[-lookback:] if len(bars) >= lookback else bars
    if len(window) < 20:
        return 0, "unknown", False, False

    highs, lows = detect_swing_points(window, lookback=WAVE_SWING_HALF)
    if len(highs) < 2 or len(lows) < 2:
        return 0, "unknown", False, False

    raw_seq = _build_alternating_swings(highs, lows)

    # Filter trivially small swings (flat bars, noise) — min 0.1% price move between swings
    if raw_seq:
        price_ref = raw_seq[0][1] or 1.0
        seq: list[tuple[int, float, str]] = [raw_seq[0]]
        for s in raw_seq[1:]:
            if abs(s[1] - seq[-1][1]) / price_ref >= 0.001:
                seq.append(s)
            elif (s[2] == "H" and s[1] > seq[-1][1]) or (s[2] == "L" and s[1] < seq[-1][1]):
                seq[-1] = s  # replace with more extreme within same micro-cluster
        # EW never needs more than 10 alternating swings; take most recent
        if len(seq) > 10:
            seq = seq[-10:]
    else:
        seq = []

    if len(seq) < 3:
        return 0, "unknown", False, False

    # Locate the macro anchor (most significant extreme in the sequence)
    seq_lows  = [(i, s) for i, s in enumerate(seq) if s[2] == "L"]
    seq_highs = [(i, s) for i, s in enumerate(seq) if s[2] == "H"]

    if not seq_lows or not seq_highs:
        return 0, "unknown", False, False

    min_low_seqidx,  min_low_swing  = min(seq_lows,  key=lambda x: x[1][1])
    max_high_seqidx, max_high_swing = max(seq_highs, key=lambda x: x[1][1])

    # Anchor is whichever extreme comes FIRST in time
    if min_low_seqidx < max_high_seqidx:
        anchor_idx = min_low_seqidx   # bullish: low is anchor
    else:
        anchor_idx = max_high_seqidx  # bearish: high is anchor

    sub = seq[anchor_idx:]           # sequence from anchor to present
    if len(sub) < 2:
        return 0, "unknown", False, False

    segment_count = len(sub) - 1    # completed moves from anchor
    wave_label = _WAVE_LABELS.get(segment_count, "unknown")

    # Wave 3 confirmation: third leg, length ≥ wave 1
    is_wave3 = False
    if segment_count == 3 and len(sub) >= 4:
        w1_range = abs(sub[1][1] - sub[0][1])
        w3_range = abs(sub[3][1] - sub[2][1])
        is_wave3 = w1_range > 0 and w3_range >= w1_range * 0.90

    # Wave 5 confirmation: fifth leg, shorter than wave 3
    is_wave5 = False
    if segment_count == 5 and len(sub) >= 6:
        w3_range = abs(sub[3][1] - sub[2][1])
        w5_range = abs(sub[5][1] - sub[4][1])
        # Wave 5 is typically shorter than wave 3 (no extended wave 5 case)
        is_wave5 = w3_range > 0 and w5_range < w3_range * 0.95

    return segment_count, wave_label, is_wave3, is_wave5


# ── Main classify function ─────────────────────────────────────────────────────

def classify(bars, lookback: int = 20, count_waves: bool = False) -> WavePhase:
    """Identify the current wave phase from recent bars.

    Args:
        bars:        Recent bars ascending by close_ts. At least 8 needed.
        lookback:    Bars to analyse for impulse/correction/Fib structure.
        count_waves: If True, run Elliott Wave counting (needs ≥ 40 bars in `bars`).
                     Set True when calling with 15m bars for full EW labeling.
    """
    window = bars[-lookback:] if len(bars) >= lookback else bars
    if len(window) < MIN_IMPULSE_BARS + 2:
        return _UNKNOWN

    swing_highs, swing_lows = detect_swing_points(window)
    if not swing_highs or not swing_lows:
        return _UNKNOWN

    n = len(window)
    current_idx = n - 1
    best_phase: WavePhase | None = None

    last_low_idx,  last_low_price  = swing_lows[-1]
    last_high_idx, last_high_price = swing_highs[-1]

    if last_low_idx < last_high_idx:
        # Upward impulse
        impulse_range = last_high_price - last_low_price
        min_range = last_low_price * MIN_IMPULSE_PCT
        if impulse_range >= min_range:
            retrace_now = max(0.0, min(1.0, (last_high_price - window[-1].ohlc.c) / impulse_range))
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
                reason=f"up {last_low_price:.2f}→{last_high_price:.2f} retrace {retrace_now*100:.1f}% CVD {cvd_q}",
            )

    elif last_high_idx < last_low_idx:
        # Downward impulse
        impulse_range = last_high_price - last_low_price
        min_range = last_high_price * MIN_IMPULSE_PCT
        if impulse_range >= min_range:
            retrace_now = max(0.0, min(1.0, (window[-1].ohlc.c - last_low_price) / impulse_range))
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
                reason=f"down {last_high_price:.2f}→{last_low_price:.2f} retrace {retrace_now*100:.1f}% CVD {cvd_q}",
            )

    # Elliott Wave counting (only when requested + impulse detected + enough bars)
    if count_waves and best_phase is not None and len(bars) >= 40:
        seg, label, w3, w5 = _count_waves(bars)
        best_phase.impulse_count = seg
        best_phase.wave_label = label
        best_phase.is_wave3 = w3
        best_phase.is_wave5 = w5

    return best_phase or _UNKNOWN


def _assign_phase(retrace_pct: float, cvd_quality: str, direction: str) -> tuple[str, float]:
    if retrace_pct < 0.10:
        return "impulse", 0.65
    if 0.30 <= retrace_pct <= 0.65:
        if cvd_quality == "holding":
            return "correction", 0.80
        if cvd_quality == "declining":
            return "correction", 0.55
        return "correction", 0.45
    if retrace_pct > 0.65:
        return "correction", 0.55 if cvd_quality == "holding" else 0.40
    return "correction", 0.50


def target_tier(wave: WavePhase, current_price: float) -> tuple[str, float | None]:
    if not wave.fib_extend or wave.phase == "unknown":
        return "none", None
    ext = wave.fib_extend
    t1 = ext.get(1.000)
    t2 = ext.get(1.272)
    t3 = ext.get(1.618)
    if wave.direction == "up":
        if t1 and current_price < t1: return "T1", t1
        if t2 and current_price < t2: return "T2", t2
        if t3: return "T3", t3
    else:
        if t1 and current_price > t1: return "T1", t1
        if t2 and current_price > t2: return "T2", t2
        if t3: return "T3", t3
    return "none", None


def nearest_retrace_level(wave: WavePhase, current_price: float) -> tuple[float | None, float | None]:
    if not wave.fib_retrace:
        return None, None
    return min(wave.fib_retrace.items(), key=lambda kv: abs(kv[1] - current_price))


def from_store(symbol: str, primary_tf: str, lookback: int = 20) -> WavePhase:
    """Classify wave phase from state_store bars on given TF."""
    from pipeline.state_store import store
    bars = store().recent(symbol, primary_tf, lookback + 5)
    return classify(bars, lookback=lookback)


def from_store_15m(symbol: str, lookback: int = 20) -> WavePhase:
    """Classify wave phase + Elliott Wave count from 15m bars.

    Uses WAVE_COUNT_LOOKBACK (100 bars) for wave counting and `lookback`
    bars for the impulse/correction/Fib structure.
    """
    from pipeline.state_store import store
    bars = store().recent(symbol, "15m", max(WAVE_COUNT_LOOKBACK + 5, lookback + 5))
    return classify(bars, lookback=lookback, count_waves=True)
