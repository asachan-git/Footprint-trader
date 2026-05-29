"""Mode 2 direction engine — rule-based directional bias via weighted module votes.

Replaces Claude for direction decisions when trading_mode=grid. Each analysis
module casts a vote (-1=strong short, +1=strong long) with a strength weight.
Aggregated vote → (side, bias_strength) consumed by grid_placer.

Modules voting (in order of weight):
  CVD trend          weight 0.65
  VP shape (P/b/D)   weight 0.80
  VP position        weight 0.60
  FVG fill pressure  weight 0.80
  Wave direction     weight 0.90
  Sweep signal       weight 0.70
  Confirmation       weight 0.85  (ABSORPTION/EXHAUSTION/CONTINUATION patterns)

ChoCh is used only for invalidation (cycle_manager), not direction voting.

Final score = Σ(direction × strength) / Σ(strength) × shape_regime_agreement multiplier
|score| < 0.35 → flat
bias_strength = clamp(round(|score| × 4) + 1, 1, 5)
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
        from pipeline.features.wave import from_store_15m
        w = from_store_15m(symbol)
        if w is None or w.phase == "unknown":
            return None
        direction = w.direction
        label = w.wave_label
        impulse_dir = 1.0 if direction == "up" else -1.0

        # Wave 3 in progress — highest confidence (strongest impulse leg)
        if w.is_wave3:
            return Vote("wave", impulse_dir, 0.90, f"{label} wave3_confirmed")

        # In an impulse leg (w1/w3/w5) — trend continuation vote
        if w.phase == "impulse" and label in ("w1", "w3", "w5"):
            conf = 0.55 if w.is_wave5 else 0.75
            note = f"{label} impulse{'_watch_exhaustion' if w.is_wave5 else ''}"
            return Vote("wave", impulse_dir, conf, note)

        # In a correction (w2/w4) — favour mean-reversion entry in trend direction
        if w.phase == "correction" and label in ("w2", "w4"):
            conf = 0.65 if w.cvd_quality == "holding" else 0.50
            return Vote("wave", impulse_dir, conf, f"{label} pullback cvd={w.cvd_quality}")

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


def _confirmation_vote(symbol: str, primary_tf: str) -> Vote | None:
    """Weighted-score confirmation patterns (ABSORPTION/EXHAUSTION/CONTINUATION).

    Uses confirmation.from_store() for absorption + exhaustion (direction-agnostic).
    Fires the stronger of the two confirmed patterns as a vote.
    """
    try:
        from pipeline.features.confirmation import from_store as conf_from_store
        absorption, exhaustion = conf_from_store(symbol, primary_tf)

        best: tuple[float, str] | None = None
        if absorption.fired:
            dir_ = 1.0 if absorption.side == "long" else -1.0
            if best is None or absorption.confidence > best[0]:
                best = (absorption.confidence, absorption.side)
                return Vote(
                    "confirmation",
                    dir_,
                    min(absorption.confidence, 1.0) * 0.85,
                    f"absorption({absorption.side}) conf={absorption.confidence:.2f}",
                )
        if exhaustion.fired:
            dir_ = 1.0 if exhaustion.side == "long" else -1.0
            return Vote(
                "confirmation",
                dir_,
                min(exhaustion.confidence, 1.0) * 0.85,
                f"exhaustion({exhaustion.side}) conf={exhaustion.confidence:.2f}",
            )
    except Exception:
        pass
    return None


def _shape_regime_agreement(vp_shape: str, regime_type: str) -> float:
    """Multiplier [0.7, 1.2] based on VP shape × day_type agreement."""
    agree = {
        ("P", "trend_down"): 1.2,
        ("b", "trend_up"): 1.2,
        ("D", "range"): 1.1,
        ("B", "uncertain"): 1.0,
    }
    key = (vp_shape, regime_type)
    if key in agree:
        return agree[key]
    # Mismatch: cap directional conviction
    if vp_shape in ("P", "b") and regime_type not in ("trend_down", "trend_up", "range"):
        return 0.85
    if vp_shape in ("P", "b") and (
        (vp_shape == "P" and regime_type == "trend_up") or
        (vp_shape == "b" and regime_type == "trend_down")
    ):
        return 0.7   # strong disagreement
    return 1.0


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
        # _structure_vote dropped: ChoCh used only for invalidation in cycle_manager
        _fvg_vote(bars),
        _wave_vote(symbol, primary_tf),
        _sweep_vote(symbol, primary_tf),
        _confirmation_vote(symbol, primary_tf),
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
    raw_score = weighted_sum / total_weight

    # Apply VP shape × regime agreement multiplier
    multiplier = 1.0
    try:
        from pipeline.features.vp_cache import get as vp_get
        from pipeline.features.day_type import classify as day_classify
        from pipeline.state_store import store as _store
        _bars = _store().recent(symbol, primary_tf, 100)
        vp = vp_get(symbol, "daily")
        vp_shape = (vp.get("shape") or "") if vp else ""
        dt = day_classify(_bars, symbol) if _bars else None
        if vp_shape and dt:
            multiplier = _shape_regime_agreement(vp_shape, dt.type)
    except Exception:
        pass

    score = raw_score * multiplier
    if abs(score) < FLAT_THRESHOLD:
        return DirectionDecision(side="flat", bias_strength=1, score=round(score, 3),
                                 votes=votes,
                                 note=f"|score|={abs(score):.2f} < {FLAT_THRESHOLD} (mult={multiplier:.2f})")
    side = "long" if score > 0 else "short"
    # bias_strength: map |score| from FLAT_THRESHOLD..1.0 to 1..5
    span = 1.0 - FLAT_THRESHOLD
    norm = (abs(score) - FLAT_THRESHOLD) / span
    bs = max(1, min(5, round(norm * 4) + 1))
    return DirectionDecision(
        side=side, bias_strength=bs, score=round(score, 3),
        votes=votes,
        note=f"votes={len(votes)} score={score:.2f} mult={multiplier:.2f} bias={bs}",
    )
