"""CVD Candlestick — cumulative delta plotted as OHLC candles.

Plotting CVD as a candle series surfaces patterns that LEAD price:
  - CVD breakout: CVD exceeds prior swing high/low (momentum confirmation)
  - CVD hidden buying: price LL but CVD HL (absorption at bottom)
  - CVD hidden selling: price HH but CVD LH (distribution at top)
  - CVD divergence: price HH + CVD failed to HH (reversal warning)
  - CVD reclaim: CVD recovered above prior support level
  - CVD wick trap: cvd_high made new HH but cvd_close reversed below open (trapped aggressors)

Intra-bar CVD extremes (high/low) are approximated from OHLC direction + delta:
  - Bull price bar (close > open): CVD likely rose → cvd_high ≈ cvd_close, cvd_low ≈ cvd_open
  - Bear price bar (close < open): CVD likely fell → cvd_high ≈ cvd_open, cvd_low ≈ cvd_close
  - Divergence bars (price bull + delta<0 or price bear + delta>0): extremes estimated conservatively

Replaces pipeline/features/delta_divergence.py (which was deleted in Phase 1).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CVDCandle:
    bar_id: str
    ts: int
    cvd_open: float    # cumulative delta at bar start
    cvd_high: float    # estimated CVD max intra-bar
    cvd_low: float     # estimated CVD min intra-bar
    cvd_close: float   # cumulative delta at bar close
    delta: float       # bar net delta = cvd_close - cvd_open
    price_open: float
    price_close: float


@dataclass
class CVDSignal:
    pattern: str       # "breakout_long"|"breakout_short"|"hidden_buying"|"hidden_selling"|
                       # "divergence_bear"|"divergence_bull"|"reclaim"|"wick_trap"|"none"
    side: str          # "long" | "short" | "neutral"
    confidence: float  # 0.0–1.0
    reference_level: float  # CVD level being broken/reclaimed/diverged
    reason: str


_NONE_SIGNAL = CVDSignal(pattern="none", side="neutral", confidence=0.0,
                         reference_level=0.0, reason="")

SWING_LOOKBACK = 20   # bars for CVD swing high/low detection


def build_cvd_candles(bars: list) -> list[CVDCandle]:
    """Build CVD OHLC candle series from bars.

    Each bar's delta = net CVD change for that bar.
    Intra-bar extremes approximated from price direction + delta sign.
    """
    if not bars:
        return []

    candles: list[CVDCandle] = []
    running_cvd = 0.0

    for bar in bars:
        delta = bar.delta or 0.0
        cvd_open = running_cvd
        cvd_close = running_cvd + delta

        price_bull = bar.ohlc.c >= bar.ohlc.o
        delta_positive = delta > 0

        if price_bull and delta_positive:
            # Classic bull bar: CVD rose throughout
            cvd_high, cvd_low = cvd_close, cvd_open
        elif not price_bull and not delta_positive:
            # Classic bear bar: CVD fell throughout
            cvd_high, cvd_low = cvd_open, cvd_close
        elif price_bull and not delta_positive:
            # Divergence: price up, CVD down (hidden selling / distribution)
            # CVD oscillated — conservative estimate: min/max of open+close
            cvd_high = max(cvd_open, cvd_close)
            cvd_low = min(cvd_open, cvd_close)
        else:
            # Divergence: price down, CVD up (hidden buying / absorption)
            cvd_high = max(cvd_open, cvd_close)
            cvd_low = min(cvd_open, cvd_close)

        candles.append(CVDCandle(
            bar_id=bar.bar_id,
            ts=bar.close_ts,
            cvd_open=cvd_open,
            cvd_high=cvd_high,
            cvd_low=cvd_low,
            cvd_close=cvd_close,
            delta=delta,
            price_open=bar.ohlc.o,
            price_close=bar.ohlc.c,
        ))
        running_cvd = cvd_close

    return candles


def _swing_high_low(values: list[float], half: int = 2) -> tuple[float | None, float | None]:
    """Return (swing_high, swing_low) from a series using ±half-bar window."""
    n = len(values)
    if n < (2 * half + 1):
        return None, None
    swing_h = max(values[half: n - half]) if n > 2 * half else None
    swing_l = min(values[half: n - half]) if n > 2 * half else None
    return swing_h, swing_l


def detect(bars: list, lookback: int = SWING_LOOKBACK) -> CVDSignal:
    """Detect the dominant CVD pattern over the last `lookback` bars.

    Returns the highest-confidence signal found, or _NONE_SIGNAL.
    """
    window = bars[-lookback:] if len(bars) >= lookback else bars
    if len(window) < 6:
        return _NONE_SIGNAL

    candles = build_cvd_candles(window)
    if not candles:
        return _NONE_SIGNAL

    # Reference windows: prior 15 bars for swing detection
    ref = candles[:-3] if len(candles) > 3 else candles
    cur = candles[-1]
    prev = candles[-2]

    cvd_highs = [c.cvd_high for c in ref]
    cvd_lows = [c.cvd_low for c in ref]
    prior_cvd_swing_high = max(cvd_highs) if cvd_highs else None
    prior_cvd_swing_low = min(cvd_lows) if cvd_lows else None

    price_highs = [c.price_close for c in ref]
    price_lows = [c.price_close for c in ref]
    prior_price_high = max(price_highs) if price_highs else None
    prior_price_low = min(price_lows) if price_lows else None

    candidates: list[CVDSignal] = []

    # ── 1. CVD breakout ────────────────────────────────────────────────────────
    if prior_cvd_swing_high is not None and cur.cvd_high > prior_cvd_swing_high:
        candidates.append(CVDSignal(
            pattern="breakout_long", side="long",
            confidence=min(0.90, 0.65 + (cur.cvd_high - prior_cvd_swing_high) / (abs(prior_cvd_swing_high) + 1) * 2),
            reference_level=prior_cvd_swing_high,
            reason=f"cvd_high={cur.cvd_high:.0f} > prior_swing={prior_cvd_swing_high:.0f}",
        ))
    if prior_cvd_swing_low is not None and cur.cvd_low < prior_cvd_swing_low:
        candidates.append(CVDSignal(
            pattern="breakout_short", side="short",
            confidence=min(0.90, 0.65 + (prior_cvd_swing_low - cur.cvd_low) / (abs(prior_cvd_swing_low) + 1) * 2),
            reference_level=prior_cvd_swing_low,
            reason=f"cvd_low={cur.cvd_low:.0f} < prior_swing={prior_cvd_swing_low:.0f}",
        ))

    # ── 2. Hidden buying (price LL, CVD HL) ────────────────────────────────────
    if (prior_price_low is not None and prior_cvd_swing_low is not None
            and cur.price_close < prior_price_low and cur.cvd_close > prior_cvd_swing_low):
        candidates.append(CVDSignal(
            pattern="hidden_buying", side="long",
            confidence=0.75,
            reference_level=prior_cvd_swing_low,
            reason=f"price LL ({cur.price_close:.2f} < {prior_price_low:.2f}) but CVD HL ({cur.cvd_close:.0f} > {prior_cvd_swing_low:.0f})",
        ))

    # ── 3. Hidden selling (price HH, CVD LH) ───────────────────────────────────
    if (prior_price_high is not None and prior_cvd_swing_high is not None
            and cur.price_close > prior_price_high and cur.cvd_close < prior_cvd_swing_high):
        candidates.append(CVDSignal(
            pattern="hidden_selling", side="short",
            confidence=0.75,
            reference_level=prior_cvd_swing_high,
            reason=f"price HH ({cur.price_close:.2f} > {prior_price_high:.2f}) but CVD LH ({cur.cvd_close:.0f} < {prior_cvd_swing_high:.0f})",
        ))

    # ── 4. CVD divergence (price HH but CVD failed to HH) ─────────────────────
    if (prior_price_high is not None and prior_cvd_swing_high is not None
            and cur.price_close > prior_price_high
            and cur.cvd_close <= prior_cvd_swing_high * 0.95):  # CVD ≤ 95% of prior high
        candidates.append(CVDSignal(
            pattern="divergence_bear", side="short",
            confidence=0.70,
            reference_level=prior_cvd_swing_high,
            reason=f"price new HH={cur.price_close:.2f} but CVD={cur.cvd_close:.0f} failed prior {prior_cvd_swing_high:.0f}",
        ))
    if (prior_price_low is not None and prior_cvd_swing_low is not None
            and cur.price_close < prior_price_low
            and cur.cvd_close >= prior_cvd_swing_low * 0.95):  # CVD ≥ 95% of prior low (closer to 0)
        candidates.append(CVDSignal(
            pattern="divergence_bull", side="long",
            confidence=0.70,
            reference_level=prior_cvd_swing_low,
            reason=f"price new LL={cur.price_close:.2f} but CVD={cur.cvd_close:.0f} failed prior {prior_cvd_swing_low:.0f}",
        ))

    # ── 5. CVD reclaim ─────────────────────────────────────────────────────────
    # CVD fell below prior support (prev bar) then recovered above it this bar
    if (len(candles) >= 3
            and prior_cvd_swing_low is not None
            and prev.cvd_close < prior_cvd_swing_low
            and cur.cvd_close >= prior_cvd_swing_low):
        candidates.append(CVDSignal(
            pattern="reclaim", side="long",
            confidence=0.65,
            reference_level=prior_cvd_swing_low,
            reason=f"CVD reclaimed support {prior_cvd_swing_low:.0f} (was {prev.cvd_close:.0f} → now {cur.cvd_close:.0f})",
        ))

    # ── 6. CVD wick trap ───────────────────────────────────────────────────────
    # cvd_high made new high BUT cvd_close reversed below cvd_open (trapped aggressors)
    if (prior_cvd_swing_high is not None
            and cur.cvd_high > prior_cvd_swing_high
            and cur.cvd_close < cur.cvd_open):
        candidates.append(CVDSignal(
            pattern="wick_trap", side="short",
            confidence=0.72,
            reference_level=cur.cvd_open,
            reason=f"cvd_high={cur.cvd_high:.0f} new HH but closed at {cur.cvd_close:.0f} < open {cur.cvd_open:.0f}",
        ))
    if (prior_cvd_swing_low is not None
            and cur.cvd_low < prior_cvd_swing_low
            and cur.cvd_close > cur.cvd_open):
        candidates.append(CVDSignal(
            pattern="wick_trap", side="long",
            confidence=0.72,
            reference_level=cur.cvd_open,
            reason=f"cvd_low={cur.cvd_low:.0f} new LL but closed at {cur.cvd_close:.0f} > open {cur.cvd_open:.0f}",
        ))

    if not candidates:
        return _NONE_SIGNAL

    return max(candidates, key=lambda s: s.confidence)


def from_store(symbol: str, primary_tf: str = "15m", lookback: int = SWING_LOOKBACK) -> CVDSignal:
    """Convenience: detect CVD pattern from state_store bars."""
    try:
        from pipeline.state_store import store
        bars = store().recent(symbol, primary_tf, lookback + 5)
        return detect(bars, lookback=lookback)
    except Exception:
        return _NONE_SIGNAL
