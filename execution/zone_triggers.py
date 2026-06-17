"""Zone trigger detectors — uniform wrapper over the decision-point detectors
that arm a neutral grid.

A *trigger* is a price level (the "fulcrum") at which the market is coiled at a
binary decision: it will resolve up OR down soon, and a neutral straddle placed
around it pays either way (breakout / breakdown / fakeout / trapped-trader flush).

All five detectors normalise to one `Trigger` interface so the planner
(execution/grid_planner.py) can score and pick the best fulcrum without caring
which detector produced it.

Detectors (v1):
  imbalance        — 5m/15m per-level 3:1 diagonal imbalance (REUSE imbalance_per_level)
  hvn_edge         — nearest HVN boundary; node width sizes the grid (REUSE vp_cache)
  hvn_inside_touch — candle CLOSED INSIDE an HVN, then (≤2 bars) TOUCHED an edge →
                     straddle the touched edge; node width sizes the grid. HVN source
                     is session-aware (NY=rolling, London/Overlap=rolling+cached,
                     Asia/Off=cached).
  anchor           — high-vol+high-delta anchor candle retest (REUSE anchor_bar)
  va               — VAL/VAH reclaim-or-break-sustain, regime-aware (NEW interpretation)
  cvd_div          — CVD divergence pivot (REUSE delta_divergence + cvd_div_state)

BB-extreme is NOT a trigger here — it is a confluence multiplier applied by the
planner's scorer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from pipeline.types import Bar

# ~24h trailing VP window per TF (matches reversal_hvn / continuation_hvn).
_VP_WIN = {"15m": 96, "5m": 288, "1m": 1440}

# Which HVN source(s) feed the inside-touch trigger, per session. London/Overlap
# (deep, two-sided liquidity) use BOTH the price-tracking rolling profile and the
# stable cached-daily node; NY trusts the rolling profile; the thin Asia/Off books
# use only the cached structural node.
_SESSION_HVN_SRC = {
    "NY":      ("rolling",),
    "London":  ("rolling", "cached"),
    "Overlap": ("rolling", "cached"),
    "Asia":    ("cached",),
    "Off":     ("cached",),
}


@dataclass
class Trigger:
    kind: str                 # "imbalance"|"hvn_edge"|"anchor"|"va"|"cvd_div"
    fulcrum_price: float      # the decision-point level to straddle
    raw_range: float          # node width / candle range / imbalance band — feeds sizing
    confidence: float         # 0..1 intrinsic strength
    context: dict[str, Any] = field(default_factory=dict)
    # context keys (all optional, flat):
    #   side           : "buy"|"sell" (imbalance aggressor)
    #   interpretation : "reclaim"|"break_sustain"|"continuation"|"reversal"
    #   bias           : "buy"|"sell"|"none" (which way this trigger leans, if any)


# ── individual detectors ────────────────────────────────────────────────────

def _t_imbalance(symbol: str, tf: str, current_price: float) -> Trigger | None:
    """Nearest 3:1 diagonal imbalance to current price on the latest bar's
    footprint. Stacked imbalances (absorption walls) boost confidence."""
    try:
        from pipeline.state_store import store
        from pipeline.footprint import build as build_fp
        from pipeline.features.imbalance import imbalance_per_level
        from pipeline.features.stacked_imbalance import stacked_imbalances
    except Exception:
        return None

    latest = store().latest(symbol, tf)
    if latest is None:
        return None
    try:
        fp = build_fp(latest)
        imbs = imbalance_per_level(fp, ratio=3.0)
    except Exception:
        return None
    if not imbs:
        return None

    nearest = min(imbs, key=lambda im: abs(im.price - current_price))

    # Confidence from ratio (3:1 → ~0.5, scaling up, capped).
    conf = min(0.95, 0.4 + (nearest.ratio - 3.0) * 0.05)

    # Absorption-wall boost: if this imbalance sits inside a stacked run.
    raw_range = 0.0
    try:
        for z in stacked_imbalances(imbs, min_stack=3):
            if z.price_low <= nearest.price <= z.price_high:
                conf = min(0.98, conf + 0.15)
                raw_range = z.price_high - z.price_low
                break
    except Exception:
        pass

    return Trigger(
        kind="imbalance",
        fulcrum_price=float(nearest.price),
        raw_range=float(raw_range),
        confidence=float(conf),
        context={"side": nearest.side, "bias": "none"},
    )


def _t_hvn_edge(symbol: str, current_price: float) -> Trigger | None:
    """Nearest HVN boundary edge. Node width (high-low) is the raw_range that
    sizes the grid: price reverts to the other edge or breaks to the next HVN."""
    try:
        from pipeline.features.vp_cache import get as vp_get
    except Exception:
        return None

    best: tuple[float, float, float] | None = None  # (dist, edge_price, node_width)
    for period in ("daily", "weekly"):
        vp = vp_get(symbol, period)
        if not vp:
            continue
        for hvn in vp.get("hvn_zones") or []:
            lo, hi = float(hvn["low"]), float(hvn["high"])
            width = hi - lo
            for edge in (lo, hi):
                d = abs(edge - current_price)
                if best is None or d < best[0]:
                    best = (d, edge, width)
    if best is None:
        return None

    _dist, edge, width = best
    # Confidence higher when price is genuinely AT the edge (within ~25% of width).
    proximity = 1.0 - min(1.0, _dist / width) if width > 0 else 0.0
    conf = min(0.9, 0.5 + 0.4 * proximity)
    return Trigger(
        kind="hvn_edge",
        fulcrum_price=float(edge),
        raw_range=float(width),
        confidence=float(conf),
        context={"bias": "none"},
    )


# ── session-aware HVN sources (for the inside-touch trigger) ─────────────────

def _rolling_hvn(symbol: str, tf: str, bars: list[Bar]) -> list[tuple[float, float]]:
    """Price-tracking rolling-VP HVN zones over the ~24h window for `tf`."""
    win = _VP_WIN.get(tf, 96)
    if len(bars) < win:
        return []
    try:
        from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE
        vp = vp_compute(bars[-win:], "daily", bars[-1].ohlc.c,
                        bin_size=DEFAULT_BIN_SIZE.get(symbol))
        return [(float(z["low"]), float(z["high"])) for z in (vp.hvn_zones or [])]
    except Exception:
        return []


def _cached_hvn(symbol: str) -> list[tuple[float, float]]:
    """Stable cached-daily HVN zones (the session-anchored structural node)."""
    try:
        from pipeline.features.vp_cache import get as vp_get
        vp = vp_get(symbol, "daily") or {}
        return [(float(z["low"]), float(z["high"])) for z in (vp.get("hvn_zones") or [])]
    except Exception:
        return []


def _session_hvn_zones(symbol: str, tf: str, bars: list[Bar]) -> tuple[list[tuple[float, float]], str]:
    """HVN zones for the current session per `_SESSION_HVN_SRC`. Returns (zones, session)."""
    from pipeline.features.session import current_session
    ts = bars[-1].close_ts if bars else None
    sess = current_session(ts, symbol).session
    srcs = _SESSION_HVN_SRC.get(sess, ("cached",))
    zones: list[tuple[float, float]] = []
    if "rolling" in srcs:
        zones += _rolling_hvn(symbol, tf, bars)
    if "cached" in srcs:
        zones += _cached_hvn(symbol)
    return zones, sess


def _t_hvn_inside_touch(symbol: str, tf: str, current_price: float) -> Trigger | None:
    """The just-closed candle CLOSES INSIDE an HVN *and* TAPS one of its edges — its
    wick reaches the boundary but the body closes back inside (the edge held). That
    rejection-at-edge candle is the trigger; straddle the tapped edge (the fulcrum).
    Node width is raw_range → the planner sizes more legs into wider nodes.

    One candle, not a multi-bar sequence: requires lo < close < hi AND (high ≥ hi or
    low ≤ lo). A candle that closes BEYOND the edge is a breakout, not this setup, and
    is excluded. Stateless / causal — behaves identically live and in the sim.
    """
    from pipeline.state_store import store
    win = _VP_WIN.get(tf, 96)
    bars = store().recent(symbol, tf, win + 5)
    if len(bars) < 2:
        return None
    zones, sess = _session_hvn_zones(symbol, tf, bars)
    if not zones:
        return None

    cur = bars[-1]
    c, h, lo_p = cur.ohlc.c, cur.ohlc.h, cur.ohlc.l

    best = None   # (dist_to_close, edge, width, edge_side, reject_frac)
    for lo, hi in zones:
        width = hi - lo
        if width <= 0:
            continue
        if not (lo < c < hi):            # the candle must CLOSE inside this node
            continue
        touch_top = h >= hi
        touch_bot = lo_p <= lo
        if not (touch_top or touch_bot):  # …and tap an edge with its wick
            continue
        # which edge: if both wicks pierced, take the one the close sits nearer
        if touch_top and touch_bot:
            edge, side = (hi, "top") if abs(hi - c) <= abs(lo - c) else (lo, "bottom")
        else:
            edge, side = (hi, "top") if touch_top else (lo, "bottom")
        # rejection strength: how far the wick poked beyond the edge, in node-widths
        poke = (h - hi) if side == "top" else (lo - lo_p)
        reject_frac = max(0.0, poke) / width
        dist = abs(edge - c)
        if best is None or dist < best[0]:
            best = (dist, edge, width, side, reject_frac)
    if best is None:
        return None

    _dist, edge, width, side, reject_frac = best
    conf = min(0.9, 0.55 + min(reject_frac, 0.3))   # cleaner/deeper rejection → higher

    # HVN→HVN momentum targets (the thesis): upside TP = the first node-TOP above the
    # tapped edge; downside TP = the first node-BOTTOM below it. When the edge is the
    # NEAR side of its node this is the SAME node's opposite edge; when it's the FAR
    # side, it's the NEXT HVN's far edge across the LVN between. None if none exists.
    tops_above = [hi for lo, hi in zones if hi > edge]
    bots_below = [lo for lo, hi in zones if lo < edge]
    tp_up = min(tops_above) if tops_above else 0.0
    tp_down = max(bots_below) if bots_below else 0.0

    return Trigger(
        kind="hvn_inside_touch",
        fulcrum_price=float(edge),
        raw_range=float(width),
        confidence=float(conf),
        context={"bias": "none", "edge": side, "session": sess,
                 "reject_frac": round(reject_frac, 4),
                 "tp_up": float(tp_up), "tp_down": float(tp_down)},
    )


def _t_anchor(symbol: str, current_price: float, atr: float, latest: Bar | None) -> Trigger | None:
    """High-delta anchor candle whose [low,high] price is being retested.
    Continuation (defended) or reversal (trapped) sets the bias."""
    try:
        from pipeline.features import anchor_bar
    except Exception:
        return None
    if atr <= 0:
        return None

    anchors = anchor_bar.active_anchors(symbol, current_price, atr)
    if not anchors or latest is None:
        return None

    # Pick the anchor whose defended extreme is nearest current price.
    best_res = None
    best_dist = None
    for a in anchors:
        res = anchor_bar.test_retest(a, latest)
        if res.pattern == "none":
            continue
        # fulcrum = the defended extreme (low for bull anchor, high for bear)
        edge = a.low if a.delta_sign == "bull" else a.high
        d = abs(edge - current_price)
        if best_dist is None or d < best_dist:
            best_dist = d
            best_res = (res, a, edge)

    if best_res is None:
        return None

    res, a, edge = best_res
    # bias: continuation defends the anchor (with anchor delta);
    #       reversal breaks it (against anchor delta).
    if res.pattern == "continuation":
        bias = "buy" if a.delta_sign == "bull" else "sell"
    else:  # reversal
        bias = "sell" if a.delta_sign == "bull" else "buy"

    return Trigger(
        kind="anchor",
        fulcrum_price=float(edge),
        raw_range=float(a.high - a.low),
        confidence=float(res.strength),
        context={"interpretation": res.pattern, "bias": bias, "reason": res.reason},
    )


def _t_va(symbol: str, current_price: float, regime, daily_vp: dict | None) -> Trigger | None:
    """Value-area boundary trigger, regime-aware.

    range regime  → VAL reclaim / VAH break-fail = mean-reversion binary (fade).
    trend regime  → close sustained beyond VA edge = break-and-sustain (continue).
    """
    if not daily_vp:
        return None
    vah, val = daily_vp.get("vah"), daily_vp.get("val")
    pos = daily_vp.get("current_position") or "unknown"
    if vah is None or val is None:
        return None
    vah, val = float(vah), float(val)

    # Choose the boundary price is interacting with.
    d_vah, d_val = abs(current_price - vah), abs(current_price - val)
    edge = vah if d_vah <= d_val else val
    at_top = edge == vah

    rtype = getattr(regime, "type", "uncertain")
    if rtype in ("trend_up", "trend_down") and pos in ("above_vah", "below_val"):
        interp = "break_sustain"
        bias = "buy" if pos == "above_vah" else "sell"
        conf = 0.7
    elif rtype == "range":
        interp = "reclaim"
        # fade the edge back into value
        bias = "sell" if at_top else "buy"
        conf = 0.65
    else:
        return None

    va_width = daily_vp.get("va_width") or abs(vah - val)
    return Trigger(
        kind="va",
        fulcrum_price=float(edge),
        raw_range=float(va_width or 0.0),
        confidence=float(conf),
        context={"interpretation": interp, "bias": bias},
    )


_VP_LEVEL_BASE_CONF = {"naked_poc": 0.80, "poc": 0.75, "vah": 0.70, "val": 0.70, "lvn": 0.60}


def _t_vp_level_touch(symbol: str, tf: str, current_price: float,
                      daily_vp: dict | None, atr: float, cfg: dict | None = None) -> Trigger | None:
    """Neutral straddle armed at a VP LEVEL (POC / VAH / VAL / naked-POC / LVN) when
    the just-closed candle TAPS it and TESTS it — wick reaches the level, close stays
    within `tol` of it (a test, not a decisive break). Companion to _t_hvn_inside_touch
    for line-shaped levels; TPs target the next VP structure either side (LVN's vacuum
    naturally targets its bounding HVN/value). Stateless / causal.

    Anti-churn: fires only on a FRESH approach — the prior bar's close must NOT already
    be parked within `tol` of the same level (else camping on POC fires every bar). The
    route-level fulcrum dedup is the second guard.
    """
    if not daily_vp:
        return None
    cfg = cfg or {}
    enabled = set(cfg.get("vp_fulcrum_levels", ["poc", "vah", "val", "naked_poc", "lvn"]))
    tol_atr = float(cfg.get("vp_tol_atr_mult", 0.25))
    tol_pct = float(cfg.get("vp_tol_pct", 0.0005))
    merge_atr = float(cfg.get("vp_merge_atr_mult", 0.15))
    if not enabled:
        return None

    from pipeline.state_store import store
    bars = store().recent(symbol, tf, 3)
    if len(bars) < 2:
        return None
    cur, prev = bars[-1], bars[-2]
    c, h, lo_p, pc = cur.ohlc.c, cur.ohlc.h, cur.ohlc.l, prev.ohlc.c

    def _tol(price: float) -> float:
        return max(tol_atr * atr if atr > 0 else 0.0, tol_pct * price)

    # candidate fulcrum levels (point levels + LVN mids), tagged with base confidence
    cands: list[tuple[float, str, float]] = []
    for key in ("naked_poc", "poc", "vah", "val"):
        if key in enabled:
            v = daily_vp.get(key)
            if isinstance(v, (int, float)) and v > 0:
                cands.append((float(v), key, _VP_LEVEL_BASE_CONF[key]))
    if "lvn" in enabled:
        for z in (daily_vp.get("lvn_zones") or []):
            mid = (float(z["low"]) + float(z["high"])) / 2.0
            if mid > 0:
                cands.append((mid, "lvn", _VP_LEVEL_BASE_CONF["lvn"]))
    if not cands:
        return None

    # collision merge: levels within merge_tol coincide → keep the higher-conf one
    # (its exact price), strongest type wins (naked_poc > poc > va > lvn).
    cands.sort(key=lambda x: x[0])
    merged: list[tuple[float, str, float]] = []
    for price, lt, bc in cands:
        if merged and abs(price - merged[-1][0]) <= max(merge_atr * atr if atr > 0 else 0.0,
                                                         tol_pct * price):
            if bc > merged[-1][2]:
                merged[-1] = (price, lt, bc)
            continue
        merged.append((price, lt, bc))

    # tapped + tested + fresh-approach; pick the level nearest the close
    best = None   # (dist, price, level_type, base_conf, tol)
    for price, lt, bc in merged:
        tol = _tol(price)
        if tol <= 0 or not (lo_p <= price <= h):
            continue
        if abs(c - price) > tol:          # close is a break, not a test
            continue
        if abs(pc - price) <= tol:        # prior bar already parked here → not fresh
            continue
        dist = abs(price - c)
        if best is None or dist < best[0]:
            best = (dist, price, lt, bc, tol)
    if best is None:
        return None
    _d, L, lt, bc, tol = best

    # combined VP target set (point levels + HVN edges + LVN edges); nearest beyond
    # the fulcrum, excluding anything within tol of it.
    targets: list[float] = []
    for key in ("poc", "vah", "val", "naked_poc"):
        v = daily_vp.get(key)
        if isinstance(v, (int, float)) and v > 0:
            targets.append(float(v))
    for z in (daily_vp.get("hvn_zones") or []):
        targets += [float(z["low"]), float(z["high"])]
    for z in (daily_vp.get("lvn_zones") or []):
        targets += [float(z["low"]), float(z["high"])]
    above = [t for t in targets if t > L + tol]
    below = [t for t in targets if t < L - tol]
    tp_up = min(above) if above else 0.0
    tp_down = max(below) if below else 0.0

    # sizing reach: gap to nearest other VP level (fallback va_width, ~3·ATR)
    others = [p for p, _, _ in merged if abs(p - L) > tol]
    raw_range = (min(abs(p - L) for p in others) if others
                 else (float(daily_vp.get("va_width") or 0.0) or (3.0 * atr if atr > 0 else 0.0)))
    if raw_range <= 0:
        raw_range = max(L * 0.001, 1e-6)

    conf = min(0.85, bc + (1.0 - abs(c - L) / tol) * 0.1) if tol > 0 else bc
    return Trigger(
        kind="vp_level_touch",
        fulcrum_price=float(L),
        raw_range=float(raw_range),
        confidence=float(conf),
        context={"bias": "none", "level_type": lt, "edge": lt,
                 "tp_up": float(tp_up), "tp_down": float(tp_down)},
    )


def _t_squeeze(symbol: str, tf: str, current_price: float, atr: float,
              cfg: dict | None = None) -> Trigger | None:
    """Volatility squeeze → expansion (TTM Squeeze, John Carter). Squeeze is ON while
    the Bollinger Bands (20, 2.0) sit ENTIRELY inside the Keltner Channels (20, 1.5·meanTR);
    it FIRES on the RELEASE bar (BB expands back outside KC) after a run of ≥ min_on_bars.
    A neutral straddle positions BEFORE the displacement — no direction needed, so the
    momentum histogram is omitted. fulcrum = current price; raw_range = KC width (the
    expanding band) sizes the legs. Stateless / causal.
    """
    cfg = cfg or {}
    if not bool(cfg.get("squeeze_enabled", True)):
        return None
    period = int(cfg.get("squeeze_bb_period", 20))
    bb_mult = float(cfg.get("squeeze_bb_mult", 2.0))
    kc_period = int(cfg.get("squeeze_kc_period", period))
    kc_mult = float(cfg.get("squeeze_kc_mult", 1.5))
    min_on = int(cfg.get("squeeze_min_on_bars", 6))

    from pipeline.state_store import store
    win = max(period, kc_period)
    bars = store().recent(symbol, tf, win + min_on + 6)
    if len(bars) < win + min_on + 1:
        return None
    closes = [b.ohlc.c for b in bars]
    highs = [b.ohlc.h for b in bars]
    lows = [b.ohlc.l for b in bars]

    def _stdev(vals: list[float]) -> float:
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))

    def _ema_at(e: int, p: int) -> float:
        a = 2.0 / (p + 1)
        s = max(0, e - p * 3)
        val = closes[s]
        for j in range(s + 1, e + 1):
            val = a * closes[j] + (1.0 - a) * val
        return val

    def _tr(i: int) -> float:
        if i == 0:
            return highs[i] - lows[i]
        return max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    def _squeeze_at(e: int) -> tuple[bool, float, float]:
        w = closes[e - period + 1:e + 1]
        mid, sd = sum(w) / len(w), _stdev(w)
        bb_u, bb_l = mid + bb_mult * sd, mid - bb_mult * sd
        kmid = _ema_at(e, kc_period)
        trs = [_tr(i) for i in range(e - kc_period + 1, e + 1)]
        meantr = sum(trs) / len(trs)
        kc_u, kc_l = kmid + kc_mult * meantr, kmid - kc_mult * meantr
        return (bb_l > kc_l and bb_u < kc_u), (bb_u - bb_l), (kc_u - kc_l)

    last = len(bars) - 1
    on_now, bbw_now, kcw_now = _squeeze_at(last)
    if on_now:
        return None                       # still squeezed → wait for the release
    on_prev, _, _ = _squeeze_at(last - 1)
    if not on_prev:                        # released a while ago → not a fresh fire
        return None
    # count the consecutive ON run ending at last-1
    run, e = 0, last - 1
    while e >= period and run < min_on + 2:
        o, _, _ = _squeeze_at(e)
        if not o:
            break
        run += 1
        e -= 1
    if run < min_on:
        return None

    kc_width = kcw_now if kcw_now > 0 else (3.0 * atr if atr > 0 else max(current_price * 0.001, 1e-6))
    bb_kc = (bbw_now / kcw_now) if kcw_now > 0 else 1.0
    conf = min(0.8, 0.5 + 0.1 * min(run / max(min_on, 1), 2.0))   # longer squeeze → higher
    return Trigger(
        kind="squeeze",
        fulcrum_price=float(current_price),
        raw_range=float(kc_width),
        confidence=float(conf),
        context={"bias": "none", "interpretation": "volatility_release",
                 "squeeze_bars": int(run), "bb_kc_ratio": round(bb_kc, 3)},
    )


def _t_cvd_div(symbol: str, tf: str, current_price: float, latest: Bar | None) -> Trigger | None:
    """CVD / delta divergence pivot. Exhaustion at the current bar's extreme is
    the fulcrum; bias points toward the divergence resolution."""
    try:
        from pipeline.features.delta_divergence import from_store as dd_from_store
    except Exception:
        return None

    dd = dd_from_store(symbol, tf, window=5)
    if not dd.fired or latest is None:
        return None

    # bearish divergence (price up, delta lagging) → resolution down → sell bias.
    bias = "sell" if dd.direction == "bearish" else "buy"
    # fulcrum = the extreme that printed the divergence.
    edge = latest.ohlc.h if dd.direction == "bearish" else latest.ohlc.l
    # confidence scales with how far delta lagged.
    conf = min(0.85, 0.45 + dd.delta_vs_window * 0.0)  # magnitude unit-dependent; keep base
    conf = max(conf, 0.5)
    return Trigger(
        kind="cvd_div",
        fulcrum_price=float(edge),
        raw_range=float(latest.ohlc.h - latest.ohlc.l),
        confidence=float(conf),
        context={"interpretation": "reversal", "bias": bias, "direction": dd.direction},
    )


# ── public entry ────────────────────────────────────────────────────────────

def detect_all(symbol: str, tf: str, current_price: float, regime,
               atr: float = 0.0, daily_vp: dict | None = None,
               cfg: dict | None = None) -> list[Trigger]:
    """Run every detector; return the non-None triggers.

    `regime`   : DayType from day_type.get_regime (may be None).
    `atr`      : current ATR for anchor proximity / sizing (0 → anchor skipped).
    `daily_vp` : vp_cache.get(symbol, "daily") dict (fetched once by caller).
    `cfg`      : grid_levels config (VP-level tolerances / enabled levels).
    """
    from pipeline.state_store import store
    latest = store().latest(symbol, tf)

    out: list[Trigger] = []
    for t in (
        _t_imbalance(symbol, tf, current_price),
        _t_hvn_edge(symbol, current_price),
        _t_hvn_inside_touch(symbol, tf, current_price),
        _t_anchor(symbol, current_price, atr, latest),
        _t_va(symbol, current_price, regime, daily_vp),
        _t_vp_level_touch(symbol, tf, current_price, daily_vp, atr, cfg),
        _t_squeeze(symbol, tf, current_price, atr, cfg),
        _t_cvd_div(symbol, tf, current_price, latest),
    ):
        if t is not None:
            out.append(t)
    return out
