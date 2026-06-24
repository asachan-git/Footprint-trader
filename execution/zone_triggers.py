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
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from pipeline.types import Bar


def _hvn_dbg(msg: str) -> None:
    """One-shot debug trace for the hvn_inside_touch detector. Enable by setting
    FB_DEBUG_HVN_TOUCH=1 in the server's environment; prints to stderr (server log)."""
    if os.environ.get("FB_DEBUG_HVN_TOUCH"):
        print(f"[hvn_touch_dbg] {msg}", file=sys.stderr, flush=True)

# ~24h trailing VP window per TF (matches reversal_hvn / continuation_hvn).
_VP_WIN = {"15m": 96, "5m": 288, "1m": 1440, "1h": 24}

# Which HVN source(s) feed the inside-touch trigger, per session. London/Overlap
# (deep, two-sided liquidity) use BOTH the price-tracking rolling profile and the
# stable cached-daily node; NY trusts the rolling profile; the thin Asia/Off books
# use only the cached structural node.
_SESSION_HVN_SRC = {
    "NY":      ("rolling", "cached"),
    "London":  ("rolling", "cached"),
    "Overlap": ("rolling", "cached"),
    "Asia":    ("rolling", "cached"),
    "Off":     ("rolling", "cached"),
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


_FIB_EXT = 1.618


def _fib_ext_tp(edge: float, zones: list[tuple[float, float]], direction: str) -> float:
    """1.618× fib extension beyond edge using nearest HVN zone height as the swing.

    Uses the closest HVN zone to edge as the measured leg (its height = swing).
    direction='up'  → edge + swing × 1.618
    direction='down'→ edge - swing × 1.618
    Returns 0.0 if no zone available.
    """
    if not zones:
        return 0.0
    nearest = min(zones, key=lambda z: min(abs(z[0] - edge), abs(z[1] - edge)))
    swing = nearest[1] - nearest[0]
    if swing <= 0:
        return 0.0
    if direction == "up":
        return round(edge + swing * _FIB_EXT, 4)
    return round(edge - swing * _FIB_EXT, 4)


def compute_hvn_tps(symbol: str, edge: float,
                    zones: list[tuple[float, float]],
                    min_dist: float = 0.0,
                    top_leg: float = 0.0,
                    bot_leg: float = 0.0,
                    skip_node: tuple[float, float] | None = None) -> tuple[float, float]:
    """Recompute (tp_up, tp_down) from current HVN zones for a given edge price.

    `top_leg`/`bot_leg`: outermost buy/sell leg prices — the TP must clear the WHOLE
    ladder, so candidate levels are measured BEYOND these (not just beyond the edge).
    Defaults to `edge` when 0. `min_dist`: a structural level closer than this to the
    reference leg is skipped (keep walking out) so a TP can never land trivially close
    (the small-TP-into-own-node bug). Falls back to fib 1.618× ext if nothing clears it.

    `skip_node`: (node_low, node_high) of the HVN the fulcrum sits INSIDE
    (hvn_inside_touch). When given, that bracketing node's own edges are excluded so the
    HVN candidate is the NEXT node's far edge beyond it — never the node price is already in.

    Priority per direction (each candidate must clear ref leg + min_dist):
      1. Far edge (hi/lo) of nearest qualifying HVN beyond the leg
      2. VAH (up) / VAL (down)
      3. Nearest LVN boundary beyond the leg
      4. POC
      5. Fib 1.618× extension from edge using nearest HVN zone height as swing
    """
    up_ref  = top_leg if top_leg > 0 else edge
    dn_ref  = bot_leg if bot_leg > 0 else edge

    # candidate HVN far-edges that clear the outer leg + min_dist. When skip_node is set,
    # restrict to nodes BEYOND the bracketing node (next node up / next node down).
    if skip_node:
        _nlo, _nhi = float(skip_node[0]), float(skip_node[1])
        up_cands = sorted(hi for lo, hi in zones if hi > up_ref + min_dist and lo >= _nhi - 1e-6)
        dn_cands = sorted((lo for lo, hi in zones if lo < dn_ref - min_dist and hi <= _nlo + 1e-6), reverse=True)
    else:
        up_cands = sorted(hi for lo, hi in zones if hi > up_ref + min_dist)
        dn_cands = sorted((lo for lo, hi in zones if lo < dn_ref - min_dist), reverse=True)
    tp_up   = up_cands[0] if up_cands else 0.0
    tp_down = dn_cands[0] if dn_cands else 0.0

    if tp_up == 0.0 or tp_down == 0.0:
        from pipeline.features.vp_cache import get as vp_get
        _dvp   = vp_get(symbol, "daily") or {}
        _vah   = float(_dvp.get("vah") or 0.0)
        _val   = float(_dvp.get("val") or 0.0)
        _poc   = float(_dvp.get("poc") or 0.0)
        _lvns  = [(float(z["low"]), float(z["high"]))
                  for z in (_dvp.get("lvn_zones") or [])]

        if tp_up == 0.0:
            if _vah > up_ref + min_dist:
                tp_up = _vah
            else:
                lvn_above = [lo for lo, hi in _lvns if lo > up_ref + min_dist]
                if lvn_above:
                    tp_up = min(lvn_above)
            if tp_up == 0.0 and _poc > up_ref + min_dist:
                tp_up = _poc
            if tp_up == 0.0:
                tp_up = _fib_ext_tp(edge, zones, "up")

        if tp_down == 0.0:
            if _val > 0 and _val < dn_ref - min_dist:
                tp_down = _val
            else:
                lvn_below = [hi for lo, hi in _lvns if hi < dn_ref - min_dist]
                if lvn_below:
                    tp_down = max(lvn_below)
            if tp_down == 0.0 and 0 < _poc < dn_ref - min_dist:
                tp_down = _poc
            if tp_down == 0.0:
                tp_down = _fib_ext_tp(edge, zones, "down")

    return tp_up, tp_down


def hvn_or_vp_tp(symbol: str, zones: list[tuple[float, float]],
                 top_leg: float, bot_leg: float, step: float,
                 min_tp_dist: float = 0.0,
                 skip_node: tuple[float, float] | None = None) -> tuple[float, float]:
    """Unified grid TP: next HVN far edge beyond each outer leg, with two VP-level
    refinements (per user rule):

      Case 1 — VP level near the next HVN: if a VP level (VAH/VAL/POC/naked-POC) sits
        within 1× step of the chosen HVN far edge, target the VP level instead (cleaner
        acceptance/rejection magnet than the raw node boundary).
      Case 2 — next HVN too close: if the nearest HVN far edge is < 2× step beyond the
        outer leg, skip it and target the next VP level beyond the leg; if no VP qualifies,
        walk to the next HVN beyond.

    `skip_node`: (node_low, node_high) of the HVN the fulcrum sits INSIDE
    (hvn_inside_touch). When given, the bracketing node's own edges are excluded so TP
    targets the NEXT node's far edge beyond it, and Case 2 (too-close overshoot) is
    bypassed — jumping past a whole node already guarantees the TP clears the ladder.

    All candidates must clear the outer leg + min_tp_dist. Returns (tp_up, tp_down);
    a side is 0.0 when nothing structural sits beyond it. Pure-structural — no ATR/fib here.
    """
    from pipeline.features.vp_cache import get as _vp_get
    _dvp = _vp_get(symbol, "daily") or {}
    # VP point-levels (drop zeros / None)
    _vps = [float(_dvp.get(k) or 0.0) for k in ("vah", "val", "poc", "naked_poc")]
    _vps = [v for v in _vps if v > 0]

    near = max(step, 1e-9)         # Case-1 proximity window
    too_close = 2.0 * max(step, 1e-9)   # Case-2 minimum HVN distance from the outer leg
    _skip_case2 = skip_node is not None   # already targeting the next node → no overshoot

    def _pick(edges_sorted: list[float], leg: float, sign: int) -> float:
        # sign +1 → upward (buy TP above leg); sign -1 → downward (sell TP below leg).
        # edges_sorted is ordered nearest-first in the travel direction.
        if not edges_sorted:
            # no HVN beyond → nearest qualifying VP level beyond the leg
            vp_beyond = sorted((v for v in _vps if sign * (v - leg) > min_tp_dist),
                               key=lambda v: sign * (v - leg))
            return vp_beyond[0] if vp_beyond else 0.0
        hvn = edges_sorted[0]
        dist = sign * (hvn - leg)
        # Case 2: HVN too close → prefer next VP level beyond the leg, else next HVN beyond.
        # Skipped when skip_node is set (the candidate is already the next node beyond).
        if not _skip_case2 and dist < too_close:
            vp_beyond = sorted((v for v in _vps if sign * (v - leg) >= too_close),
                               key=lambda v: sign * (v - leg))
            if vp_beyond:
                return vp_beyond[0]
            return edges_sorted[1] if len(edges_sorted) > 1 else hvn
        # Case 1: a VP level within `near` of the chosen HVN edge → use the VP level.
        vp_near = [v for v in _vps if abs(v - hvn) <= near and sign * (v - leg) > min_tp_dist]
        if vp_near:
            # closest VP to the HVN edge (the magnet we'd actually fill at)
            return min(vp_near, key=lambda v: abs(v - hvn))
        return hvn

    if skip_node:
        _nlo, _nhi = float(skip_node[0]), float(skip_node[1])
        up_edges = sorted((hi for lo, hi in zones if hi > top_leg + min_tp_dist and lo >= _nhi - 1e-6))
        dn_edges = sorted((lo for lo, hi in zones if lo < bot_leg - min_tp_dist and hi <= _nlo + 1e-6), reverse=True)
    else:
        up_edges = sorted((hi for lo, hi in zones if hi > top_leg + min_tp_dist))
        dn_edges = sorted((lo for lo, hi in zones if lo < bot_leg - min_tp_dist), reverse=True)
    tp_up   = round(_pick(up_edges, top_leg, +1), 4)
    tp_down = round(_pick(dn_edges, bot_leg, -1), 4)
    # final leg-clear guard
    if not (tp_up   > top_leg):          tp_up   = 0.0
    if not (0 < tp_down < bot_leg):      tp_down = 0.0
    return tp_up, tp_down


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
    """HVN zones from both prev-day and today's cached daily VP."""
    try:
        from pipeline.features.vp_cache import get as vp_get, get_prev_and_today
        prev_vp, today_vp = get_prev_and_today(symbol)
        zones: list[tuple[float, float]] = []
        for vp in (prev_vp, today_vp):
            if vp:
                zones += [(float(z["low"]), float(z["high"])) for z in (vp.get("hvn_zones") or [])]
        return zones
    except Exception:
        return []


def _merge_zone_tuples(zones: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping/touching (lo,hi) HVN spans. Rolling + cached sources are
    concatenated, so the same node can appear twice at slightly offset prices —
    collapse them so the grid straddles ONE fulcrum, not two near-duplicate edges."""
    if not zones:
        return zones
    out: list[list[float]] = [list(zones[0])]
    for lo, hi in sorted(zones):
        if lo <= out[-1][1]:                       # overlap or touch
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, hi) for lo, hi in out]


def _outside_daily_va(symbol: str, price: float) -> bool:
    """True when price sits beyond today's cached value area (above VAH / below VAL)."""
    try:
        from pipeline.features.vp_cache import get as vp_get
        vp = vp_get(symbol, "daily") or {}
    except Exception:
        return False
    val, vah = vp.get("val"), vp.get("vah")
    if val is None or vah is None:
        return False
    return price > float(vah) or price < float(val)


def _prior_day_hvn(symbol: str, price: float) -> list[tuple[float, float]]:
    """Borrow HVN structure from the most recent PRIOR-DAY profile whose value area
    still brackets `price`. When price has run into a thin tail outside today's value,
    today's profile offers no node to straddle — but a previous session that traded
    AROUND this price did build one. Venue-offset is already applied by get_history."""
    try:
        from pipeline.features.vp_cache import get_history
        hist = get_history(symbol, "daily", n=5)
    except Exception:
        return []
    for e in reversed(hist):   # newest prior day first
        val, vah = e.get("val"), e.get("vah")
        if val is None or vah is None:
            continue
        if float(val) <= price <= float(vah):
            return [(float(z["low"]), float(z["high"]))
                    for z in (e.get("hvn_zones") or [])]
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
    zones = _merge_zone_tuples(zones)   # collapse rolling/cached near-duplicate nodes

    # Vacuum fallback: price ran outside today's value AND no current node holds it →
    # borrow the most recent prior-day node whose value area still brackets price.
    price = bars[-1].ohlc.c if bars else 0.0
    if price > 0 and not any(lo <= price <= hi for lo, hi in zones) \
       and _outside_daily_va(symbol, price):
        zones = _merge_zone_tuples(zones + _prior_day_hvn(symbol, price))

    return zones, sess


def _t_hvn_inside_touch(symbol: str, tf: str, current_price: float) -> Trigger | None:
    """A recent candle CLOSES INSIDE an HVN *and* TAPS one of its edges — wick reaches
    (or comes within hvn_touch_buffer of) the boundary but body closes back inside (edge
    held). Scans the last `hvn_lookback_bars` candles (default 3) so a qualifying setup
    from a few bars ago still arms on restart or when the emitter missed a bar.

    Returns the best qualifying candle (closest close to an edge). A candle that closes
    BEYOND the edge is a breakout and is excluded. Stateless / causal.
    """
    import yaml as _yaml
    from pipeline.state_store import store
    try:
        _cfg = _yaml.safe_load(
            (__import__("pathlib").Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text()
        ) or {}
        _gcfg = _cfg.get("grid_levels") or {}
        _buf = float(_gcfg.get("hvn_touch_buffer", 0.0))
        _lookback = int(_gcfg.get("hvn_lookback_bars", 3))
    except Exception:
        _buf = 0.0
        _lookback = 3

    win = _VP_WIN.get(tf, 96)
    bars = store().recent(symbol, tf, win + 5)
    if len(bars) < 2:
        _hvn_dbg(f"{symbol}/{tf}: too few bars ({len(bars)}) → None")
        return None
    zones, sess = _session_hvn_zones(symbol, tf, bars)
    if not zones:
        _hvn_dbg(f"{symbol}/{tf} sess={sess}: NO session zones (rolling+cached empty) "
                 f"→ None (this, not geometry, is the miss)")
        return None
    _hvn_dbg(f"{symbol}/{tf} sess={sess} buf={_buf} lookback={_lookback} "
             f"price={bars[-1].ohlc.c:.2f} zones={[(round(lo,2), round(hi,2)) for lo, hi in zones]}")

    # scan up to _lookback recent closed bars (newest first), return first qualifying
    candidate_bars = bars[-(1 + _lookback):-1]   # exclude the forming (last) bar
    candidate_bars = list(reversed(candidate_bars))  # newest first

    best_trigger = None
    for bar_idx, cur in enumerate(candidate_bars):
        c, h, lo_p = cur.ohlc.c, cur.ohlc.h, cur.ohlc.l
        best = None   # (dist_to_close, edge, width, edge_side, reject_frac)
        for lo, hi in zones:
            width = hi - lo
            if width <= 0:
                continue
            if not (lo < c < hi):            # candle must CLOSE inside this node
                continue
            touch_top = h >= hi - _buf       # wick reached edge or came within buffer
            touch_bot = lo_p <= lo + _buf
            if not (touch_top or touch_bot):
                # closed inside but no edge tapped — report the nearest miss
                _hvn_dbg(f"  bar[{bar_idx}] c={c:.2f} h={h:.2f} l={lo_p:.2f} INSIDE "
                         f"[{lo:.2f},{hi:.2f}]: no edge tap "
                         f"(top short by {hi - _buf - h:.2f}, bot short by {lo_p - (lo + _buf):.2f})")
                continue
            if touch_top and touch_bot:
                edge, side = (hi, "top") if abs(hi - c) <= abs(lo - c) else (lo, "bottom")
            else:
                edge, side = (hi, "top") if touch_top else (lo, "bottom")
            poke = (h - hi) if side == "top" else (lo - lo_p)
            reject_frac = max(0.0, poke) / width
            dist = abs(edge - c)
            if best is None or dist < best[0]:
                best = (dist, edge, width, side, reject_frac)
        if best is None:
            _inside = [(round(lo, 2), round(hi, 2)) for lo, hi in zones if lo < c < hi]
            _hvn_dbg(f"  bar[{bar_idx}] c={c:.2f} h={h:.2f} l={lo_p:.2f} "
                     f"no qualifying node (closed-inside nodes={_inside or 'none'})")
        if best is not None:
            # decay confidence slightly for older bars (bar_idx=0 is newest)
            _dist, edge, width, side, reject_frac = best
            conf = min(0.9, 0.55 + min(reject_frac, 0.3)) * (1.0 - bar_idx * 0.05)
            best_trigger = (edge, width, side, reject_frac, conf, sess)
            _hvn_dbg(f"  bar[{bar_idx}] QUALIFIES: edge={edge:.2f} side={side} "
                     f"width={width:.2f} reject_frac={reject_frac:.3f} conf={conf:.3f} → ARM")
            break   # take the most recent qualifying bar

    if best_trigger is None:
        _hvn_dbg(f"{symbol}/{tf}: no qualifying bar in last {_lookback} → None")
        return None

    edge, width, side, reject_frac, conf, sess = best_trigger

    try:
        from pipeline.features.vp_cache import get as vp_get
        _dvp_d  = vp_get(symbol, "daily") or {}
        _daily_zones = [(float(z["low"]), float(z["high"]))
                        for z in (_dvp_d.get("hvn_zones") or [])]
    except Exception:
        _daily_zones = list(zones)
    tp_up, tp_down = compute_hvn_tps(symbol, edge, _daily_zones or list(zones))

    node_low  = (edge - width) if side == "top" else edge
    node_high = edge if side == "top" else (edge + width)
    return Trigger(
        kind="hvn_inside_touch",
        fulcrum_price=float(edge),
        raw_range=float(width),
        confidence=float(conf),
        context={"bias": "none", "edge": side, "session": sess,
                 "reject_frac": round(reject_frac, 4),
                 "node_low": float(node_low), "node_high": float(node_high),
                 "tp_up": float(tp_up), "tp_down": float(tp_down)},
    )


def touch_arm_trigger(symbol: str, tf: str, live_price: float) -> Trigger | None:
    """INTRABAR variant of _t_hvn_inside_touch — arm on LIVE price tapping an HVN edge
    without waiting for the candle to close. Caller (the poll handler) owns the
    tick-reversal confirm; this just resolves which edge live_price is tapping and
    builds the same Trigger _t_hvn_inside_touch would, with the live edge as fulcrum.

    Returns None if live_price isn't inside any session HVN within hvn_touch_buffer of
    an edge. Same TP/node geometry as the close-driven path so downstream sizing is
    identical — only the trigger moment differs (touch vs close)."""
    import yaml as _yaml
    from pipeline.state_store import store
    try:
        _cfg = _yaml.safe_load(
            (__import__("pathlib").Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text()
        ) or {}
        _buf = float((_cfg.get("grid_levels") or {}).get("hvn_touch_buffer", 0.0))
    except Exception:
        _buf = 0.0

    win = _VP_WIN.get(tf, 96)
    bars = store().recent(symbol, tf, win + 5)
    if len(bars) < 2 or live_price <= 0:
        return None
    zones, sess = _session_hvn_zones(symbol, tf, bars)
    if not zones:
        return None

    # Tap must come FROM INSIDE the HVN body: live price sits within the node [lo,hi] AND
    # within hvn_touch_buffer of an edge — an edge-rejection from within value. An OUTSIDE
    # approach (price below the node tapping the bottom from below, or above tapping the top
    # from above) is NOT an inside-touch and is excluded (matches the close-driven path,
    # which requires the candle to CLOSE inside the node).
    best = None   # (dist_to_edge, edge, width, side)
    for lo, hi in zones:
        width = hi - lo
        if width <= 0:
            continue
        if not (lo <= live_price <= hi):       # live price must be INSIDE the node body
            continue
        touch_top = live_price >= hi - _buf    # inside, within buffer of the top edge
        touch_bot = live_price <= lo + _buf    # inside, within buffer of the bottom edge
        if not (touch_top or touch_bot):       # inside but not near an edge → no tap
            continue
        if touch_top and touch_bot:   # degenerate thin node — take nearer edge
            edge, side = (hi, "top") if abs(hi - live_price) <= abs(lo - live_price) else (lo, "bottom")
        else:
            edge, side = (hi, "top") if touch_top else (lo, "bottom")
        dist = abs(edge - live_price)
        if best is None or dist < best[0]:
            best = (dist, edge, width, side)
    if best is None:
        return None
    _dist, edge, width, side = best

    # TP from daily zones (same fallback chain as the close path)
    try:
        from pipeline.features.vp_cache import get as vp_get
        _dvp_d = vp_get(symbol, "daily") or {}
        _daily_zones = [(float(z["low"]), float(z["high"]))
                        for z in (_dvp_d.get("hvn_zones") or [])]
    except Exception:
        _daily_zones = list(zones)
    tp_up, tp_down = compute_hvn_tps(symbol, edge, _daily_zones or list(zones))

    node_low  = (edge - width) if side == "top" else edge
    node_high = edge if side == "top" else (edge + width)
    return Trigger(
        kind="hvn_inside_touch",
        fulcrum_price=float(edge),
        raw_range=float(width),
        confidence=0.6,   # touch-armed: no close-rejection depth, fixed mid confidence
        context={"bias": "none", "edge": side, "session": sess,
                 "touch_armed": True,
                 "node_low": float(node_low), "node_high": float(node_high),
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
    # LVN mids carry their zone bounds too: the displacement grid straddles the
    # VACUUM (step = node_width/2 puts the inner legs on the LVN edges), so the
    # planner needs the node width, not the gap-to-next-level raw_range below.
    lvn_bounds: dict[float, tuple[float, float]] = {}
    if "lvn" in enabled:
        for z in (daily_vp.get("lvn_zones") or []):
            lo_z, hi_z = float(z["low"]), float(z["high"])
            mid = (lo_z + hi_z) / 2.0
            if mid > 0:
                cands.append((mid, "lvn", _VP_LEVEL_BASE_CONF["lvn"]))
                lvn_bounds[mid] = (lo_z, hi_z)
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
    tp_up   = min(above) if above else 0.0
    tp_down = max(below) if below else 0.0
    # fib extension fallback when no VP level exists in a direction
    _hvn_z = [(float(z["low"]), float(z["high"])) for z in (daily_vp.get("hvn_zones") or [])]
    if tp_up   == 0.0 and _hvn_z: tp_up   = _fib_ext_tp(L, _hvn_z, "up")
    if tp_down == 0.0 and _hvn_z: tp_down = _fib_ext_tp(L, _hvn_z, "down")

    # sizing reach: gap to nearest other VP level (fallback va_width, ~3·ATR)
    others = [p for p, _, _ in merged if abs(p - L) > tol]
    raw_range = (min(abs(p - L) for p in others) if others
                 else (float(daily_vp.get("va_width") or 0.0) or (3.0 * atr if atr > 0 else 0.0)))
    if raw_range <= 0:
        raw_range = max(L * 0.001, 1e-6)

    conf = min(0.85, bc + (1.0 - abs(c - L) / tol) * 0.1) if tol > 0 else bc
    ctx = {"bias": "none", "level_type": lt, "edge": lt,
           "tp_up": float(tp_up), "tp_down": float(tp_down)}
    if lt == "lvn" and L in lvn_bounds:
        lo_z, hi_z = lvn_bounds[L]
        ctx["node_low"], ctx["node_high"] = lo_z, hi_z
        ctx["node_width"] = round(hi_z - lo_z, 6)
    return Trigger(
        kind="vp_level_touch",
        fulcrum_price=float(L),
        raw_range=float(raw_range),
        confidence=float(conf),
        context=ctx,
    )


_HTF_MAP = {"1m": "15m", "5m": "15m", "15m": "1h", "1h": "4h"}


def bb_htf_bias(symbol: str, grid_tf: str, cfg: dict | None = None) -> tuple[int, str]:
    """Higher-timeframe Bollinger directional bias as a skew VOTE (+1 buy / −1 sell / 0).

    Reads the NEXT TF up (grid_tf → htf via _HTF_MAP, e.g. 5m→15m, 15m→1h): where is
    price vs the HTF 20-SMA basis AND is the basis sloping the same way. Above a rising
    20MA → bull (+1); below a falling 20MA → bear (−1); mixed/chop → 0. Stateless/causal.
    Returns 0 gracefully when the HTF has no bars (e.g. 4h not ingested)."""
    cfg = cfg or {}
    if not bool(cfg.get("htf_bias_enabled", True)):
        return 0, "htf_bias off"
    htf = (cfg.get("htf_bias_map") or _HTF_MAP).get(grid_tf)
    if not htf:
        return 0, f"no htf for {grid_tf}"
    period = int(cfg.get("htf_bias_period", 20))
    slope_bars = int(cfg.get("htf_bias_slope_bars", 3))
    from pipeline.state_store import store
    bars = store().recent(symbol, htf, period + slope_bars + 2)
    if len(bars) < period + slope_bars:
        return 0, f"{htf} insufficient bars"
    closes = [b.ohlc.c for b in bars]
    sma_now = sum(closes[-period:]) / period
    sma_prev = sum(closes[-period - slope_bars:-slope_bars]) / period
    slope = sma_now - sma_prev
    px = closes[-1]
    if px > sma_now and slope > 0:
        return 1, f"{htf} bull (px>20MA, rising)"
    if px < sma_now and slope < 0:
        return -1, f"{htf} bear (px<20MA, falling)"
    return 0, f"{htf} mixed"


def bb_edge_vote(symbol: str, tf: str, cfg: dict | None = None) -> tuple[int, str]:
    """2.5σ Bollinger 'edge' as a mean-reversion skew VOTE (+1 buy / −1 sell / 0).

    Where is price vs the 20-SMA basis on THIS tf, in σ? Beyond the `bb_edge_sigma`
    (2.5σ default — the 'edge', distinct from the 3.0σ squeeze) price is stretched →
    fade back toward the mean: ≥ +2.5σ → vote sell (−1); ≤ −2.5σ → vote buy (+1)."""
    cfg = cfg or {}
    if not bool(cfg.get("bb_edge_enabled", True)):
        return 0, "bb_edge off"
    period = int(cfg.get("squeeze_bb_period", 20))
    edge_sigma = float(cfg.get("bb_edge_sigma", 2.5))
    from pipeline.state_store import store
    bars = store().recent(symbol, tf, period + 2)
    if len(bars) < period:
        return 0, "insufficient bars"
    closes = [b.ohlc.c for b in bars]
    sma = sum(closes[-period:]) / period
    var = sum((c - sma) ** 2 for c in closes[-period:]) / period
    sd = math.sqrt(var)
    if sd <= 0:
        return 0, "flat"
    z = (closes[-1] - sma) / sd
    if z >= edge_sigma:
        return -1, f"px +{z:.1f}σ (≥{edge_sigma}) → fade sell"
    if z <= -edge_sigma:
        return 1, f"px {z:.1f}σ (≤−{edge_sigma}) → fade buy"
    return 0, f"px {z:.1f}σ (inside edge)"


def squeeze_gate(symbol: str, tf: str, cfg: dict | None = None) -> tuple[bool, float]:
    """Vol-compression GATE for grid arming (NOT a release trigger). A neutral straddle
    should only be placed when volatility has COILED — it profits on the expansion either
    way, and bleeds in trending/uncoiled regimes (the whipsaw-fills-both-sides trap).

    Returns (ok, rank). ok = BBW is compressed NOW, or a compression run of ≥ `min_on`
    bars ended within `squeeze_gate_max_bars` (so the breakout is still ahead). Same
    BBW = (BB_upper − BB_lower)/mid with BB(period, bb_mult·σ), compression = bottom
    `bbw_pct` of the trailing `bbw_window` — identical math to _t_squeeze, reused as a
    regime filter. Stateless / causal — identical live and in sim.
    """
    cfg = cfg or {}
    period = int(cfg.get("squeeze_bb_period", 20))
    bb_mult = float(cfg.get("squeeze_bb_mult", 3.0))        # 3σ Bollinger band
    bbw_pct = float(cfg.get("squeeze_bbw_pct", 0.15))       # compression = bottom 15% of trailing BBW
    bbw_window = int(cfg.get("squeeze_bbw_window", 100))
    min_on = int(cfg.get("squeeze_min_on_bars", 6))
    gate_max = int(cfg.get("squeeze_gate_max_bars", 10))    # how stale a just-ended coil may be

    from pipeline.state_store import store
    bars = store().recent(symbol, tf, period + bbw_window + gate_max + min_on + 6)
    n = len(bars)
    if n < period + bbw_window + 1:
        return False, 1.0
    closes = [b.ohlc.c for b in bars]

    def _stdev(vals: list[float]) -> float:
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))

    bbw = [0.0] * n
    for e in range(period - 1, n):
        w = closes[e - period + 1:e + 1]
        mid, sd = sum(w) / len(w), _stdev(w)
        bbw[e] = (2.0 * bb_mult * sd) / mid if mid > 0 else 0.0

    def _on_at(e: int) -> tuple[bool, float]:
        lo = e - bbw_window + 1
        if lo < period - 1:
            return False, 1.0
        hist = bbw[lo:e + 1]
        rank = sum(1 for v in hist if v <= bbw[e]) / len(hist)
        return (rank <= bbw_pct), rank

    last = n - 1
    on_now, rank_now = _on_at(last)
    if on_now:
        return True, rank_now                # still coiled — best time to straddle

    # recently released: find a coil END within gate_max bars, then verify the run length
    e, scanned = last - 1, 0
    while e >= period - 1 and scanned < gate_max:
        o, rank = _on_at(e)
        if o:
            run, ee = 0, e
            while ee >= period - 1 and run < min_on:
                oo, _ = _on_at(ee)
                if not oo:
                    break
                run += 1
                ee -= 1
            return (run >= min_on), rank
        e -= 1
        scanned += 1
    return False, 1.0


def _t_squeeze(symbol: str, tf: str, current_price: float, atr: float,
              cfg: dict | None = None) -> Trigger | None:
    """Volatility compression → expansion via BOLLINGER BANDWIDTH PERCENTILE — a
    vol-regime gate (how desks frame compression: vol relative to its OWN recent
    history), replacing the binary TTM Squeeze (BB-inside-Keltner).

    BandWidth  BBW(e) = (BB_upper − BB_lower) / mid  with BB(period, bb_mult·stdev).
    Compression is ON when BBW(e) sits in the bottom `bbw_pct` of its trailing
    `bbw_window` distribution — self-calibrating per instrument and regime. It FIRES on
    the RELEASE: BBW climbs back out of that low-percentile band after a coil run of
    ≥ min_on bars. The coil DEPTH (how deep into the low percentile) drives a continuous
    confidence, not a flat on/off. Stateless / causal — identical live and in sim.
    """
    cfg = cfg or {}
    if not bool(cfg.get("squeeze_enabled", True)):
        return None
    period = int(cfg.get("squeeze_bb_period", 20))
    bb_mult = float(cfg.get("squeeze_bb_mult", 3.0))        # 3σ Bollinger band
    bbw_pct = float(cfg.get("squeeze_bbw_pct", 0.15))       # compression = bottom 15% of trailing BBW
    bbw_window = int(cfg.get("squeeze_bbw_window", 100))    # trailing window for the percentile rank
    min_on = int(cfg.get("squeeze_min_on_bars", 6))
    # Release window: once compression is detected the expansion may arrive any number
    # of bars later — 0 = unlimited (no bar cap). `lookback` only bounds how far back we
    # SCAN for the coil (available history is finite); it is not a freshness limit.
    max_since = int(cfg.get("squeeze_max_bars_since_release", 0))
    lookback = int(cfg.get("squeeze_release_lookback", 120))

    from pipeline.state_store import store
    need = period + bbw_window + lookback + min_on + 6
    bars = store().recent(symbol, tf, need)
    n = len(bars)
    if n < period + bbw_window + 1:
        return None
    closes = [b.ohlc.c for b in bars]

    def _stdev(vals: list[float]) -> float:
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))

    # Precompute BBW for every bar once (avoids O(window²) recompute in the scans).
    bbw = [0.0] * n
    for e in range(period - 1, n):
        w = closes[e - period + 1:e + 1]
        mid, sd = sum(w) / len(w), _stdev(w)
        bbw[e] = (2.0 * bb_mult * sd) / mid if mid > 0 else 0.0   # (upper−lower)/mid

    def _on_at(e: int) -> tuple[bool, float]:
        """(compressed?, percentile-rank of BBW[e] within its trailing window)."""
        lo = e - bbw_window + 1
        if lo < period - 1:
            return False, 1.0                # not enough history for the window
        cur = bbw[e]
        hist = bbw[lo:e + 1]
        rank = sum(1 for v in hist if v <= cur) / len(hist)
        return (rank <= bbw_pct), rank

    last = n - 1
    on_now, _ = _on_at(last)
    if on_now:
        return None                          # still compressed → wait for the release

    # Walk back through the post-release (OFF) bars to the END of the coil — the most
    # recent compressed bar. No 1-bar edge: the release may be several bars old and we
    # still arm. All bars between rel_end and `last` are OFF by construction.
    rel_end, scanned, e = -1, 0, last - 1
    while e >= period - 1 and scanned < lookback:
        o, _ = _on_at(e)
        if o:
            rel_end = e
            break
        e -= 1
        scanned += 1
    if rel_end < 0:
        return None                          # no compression within lookback → nothing released
    bars_since_release = last - rel_end
    if max_since > 0 and bars_since_release > max_since:
        return None                          # only enforced when a cap is configured

    # coil length: consecutive compressed bars ending at rel_end, + track the deepest rank
    run, e, min_rank = 0, rel_end, 1.0
    while e >= period - 1 and run < min_on + 2:
        o, r = _on_at(e)
        if not o:
            break
        min_rank = min(min_rank, r)
        run += 1
        e -= 1
    if run < min_on:
        return None

    # Anchor the straddle to the RELEASE bar's close (the coil-exit reference), NOT the
    # drifting current price. Stable across the whole post-release window → the emitter's
    # fulcrum dedup arms ONCE per episode instead of re-arming every flat bar (churn).
    # The proximity gate (max_fulcrum_dist_pct) is then the natural staleness guard.
    release_px = closes[rel_end]
    band_w = bbw[rel_end] * release_px       # BB(3σ) width in price at the coil → leg sizing
    raw_range = band_w if band_w > 0 else (3.0 * atr if atr > 0 else max(release_px * 0.001, 1e-6))
    # Continuous confidence from coil DEPTH: tighter than the threshold → higher. A coil
    # at rank 0 (tightest in window) → 0.9; one barely under bbw_pct → 0.55. Now able to
    # top hvn_inside_touch (~0.85) in the planner's confluence pick when genuinely tight.
    depth = max(0.0, (bbw_pct - min_rank) / bbw_pct) if bbw_pct > 0 else 0.0
    conf = min(0.9, 0.55 + 0.35 * depth)

    # HVN-aware TP: same far-edge logic as hvn_inside_touch — nearest HVN hi above
    # release_px → tp_up; nearest HVN lo below → tp_down. VAH/VAL fallback.
    tp_up, tp_down = 0.0, 0.0
    try:
        from pipeline.features.vp_cache import get as vp_get
        _dvp = vp_get(symbol, "daily") or {}
        _zones = [(float(z["low"]), float(z["high"]))
                  for z in (_dvp.get("hvn_zones") or [])]
        if _zones:
            tp_up, tp_down = compute_hvn_tps(symbol, release_px, _zones)
    except Exception:
        pass

    return Trigger(
        kind="squeeze",
        fulcrum_price=float(release_px),
        raw_range=float(raw_range),
        confidence=float(conf),
        context={"bias": "none", "interpretation": "volatility_release",
                 "squeeze_bars": int(run), "bbw_rank": round(min_rank, 3),
                 "bars_since_release": int(bars_since_release),
                 "tp_up": float(tp_up), "tp_down": float(tp_down)},
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


# ── HVN displacement detector ───────────────────────────────────────────────

def _t_hvn_displacement(symbol: str, tf: str, current_price: float,
                        daily_vp: dict | None = None) -> Trigger | None:
    """PREV closed candle opens inside HVN-A, closes inside HVN-B (different zone).

    The emitter fires on each bar CLOSE — at that moment bars[-1] is the new (forming)
    bar and bars[-2] is the just-completed candle we want to check. This means the
    displacement is confirmed at candle close and the grid arms on the NEXT bar's open,
    avoiding intrabar noise from a candle that hasn't committed yet.

    Entry: neutral straddle at the near edge of HVN-B (the edge price crossed into).
    Context carries:
      origin_lo / origin_hi   — HVN-A bounds (for counter-side TP = A's far edge)
      dest_lo / dest_hi       — HVN-B bounds (for bias-side TP = B's far edge)
      direction                — "buy" (A below B) | "sell" (A above B)
      candle_extreme           — low (buy) or high (sell) of the displacement candle → SL
      near_edge                — the edge of HVN-B that price just crossed (= fulcrum)
    """
    if not daily_vp:
        return None
    hvn_zones = [(float(z["low"]), float(z["high"]))
                 for z in (daily_vp.get("hvn_zones") or [])]
    if len(hvn_zones) < 2:
        return None

    try:
        from pipeline.state_store import store as _store
        bars = _store().recent(symbol, tf, 4)
        if len(bars) < 2:
            return None
    except Exception:
        return None

    # The emitter fires with an offset (e.g. 12s) after the bar boundary, by which time
    # the ingester has committed the just-closed bar as bars[-1]. So bars[-1] IS the
    # completed displacement candle — check it first. bars[-2] is the fallback for the
    # case where the emitter fires very early and the bar isn't committed yet, or when
    # the emitter missed a bar entirely (same lookback grace as hvn_inside_touch).

    def _find_zone(price: float) -> tuple[float, float] | None:
        for lo, hi in hvn_zones:
            if lo <= price <= hi:
                return lo, hi
        return None

    # Check bars[-1] (just-closed) then bars[-2] (one bar older) — newest first
    result: tuple | None = None
    for candidate in reversed(bars):   # bars[-1], bars[-2], bars[-3], ...
        o = float(candidate.ohlc.o)
        h = float(candidate.ohlc.h)
        l = float(candidate.ohlc.l)
        c = float(candidate.ohlc.c)

        origin = _find_zone(o)
        dest   = _find_zone(c)
        if origin is None or dest is None or origin == dest:
            continue

        o_lo, o_hi = origin
        d_lo, d_hi = dest

        if d_lo > o_hi:
            direction = "buy"
            near_edge = d_lo
            candle_extreme = l
        elif d_hi < o_lo:
            direction = "sell"
            near_edge = d_hi
            candle_extreme = h
        else:
            continue

        result = (o_lo, o_hi, d_lo, d_hi, direction, near_edge, candle_extreme, c)
        break   # newest qualifying bar wins

    if result is None:
        return None

    o_lo, o_hi, d_lo, d_hi, direction, near_edge, candle_extreme, c = result
    width = d_hi - d_lo
    # Confidence: stronger if full candle body committed (close well inside dest)
    body_depth = abs(c - near_edge) / width if width > 0 else 0.0
    conf = min(0.9, 0.55 + 0.35 * body_depth)

    return Trigger(
        kind="hvn_displacement",
        fulcrum_price=float(near_edge),
        raw_range=float(width),
        confidence=float(conf),
        context={
            "bias": direction,
            "direction": direction,
            "origin_lo": o_lo, "origin_hi": o_hi,
            "dest_lo": d_lo,   "dest_hi": d_hi,
            "near_edge": near_edge,
            "candle_extreme": candle_extreme,
            "edge": "bottom" if direction == "buy" else "top",
        },
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
        _t_hvn_displacement(symbol, tf, current_price, daily_vp),
        _t_anchor(symbol, current_price, atr, latest),
        _t_va(symbol, current_price, regime, daily_vp),
        _t_vp_level_touch(symbol, tf, current_price, daily_vp, atr, cfg),
        _t_squeeze(symbol, tf, current_price, atr, cfg),
        _t_cvd_div(symbol, tf, current_price, latest),
    ):
        if t is not None:
            out.append(t)
    return out
