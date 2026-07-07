"""Day type classifier — range vs trend detection for current session.

Signals used:
1. Initial Balance (IB) expansion: price moving beyond first-hour range = trend
2. Developing VP shape: D = range, elongated/P/b = trend
3. Rolling session CVD: consecutive same-direction readings = trend, oscillating = range
4. VA rotation: price returning inside VA after edge tests = range

Grid rules derived from day type:
  range        → mean reversion, up to 6 legs
  trend_up     → directional long only, max 3 legs (add on pullbacks)
  trend_down   → directional short only, max 3 legs
  uncertain    → cautious, max 2 legs, only at strongest zones
"""

from __future__ import annotations

from dataclasses import dataclass

IB_BARS = 60  # 1 hour of 1m bars = Initial Balance window

_MAX_LEGS: dict[str, int] = {
    "range":       6,
    "trend_up":    3,
    "trend_down":  3,
    "uncertain":   2,
}

_GRID_MODE: dict[str, str] = {
    "range":       "mean_reversion",
    "trend_up":    "directional_long",
    "trend_down":  "directional_short",
    "uncertain":   "cautious",
}


@dataclass
class DayType:
    type: str             # "range" | "trend_up" | "trend_down" | "uncertain"
    confidence: float     # 0.0 – 1.0
    ib_range: float       # initial balance price width (0 if not yet established)
    ib_expansion: float   # absolute price extension beyond IB edges
    ib_expansion_pct: float  # expansion as multiple of IB range (1.0 = 100% extension)
    reason: str
    max_legs: int         # derived from type
    grid_mode: str        # derived from type


def _ib_stats(session_bars, ib_bars: int = IB_BARS) -> tuple[float, float, float, float, float]:
    """Return (ib_high, ib_low, ib_range, max_expansion, expansion_pct).

    IB is the high/low of the first `ib_bars` bars (~1h of real time — scaled per TF
    by the caller so a 5m/15m regime read uses a comparable initial-balance window).
    Expansion measures the furthest price has moved beyond either IB edge
    across ALL session bars so far.
    """
    if len(session_bars) < ib_bars:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    ib = session_bars[:ib_bars]
    ib_high = max(b.ohlc.h for b in ib)
    ib_low = min(b.ohlc.l for b in ib)
    ib_range = ib_high - ib_low

    if ib_range <= 0:
        return ib_high, ib_low, 0.0, 0.0, 0.0

    all_high = max(b.ohlc.h for b in session_bars)
    all_low = min(b.ohlc.l for b in session_bars)
    expansion = max(0.0, all_high - ib_high, ib_low - all_low)
    expansion_pct = expansion / ib_range

    return ib_high, ib_low, ib_range, expansion, expansion_pct


def _cvd_history(session_bars) -> list[float]:
    """Compute rolling session CVD from bar deltas (cumulative sum, bar by bar)."""
    hist: list[float] = []
    running = 0.0
    for bar in session_bars:
        running += bar.delta or 0.0
        hist.append(running)
    return hist


def _cvd_trend(cvd_hist: list[float], n: int = 3) -> str:
    """Classify recent CVD momentum direction.

    Returns: "bullish" | "bearish" | "oscillating" | "insufficient"
    """
    if len(cvd_hist) < n + 1:
        return "insufficient"

    recent = cvd_hist[-(n + 1):]
    if all(recent[i] > recent[i - 1] for i in range(1, len(recent))):
        return "bullish"
    if all(recent[i] < recent[i - 1] for i in range(1, len(recent))):
        return "bearish"

    # Count sign changes over last 6 readings to detect oscillation
    tail = cvd_hist[-6:]
    signs = [1 if v >= 0 else -1 for v in tail]
    changes = sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])
    return "oscillating" if changes >= 2 else "neutral"


def _va_rotation(session_bars, vah: float | None, val: float | None) -> bool:
    """Return True if price has tested a VA boundary and returned inside (range behaviour).

    Looks at the last 30 session bars. Needs at least 2 edge tests with at
    least 1 subsequent close back inside the value area.
    """
    if not vah or not val or len(session_bars) < 10:
        return False
    va_range = vah - val
    if va_range <= 0:
        return False

    tol = va_range * 0.02
    edge_tests = 0
    returns_inside = 0
    prev_outside = False

    for bar in session_bars[-30:]:
        outside = bar.ohlc.h > vah + tol or bar.ohlc.l < val - tol
        inside_close = val - tol < bar.ohlc.c < vah + tol
        if outside:
            edge_tests += 1
            prev_outside = True
        elif inside_close and prev_outside:
            returns_inside += 1
            prev_outside = False

    return edge_tests >= 2 and returns_inside >= 1


