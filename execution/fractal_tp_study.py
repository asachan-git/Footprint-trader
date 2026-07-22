"""Fractal-triggered VP refresh → TP re-target, LOG-ONLY.

When a 3-candle fractal is confirmed on a cycle's TF, recompute the ROLLING volume
profile at that moment and run the fresh HVN/LVN/POC through the SAME TP cascade the
planner uses at arm time. Record what the TP *would* become. Nothing is executed —
this branch has no MODIFY_TP command (the full set is PLACE_PENDING / CLOSE_ALL /
CANCEL_PENDINGS / CLOSE_SIDE / MOVE_BE / OPEN_MARKET), so acting on the signal would
need a new command type plus an EA recompile. Measure first.

The fractal is a REFRESH TRIGGER, not a level: the pivot price is never injected into
the zone set. It only says "structure just changed, rebuild the profile now" instead of
waiting for the scheduled refresh.

FRAMES — the one thing most likely to make this study lie:
  * bars from state_store, and therefore fractal prices, are ANALYSIS frame (Binance)
  * a fresh volume_profile.compute() is ANALYSIS frame (no venue shift applied)
  * a live cycle's tp_up/tp_down are VENUE frame (post grid_planner._rebase_to_venue)
Comparing tp_*_new against tp_*_live without converting yields a constant fake delta
equal to the venue basis (~3pt on gold). Every row therefore carries BOTH frames plus
the shift used, so a bad conversion is visible in the data rather than silent.
"""

from __future__ import annotations

import time
from typing import Any

# (symbol, tf, pivot_ts, kind) -> True. Module-level so a pivot is reported once, not
# once per poll. Nothing in the repo tracks "new pivot since last bar", so this is it.
_seen_fractals: dict[tuple, bool] = {}

# (symbol, magic) -> last bar close_ts the study ran on. monitor_cycle fires at ~1Hz;
# this throttles the study to once per closed bar per cycle.
_last_bar: dict[tuple, int] = {}

_MAX_SEEN = 4000   # bound the dedup dict so a long session can't grow it without limit


