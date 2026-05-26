"""Mode 2 direction engine — rule-based directional bias via weighted module votes.

Replaces Claude for direction decisions when trading_mode=grid. Each analysis
module casts a vote (-1=strong short, +1=strong long) with a strength weight.
Aggregated vote → (side, bias_strength) consumed by grid_placer.

Modules voting (in order of weight):
  CVD trend          weight 1.0
  VP shape (P/b/D)   weight 0.8
  Market structure   weight 0.9
  FVG fill pressure  weight 0.6
  Wave direction     weight 0.7
  Sweep signal       weight 0.7
  Wick trap          weight 0.6
  Absorption bias    weight 0.5

Final score = Σ(direction × strength) / Σ(strength)
|score| < 0.30 → flat
bias_strength = clamp(round(|score| × 5), 1, 5)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.types import Bar
from pipeline.footprint import build as build_fp


@dataclass
class Vote:
    module: str
    direction: float        # -1.0 .. +1.0
    strength: float         # 0..1 (weight × confidence)
    reason: str


def _cvd_vote(bars: list[Bar], lookback: int = 20) -> Vote | None:
    """CVD trend vote — weight reduced 1.0→0.65, threshold raised 0.10→0.20."""
    recent = [b for b in bars[-lookback:] if b.delta is not None]
    if len(recent) < 3:
        return None
    cvd_sum = sum(b.delta or 0 for b in recent)
    total_vol = sum(abs(b.delta or 0) for b in recent) + 1e-9
    ratio = cvd_sum / total_vol
    if abs(ratio) < 0.20:        # raised from 0.10 → requires stronger conviction
        return None
    direction = 1.0 if ratio > 0 else -1.0
    strength = min(1.0, abs(ratio) * 2) * 0.65    # weight reduced 1.0 → 0.65
    return Vote("cvd", direction, strength, f"cvd_sum={cvd_sum:.0f} ratio={ratio:.2f}")


def _vp_shape_vote(symbol: str) -> Vote | None:
    """VP shape: P = top-heavy short bias, b = bottom-heavy long bias, D/B = neutral."""
    try:
        from pipeline.features.vp_cache import get as vp_get
        vp = vp_get(symbol, "daily")
        if not vp:
            return None
        shape = (vp.get("shape") or "").strip()
        if not shape:
            # Cache returned empty — log for debug visibility
            import logging
            logging.getLogger(__name__).debug(f"[vp_shape_vote] empty shape for {symbol}, vp keys: {list(vp.keys())}")
            return None
        if shape == "P":
            return Vote("vp_shape", -1.0, 0.8, "P shape (top-heavy distribution)")
        if shape == "b":
            return Vote("vp_shape", 1.0, 0.8, "b shape (bottom-heavy accumulation)")
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"[vp_shape_vote] failed: {e}")
    return None


def _vp_position_vote(symbol: str, current_close: float) -> Vote | None:
    """Position vs VP value area — bidirectional with 0.5% hysteresis margin.

    Vote LONG when close > VAH * 1.005 (clear breakout)
    Vote SHORT when close < VAL * 0.995 (clear breakdown)
    Otherwise abstain (price in/near value area is neutral).
    """
    try:
        from pipeline.features.vp_cache import get as vp_get
        vp = vp_get(symbol, "daily")
        if not vp:
            return None
        vah, val, poc = vp.get("vah"), vp.get("val"), vp.get("poc")
        if not all((vah, val, poc)):
            return None
        if current_close > vah * 1.005:    # 0.5% above VAH = confirmed breakout
            return Vote("vp_position", 0.7, 0.6, f"above VAH+0.5% ({vah:.2f})")
        if current_close < val * 0.995:    # 0.5% below VAL = confirmed breakdown
            return Vote("vp_position", -0.7, 0.6, f"below VAL-0.5% ({val:.2f})")
    except Exception:
        pass
    return None


def _structure_vote(symbol: str, primary_tf: str = "15m") -> Vote | None:
    """ChoCh + HH/HL trend. Weight reduced 0.9→0.7 to avoid coalition dominance."""
    try:
        from pipeline.state_store import store
        from pipeline.features.choch import detect_choch, detect_swing_points, _classify_trend
        bars = store().recent(symbol, primary_tf, 100)
        if len(bars) < 10:
            return None
        swings = detect_swing_points(bars, n=2)
        trend = _classify_trend(swings) if len(swings) >= 4 else "none"
        ev = detect_choch(bars, n=2)
        if ev:
            dir_ = 1.0 if ev.direction == "bull" else -1.0
            return Vote("structure", dir_, 0.7, f"ChoCh {ev.direction} @ {ev.broken_level:.2f}")
        if trend == "up":
            return Vote("structure", 1.0, 0.6, "HH/HL uptrend")
        if trend == "down":
            return Vote("structure", -1.0, 0.6, "LL/LH downtrend")
    except Exception:
        pass
    return None


def _fvg_vote(bars: list[Bar]) -> Vote | None:
    """FVG fill pressure — weight raised 0.6→0.8, threshold lowered 0.25→0.15."""
    try:
        from pipeline.features.fvg import detect_fvgs
        fvgs = detect_fvgs(bars, max_age_bars=50)
        if not fvgs:
            return None
        recent = fvgs[-8:]
        bull_n = sum(1 for f in recent if f.side == "bull")
        bear_n = sum(1 for f in recent if f.side == "bear")
        total = bull_n + bear_n
        if total < 2:
            return None
        ratio = (bull_n - bear_n) / total
        if abs(ratio) < 0.15:        # lowered from 0.25 → fires more often
            return None
        return Vote("fvg", 1.0 if ratio > 0 else -1.0, min(1.0, abs(ratio)) * 0.8,
                    f"unfilled fvgs bull={bull_n} bear={bear_n}")
    except Exception:
        pass
    return None


def _wave_vote(symbol: str, primary_tf: str) -> Vote | None:
    try:
        from pipeline.features.wave import from_store as wave_from_store
        w = wave_from_store(symbol, primary_tf)
        if w is None:
            return None
        direction = getattr(w, "direction", "")
        phase = getattr(w, "phase", "")
        if direction == "up" and phase in ("impulse", "extension"):
            return Vote("wave", 1.0, 0.7, f"wave up {phase}")
        if direction == "down" and phase in ("impulse", "extension"):
            return Vote("wave", -1.0, 0.7, f"wave down {phase}")
    except Exception:
        pass
    return None


def _sweep_vote(symbol: str, primary_tf: str) -> Vote | None:
    """Last sweep signal — regime-aware interpretation.

    Range market: sweep_high → short reversal, sweep_low → long reversal (pure liquidity grab).
    Trending market: sweep WITH trend = continuation (trapped opposite side fuels next leg).
    Uncertain: half-strength reversal vote.
    """
    try:
        from pipeline.features.sweep import from_store as sweep_from_store
        ev = sweep_from_store(symbol, primary_tf)
        if ev is None:
            return None
        # SweepSignal dataclass field is `type` not `kind`
        kind = getattr(ev, "type", None)
        if kind is None or kind == "none":
            return None
        conf = float(getattr(ev, "confidence", 0.5))
        # Pull regime
        regime = "uncertain"
        try:
            from pipeline.features.day_type import classify as _day_classify
            from pipeline.state_store import store
            session_bars = store().recent(symbol, primary_tf, 100)
            if session_bars:
                dt = _day_classify(session_bars, symbol)
                if dt is not None:
                    regime = dt.type
        except Exception:
            pass

        # Range = pure reversal
        if regime == "range":
            if kind == "sweep_high":
                return Vote("sweep", -1.0, conf * 0.7, f"sweep_high reversal (range, {ev.level_label})")
            if kind == "sweep_low":
                return Vote("sweep", 1.0, conf * 0.7, f"sweep_low reversal (range, {ev.level_label})")
        # Trend up = sweep_high is continuation up; sweep_low is also continuation up
        if regime == "trend_up":
            return Vote("sweep", 1.0, conf * 0.7, f"{kind} continuation (trend_up, {ev.level_label})")
        # Trend down = both sweeps signal continuation down
        if regime == "trend_down":
            return Vote("sweep", -1.0, conf * 0.7, f"{kind} continuation (trend_down, {ev.level_label})")
        # Uncertain — half-strength reversal vote
        if kind == "sweep_high":
            return Vote("sweep", -1.0, conf * 0.35, f"sweep_high reversal (uncertain, {ev.level_label})")
        if kind == "sweep_low":
            return Vote("sweep", 1.0, conf * 0.35, f"sweep_low reversal (uncertain, {ev.level_label})")
    except Exception:
        pass
    return None


def _wick_trap_vote(bars: list[Bar]) -> Vote | None:
    try:
        from pipeline.features.wick_trap import wick_trap_signal
        n = min(20, len(bars))
        recent = bars[-n:]
        fps = [build_fp(b) for b in recent]
        t = wick_trap_signal(recent, fps, lookback=20)
        if t is None or t.confidence < 0.40:
            return None
        if t.side == "bull_trap":
            return Vote("wick_trap", 1.0, t.confidence * 0.6, "bull trap (shorts trapped)")
        return Vote("wick_trap", -1.0, t.confidence * 0.6, "bear trap (longs trapped)")
    except Exception:
        pass
    return None


def collect_votes(symbol: str, primary_tf: str = "15m") -> list[Vote]:
    """Run all vote modules. Returns list of valid votes (None excluded)."""
    from pipeline.state_store import store

    bars = store().recent(symbol, primary_tf, 100)
    if not bars:
        return []
    current_close = bars[-1].ohlc.c

    raw = [
        _cvd_vote(bars),
        _vp_shape_vote(symbol),
        _vp_position_vote(symbol, current_close),
        _structure_vote(symbol, primary_tf),
        _fvg_vote(bars),
        _wave_vote(symbol, primary_tf),
        _sweep_vote(symbol, primary_tf),
        _wick_trap_vote(bars),
    ]
    return [v for v in raw if v is not None]


@dataclass
class DirectionDecision:
    side: Literal["long", "short", "flat"]
    bias_strength: int       # 1..5
    score: float             # -1..+1
    votes: list[Vote]
    note: str


FLAT_THRESHOLD = 0.35   # raised from 0.30 — reduce noise after weight rebalance


def decide_direction(symbol: str, primary_tf: str = "15m") -> DirectionDecision:
    """Aggregate module votes into a directional decision."""
    votes = collect_votes(symbol, primary_tf)
    if not votes:
        return DirectionDecision(side="flat", bias_strength=1, score=0.0,
                                 votes=[], note="no votes")
    weighted_sum = sum(v.direction * v.strength for v in votes)
    total_weight = sum(v.strength for v in votes) + 1e-9
    score = weighted_sum / total_weight
    if abs(score) < FLAT_THRESHOLD:
        return DirectionDecision(side="flat", bias_strength=1, score=score,
                                 votes=votes,
                                 note=f"|score|={abs(score):.2f} < {FLAT_THRESHOLD}")
    side = "long" if score > 0 else "short"
    # bias_strength: map |score| from FLAT_THRESHOLD..1.0 to 1..5
    span = 1.0 - FLAT_THRESHOLD
    norm = (abs(score) - FLAT_THRESHOLD) / span
    bs = max(1, min(5, round(norm * 4) + 1))
    return DirectionDecision(
        side=side, bias_strength=bs, score=round(score, 3),
        votes=votes,
        note=f"votes={len(votes)} score={score:.2f} bias={bs}",
    )
