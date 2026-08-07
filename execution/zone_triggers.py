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
                     blends rolling + cached-daily for every session (see
                     _SESSION_HVN_SRC — per-session differentiation is available but
                     currently unused; all sessions use the same superset).
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
# EVERY live TF must be listed: the six call sites use `.get(tf, 96)`, so a missing
# TF silently gets a 96-bar window — on 3m that is 4.8h, not 24h, i.e. a truncated
# profile with no error. 3m: 1440/3=480, 10m: 1440/10=144.
_VP_WIN = {"1m": 1440, "3m": 480, "5m": 288, "10m": 144, "15m": 96, "1h": 24}

# Which HVN source(s) feed the inside-touch trigger, per session. Every session
# currently blends BOTH the price-tracking rolling profile and the stable
# cached-daily node — the safe superset. Per-session differentiation (e.g.
# NY=rolling-only, thin Asia=cached-only) is intentionally NOT enabled: it was
# never validated on live data. Tune per session here only after A/B evidence.
# CACHED-ONLY (2026-07-10): arm on the HVNs VISIBLE on the MT5 chart (get_prev_and_today
# daily VP via /exec/zones), NOT a rolling per-TF window that adds nodes the chart never
# draws. Mirrors the LVN switch made 2026-07-09 (_session_lvn_zones cached-only). Re-add
# "rolling" here to restore the hybrid (drawn≠armed) behaviour.
# ROLLING RE-ADDED (2026-08-05, user). The cached-only rule above kept armed zones ==
# drawn zones, but cached daily VP lags a trending session badly: at 20:09 IST price was
# 4242.4 while the highest CACHED node topped out at 4205.6 — 37pt below, so the detector
# could not see the node price was actually trading inside, and hvn_inside_touch went
# dormant for hours. The ROLLING profile had (4231.2, 4242.8), containing price.
# TRADE-OFF, accepted deliberately: this restores "drawn ≠ armed" — the system can now arm
# on a rolling node the MT5 chart never draws (the chart renders the cached daily VP via
# /exec/zones). Revert to ("cached",) to make armed zones match the chart again.
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

    # candidate HVN nodes that clear the outer leg + min_dist. When skip_node is set,
    # restrict to nodes BEYOND the bracketing node (next node up / next node down).
    if skip_node:
        _nlo, _nhi = float(skip_node[0]), float(skip_node[1])
        up_zones = sorted([(lo, hi) for lo, hi in zones if hi > up_ref + min_dist and lo >= _nhi - 1e-6],
                          key=lambda z: z[1])
        dn_zones = sorted([(lo, hi) for lo, hi in zones if lo < dn_ref - min_dist and hi <= _nlo + 1e-6],
                          key=lambda z: -z[0])
    else:
        up_zones = sorted([(lo, hi) for lo, hi in zones if hi > up_ref + min_dist], key=lambda z: z[1])
        dn_zones = sorted([(lo, hi) for lo, hi in zones if lo < dn_ref - min_dist], key=lambda z: -z[0])

    # Far edge for an intermediate node; CENTER for the extreme (outermost) node on the
    # chart — price rarely clears the last high-volume node, so its mid is the magnet.
    def _hvn_target(zs: list, ref: float, sign: int) -> float:
        if not zs:
            return 0.0
        z = zs[0]
        edge = z[1] if sign > 0 else z[0]
        if len(zs) == 1:   # extreme node → center (fall back to edge if mid doesn't clear)
            center = (z[0] + z[1]) / 2.0
            return center if sign * (center - ref) > min_dist else edge
        return edge

    tp_up   = _hvn_target(up_zones, up_ref, +1)
    tp_down = _hvn_target(dn_zones, dn_ref, -1)

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