def classify(session_bars, daily_vp=None, ib_bars: int = IB_BARS) -> DayType:
    """Classify current session as range, trend, or uncertain.

    Args:
        session_bars: 1m bars since session open, ascending by close_ts.
        daily_vp:     VolumeProfilePeriod for today (optional but improves accuracy).
    """
    if len(session_bars) < 10:
        return DayType(
            type="uncertain", confidence=0.0,
            ib_range=0.0, ib_expansion=0.0, ib_expansion_pct=0.0,
            reason="insufficient session bars",
            max_legs=_MAX_LEGS["uncertain"],
            grid_mode=_GRID_MODE["uncertain"],
        )

    trend_score = 0.0
    range_score = 0.0
    trend_dir = "up"
    reasons: list[str] = []

    # 1 — IB expansion
    _, _, ib_range, ib_expansion, exp_pct = _ib_stats(session_bars, ib_bars=ib_bars)
    if ib_range > 0:
        if exp_pct >= 1.0:
            trend_score += 2.5
            reasons.append(f"IB expansion {exp_pct:.1f}× — strong trend extension")
        elif exp_pct >= 0.5:
            trend_score += 1.0
            reasons.append(f"IB expansion {exp_pct:.1f}× — moderate extension")
        else:
            range_score += 1.5
            reasons.append(f"price contained within IB (expansion {exp_pct:.1f}×)")

    # 2 — Developing VP shape. daily_vp is a DICT (vp_cache.get) — use .get(), not attribute
    # access. The old `daily_vp.shape` raised AttributeError on every real call, which
    # get_regime's bare except swallowed → regime silently ALWAYS "uncertain" in production
    # whenever daily_vp was present. Fixed 2026-07-07. Tolerate a rare object caller too.
    def _vp_attr(v, key):
        if v is None:
            return None
        return v.get(key) if isinstance(v, dict) else getattr(v, key, None)
    shape = _vp_attr(daily_vp, "shape")
    vah = _vp_attr(daily_vp, "vah")
    val = _vp_attr(daily_vp, "val")

    if shape == "D":
        range_score += 2.0
        reasons.append("D-shaped profile (balanced range)")
    elif shape == "double":
        range_score += 1.0
        reasons.append("double profile (bimodal rotation)")
    elif shape in ("P", "b"):
        trend_score += 1.5
        reasons.append(f"{shape}-shaped profile (skewed, trend bias)")
    elif shape == "elongated":
        trend_score += 2.0
        reasons.append("elongated profile (thin distribution, trending)")

    # 3 — CVD direction
    cvd_hist = _cvd_history(session_bars)
    cvd_state = _cvd_trend(cvd_hist, n=3)

    if cvd_state == "bullish":
        trend_score += 2.0
        trend_dir = "up"
        reasons.append("CVD consecutive highs (sustained buying pressure)")
    elif cvd_state == "bearish":
        trend_score += 2.0
        trend_dir = "down"
        reasons.append("CVD consecutive lows (sustained selling pressure)")
    elif cvd_state == "oscillating":
        range_score += 1.5
        reasons.append("CVD oscillating (balanced two-sided flow)")

    # Resolve trend direction from net CVD if state is ambiguous
    if cvd_hist:
        net = cvd_hist[-1]
        if cvd_state not in ("bullish", "bearish"):
            trend_dir = "up" if net >= 0 else "down"

    # 4 — VA rotation
    if _va_rotation(session_bars, vah, val):
        range_score += 1.0
        reasons.append("VA rotation: price returning inside VA after edge tests")

    # 5 — VA width regime (cross-session expansion/contraction)
    try:
        if session_bars:
            from pipeline.features.vp_cache import get_va_regime as _va_regime
            _sym = session_bars[0].symbol
            _va = _va_regime(_sym, "daily", n=3)
            if _va["confidence"] >= 0.4:
                if _va["regime"] == "expanding":
                    trend_score += 1.0
                    reasons.append(f"VA expanding +{_va['pct_change']:.0f}% (trend developing)")
                elif _va["regime"] == "contracting":
                    range_score += 1.0
                    reasons.append(f"VA contracting {_va['pct_change']:.0f}% (compression→mean-revert)")
    except Exception:
        pass

    # --- Classify ---
    total = trend_score + range_score
    if total == 0:
        day_type = "uncertain"
        confidence = 0.0
    else:
        margin = trend_score - range_score  # positive = trend, negative = range
        norm_margin = abs(margin) / total

        if margin > 1.0:
            day_type = f"trend_{trend_dir}"
            confidence = min(0.90, 0.40 + norm_margin * 0.6)
        elif margin < -1.0:
            day_type = "range"
            confidence = min(0.90, 0.40 + norm_margin * 0.6)
        else:
            day_type = "uncertain"
            confidence = max(0.25, norm_margin * 0.5)

    return DayType(
        type=day_type,
        confidence=round(confidence, 2),
        ib_range=round(ib_range, 4),
        ib_expansion=round(ib_expansion, 4),
        ib_expansion_pct=round(exp_pct, 2),
        reason=" | ".join(reasons) if reasons else "no clear signal",
        max_legs=_MAX_LEGS.get(day_type, 2),
        grid_mode=_GRID_MODE.get(day_type, "cautious"),
    )


# ── Regime helpers (relocated from execution/regime.py) ───────────────────────

from typing import Literal as _Literal