def _prune_seen() -> None:
    if len(_seen_fractals) > _MAX_SEEN:
        for k in list(_seen_fractals)[: len(_seen_fractals) // 2]:
            _seen_fractals.pop(k, None)


def newly_confirmed_fractal(symbol: str, tf: str, bars: list) -> Any | None:
    """The 3-candle fractal (1 left + 1 right) that just became confirmable, or None.

    choch.detect_swing_points(bars, n=1) IS the 3-bar fractal — reused rather than
    reimplemented. It carries idx/ts/bar_id/kind/price and uses strict comparison, so
    there are no plateau duplicates. swing._structural_pivots can't be used here: it
    returns prices only and cannot say WHEN a pivot formed.

    The newest confirmable pivot sits at idx == len(bars) - 2 (one bar of lag — the
    asymmetric reversal_pattern.py precedent). Returns it only the first time it is
    seen, so a caller polling repeatedly gets one event per pivot.
    """
    if len(bars) < 3:
        return None
    try:
        from pipeline.features.choch import detect_swing_points
        swings = detect_swing_points(bars, n=1)
    except Exception:
        return None
    if not swings:
        return None
    newest_idx = len(bars) - 2
    fresh = [s for s in swings if s.idx == newest_idx]
    if not fresh:
        return None
    sp = fresh[0]
    key = (symbol, tf, int(sp.ts), sp.kind)
    if _seen_fractals.get(key):
        return None
    _seen_fractals[key] = True
    _prune_seen()
    return sp


def recompute_rolling_vp(symbol: str, tf: str, bars: list):
    """Fresh rolling VP over this TF's window — mirrors zone_triggers._rolling_hvn but
    keeps the WHOLE profile (poc/vah/val/lvn) instead of discarding all but hvn_zones.

    Returns (vp, used_bars, skip_reason). vp is None when there is not enough history;
    the caller logs skip_reason rather than emitting nothing, so a silent absence of
    rows is distinguishable from "no fractals fired".
    """
    from execution.zone_triggers import _VP_WIN
    win = _VP_WIN.get(tf, 96)
    if len(bars) < win:
        return None, 0, f"insufficient_bars:{len(bars)}<{win}"
    try:
        from pipeline.features.volume_profile import compute as vp_compute, _resolve_bin_size
        window = bars[-win:]
        vp = vp_compute(window, "daily", window[-1].ohlc.c,
                        bin_size=_resolve_bin_size(symbol))
        return vp, len(window), ""
    except Exception as e:
        return None, 0, f"vp_error:{type(e).__name__}"


def refreshed_session_zones(symbol: str, tf: str, bars: list):
    """The zone set the ARM path actually uses — zone_triggers._session_hvn_zones —
    recomputed now. Returns (zones_as_(lo,hi), session_name, skip_reason).

    Using a raw rolling VP here instead was the defect the first live rows exposed:
    _session_hvn_zones blends rolling + cached-daily depending on session, so a bare
    24h rolling profile produces a DIFFERENT zone set than the arm saw, and the
    resulting "delta" measured the mismatch rather than what the fractal changed.
    """
    try:
        from execution.zone_triggers import _session_hvn_zones
        zones, sess = _session_hvn_zones(symbol, tf, bars)
        return zones, sess, ("" if zones else "no_session_zones")
    except Exception as e:
        return [], "", f"zones_error:{type(e).__name__}"


def resolve_tps_generic(symbol: str, *, top_leg: float, bot_leg: float, atr: float,
                        tp_mult: float) -> tuple[float, float]:
    """Replicates grid_planner._resolve_tps — the path hvn_edge / squeeze / any
    non-inside-touch trigger uses: outer_leg +/- tp_mult*ATR, then snapped to the
    nearest zone_collector zone with strength >= 0.6 beyond that leg.

    FRAME CAVEAT (pre-existing in the planner, replicated faithfully so the study
    reproduces live behaviour rather than an idealised version): _all_zones reads
    vp_cache.get(), which returns VENUE-shifted prices, and compares them against
    ANALYSIS-frame legs. On a symbol with a nonzero venue offset the two sides of that
    comparison are in different frames.
    """
    buy_tp = top_leg + tp_mult * atr
    sell_tp = bot_leg - tp_mult * atr
    try:
        from execution.zone_collector import _all_zones
        zones = [z for z in _all_zones(symbol) if z.strength >= 0.6]
        above = [z.price for z in zones if z.price > top_leg]
        below = [z.price for z in zones if z.price < bot_leg]
        if above:
            buy_tp = min(above)
        if below:
            sell_tp = max(below)
    except Exception:
        pass
    return round(buy_tp, 4), round(sell_tp, 4)


def tp_cascade(vp, *, zones, edge: float, fulcrum: float, step: float, buy_n: int,
               sell_n: int, trigger_kind: str, edge_side: str,
               hvn_reversion_bias: bool, symbol: str = "", atr: float = 0.0,
               tp_mult: float = 2.0) -> dict:
    """The planner's TP resolution, run against freshly recomputed structure.

    Branches by trigger_kind, because the planner does. Getting this wrong was the
    defect the first live rows exposed: applying the inside-touch rule to hvn_edge /
    squeeze cycles produced 0.0 targets while the live cycles had real TPs.

      hvn_inside_touch -> node-edge rule (zone_triggers.py:304-307): tp_up = nearest
                          node-TOP above the tapped edge, tp_down = nearest node-BOTTOM
                          below it. Then the POC reversion override.
      everything else  -> grid_planner._resolve_tps: outer_leg +/- tp_mult*ATR snapped
                          to the nearest strong zone_collector zone beyond that leg.

    Both branches then take the beyond-outer-leg guard (grid_planner.py:577-590): a
    structural target is accepted only if it clears the outer leg, else it would sit
    inside the ladder and the grid could never profit. Absence of that guard is the
    regression recorded in project_tp_refresh_ladder_guard.

    Prices in/out are ANALYSIS frame; the caller converts for comparison.
    """
    # Outer legs: _build_legs places fulcrum +/- i*step, so each side's outer leg is
    # fulcrum +/- n*step for THAT side's count (skew makes the two differ).
    top_leg = fulcrum + max(1, int(buy_n or 0)) * step
    bot_leg = fulcrum - max(1, int(sell_n or 0)) * step

    poc_override = ""
    rule = ""

    if trigger_kind == "hvn_inside_touch":
        rule = "node_edge"
        tops_above = [hi for lo, hi in zones if hi > edge]
        bots_below = [lo for lo, hi in zones if lo < edge]
        tp_up = min(tops_above) if tops_above else 0.0
        tp_down = max(bots_below) if bots_below else 0.0
        if not (tp_up > top_leg):
            tp_up = 0.0
        if not (0.0 < tp_down < bot_leg):
            tp_down = 0.0
        poc = float(getattr(vp, "poc", 0.0) or 0.0)
        if hvn_reversion_bias and poc > 0:
            if edge_side == "top" and poc < bot_leg:
                tp_down = round(poc, 4)
                poc_override = "sell"
            elif edge_side == "bottom" and poc > top_leg:
                tp_up = round(poc, 4)
                poc_override = "buy"
    else:
        rule = "resolve_tps"
        tp_up, tp_down = resolve_tps_generic(
            symbol, top_leg=top_leg, bot_leg=bot_leg, atr=atr, tp_mult=tp_mult)
        # _resolve_tps already measures from the outer leg, so its ATR fallback always
        # clears the ladder; the snap can only move the target further out. Guard kept
        # as a belt-and-braces assertion rather than a filter.
        if not (tp_up > top_leg):
            tp_up = 0.0
        if not (0.0 < tp_down < bot_leg):
            tp_down = 0.0

    return {
        "tp_up": round(tp_up, 4), "tp_down": round(tp_down, 4),
        "poc_override_side": poc_override, "rule": rule,
        "top_leg": round(top_leg, 4), "bot_leg": round(bot_leg, 4),
        "n_hvn": len(zones),
        "n_lvn": len(getattr(vp, "lvn_zones", None) or []),
    }


def build_row(*, cycle_id: str, magic: int, tf: str, cyc: dict, symbol: str,
              bars: list, sp, venue_mid: float, hvn_reversion_bias: bool,
              tp_mult: float = 2.0) -> dict:
    """One study row: refreshed-structure cascade result beside the cycle's live TPs."""
    vp, vp_bars, skip = recompute_rolling_vp(symbol, tf, bars)
    zones, sess, zskip = refreshed_session_zones(symbol, tf, bars)
    try:
        from pipeline.features.atr import atr_from_store
        atr = float(atr_from_store(symbol, tf) or 0.0)
    except Exception:
        atr = 0.0

    fulcrum_v = float(cyc.get("fulcrum") or 0.0)    # VENUE frame — set_last_arm stores
                                                    # plan.fulcrum AFTER _rebase_to_venue
    step = float(cyc.get("step") or 0.0)            # a WIDTH — frame-invariant
    tp_up_live = float(cyc.get("tp_up") or 0.0)     # VENUE frame
    tp_down_live = float(cyc.get("tp_down") or 0.0)

    # analysis<->venue basis. The arm records both anchors; on this branch the rebase is
    # ADDITIVE (commit b93af34), matching vp_cache._shift_vp, so one offset converts.
    a_anchor = float(cyc.get("analysis_anchor") or 0.0)
    v_anchor = float(cyc.get("venue_anchor") or 0.0)
    shift = (v_anchor - a_anchor) if (a_anchor > 0 and v_anchor > 0) else 0.0

    # The cascade runs against a freshly computed VP, which is ANALYSIS frame — so the
    # fulcrum must be converted BACK out of the venue frame before it is used as the
    # edge. Comparing a venue-frame fulcrum to analysis-frame zones would offset every
    # target by the basis (~3pt on gold) and silently corrupt the study.
    fulcrum = round(fulcrum_v - shift, 4) if fulcrum_v else 0.0

    row: dict = {
        "cycle_id": cycle_id, "magic": int(magic), "tf": tf, "symbol": symbol,
        "trigger_kind": cyc.get("trigger_kind", ""), "edge": cyc.get("edge", ""),
        "fulcrum_venue": fulcrum_v, "fulcrum_analysis": fulcrum, "step": step,
        "buy_n": cyc.get("buy_n"), "sell_n": cyc.get("sell_n"),
        "bar_close_ts": int(bars[-1].close_ts) if bars else None,
        "fractal_ts": int(sp.ts), "fractal_kind": sp.kind,
        "fractal_price": round(float(sp.price), 4),
        "vp_bars": vp_bars, "session": sess, "atr": round(atr, 4),
        "n_session_zones": len(zones),
        "tp_up_live_venue": tp_up_live, "tp_down_live_venue": tp_down_live,
        "venue_mid": round(float(venue_mid or 0.0), 4),
        "analysis_anchor": a_anchor, "venue_anchor": v_anchor, "shift": round(shift, 4),
        "frame_new": "analysis", "frame_live": "venue",
    }
    if vp is None:
        row["skip_reason"] = skip
        return row
    if zskip:
        row["zones_skip"] = zskip

    res = tp_cascade(
        vp, zones=zones, edge=fulcrum, fulcrum=fulcrum, step=step,
        buy_n=int(cyc.get("buy_n") or 0), sell_n=int(cyc.get("sell_n") or 0),
        trigger_kind=str(cyc.get("trigger_kind") or ""),
        edge_side=str(cyc.get("edge") or ""),
        hvn_reversion_bias=hvn_reversion_bias,
        symbol=symbol, atr=atr, tp_mult=tp_mult,
    )
    row["rule"] = res["rule"]
    row.update({
        "vp_poc": round(float(getattr(vp, "poc", 0.0) or 0.0), 4),
        "vp_vah": round(float(getattr(vp, "vah", 0.0) or 0.0), 4),
        "vp_val": round(float(getattr(vp, "val", 0.0) or 0.0), 4),
        "n_hvn": res["n_hvn"], "n_lvn": res["n_lvn"],
        "top_leg": res["top_leg"], "bot_leg": res["bot_leg"],
        "tp_up_new": res["tp_up"], "tp_down_new": res["tp_down"],
        "poc_override_side": res["poc_override_side"],
    })
    # venue-frame projection of the new TPs — the only apples-to-apples comparison
    tp_up_new_v = round(res["tp_up"] + shift, 4) if res["tp_up"] else 0.0
    tp_down_new_v = round(res["tp_down"] + shift, 4) if res["tp_down"] else 0.0
    row["tp_up_new_venue"] = tp_up_new_v
    row["tp_down_new_venue"] = tp_down_new_v
    row["delta_up"] = round(tp_up_new_v - tp_up_live, 4) if (tp_up_new_v and tp_up_live) else None
    row["delta_down"] = round(tp_down_new_v - tp_down_live, 4) if (tp_down_new_v and tp_down_live) else None
    return row


def should_run(symbol: str, magic: int, bar_close_ts: int) -> bool:
    """True once per closed bar per cycle. monitor_cycle runs at ~1Hz (InpPollMs=1000),
    so without this the study would recompute a full VP every second."""
    key = (symbol, int(magic))
    if _last_bar.get(key) == int(bar_close_ts):
        return False
    _last_bar[key] = int(bar_close_ts)
    return True