def compute_lvn_edge_tps(symbol: str, edge: float,
                         zones: list[tuple[float, float]],
                         min_dist: float = 0.0,
                         top_leg: float = 0.0,
                         bot_leg: float = 0.0) -> tuple[float, float]:
    """Recompute (tp_up, tp_down) for lvn_edge_touch — same zone-walk as
    compute_hvn_tps, but targets the NEAR edge of an intermediate qualifying HVN (the
    boundary facing back toward the LVN) instead of the far edge — a more conservative
    target than hvn_inside_touch's "punch through the whole node" convention, since
    there's no directional/reversion thesis here to justify assuming a full pass-through.
    Extreme (outermost) node still targets its CENTER, same rule as compute_hvn_tps.
    No skip_node: the fulcrum is an LVN edge, not inside an HVN, so there's no bracketing
    node to exclude. Same VAH/VAL/LVN/POC/fib fallback cascade as compute_hvn_tps when no
    HVN qualifies at all."""
    up_ref  = top_leg if top_leg > 0 else edge
    dn_ref  = bot_leg if bot_leg > 0 else edge

    up_zones = sorted([(lo, hi) for lo, hi in zones if hi > up_ref + min_dist], key=lambda z: z[1])
    dn_zones = sorted([(lo, hi) for lo, hi in zones if lo < dn_ref - min_dist], key=lambda z: -z[0])

    # NEAR edge for an intermediate node (opposite of compute_hvn_tps's far edge);
    # CENTER for the extreme (outermost) node — same magnet rule as compute_hvn_tps.
    def _near_target(zs: list, ref: float, sign: int) -> float:
        if not zs:
            return 0.0
        z = zs[0]
        near_edge = z[0] if sign > 0 else z[1]
        if len(zs) == 1:   # extreme node → center (fall back to near edge if mid doesn't clear)
            center = (z[0] + z[1]) / 2.0
            return center if sign * (center - ref) > min_dist else near_edge
        return near_edge

    tp_up   = _near_target(up_zones, up_ref, +1)
    tp_down = _near_target(dn_zones, dn_ref, -1)

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
    """Unified grid TP: next HVN beyond each outer leg, with two VP-level refinements and
    an extreme-node rule (per user rule):

      Far edge vs center — target the chosen HVN's FAR edge when there is another HVN
        beyond it (price travels through an intermediate node), but target its CENTER when
        it is the EXTREME (outermost) HVN in that direction on the chart: price rarely
        punches clean through the last high-volume node, so its mid is the realistic magnet.
      Case 1 — VP level near the next HVN: if a VP level (VAH/VAL/POC/naked-POC) sits
        within 1× step of the chosen HVN far edge, target the VP level instead (cleaner
        acceptance/rejection magnet than the raw node boundary). Skipped for the extreme
        node (its target is the center, not the edge a VP would snap to).
      Case 2 — next HVN too close: if the nearest HVN far edge is < 2× step beyond the
        outer leg, skip it and target the next VP level beyond the leg; if no VP qualifies,
        walk to the next HVN beyond.

    `skip_node`: (node_low, node_high) of the HVN the fulcrum sits INSIDE
    (hvn_inside_touch). When given, the bracketing node's own edges are excluded so TP
    targets the NEXT node beyond it, and Case 2 (too-close overshoot) is bypassed —
    jumping past a whole node already guarantees the TP clears the ladder.

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

    def _pick(zones_sorted: list, leg: float, sign: int) -> float:
        # sign +1 → upward (buy TP above leg); sign -1 → downward (sell TP below leg).
        # zones_sorted is the candidate (lo,hi) nodes ordered nearest-far-edge-first.
        if not zones_sorted:
            # no HVN beyond → nearest qualifying VP level beyond the leg
            vp_beyond = sorted((v for v in _vps if sign * (v - leg) > min_tp_dist),
                               key=lambda v: sign * (v - leg))
            return vp_beyond[0] if vp_beyond else 0.0
        ci = 0
        edge = zones_sorted[ci][1] if sign > 0 else zones_sorted[ci][0]   # far edge
        dist = sign * (edge - leg)
        # Case 2: HVN too close → prefer next VP level beyond the leg, else next HVN beyond.
        # Skipped when skip_node is set (the candidate is already the next node beyond).
        if not _skip_case2 and dist < too_close:
            vp_beyond = sorted((v for v in _vps if sign * (v - leg) >= too_close),
                               key=lambda v: sign * (v - leg))
            if vp_beyond:
                return vp_beyond[0]
            if len(zones_sorted) > 1:
                ci = 1
                edge = zones_sorted[ci][1] if sign > 0 else zones_sorted[ci][0]
        z = zones_sorted[ci]
        # Extreme (outermost) HVN — nothing beyond it on the chart → target its CENTER. The
        # center must still clear the ladder; if the leg pokes into this node so the mid
        # sits behind it, fall back to the far edge (which does clear).
        if ci == len(zones_sorted) - 1:
            center = (z[0] + z[1]) / 2.0
            return center if sign * (center - leg) > min_tp_dist else edge
        # Case 1: a VP level within `near` of the chosen HVN edge → use the VP level.
        vp_near = [v for v in _vps if abs(v - edge) <= near and sign * (v - leg) > min_tp_dist]
        if vp_near:
            # closest VP to the HVN edge (the magnet we'd actually fill at)
            return min(vp_near, key=lambda v: abs(v - edge))
        return edge

    if skip_node:
        _nlo, _nhi = float(skip_node[0]), float(skip_node[1])
        up_zones = sorted([(lo, hi) for lo, hi in zones if hi > top_leg + min_tp_dist and lo >= _nhi - 1e-6],
                          key=lambda z: z[1])
        dn_zones = sorted([(lo, hi) for lo, hi in zones if lo < bot_leg - min_tp_dist and hi <= _nlo + 1e-6],
                          key=lambda z: -z[0])
    else:
        up_zones = sorted([(lo, hi) for lo, hi in zones if hi > top_leg + min_tp_dist], key=lambda z: z[1])
        dn_zones = sorted([(lo, hi) for lo, hi in zones if lo < bot_leg - min_tp_dist], key=lambda z: -z[0])
    tp_up   = round(_pick(up_zones, top_leg, +1), 4)
    tp_down = round(_pick(dn_zones, bot_leg, -1), 4)
    # final leg-clear guard
    if not (tp_up   > top_leg):          tp_up   = 0.0
    if not (0 < tp_down < bot_leg):      tp_down = 0.0
    return tp_up, tp_down


def _t_hvn_edge(symbol: str, tf: str, current_price: float,
                cfg: dict | None = None,
                bars_override: list | None = None,
                zone_shift: float = 0.0) -> Trigger | None:
    """HVN edge breakout-retest trigger.

    Fires when two conditions are met on the last closed bar:
      1. A prior bar (within hvn_edge_lookback bars) closed OUTSIDE an HVN zone on one side.
      2. The last closed bar's wick tapped back INTO that edge (within hvn_edge_tap_buffer)
         AND the bar closed back outside — a rejection confirming the edge as
         support/resistance.

    Two patterns covered:
      • Immediate: breakout bar → next bar taps edge and closes back outside.
      • Pullback:  breakout bar → moves away → returns after N bars → taps and rejects.

    Bias is directional:
      • Bullish break (closed above HVN top) → bias="buy"  fulcrum=HVN top edge
      • Bearish break (closed below HVN bot) → bias="sell" fulcrum=HVN bottom edge

    bars_override / zone_shift (2026-07-10, VANTAGE-ONLY): when bars_override is given, detect on
    THOSE bars (EA CopyRates venue OHLC) instead of the analysis-feed store — so the breakout /
    retest is judged on the SAME candles the broker fills against. zone_shift (venue − analysis)
    is ADDED to the HVN edges ONLY for the venue-bar tap comparison (so bars + zones share one
    frame during detection); it is then SUBTRACTED back off the returned fulcrum/range so the
    Trigger is in the ANALYSIS frame — identical convention to touch/lvn triggers. plan_grid then
    does the SINGLE venue rebase (2026-07-14 fix): previously this returned a venue-frame fulcrum
    AND plan_grid re-rebased it → ~3pt double-shift. HVN zones now come from _session_hvn_zones
    (the SAME per-TF session source /exec/zones draws), not vp_get('daily') → chart edge == arm."""
    cfg = cfg or {}
    _lb_by_tf = cfg.get("hvn_edge_lookback_by_tf") or {}
    lookback = int(_lb_by_tf.get(tf, cfg.get("hvn_edge_lookback", 20)) or 20)
    tap_buf = float(cfg.get("hvn_edge_tap_buffer", cfg.get("hvn_touch_buffer", 0.5)) or 0.5)

    try:
        from pipeline.state_store import store as _store
    except Exception:
        return None

    if bars_override is not None:
        bars = bars_override
    else:
        bars = _store().recent(symbol, tf, lookback + 2)
    if len(bars) < 3:
        return None

    # Tap bar = last closed bar
    tap = bars[-1]
    tap_hi = float(tap.ohlc.h)
    tap_lo = float(tap.ohlc.l)
    tap_close = float(tap.ohlc.c)

    # HVN zones from the SAME per-TF session source the /exec/zones chart draws
    # (_session_hvn_zones) — NOT vp_get('daily'), which is a different node set (no
    # prev/today band, no merge, no vacuum fallback) and drifted ~3-4pt from the drawn
    # edge. Shift into the venue frame (zone_shift) only for the venue-bar tap test below;
    # the returned fulcrum is un-shifted back to analysis frame (see end of fn).
    _abars = _store().recent(symbol, tf, 120)
    _sz, _ = _session_hvn_zones(symbol, tf, _abars) if _abars else ([], "")
    hvn_zones = [{"low": float(lo) + zone_shift, "high": float(hi) + zone_shift}
                 for (lo, hi) in _sz]
    if not hvn_zones:
        return None

    best: tuple[float, str, float, float] | None = None  # (confidence, bias, edge, width)

    for hvn in hvn_zones:
        lo, hi = float(hvn["low"]), float(hvn["high"])
        width = hi - lo
        if width <= 0:
            continue

        # ── bullish: wick tapped top edge from outside, closed back above ──
        top_tapped = (tap_lo <= hi + tap_buf) and (tap_lo >= lo - tap_buf) and (tap_close > hi)
        # ── bearish: wick tapped bottom edge from outside, closed back below ──
        bot_tapped = (tap_hi >= lo - tap_buf) and (tap_hi <= hi + tap_buf) and (tap_close < lo)

        if not top_tapped and not bot_tapped:
            continue

        bias = "buy" if top_tapped else "sell"
        edge = hi if top_tapped else lo

        # Must have a prior breakout close on the same side within lookback bars
        breakout_found = False
        for pb in bars[:-1]:  # all bars before the tap bar
            pb_c = float(pb.ohlc.c)
            if bias == "buy" and pb_c > hi:
                breakout_found = True
                break
            if bias == "sell" and pb_c < lo:
                breakout_found = True
                break

        if not breakout_found:
            continue

        # Prefer the zone whose edge the tap was tightest to
        tap_dist = abs((tap_lo if top_tapped else tap_hi) - edge)
        conf = 0.75 * (1.0 - min(1.0, tap_dist / max(tap_buf, 0.01)))
        if best is None or conf > best[0]:
            best = (conf, bias, edge, width)

    if best is None:
        return None

    conf, bias, edge, width = best
    # Un-shift back to ANALYSIS frame (detection ran on venue-shifted zones/bars; the
    # Trigger must be analysis-frame so plan_grid does the SINGLE venue rebase). width is
    # frame-invariant (a difference); only absolute prices carry zone_shift.
    edge = edge - zone_shift
    # Recover the matched zone bounds for asymmetric TP in grid_planner
    _matched_lo = edge - width if bias == "buy" else edge
    _matched_hi = edge if bias == "buy" else edge + width
    return Trigger(
        kind="hvn_edge",
        fulcrum_price=float(edge),
        raw_range=float(width),
        confidence=float(conf),
        context={
            "bias": "none",           # neutral straddle
            "breakout_bias": bias,    # "buy" or "sell" — which side is continuation
            "node_low": round(_matched_lo, 4),
            "node_high": round(_matched_hi, 4),
        },
    )


# ── session-aware HVN sources (for the inside-touch trigger) ─────────────────

# ── fractal-gated rolling VP ────────────────────────────────────────────────
# (symbol, tf) → (last_fractal_close_ts, zones). The rolling profile is recomputed
# ONLY when a new fractal confirms on the SAME tf; between fractals the last result
# is served unchanged, so HVN edges stop drifting mid-move (a drifting edge moves the
# fulcrum under a live cycle and re-registers taps that already fired).
_ROLLING_VP_CACHE: dict[tuple[str, str], tuple[int, list[tuple[float, float]]]] = {}

# Fractal half-width: 1 → 3-bar (1 left + 1 right), 2 → 5-bar. 3-bar chosen 2026-07-21
# (more pivots → the profile still tracks price, just no longer drifts every poll).
_VP_FRACTAL_N = 1


def _last_fractal_ts(bars: list[Bar], n: int = 1) -> int:
    """close_ts of the most recent CONFIRMED fractal (swing high or low).

    n=1 → 3-bar fractal: centre bar's high strictly above (or low strictly below)
    both neighbours. Confirmation needs `n` bars to the RIGHT, so the newest
    candidate centre is bars[-1-n] — never the forming bar. Returns 0 if none.
    """
    if len(bars) < 2 * n + 1:
        return 0
    for c in range(len(bars) - 1 - n, n - 1, -1):
        hi, lo = bars[c].ohlc.h, bars[c].ohlc.l
        is_high = all(hi > bars[c + d].ohlc.h and hi > bars[c - d].ohlc.h
                      for d in range(1, n + 1))
        is_low = all(lo < bars[c + d].ohlc.l and lo < bars[c - d].ohlc.l
                     for d in range(1, n + 1))
        if is_high or is_low:
            return int(bars[c].close_ts or 0)
    return 0


def _rolling_hvn(symbol: str, tf: str, bars: list[Bar]) -> list[tuple[float, float]]:
    """Price-tracking rolling-VP HVN zones over the ~24h window for `tf`.

    Recomputed only on a NEW confirmed fractal for this tf (see _ROLLING_VP_CACHE);
    otherwise the previously computed zones are returned as-is.
    """
    win = _VP_WIN.get(tf, 96)
    if len(bars) < win:
        return []

    _key = (symbol, tf)
    _frac_ts = _last_fractal_ts(bars, n=int(_VP_FRACTAL_N))
    _cached = _ROLLING_VP_CACHE.get(_key)
    if _cached is not None and _frac_ts and _cached[0] == _frac_ts:
        return _cached[1]            # no new fractal → serve the frozen profile
    try:
        # Honor the configured vp_bin_size[symbol] (falls back to DEFAULT_BIN_SIZE) so the
        # rolling VP matches the cached daily VP — else a config bin change only affects the
        # cache and the rolling source (the trigger's primary) keeps the hardcoded default.
        from pipeline.features.volume_profile import compute as vp_compute, _resolve_bin_size
        vp = vp_compute(bars[-win:], "daily", bars[-1].ohlc.c,
                        bin_size=_resolve_bin_size(symbol))
        _zones = [(float(z["low"]), float(z["high"])) for z in (vp.hvn_zones or [])]
        if _frac_ts:
            _ROLLING_VP_CACHE[_key] = (_frac_ts, _zones)
        return _zones
    except Exception:
        # keep serving the last good profile rather than dropping to no zones
        return _cached[1] if _cached is not None else []


def _cached_hvn(symbol: str) -> list[tuple[float, float]]:
    """HVN zones from the cached daily VP. From 20:00 IST (NY reference — matches the
    /exec/zones chart band) today's session is structural enough on its own: use TODAY
    ONLY. Before that, merge prev-day + today (today may still be too thin alone)."""
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from execution import clock as _clock
        from pipeline.features.vp_cache import get_prev_and_today
        prev_vp, today_vp = get_prev_and_today(symbol)
        # Off clock.now(), not wall time: a replay must evaluate this branch at the
        # SIMULATED hour, or it reads the live server's current IST hour and picks the
        # wrong source (today-only vs prev+today) for every historical bar.
        ist_hour = _dt.fromtimestamp(_clock.now(), tz=_tz(_td(hours=5, minutes=30))).hour
        sources = (today_vp,) if ist_hour >= 20 else (prev_vp, today_vp)
        zones: list[tuple[float, float]] = []
        for vp in sources:
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
    # Seed from the SORTED list, not the unsorted zones[0]. Seeding with an arbitrary
    # (possibly high) first element made the merge walk compare the lowest sorted zone
    # against a higher seed: lo <= out[-1][1] was spuriously True, so the lowest zone got
    # absorbed into the seed and its low silently discarded (observed: LVN 4130-4134
    # swallowed by rolling's 4146.8 seed → price sat inside a real LVN with no arm).
    ordered = sorted(zones)
    out: list[list[float]] = [list(ordered[0])]
    for lo, hi in ordered[1:]:
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


def _rolling_lvn(symbol: str, tf: str, bars: list[Bar]) -> list[tuple[float, float]]:
    """Price-tracking rolling-VP LVN zones over the ~24h window for `tf`. Mirrors
    _rolling_hvn exactly — same VP compute call, reads lvn_zones instead of hvn_zones."""
    win = _VP_WIN.get(tf, 96)
    if len(bars) < win:
        return []
    try:
        from pipeline.features.volume_profile import compute as vp_compute, _resolve_bin_size
        vp = vp_compute(bars[-win:], "daily", bars[-1].ohlc.c,
                        bin_size=_resolve_bin_size(symbol))
        return [(float(z["low"]), float(z["high"])) for z in (vp.lvn_zones or [])]
    except Exception:
        return []


def _cached_lvn(symbol: str) -> list[tuple[float, float]]:
    """LVN zones from the cached daily VP. Mirrors _cached_hvn's ≥20:00 IST today-only
    rule for consistency (same reference session both draw from)."""
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from execution import clock as _clock
        from pipeline.features.vp_cache import get_prev_and_today
        prev_vp, today_vp = get_prev_and_today(symbol)
        # Off clock.now(), not wall time: a replay must evaluate this branch at the
        # SIMULATED hour, or it reads the live server's current IST hour and picks the
        # wrong source (today-only vs prev+today) for every historical bar.
        ist_hour = _dt.fromtimestamp(_clock.now(), tz=_tz(_td(hours=5, minutes=30))).hour
        sources = (today_vp,) if ist_hour >= 20 else (prev_vp, today_vp)
        zones: list[tuple[float, float]] = []
        for vp in sources:
            if vp:
                zones += [(float(z["low"]), float(z["high"])) for z in (vp.get("lvn_zones") or [])]
        return zones
    except Exception:
        return []


def _session_lvn_zones(symbol: str, tf: str, bars: list[Bar]) -> tuple[list[tuple[float, float]], str]:
    """LVN zones for the current session — CACHED DAILY VP ONLY (the exact source the EA
    chart draws via /exec/zones get_prev_and_today). The rolling per-TF LVN was dropped
    2026-07-09: user wants to arm on the LVNs VISIBLE on the chart, not a rolling-window
    edge that sits ~3-4pt off the drawn rectangle. _cached_lvn already mirrors the chart's
    prev-D/today IST-band union, so trigger LVN == chart LVN now. (HVN path keeps its
    rolling∪cached merge — this change is LVN-only.)"""
    from pipeline.features.session import current_session
    ts = bars[-1].close_ts if bars else None
    sess = current_session(ts, symbol).session
    zones = _merge_zone_tuples(_cached_lvn(symbol))
    return zones, sess


def _filter_lvn_zones_by_hvn_context(
    zones: list[tuple[float, float]], hvn_zones: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """LVN edge-touch policy: exclude an LVN ONLY if it is FULLY CONTAINED (surrounded)
    within a single HVN — that's the true "not a real vacuum, just smoothing noise inside a
    high-volume node" case. A PARTIAL overlap is allowed: an LVN that pokes into an HVN edge
    (or straddles it) still carries a genuine vacuum on its non-overlapping side and is a
    valid edge-touch anchor.

    Rationale (2026-07-10 fix): the old rule dropped ANY LVN that overlapped an HVN at all.
    When the daily HVN is WIDE/thin (e.g. day-roll VP with one broad node), a sliver overlap
    (~2pt of a 13pt LVN) nuked the LVN sitting right where price was → lvn_edge_touch armed
    NOTHING. Fully-inside is the real exclusion; slivers survive.
    """
    if not hvn_zones or not zones:
        return zones
    allowed = []
    for lo, hi in zones:
        # Fully engulfed by a SINGLE HVN → smoothing noise inside a node, drop it.
        fully_inside = any(h_lo <= lo and hi <= h_hi for h_lo, h_hi in hvn_zones)
        if fully_inside:
            continue
        # PARTIAL overlap (2026-07-10): the LVN straddles an HVN edge — its overlapping
        # portion sits INSIDE the node (a fake vacuum edge there). CLIP the LVN to the HVN
        # boundary so ONLY the genuine vacuum OUTSIDE the node survives as an arm anchor.
        # Keeps the real vacuum, kills the false inside-edge (concern: LVN edge inside HVN).
        clo, chi = lo, hi
        for h_lo, h_hi in hvn_zones:
            if chi <= h_lo or clo >= h_hi:
                continue   # no overlap with this HVN
            # overlap on the LOW side of the LVN (LVN bottom inside HVN) → lift clo to h_hi
            if clo < h_hi <= chi and clo >= h_lo:
                clo = h_hi
            # overlap on the HIGH side (LVN top inside HVN) → drop chi to h_lo
            elif clo <= h_lo < chi and chi <= h_hi:
                chi = h_lo
        if chi - clo > 0:
            allowed.append((clo, chi))
    return allowed


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
        _buf_pct = float(_gcfg.get("hvn_touch_buffer_pct", 0.0))
        _lookback = int(_gcfg.get("hvn_lookback_bars", 3))
    except Exception:
        _buf = 0.0
        _buf_pct = 0.0
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
            _buf_eff = max(_buf, width * _buf_pct)   # width-relative tap tolerance
            touch_top = h >= hi - _buf_eff   # wick reached edge or came within (relative) buffer
            touch_bot = lo_p <= lo + _buf_eff
            if not (touch_top or touch_bot):
                # closed inside but no edge tapped — report the nearest miss
                _hvn_dbg(f"  bar[{bar_idx}] c={c:.2f} h={h:.2f} l={lo_p:.2f} INSIDE "
                         f"[{lo:.2f},{hi:.2f}] buf_eff={_buf_eff:.2f}: no edge tap "
                         f"(top short by {hi - _buf_eff - h:.2f}, bot short by {lo_p - (lo + _buf_eff):.2f})")
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
    # NEAR edge of the next HVN (2026-08-05, user) — was far edge (compute_hvn_tps),
    # switched to compute_lvn_edge_tps's near-edge convention: stop at the entrance of
    # the next node instead of assuming a full pass-through. Neither call site passes
    # skip_node (a pre-existing gap, unrelated to this change), so this is a drop-in swap.
    tp_up, tp_down = compute_lvn_edge_tps(symbol, edge, _daily_zones or list(zones))

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


def touch_arm_trigger(symbol: str, tf: str, live_price: float,
                      zone_shift: float = 0.0) -> Trigger | None:
    """INTRABAR variant of _t_hvn_inside_touch — arm on LIVE price tapping an HVN edge
    without waiting for the candle to close. Caller (the poll handler) owns the
    tick-reversal confirm; this just resolves which edge live_price is tapping and
    builds the same Trigger _t_hvn_inside_touch would, with the live edge as fulcrum.

    Returns None if live_price isn't inside any session HVN within hvn_touch_buffer of
    an edge. Same TP/node geometry as the close-driven path so downstream sizing is
    identical — only the trigger moment differs (touch vs close).

    zone_shift: additive offset (venue − analysis) added to every analysis-frame zone edge
    (HVN zones AND the daily-VP TP candidates) so they are compared against — and returned
    in — the VENUE frame the live_price and the EA orders use. 0.0 = frames identical."""
    import yaml as _yaml
    from pipeline.state_store import store
    try:
        _cfg = _yaml.safe_load(
            (__import__("pathlib").Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text()
        ) or {}
        _gcfg2 = _cfg.get("grid_levels") or {}
        _buf = float(_gcfg2.get("hvn_touch_buffer", 0.0))
        _buf_pct = float(_gcfg2.get("hvn_touch_buffer_pct", 0.0))
    except Exception:
        _gcfg2 = {}          # must exist — read below for hvn_accept_lookback_bars
        _buf = 0.0
        _buf_pct = 0.0

    win = _VP_WIN.get(tf, 96)
    bars = store().recent(symbol, tf, win + 5)
    if len(bars) < 2 or live_price <= 0:
        return None
    zones, sess = _session_hvn_zones(symbol, tf, bars)
    if not zones:
        return None
    # Frame: zones are analysis-frame; live_price arrives venue-frame. Bring live_price into
    # analysis frame for the tap comparison (−zone_shift). Everything returned (edge, node,
    # TP) STAYS analysis-frame — plan_grid_levels + the caller's edge*ratio rebase it to venue
    # downstream. This keeps the detector's output frame identical to the pre-rebase contract.
    live_price = live_price - zone_shift

    # Tap must come FROM INSIDE the HVN body: live price sits within the node [lo,hi] AND
    # within hvn_touch_buffer of an edge — an edge-rejection from within value. An OUTSIDE
    # approach (price below the node tapping the bottom from below, or above tapping the top
    # from above) is NOT an inside-touch and is excluded (matches the close-driven path,
    # which requires the candle to CLOSE inside the node).
    # ACCEPTANCE PRECONDITION (2026-08-05, user): "requires a candle to close inside the
    # HVN first, then it arms on a tap + 0.2$ retracement". Live price merely being inside
    # the node is NOT enough — that also matches a wick spearing through an untested node.
    # A prior CLOSE inside means the market actually accepted value there, which is what
    # makes the edge worth fading, and it restores parity with the close-driven path (whose
    # whole premise is "the candle closed inside"). Only CLOSED bars count.
    _accept_lb = int(_gcfg2.get("hvn_accept_lookback_bars", 3) or 3)
    _closed = [b for b in bars if b.close_ts and b.close_ts < 9_000_000_000][-_accept_lb:]

    best = None   # (dist_to_edge, edge, width, side)
    for lo, hi in zones:
        width = hi - lo
        # a recent candle must have CLOSED inside THIS node (analysis frame)
        if not any(lo <= float(b.ohlc.c) <= hi for b in _closed):
            continue
        if width <= 0:
            continue
        if not (lo <= live_price <= hi):       # live price must be INSIDE the node body
            continue
        _buf_eff = max(_buf, width * _buf_pct)  # width-relative tap tolerance
        touch_top = live_price >= hi - _buf_eff  # inside, within (relative) buffer of top edge
        touch_bot = live_price <= lo + _buf_eff  # inside, within (relative) buffer of bottom edge
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
                        for z in (_dvp_d.get("hvn_zones") or [])]   # analysis frame (edge is too)
    except Exception:
        _daily_zones = list(zones)
    # NEAR edge of the next HVN (2026-08-05, user) — was far edge (compute_hvn_tps),
    # switched to compute_lvn_edge_tps's near-edge convention: stop at the entrance of
    # the next node instead of assuming a full pass-through. Neither call site passes
    # skip_node (a pre-existing gap, unrelated to this change), so this is a drop-in swap.
    tp_up, tp_down = compute_lvn_edge_tps(symbol, edge, _daily_zones or list(zones))

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


def _t_lvn_edge_touch(symbol: str, tf: str, current_price: float) -> Trigger | None:
    """A recent candle CLOSES INSIDE an LVN (low-volume vacuum) *and* TAPS one of its
    edges — geometrically identical to _t_hvn_inside_touch, reading LVN zone bounds
    instead of HVN. No fade-side/breakout-side asymmetry (no reversion thesis) — TP for
    both directions comes from compute_lvn_edge_tps' near-edge-of-next-HVN rule.
    Stateless / causal."""
    import yaml as _yaml
    from pipeline.state_store import store
    try:
        _cfg = _yaml.safe_load(
            (__import__("pathlib").Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text()
        ) or {}
        _gcfg = _cfg.get("grid_levels") or {}
        _buf = float(_gcfg.get("lvn_touch_buffer", 0.0))
        _buf_pct = float(_gcfg.get("lvn_touch_buffer_pct", 0.0))
        _buf_ppct = float(_gcfg.get("lvn_touch_buffer_price_pct", 0.0))
        _lookback = int(_gcfg.get("lvn_lookback_bars", 3))
    except Exception:
        _buf = 0.0
        _buf_pct = 0.0
        _buf_ppct = 0.0
        _lookback = 3

    win = _VP_WIN.get(tf, 96)
    bars = store().recent(symbol, tf, win + 5)
    if len(bars) < 2:
        return None
    zones, sess = _session_lvn_zones(symbol, tf, bars)
    if not zones:
        return None
    try:
        from pipeline.features.vp_cache import get as _vp_get_hvn
        _dvp_ctx = _vp_get_hvn(symbol, "daily") or {}
        _ctx_hvn_zones = [(float(z["low"]), float(z["high"]))
                          for z in (_dvp_ctx.get("hvn_zones") or [])]
        zones = _filter_lvn_zones_by_hvn_context(zones, _ctx_hvn_zones)
    except Exception:
        pass
    if not zones:
        return None

    candidate_bars = bars[-(1 + _lookback):-1]
    candidate_bars = list(reversed(candidate_bars))

    best_trigger = None
    for bar_idx, cur in enumerate(candidate_bars):
        c, h, lo_p = cur.ohlc.c, cur.ohlc.h, cur.ohlc.l
        best = None
        for lo, hi in zones:
            width = hi - lo
            if width <= 0:
                continue
            # Edge proximity only — no close-inside-zone gate. A bar whose wick reaches
            # an LVN edge (from either side) is a valid tap; the close needn't sit inside.
            _buf_eff = max(_buf, width * _buf_pct, abs(current_price) * _buf_ppct)
            touch_top = (h >= hi - _buf_eff) and (lo_p <= hi + _buf_eff)
            touch_bot = (lo_p <= lo + _buf_eff) and (h >= lo - _buf_eff)
            if not (touch_top or touch_bot):
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
        if best is not None:
            _dist, edge, width, side, reject_frac = best
            conf = min(0.9, 0.55 + min(reject_frac, 0.3)) * (1.0 - bar_idx * 0.05)
            best_trigger = (edge, width, side, reject_frac, conf, sess)
            break

    if best_trigger is None:
        return None

    edge, width, side, reject_frac, conf, sess = best_trigger

    try:
        from pipeline.features.vp_cache import get as vp_get
        _dvp_d = vp_get(symbol, "daily") or {}
        _daily_hvn_zones = [(float(z["low"]), float(z["high"]))
                            for z in (_dvp_d.get("hvn_zones") or [])]
    except Exception:
        _daily_hvn_zones = []
    tp_up, tp_down = compute_lvn_edge_tps(symbol, edge, _daily_hvn_zones)

    node_low  = (edge - width) if side == "top" else edge
    node_high = edge if side == "top" else (edge + width)
    return Trigger(
        kind="lvn_edge_touch",
        fulcrum_price=float(edge),
        raw_range=float(width),
        confidence=float(conf),
        context={"bias": "none", "edge": side, "session": sess,
                 "reject_frac": round(reject_frac, 4),
                 "node_low": float(node_low), "node_high": float(node_high),
                 "tp_up": float(tp_up), "tp_down": float(tp_down)},
    )


def lvn_touch_arm_trigger(symbol: str, tf: str, live_price: float,
                          lookback_bars: int = 1, zone_shift: float = 0.0) -> Trigger | None:
    """INTRABAR variant of _t_lvn_edge_touch — arm on LIVE price tapping an LVN edge
    without waiting for the candle to close. Mirrors touch_arm_trigger exactly, reading
    LVN zone bounds instead of HVN. Caller owns the tick-reversal confirm.

    lookback_bars: after checking the current LIVE price, also check the last N
    CLOSED bars' high/low against each zone edge. A wick can tap an edge and reverse
    before the next intrabar poll catches it — since this trigger is touch_only (the
    bar-close detector's confirmation of that same wick is otherwise discarded), a
    short lookback recovers a touch that already happened and passed. 0 = live-price
    only (original behavior).

    zone_shift: additive offset (venue − analysis) added to every zone edge so analysis-
    frame LVN zones register taps from — and return prices in — the venue frame the live
    price and EA orders use. 0.0 = frames identical. See touch_arm_trigger."""
    import yaml as _yaml
    from pipeline.state_store import store
    try:
        _cfg = _yaml.safe_load(
            (__import__("pathlib").Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text()
        ) or {}
        _gcfg2 = _cfg.get("grid_levels") or {}
        _buf = float(_gcfg2.get("lvn_touch_buffer", 0.0))
        _buf_pct = float(_gcfg2.get("lvn_touch_buffer_pct", 0.0))
        _buf_ppct = float(_gcfg2.get("lvn_touch_buffer_price_pct", 0.0))
    except Exception:
        _buf = 0.0
        _buf_pct = 0.0
        _buf_ppct = 0.0

    win = _VP_WIN.get(tf, 96)
    bars = store().recent(symbol, tf, win + 5)
    if len(bars) < 2 or live_price <= 0:
        return None
    zones, sess = _session_lvn_zones(symbol, tf, bars)
    if not zones:
        return None
    try:
        from pipeline.features.vp_cache import get as _vp_get_hvn
        _dvp_ctx = _vp_get_hvn(symbol, "daily") or {}
        _ctx_hvn_zones = [(float(z["low"]), float(z["high"]))
                          for z in (_dvp_ctx.get("hvn_zones") or [])]
        zones = _filter_lvn_zones_by_hvn_context(zones, _ctx_hvn_zones)
    except Exception:
        pass
    if not zones:
        return None

    # Candidate prices to test against each edge: live price first (closest/most
    # relevant), then each recent CLOSED bar's high AND low (a wick either side could
    # have tapped an edge). bars[-1] is itself the most recently CLOSED bar (the store
    # holds only closed bars) — distinct from `live_price`, the current in-progress
    # tick — so it's included, not excluded. First candidate that qualifies wins —
    # live price is tried first so an active tap is still preferred over a stale one.
    # Frame: zones + recent bars are analysis-frame; live_price is venue-frame. Bring
    # live_price into analysis frame (−zone_shift) so all candidates compare against the
    # analysis-frame zones consistently; the resulting edge is shifted back to venue on return.
    _recent = bars[-max(0, lookback_bars):] if lookback_bars > 0 else []
    candidates = [live_price - zone_shift]   # venue → analysis
    for _b in reversed(_recent):   # most recent bar's wicks first (already analysis-frame)
        candidates.append(float(_b.ohlc.h))
        candidates.append(float(_b.ohlc.l))

    best = None
    for _px in candidates:
        for lo, hi in zones:
            width = hi - lo
            if width <= 0:
                continue
            # Edge proximity only — no inside-zone gate. Price tapping an LVN edge from
            # OUTSIDE the vacuum is just as valid a straddle anchor as from inside.
            _buf_eff = max(_buf, width * _buf_pct, abs(_px) * _buf_ppct)
            touch_top = abs(_px - hi) <= _buf_eff
            touch_bot = abs(_px - lo) <= _buf_eff
            if not (touch_top or touch_bot):
                continue
            if touch_top and touch_bot:
                edge, side = (hi, "top") if abs(hi - _px) <= abs(lo - _px) else (lo, "bottom")
            else:
                edge, side = (hi, "top") if touch_top else (lo, "bottom")
            dist = abs(edge - _px)
            if best is None or dist < best[0]:
                best = (dist, edge, width, side)
        if best is not None:
            break   # this candidate price qualified — don't fall through to staler ones
    if best is None:
        return None
    _dist, edge, width, side = best

    try:
        from pipeline.features.vp_cache import get as vp_get
        _dvp_d = vp_get(symbol, "daily") or {}
        _daily_hvn_zones = [(float(z["low"]), float(z["high"]))
                            for z in (_dvp_d.get("hvn_zones") or [])]
    except Exception:
        _daily_hvn_zones = []
    tp_up, tp_down = compute_lvn_edge_tps(symbol, edge, _daily_hvn_zones)

    # edge/TP stay ANALYSIS-frame (candidates were brought into analysis via −zone_shift
    # above). plan_grid_levels + the caller's edge*ratio rebase to venue downstream — same
    # contract as touch_arm_trigger. Do NOT shift back here (would double-rebase).
    node_low  = (edge - width) if side == "top" else edge
    node_high = edge if side == "top" else (edge + width)
    return Trigger(
        kind="lvn_edge_touch",
        fulcrum_price=float(edge),
        raw_range=float(width),
        confidence=0.6,
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


_HTF_MAP = {"1m": "15m", "3m": "15m", "5m": "15m", "10m": "1h", "15m": "1h", "1h": "4h"}


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


def bb_tp(bars: list, cfg: dict | None = None) -> tuple[float, float]:
    """Upper/lower Bollinger Band levels as a last-resort TP when no structural exit exists.

    Used as the final fallback in the TP cascade (after HVN far-edge, VAH/VAL/LVN, POC,
    fib-ext) so a cycle always has a target rather than 0. Band recomputed each refresh,
    so the target tracks the live regime rather than a frozen arm-time level.

    Returns (bb_upper, bb_lower) at `bb_tp_sigma` (default 2.5) stddevs from the
    `squeeze_bb_period`-bar (default 20) SMA. Both 0.0 when bars are insufficient.
    """
    cfg = cfg or {}
    period = int(cfg.get("squeeze_bb_period", 20))
    sigma = float(cfg.get("bb_tp_sigma", 2.5))
    if len(bars) < period:
        return 0.0, 0.0
    closes = [float(b.ohlc.c) for b in bars[-period:]]
    sma = sum(closes) / period
    var = sum((c - sma) ** 2 for c in closes) / period
    sd = math.sqrt(var)
    if sd <= 0:
        return 0.0, 0.0
    return round(sma + sigma * sd, 4), round(sma - sigma * sd, 4)


# London open (07:30–10:00 UTC) and NY open (13:30–16:00 UTC).
def session_atr_ratio(bars, window: int = 20) -> float:
    """Ratio of smoothed-recent bar range to rolling-mean range.
    Uses the last 3 bars as 'current' (avoids single-bar spikes) vs the preceding
    `window` bars as the baseline. Returns 1.0 when insufficient data."""
    if not bars or len(bars) < window + 4:
        return 1.0
    try:
        ranges = [float(b.high - b.low) for b in bars
                  if hasattr(b, "high") and hasattr(b, "low")]
        if len(ranges) < window + 4:
            return 1.0
        current  = sum(ranges[-3:]) / 3.0
        baseline = sum(ranges[-window - 3:-3]) / window
        return current / baseline if baseline > 0 else 1.0
    except Exception:
        return 1.0


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


def squeeze_bb_snapshot(symbol: str, tf: str, cfg: dict | None = None) -> dict:
    """Raw Bollinger state at the LAST bar — the numbers squeeze_gate derives its rank from
    but discards. Logged on arm rows so the squeeze A/B can regress outcomes against the
    actual band, not just the compressed/not bool. Same BB(period, bb_mult·σ) / BBW math as
    squeeze_gate — stateless, causal, identical live and sim.

    Returns (all None-safe when history is too short):
      ok        — compressed NOW (rank ≤ bbw_pct)
      rank      — BBW percentile within trailing window (== squeeze_rank)
      bbw       — (upper − lower)/mid at the last bar (absolute compression magnitude)
      bb_upper / bb_lower / bb_mid — the band in price
      px_pos    — where the last close sits in the band: 0=lower, 0.5=mid, 1=upper (can exceed [0,1])
    """
    cfg = cfg or {}
    period = int(cfg.get("squeeze_bb_period", 20))
    bb_mult = float(cfg.get("squeeze_bb_mult", 3.0))
    bbw_pct = float(cfg.get("squeeze_bbw_pct", 0.15))
    bbw_window = int(cfg.get("squeeze_bbw_window", 100))
    _null = {"ok": None, "rank": None, "bbw": None,
             "bb_upper": None, "bb_lower": None, "bb_mid": None, "px_pos": None}
    try:
        from pipeline.state_store import store
        bars = store().recent(symbol, tf, period + bbw_window + 6)
    except Exception:
        return _null
    n = len(bars)
    if n < period + bbw_window + 1:
        return _null
    closes = [b.ohlc.c for b in bars]

    def _stdev(vals: list[float]) -> float:
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))

    bbw = [0.0] * n
    for e in range(period - 1, n):
        w = closes[e - period + 1:e + 1]
        mid, sd = sum(w) / len(w), _stdev(w)
        bbw[e] = (2.0 * bb_mult * sd) / mid if mid > 0 else 0.0

    last = n - 1
    w = closes[last - period + 1:last + 1]
    mid, sd = sum(w) / len(w), _stdev(w)
    upper = mid + bb_mult * sd
    lower = mid - bb_mult * sd
    lo = last - bbw_window + 1
    hist = bbw[lo:last + 1]
    rank = sum(1 for v in hist if v <= bbw[last]) / len(hist) if hist else 1.0
    span = upper - lower
    px = closes[last]
    return {
        "ok": bool(rank <= bbw_pct),
        "rank": round(rank, 4),
        "bbw": round(bbw[last], 6),
        "bb_upper": round(upper, 5),
        "bb_lower": round(lower, 5),
        "bb_mid": round(mid, 5),
        "px_pos": round((px - lower) / span, 4) if span > 0 else None,
    }


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


def _t_bb_expansion_touch(symbol: str, tf: str, current_price: float, atr: float,
                          cfg: dict | None = None) -> Trigger | None:
    """Post-squeeze BB expansion + band edge touch trigger.

    Pattern (all three required):
      1. BBW was coiled (rank ≤ bbw_pct) for ≥ squeeze_min_on_bars within the last
         bb_expansion_post_squeeze_bars bars, and has since released.
      2. Price wick reached the bb_expansion_touch_sigma band (default 2.5σ) within the
         last bb_expansion_touch_lookback bars during the expansion phase.
      3. HTF BB direction (bb_htf_bias) HARD-GATES: upper-band touch requires HTF bearish
         (≤ −1); lower-band touch requires HTF bullish (≥ +1).  Disable with
         bb_expansion_htf_gate: false.

    Footprint overlay (bb_expansion_fp_gate: true):
      close_failure_absorption on the touching bar → fp_signal="absorption" (reversal)
      no absorption + delta confirming direction → fp_signal="continuation"

    Fulcrum = BB mid (20-SMA) at the touch bar — the natural invalidation SL.
    The counter-side SL is wired to this fulcrum in grid_planner (same mechanic as
    hvn_displacement's candle-extreme SL).
    """
    cfg = cfg or {}
    if not bool(cfg.get("bb_expansion_touch_enabled", True)):
        return None

    period       = int(cfg.get("squeeze_bb_period", 20))
    bb_mult      = float(cfg.get("squeeze_bb_mult", 3.0))
    bbw_pct      = float(cfg.get("squeeze_bbw_pct", 0.15))
    bbw_window   = int(cfg.get("squeeze_bbw_window", 100))
    min_on       = int(cfg.get("squeeze_min_on_bars", 6))
    post_bars    = int(cfg.get("bb_expansion_post_squeeze_bars", 10))
    touch_sigma  = float(cfg.get("bb_expansion_touch_sigma", 2.5))
    touch_lb     = int(cfg.get("bb_expansion_touch_lookback", 3))
    htf_gate     = bool(cfg.get("bb_expansion_htf_gate", True))
    fp_gate      = bool(cfg.get("bb_expansion_fp_gate", True))

    from pipeline.state_store import store
    need = period + bbw_window + post_bars + touch_lb + 8
    bars = store().recent(symbol, tf, need)
    n = len(bars)
    if n < period + bbw_window + 2:
        return None

    closes = [b.ohlc.c for b in bars]

    def _sd(vals: list[float]) -> float:
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))

    # --- precompute BBW + per-bar BB bands ---
    bbw        = [0.0] * n
    bb_mid_a   = [0.0] * n
    bb_upper_a = [0.0] * n   # at touch_sigma
    bb_lower_a = [0.0] * n

    for e in range(period - 1, n):
        w   = closes[e - period + 1:e + 1]
        mid = sum(w) / len(w)
        sd  = _sd(w)
        bb_mid_a[e]   = mid
        bb_upper_a[e] = mid + touch_sigma * sd
        bb_lower_a[e] = mid - touch_sigma * sd
        bbw[e] = (2.0 * bb_mult * sd) / mid if mid > 0 else 0.0

    def _rank(e: int) -> float:
        lo = e - bbw_window + 1
        if lo < period - 1:
            return 1.0
        hist = bbw[lo:e + 1]
        return sum(1 for v in hist if v <= bbw[e]) / len(hist)

    last = n - 1

    # Step 1 — must be in expansion now (not still coiled)
    if _rank(last) <= bbw_pct:
        return None

    # Step 2 — find the most recent coil that ended within post_bars + touch_lb bars
    coil_end = -1
    min_coil_rank = 1.0
    coil_run = 0
    scan_limit = min(last - (period - 1), post_bars + touch_lb + 6)
    for i in range(1, scan_limit + 1):
        e = last - i
        if e < period - 1:
            break
        if _rank(e) <= bbw_pct:
            # walk backwards to measure consecutive coil run
            k, run = e, 0
            while k >= period - 1 and _rank(k) <= bbw_pct:
                r = _rank(k)
                if r < min_coil_rank:
                    min_coil_rank = r
                run += 1
                k -= 1
            if run >= min_on:
                coil_end = e
                coil_run = run
            break   # only need the most recent coil

    if coil_end < 0:
        return None

    bars_since_release = last - coil_end
    if bars_since_release > post_bars + touch_lb:
        return None

    # Step 3 — band touch within the expansion window (coil_end+1 … last, up to touch_lb)
    touch_idx  = -1
    touch_side = ""
    touch_price = 0.0
    touch_bb_mid = 0.0

    scan_from = max(coil_end + 1, last - touch_lb + 1)
    for i in range(last, scan_from - 1, -1):
        if bb_upper_a[i] <= 0:
            continue
        bar_i = bars[i]
        if bar_i.ohlc.h >= bb_upper_a[i]:
            touch_idx  = i
            touch_side = "upper"
            touch_price = bb_upper_a[i]
            touch_bb_mid = bb_mid_a[i]
            break
        if bar_i.ohlc.l <= bb_lower_a[i]:
            touch_idx  = i
            touch_side = "lower"
            touch_price = bb_lower_a[i]
            touch_bb_mid = bb_mid_a[i]
            break

    if touch_idx < 0:
        return None

    # Step 4 — HTF hard gate: upper touch needs HTF bearish; lower needs HTF bullish
    htf_vote, htf_why = bb_htf_bias(symbol, tf, cfg)
    if htf_gate:
        if touch_side == "upper" and htf_vote >= 0:
            return None
        if touch_side == "lower" and htf_vote <= 0:
            return None

    bias_dir = "sell" if touch_side == "upper" else "buy"

    # Step 5 — footprint at the touch bar
    fp_signal = "neutral"
    if fp_gate:
        try:
            from pipeline.footprint import build as build_fp
            from pipeline.features.absorption import detect_close_failure_absorption
            from pipeline.features.delta import bar_delta
            touch_bar = bars[touch_idx]
            fp = build_fp(touch_bar)
            absorbs = detect_close_failure_absorption(touch_bar, fp)
            delta   = bar_delta(fp)
            absorb_sides = {a.side for a in absorbs}
            if touch_side == "upper" and "sell" in absorb_sides:
                fp_signal = "absorption"   # buyers pushed to upper BB, got absorbed
            elif touch_side == "lower" and "buy" in absorb_sides:
                fp_signal = "absorption"   # sellers pushed to lower BB, got absorbed
            elif touch_side == "upper" and delta > 0:
                fp_signal = "continuation" # aggressive buyers at upper BB → breakout
            elif touch_side == "lower" and delta < 0:
                fp_signal = "continuation" # aggressive sellers at lower BB → breakdown
        except Exception:
            pass

    # confidence: coil depth + footprint boost + HTF strength
    depth = max(0.0, (bbw_pct - min_coil_rank) / bbw_pct) if bbw_pct > 0 else 0.0
    conf  = min(0.90, 0.55 + 0.30 * depth)
    if fp_signal == "absorption":
        conf = min(0.95, conf + 0.10)
    conf = min(0.95, conf + 0.03 * abs(htf_vote))

    # raw_range = full BB width at touch bar (feeds step sizing)
    raw_range = bb_upper_a[touch_idx] - bb_lower_a[touch_idx]
    if raw_range <= 0:
        raw_range = 3.0 * atr if atr > 0 else 1.0

    # TP: next HVN beyond the touch side
    tp_up, tp_down = 0.0, 0.0
    try:
        from pipeline.features.vp_cache import get as vp_get
        _dvp   = vp_get(symbol, "daily") or {}
        _zones = [(float(z["low"]), float(z["high"]))
                  for z in (_dvp.get("hvn_zones") or [])]
        if _zones:
            tp_up, tp_down = compute_hvn_tps(symbol, touch_bb_mid, _zones)
    except Exception:
        pass

    return Trigger(
        kind="bb_expansion_touch",
        fulcrum_price=float(touch_price),
        raw_range=float(raw_range),
        confidence=float(conf),
        context={
            "bias":               bias_dir,
            "interpretation":     ("reversal"      if fp_signal == "absorption"
                                   else "continuation" if fp_signal == "continuation"
                                   else "fade"),
            "touch_side":         touch_side,
            "touch_price":        round(touch_price, 4),
            "bb_mid":             round(touch_bb_mid, 4),
            "htf_why":            htf_why,
            "fp_signal":          fp_signal,
            "coil_bars":          int(coil_run),
            "bars_since_release": int(bars_since_release),
            "tp_up":              float(tp_up),
            "tp_down":            float(tp_down),
        },
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


# ── Candle sweep / engulf detector ──────────────────────────────────────────

def _t_candle_sweep(symbol: str, tf: str, current_price: float,
                    cfg: dict | None = None,
                    bars_override: list | None = None) -> "Trigger | None":
    """One-sided liquidity SWEEP+RECLAIM against the previous bar (engulf variant
    removed — sweep-only).

    BUY sweep:  current bar's low < prev low (swept sell-side liquidity), THEN
                closes ABOVE prev high (reclaimed and broke out). High needn't
                exceed prev high — only the low needs to sweep.
    SELL sweep: current bar's high > prev high (swept buy-side liquidity), THEN
                closes BELOW prev low. Low needn't undercut prev low.

    Grid arms as a breakout straddle: buy stops above candle_high, sell stops
    below candle_low. The fulcrum is the candle midpoint (for dedup); the actual
    leg anchor prices are stored in context as candle_high / candle_low.

    bars_override: when given, detect on THESE bars (e.g. Vantage-native OHLC
    from the EA's CopyRates poll) instead of the analysis-feed (Binance/Bybit)
    store — so the sweep pattern is checked on the same candle the broker fills
    against, no venue rebase needed for candle_high/candle_low afterward.
    """
    cfg = cfg or {}
    min_size = float(cfg.get("candle_sweep_min_size", 0.0) or 0.0)
    if bars_override is not None:
        bars = bars_override
        if len(bars) < 2:
            return None
    else:
        try:
            from pipeline.state_store import store as _store
            bars = _store().recent(symbol, tf, 5)
            if len(bars) < 2:
                return None
        except Exception:
            return None

    # Check ONLY the last 2 closed bars (lookback=2) so a sweep that closed one bar ago is
    # still caught when the poll fires just after the next bar opens — but a STALE sweep from
    # many bars back does NOT arm now (that was the bug: the old `range(len-1, 0, -1)` scanned
    # the WHOLE venue window (10 bars) and armed on any qualifying bar, so a sweep 5-8 bars ago
    # armed a fresh cycle with no recent confirmation). Newest-first so the most recent wins.
    _lookback_sweep = 2
    _lo_i = max(1, len(bars) - _lookback_sweep)
    best_pair: tuple | None = None
    for i in range(len(bars) - 1, _lo_i - 1, -1):
        _cur  = bars[i]
        _prev = bars[i - 1]
        _ch, _cl, _cc = _cur.ohlc.h, _cur.ohlc.l, _cur.ohlc.c
        _ph, _pl       = _prev.ohlc.h, _prev.ohlc.l
        _sb = _cl < _pl and _cc > _ph              # sweep bull: swept low, closed above prev high
        _sw = _ch > _ph and _cc < _pl              # sweep bear: swept high, closed below prev low
        if _sb or _sw:
            best_pair = (_cur, _prev, _ch, _cl, _cc, _ph, _pl, _sb, _sw)
            break  # most recent qualifying bar wins
    if best_pair is None:
        return None
    cur, prev, ch, cl, cc, ph, pl, sweep_bull, sweep_bear = best_pair

    direction  = "bull" if sweep_bull else "bear"
    candle_hl  = ch - cl
    if min_size > 0 and candle_hl < min_size:
        return None

    # VWAP proxy: session POC (most-traded price = fair value BE anchor).
    vwap = 0.0
    try:
        from pipeline.features.vp_cache import get_prev_and_today as _gpat
        _prev_d, _today_d = _gpat(symbol)
        _dvp = _today_d or _prev_d or {}
        vwap = float(_dvp.get("poc") or 0.0)
    except Exception:
        pass

    is_sweep = True   # sweep-only detector; engulf variant removed
    conf = 0.75       # outside-bar liquidity sweep
    return Trigger(
        kind="candle_sweep",
        fulcrum_price=float((ch + cl) / 2.0),
        raw_range=float(candle_hl),
        confidence=float(conf),
        context={
            "direction":   direction,
            "candle_high": float(ch),
            "candle_low":  float(cl),
            "candle_hl":   float(candle_hl),
            "is_sweep":    is_sweep,
            "vwap":        float(vwap),
        },
    )


# ── trigger-list helpers ─────────────────────────────────────────────────────

def _trigger_entry(cfg: dict | None, kind: str) -> dict | None:
    """Return the trigger config dict for `kind` if enabled, else None."""
    for entry in ((cfg or {}).get("triggers") or []):
        if isinstance(entry, str):
            if entry == kind:
                return {"kind": kind}
        elif isinstance(entry, dict) and entry.get("kind") == kind:
            if entry.get("enabled", True):
                return entry
            return None
    return None


def _kind_active_for_tf(cfg: dict | None, kind: str, tf: str) -> bool:
    """True if `kind` is enabled in the triggers list and `tf` is in its tfs."""
    entry = _trigger_entry(cfg, kind)
    if entry is None:
        return False
    tfs = entry.get("tfs")
    return (not tfs) or (tf in tfs)


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
        _t_hvn_edge(symbol, tf, current_price, cfg),
        _t_hvn_inside_touch(symbol, tf, current_price) if _kind_active_for_tf(cfg, "hvn_inside_touch", tf) else None,
        _t_lvn_edge_touch(symbol, tf, current_price) if _kind_active_for_tf(cfg, "lvn_edge_touch", tf) else None,
        _t_anchor(symbol, current_price, atr, latest),
        _t_va(symbol, current_price, regime, daily_vp),
        _t_vp_level_touch(symbol, tf, current_price, daily_vp, atr, cfg),
        _t_squeeze(symbol, tf, current_price, atr, cfg),
        _t_bb_expansion_touch(symbol, tf, current_price, atr, cfg),
        _t_cvd_div(symbol, tf, current_price, latest),
        _t_candle_sweep(symbol, tf, current_price, cfg) if _kind_active_for_tf(cfg, "candle_sweep", tf) else None,
    ):
        if t is not None:
            out.append(t)
    return out