# A cycle's regime is read from its NEXT-HIGHER TF (HTF sets the context an LTF entry
# trades within — standard multi-TF confluence): 1m→5m, 5m→15m, 15m→1h, 1h→1h.
_REGIME_TF = {"1m": "5m", "5m": "15m", "15m": "1h", "1h": "1h"}
# IB (~1h real time) in bars of the REGIME TF, so the initial-balance window is comparable
# regardless of TF. 5m:12, 15m:4, 1h:1→3 (1 bar can't form an IB; floor at 3 ≈ 3h context).
_IB_BARS_BY_TF = {"1m": 60, "5m": 12, "15m": 4, "1h": 3}
# Session window (bars of the regime TF) ≈ a full session on each.
_SESSION_N_BY_TF = {"1m": 100, "5m": 100, "15m": 96, "1h": 48}


def get_regime(symbol: str, primary_tf: str = "1m", session_bars_n: int | None = None) -> DayType:
    """Fetch day_type for symbol. `primary_tf` is the CYCLE's TF — the regime is read from
    its next-higher TF (_REGIME_TF), with IB/session windows scaled to that TF so the
    1m-tuned classifier gets comparable structure. Returns uncertain DayType on error."""
    _null = DayType(
        type="uncertain", confidence=0.0, ib_range=0.0, ib_expansion=0.0,
        ib_expansion_pct=0.0, reason="no_data",
        max_legs=_MAX_LEGS["uncertain"], grid_mode=_GRID_MODE["uncertain"],
    )
    try:
        from pipeline.state_store import store
        from pipeline.features.vp_cache import get as vp_get
        regime_tf = _REGIME_TF.get(primary_tf, primary_tf)
        ib_bars = _IB_BARS_BY_TF.get(regime_tf, IB_BARS)
        n = session_bars_n if session_bars_n is not None else _SESSION_N_BY_TF.get(regime_tf, 100)
        bars = store().recent(symbol, regime_tf, n)
        if not bars:
            return _null
        daily_vp = vp_get(symbol, "daily")
        return classify(bars, daily_vp=daily_vp, ib_bars=ib_bars)
    except Exception:
        return _null


def is_blocked_by_regime(
    regime: DayType, side: _Literal["long", "short"], confidence_floor: float = 0.75
) -> tuple[bool, str]:
    """Block entries that fight a confirmed trend. Returns (blocked, reason)."""
    if regime.confidence < confidence_floor:
        return False, ""
    if regime.type == "trend_up" and side == "short":
        return True, f"regime-blocked: trend_up (conf={regime.confidence:.2f}) opposes short"
    if regime.type == "trend_down" and side == "long":
        return True, f"regime-blocked: trend_down (conf={regime.confidence:.2f}) opposes long"
    return False, ""


def grid_shape_for_regime(regime: DayType, side: _Literal["long", "short"]) -> dict:
    """Return grid placement parameters for the current regime + side."""
    out = {
        "n_legs": 7,
        "step_mult": 0.5,
        "mode": "mean_reversion",
        "tighter_spacing": False,
        "safety_sl_atr_mult": 5.0,
    }
    if regime.type in ("trend_up", "trend_down") and regime.confidence >= 0.60:
        with_trend = (regime.type == "trend_up" and side == "long") or \
                     (regime.type == "trend_down" and side == "short")
        if with_trend:
            out.update({
                "n_legs": 5,
                "step_mult": 0.35,
                "mode": "directional",
                "tighter_spacing": True,
                "safety_sl_atr_mult": 3.0,
            })
    elif regime.type == "uncertain":
        out.update({
            "n_legs": 3,
            "step_mult": 0.5,
            "mode": "cautious",
            "tighter_spacing": False,
            "safety_sl_atr_mult": 7.0,
        })
    return out


def from_store(symbol: str, primary_tf: str, session_start_utc: int = 0) -> DayType | None:
    """Convenience: classify from state_store bars since today's session open.

    session_start_utc: UTC hour when the session opens (0 for BTC, 22 for gold).
    Returns None if insufficient bars.
    """
    import time
    from pipeline.state_store import store
    from pipeline.features.vp_cache import load as load_vp_cache

    now = int(time.time())
    # Compute session start timestamp
    import datetime
    utc_now = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
    if utc_now.hour >= session_start_utc:
        session_date = utc_now.date()
    else:
        session_date = (utc_now - datetime.timedelta(days=1)).date()
    session_start_ts = int(datetime.datetime(
        session_date.year, session_date.month, session_date.day,
        session_start_utc, 0, 0,
        tzinfo=datetime.timezone.utc,
    ).timestamp())

    s = store()
    all_bars = s.recent(symbol, primary_tf, 2000)
    session_bars = [b for b in all_bars if b.close_ts >= session_start_ts]

    if len(session_bars) < 10:
        return None

    # Load daily VP from cache — returns dict, wrap as simple namespace for classify()
    daily_vp = None
    try:
        from pipeline.features.vp_cache import get as vp_get
        from types import SimpleNamespace
        vp_dict = vp_get(symbol, "daily")
        if vp_dict:
            daily_vp = SimpleNamespace(**vp_dict)
    except Exception:
        pass

    return classify(session_bars, daily_vp=daily_vp)
