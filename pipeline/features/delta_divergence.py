"""Delta Divergence Detection.

Divergence = price and cumulative delta moving in OPPOSITE directions.
This reveals hidden order flow: smart money accumulating/distributing
while price appears to move against them.

Types:
  hidden_buying:   price making lower low, delta making higher low
                   → sellers can't push price lower, buyers absorbing
                   → long signal at support

  hidden_selling:  price making higher high, delta making lower high
                   → buyers can't push price higher, sellers absorbing
                   → short signal at resistance

  regular_bullish: price lower low, delta higher low (same as hidden_buying
                   when measured bar-to-bar — used for trend reversal context)

  regular_bearish: price higher high, delta lower high

Detection window: N bars (default 5). Evaluates start vs end of the window.
"""

from __future__ import annotations

from dataclasses import dataclass

LOOKBACK = 20         # bars used to measure divergence (standard analysis window)
MIN_PRICE_MOVE = 0.0003  # minimum price move as fraction of price (filters noise)
MIN_DELTA_BARS = 3    # need at least this many bars with non-zero delta


@dataclass
class DivergenceSignal:
    type: str           # "hidden_buying" | "hidden_selling" | "regular_bullish" | "regular_bearish" | "none"
    confidence: float   # 0.0 – 1.0
    price_start: float  # price at window start
    price_end: float    # price at window end
    delta_start: float  # cumulative delta at window start
    delta_end: float    # cumulative delta at window end
    price_change_pct: float
    delta_divergence_ratio: float  # how much delta diverged relative to price move
    reason: str


_NONE = DivergenceSignal(
    type="none", confidence=0.0,
    price_start=0.0, price_end=0.0,
    delta_start=0.0, delta_end=0.0,
    price_change_pct=0.0, delta_divergence_ratio=0.0,
    reason="",
)


def _cumulative_delta(bars) -> list[float]:
    """Running sum of per-bar delta across the window."""
    result: list[float] = []
    acc = 0.0
    for bar in bars:
        acc += bar.delta or 0.0
        result.append(acc)
    return result


def detect(bars, n: int = LOOKBACK) -> DivergenceSignal:
    """Detect divergence between price and cumulative delta over last N bars.

    Args:
        bars: Recent bars ascending by close_ts. Needs at least n bars.
        n:    Lookback window (default 5 bars).
    """
    if len(bars) < max(n, 3):
        return _NONE

    window = bars[-n:]
    valid_delta = [b for b in window if b.delta is not None and b.delta != 0]
    if len(valid_delta) < MIN_DELTA_BARS:
        return _NONE

    cum_deltas = _cumulative_delta(window)

    price_start = window[0].ohlc.c
    price_end = window[-1].ohlc.c
    delta_start = cum_deltas[0]
    delta_end = cum_deltas[-1]

    if price_start <= 0:
        return _NONE

    price_chg = (price_end - price_start) / price_start
    delta_chg = delta_end - delta_start  # absolute (units vary by instrument)

    # Normalise delta change relative to the window's max absolute delta
    max_abs_delta = max(abs(d) for d in cum_deltas) or 1.0
    delta_chg_norm = delta_chg / max_abs_delta  # -1 to +1

    # Noise filter: price must have moved meaningfully
    if abs(price_chg) < MIN_PRICE_MOVE:
        return _NONE

    signal_type = "none"
    confidence = 0.0
    reason = ""

    # Hidden buying: price down, delta up → sellers exhausted, buyers absorbing
    if price_chg < 0 and delta_chg > 0:
        signal_type = "hidden_buying"
        # Confidence scales with how much delta diverges from price direction
        divergence_ratio = abs(delta_chg_norm - price_chg)  # both negative if no div
        confidence = _divergence_confidence(abs(price_chg), divergence_ratio)
        reason = (
            f"price {price_chg*100:.2f}% lower but delta +{delta_chg:.1f} "
            f"(sellers losing force)"
        )

    # Hidden selling: price up, delta down → buyers exhausted, sellers absorbing
    elif price_chg > 0 and delta_chg < 0:
        signal_type = "hidden_selling"
        divergence_ratio = abs(delta_chg_norm - price_chg)
        confidence = _divergence_confidence(abs(price_chg), divergence_ratio)
        reason = (
            f"price {price_chg*100:.2f}% higher but delta {delta_chg:.1f} "
            f"(buyers losing force)"
        )

    # Regular bullish: both price and delta lower, but delta declining LESS (buying support)
    elif price_chg < 0 and delta_chg < 0:
        # Bullish divergence: price lows > prior lows, delta lows also less negative
        if abs(delta_chg_norm) < abs(price_chg) * 0.5:
            signal_type = "regular_bullish"
            confidence = _divergence_confidence(abs(price_chg), abs(price_chg) - abs(delta_chg_norm))
            reason = f"price lower low but delta less negative (buying support building)"

    # Regular bearish: both up, but delta gaining LESS (selling pressure)
    elif price_chg > 0 and delta_chg > 0:
        if delta_chg_norm < price_chg * 0.5:
            signal_type = "regular_bearish"
            confidence = _divergence_confidence(abs(price_chg), price_chg - delta_chg_norm)
            reason = f"price higher high but delta rising less (selling pressure building)"

    if signal_type == "none":
        return _NONE

    return DivergenceSignal(
        type=signal_type,
        confidence=round(confidence, 2),
        price_start=price_start,
        price_end=price_end,
        delta_start=delta_start,
        delta_end=delta_end,
        price_change_pct=round(price_chg * 100, 4),
        delta_divergence_ratio=round(divergence_ratio if signal_type in ("hidden_buying", "hidden_selling") else 0.0, 4),
        reason=reason,
    )


def _divergence_confidence(price_move: float, divergence: float) -> float:
    """Confidence based on size of price move and degree of divergence."""
    # Stronger price move + stronger divergence = higher confidence
    move_score = min(1.0, price_move / 0.005)         # maxes at 0.5% move
    div_score = min(1.0, divergence / 0.30)           # maxes at 30% divergence ratio
    raw = 0.50 * move_score + 0.50 * div_score
    return min(0.90, max(0.35, 0.30 + raw * 0.65))


def bullish(signal: DivergenceSignal) -> bool:
    return signal.type in ("hidden_buying", "regular_bullish")


def bearish(signal: DivergenceSignal) -> bool:
    return signal.type in ("hidden_selling", "regular_bearish")


def from_store(symbol: str, primary_tf: str, n: int = LOOKBACK) -> DivergenceSignal:
    """Convenience: detect divergence from state_store recent bars."""
    from pipeline.state_store import store
    s = store()
    bars = s.recent(symbol, primary_tf, n + 2)
    return detect(bars, n=n)
