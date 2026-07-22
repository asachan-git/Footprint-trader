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


def tp_cascade(vp, *, edge: float, fulcrum: float, step: float, buy_n: int,
               sell_n: int, trigger_kind: str, edge_side: str,
               hvn_reversion_bias: bool) -> dict:
    """The planner's 3-stage TP cascade, run against a freshly computed VP.

    Replicates, in order:
      1. HVN->HVN          zone_triggers.py:304-307 — tp_up = nearest node-TOP above the
                           tapped edge, tp_down = nearest node-BOTTOM below it.
      2. outer-leg guard   grid_planner.py:577-590 — a structural target is accepted only
                           if it lies BEYOND the outer leg, else it would sit inside the
                           ladder and the grid could never profit. This is the guard whose
                           absence caused the TP-refresh regression documented in
                           project_tp_refresh_ladder_guard.
      3. POC reversion     grid_planner.py:596-607 — on hvn_inside_touch with
                           hvn_reversion_bias, the FADE side retargets POC (tapped top ->
                           sell_tp = poc, only if poc clears the inner leg).

    All prices in/out are ANALYSIS frame — the caller converts for comparison.
    """
    zones = [(float(z["low"]), float(z["high"])) for z in (getattr(vp, "hvn_zones", None) or [])]

    # 1) HVN -> HVN
    tops_above = [hi for lo, hi in zones if hi > edge]
    bots_below = [lo for lo, hi in zones if lo < edge]
    tp_up = min(tops_above) if tops_above else 0.0
    tp_down = max(bots_below) if bots_below else 0.0

    # 2) beyond-outer-leg guard. Legs are fulcrum +/- i*step (grid_planner._build_legs),
    #    so the outer leg on each side is fulcrum +/- n*step for that side's leg count.
    top_leg = fulcrum + max(1, int(buy_n or 0)) * step
    bot_leg = fulcrum - max(1, int(sell_n or 0)) * step
    if not (tp_up > top_leg):
        tp_up = 0.0
    if not (0.0 < tp_down < bot_leg):
        tp_down = 0.0

    # 3) POC reversion on the fade side
    poc_override = ""
    poc = float(getattr(vp, "poc", 0.0) or 0.0)
    if trigger_kind == "hvn_inside_touch" and hvn_reversion_bias and poc > 0:
        if edge_side == "top" and poc < bot_leg:
            tp_down = round(poc, 4)
            poc_override = "sell"
        elif edge_side == "bottom" and poc > top_leg:
            tp_up = round(poc, 4)
            poc_override = "buy"

    return {
        "tp_up": round(tp_up, 4), "tp_down": round(tp_down, 4),
        "poc_override_side": poc_override,
        "top_leg": round(top_leg, 4), "bot_leg": round(bot_leg, 4),
        "n_hvn": len(zones),
        "n_lvn": len(getattr(vp, "lvn_zones", None) or []),
    }


def build_row(*, cycle_id: str, magic: int, tf: str, cyc: dict, symbol: str,
              bars: list, sp, venue_mid: float, hvn_reversion_bias: bool) -> dict:
    """One study row: fresh-VP cascade result beside the cycle's live (armed) TPs."""
    vp, vp_bars, skip = recompute_rolling_vp(symbol, tf, bars)

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
        "vp_bars": vp_bars,
        "tp_up_live_venue": tp_up_live, "tp_down_live_venue": tp_down_live,
        "venue_mid": round(float(venue_mid or 0.0), 4),
        "analysis_anchor": a_anchor, "venue_anchor": v_anchor, "shift": round(shift, 4),
        "frame_new": "analysis", "frame_live": "venue",
    }
    if vp is None:
        row["skip_reason"] = skip
        return row

    res = tp_cascade(
        vp, edge=fulcrum, fulcrum=fulcrum, step=step,
        buy_n=int(cyc.get("buy_n") or 0), sell_n=int(cyc.get("sell_n") or 0),
        trigger_kind=str(cyc.get("trigger_kind") or ""),
        edge_side=str(cyc.get("edge") or ""),
        hvn_reversion_bias=hvn_reversion_bias,
    )
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
