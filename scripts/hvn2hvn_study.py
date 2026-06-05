#!/usr/bin/env python3
"""HVN→HVN Study — capture moves between adjacent HVNs using CVD direction
confirmation, CVD divergence, and LVN as a navigational marker (NOT support).

Trade model per memory (project_continuation_cvddiv): HVN edges = support;
LVN = vacuum (path of least resistance, not a bounce zone). Stop anchored at
ORIGIN HVN far edge. Target = TARGET HVN far edge (next HVN in direction).

Entry variants:
  A  (sweep)         wick pierces ORIGIN HVN edge in OPPOSITE direction +
                     close reclaims; trade fades the sweep INTO the next HVN
                     on the other side of the current HVN.
  B  (continuation)  bar CLOSES beyond ORIGIN HVN edge in TREND direction +
                     HIGH favorable Δ + CVD direction agrees (delta sign
                     matches break direction). Trade with the break to the
                     next HVN.

Exit logic per setup:
  SL    = ORIGIN HVN far edge ± SL_BUFFER × ATR
  T1    = TARGET HVN near edge  (first contact; for early-scale analysis)
  T2    = TARGET HVN far edge   (full traversal)
  EARLY = at any forward bar where (a) a confirmed-pivot CVD div opposing the
          trade direction lands on that bar AND (b) price is at/inside the
          target HVN, exit at that bar's close.
  TIME  = end of forward window with no resolution.

Output:
  data/reports/hvn2hvn_<SYM>_<TF>.jsonl   one row per setup
  data/reports/hvn2hvn_study.md           sectioned aggregate report

Usage:
  .venv/bin/python scripts/hvn2hvn_study.py
  .venv/bin/python scripts/hvn2hvn_study.py --symbols BTCUSDT --tf 15m
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.disable(logging.CRITICAL)

from pipeline.state_store import _deserialize
from pipeline.footprint import build as build_fp
from pipeline.features.atr import atr
from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE
from pipeline.features.cvd_candlestick import scan_divergences

FP_DIR = ROOT / "data" / "footprint"
REPORT_OUT = ROOT / "data" / "reports" / "hvn2hvn_study.md"

SYMBOLS = ["BTCUSDT", "XAUTUSDT"]
TFS = ["15m", "5m"]

CFG = {
    "15m": {"vp_win": 96,  "fwd_n": 24, "div_lookback": 3},
    "5m":  {"vp_win": 288, "fwd_n": 48, "div_lookback": 4},
}

ATR_PERIOD = 14
SL_BUFFER_ATR = 0.5      # SL = ORIGIN HVN far edge − SL_BUFFER_ATR × ATR (long; mirror for short)
SL_WICK_BUFFER = 0.1     # if a sweep, also keep ≥ SL_WICK_BUFFER × ATR beyond the wick
SWEEP_PEN_FRAC = 0.05    # entry A: wick pierce ≥ 5% of bar range
RECLAIM_FRAC = 0.20      # entry A: close reclaims ≥ 20% of penetration
DELTA_HIGH = 0.35        # entry B: favorable delta ratio ≥ this
DELTA_WEAK = 0.15        # tier label only
TARGET_INSIDE_FRAC = 0.0 # CVD-div EARLY exit fires when price ≥ target HVN near edge
SESS_MIN_BARS = 30       # min bars to compute a session's completed VP
RR_MAX = 8.0             # skip setups whose reward_T2 / risk > RR_MAX (target unreachable in fwd window)
RR_MIN = 0.8             # skip stops too tight to be useful


# ── prior-completed-session VP levels ─────────────────────────────────────────
# Level source = the session-anchored daily VP of the PRIOR completed session
# (XAU 03:30 IST / BTC 05:30 IST). Yesterday's HVN/LVN become today's levels —
# established at the open, no lookahead. (A trailing window starved the sample;
# the developing-session VP too — prior-session is the tradeable interpretation.)
def _session_start_sec(sym): return 12600 if sym.startswith("XAU") else 19800
def _session_key(ts, sym):   return (ts + 19800 - _session_start_sec(sym)) // 86400


def _prior_session_zones(bars, symbol):
    """Per-bar-index → (hvn_zones, lvn_zones) of the most recent COMPLETED session
    before the bar's own session. None until a prior session exists."""
    bin_size = DEFAULT_BIN_SIZE.get(symbol)
    groups: dict[int, list[int]] = {}
    for idx, b in enumerate(bars):
        groups.setdefault(_session_key(b.close_ts, symbol), []).append(idx)
    zby: dict[int, tuple | None] = {}
    for k in sorted(groups):
        seg = [bars[j] for j in groups[k]]
        if len(seg) < SESS_MIN_BARS:
            zby[k] = None
            continue
        try:
            vp = vp_compute(seg, "daily", seg[-1].ohlc.c, bin_size=bin_size)
            zby[k] = (vp.hvn_zones or [], vp.lvn_zones or [])
        except Exception:
            zby[k] = None
    prior: dict[int, tuple | None] = {}
    last = None
    for k in sorted(groups):
        for j in groups[k]:
            prior[j] = last
        if zby.get(k) is not None:
            last = zby[k]
    return prior


# ── data ─────────────────────────────────────────────────────────────────────
def load_bars(symbol: str, tf: str) -> list:
    seen: dict[int, object] = {}
    for f in sorted(FP_DIR.glob(f"{symbol}_{tf}*.jsonl")):
        if ".bak" in f.name:
            continue
        for line in f.open():
            if not line.strip():
                continue
            try:
                b = _deserialize(line)
            except Exception:
                continue
            if b.symbol == symbol and b.tf == tf:
                seen[b.close_ts] = b
    return [seen[k] for k in sorted(seen)]


def _tot_vol(bar) -> float:
    fp = build_fp(bar)
    return fp.total_bid + fp.total_ask


def _ist(ts: int) -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.fromtimestamp(
        ts, tz=timezone(timedelta(hours=5, minutes=30))
    ).strftime("%Y-%m-%d %H:%M IST")


def _delta_tier(bar, side: str) -> tuple[str, float]:
    fp = build_fp(bar)
    total = fp.total_bid + fp.total_ask
    if total <= 0:
        return "none", 0.0
    d = bar.delta or 0.0
    ratio = d / total
    favorable = ratio if side == "long" else -ratio
    if favorable >= DELTA_HIGH:
        return "high", favorable
    if favorable >= DELTA_WEAK:
        return "weak", favorable
    return "none", favorable


# ── entry A: sweep ────────────────────────────────────────────────────────────
def _sweep_candidates(bar, hvn_zones: list[dict], atr_val: float) -> list[dict]:
    """Same logic as cvd_sweep_study, but produces ORIGIN-HVN + side for the
    HVN→HVN model: sweep LOW = long (target HVN above); sweep HIGH = short."""
    if not hvn_zones or atr_val <= 0:
        return []
    h, l, c = bar.ohlc.h, bar.ohlc.l, bar.ohlc.c
    rng = max(h - l, 1e-9)
    out: list[dict] = []
    for z in hvn_zones:
        if z["high"] - z["low"] <= 0:
            continue
        pen = z["low"] - l
        if pen > 0 and pen >= SWEEP_PEN_FRAC * rng and c >= z["low"] - RECLAIM_FRAC * pen:
            out.append({"side": "long", "entry_kind": "sweep", "origin_hvn": z,
                        "sweep_price": l, "penetration_atr": pen / atr_val})
        pen = h - z["high"]
        if pen > 0 and pen >= SWEEP_PEN_FRAC * rng and c <= z["high"] + RECLAIM_FRAC * pen:
            out.append({"side": "short", "entry_kind": "sweep", "origin_hvn": z,
                        "sweep_price": h, "penetration_atr": pen / atr_val})
    return out


# ── entry B: continuation ─────────────────────────────────────────────────────
def _continuation_candidates(bar, prev_bar, hvn_zones: list[dict]) -> list[dict]:
    """Bar closes BEYOND an HVN edge in the trend direction.
    Long  break = prev close inside HVN AND current close > HVN.high.
    Short break = prev close inside HVN AND current close < HVN.low.
    Requires HIGH favorable Δ AND CVD direction (bar delta sign) agrees."""
    if not hvn_zones:
        return []
    out: list[dict] = []
    pc, cc = prev_bar.ohlc.c, bar.ohlc.c
    delta = bar.delta or 0.0
    for z in hvn_zones:
        prev_inside = z["low"] <= pc <= z["high"]
        if not prev_inside:
            continue
        # break up — long continuation
        if cc > z["high"]:
            tier, fav = _delta_tier(bar, "long")
            if tier == "high" and delta > 0:
                out.append({"side": "long", "entry_kind": "continuation",
                            "origin_hvn": z, "sweep_price": None,
                            "penetration_atr": (cc - z["high"]) / (z["high"] - z["low"] + 1e-9),
                            "tier_at_entry": tier, "fav_at_entry": fav})
        # break down — short continuation
        if cc < z["low"]:
            tier, fav = _delta_tier(bar, "short")
            if tier == "high" and delta < 0:
                out.append({"side": "short", "entry_kind": "continuation",
                            "origin_hvn": z, "sweep_price": None,
                            "penetration_atr": (z["low"] - cc) / (z["high"] - z["low"] + 1e-9),
                            "tier_at_entry": tier, "fav_at_entry": fav})
    return out


# ── target selection ─────────────────────────────────────────────────────────
def _target_hvn(side: str, origin: dict, all_hvns: list[dict]) -> dict | None:
    """Next HVN in setup direction (above origin for long; below for short)."""
    if side == "long":
        outer = [z for z in all_hvns if z["low"] > origin["high"]]
        return min(outer, key=lambda z: z["low"]) if outer else None
    outer = [z for z in all_hvns if z["high"] < origin["low"]]
    return max(outer, key=lambda z: z["high"]) if outer else None


def _intermediate_lvn(side: str, origin: dict, target: dict,
                      all_lvns: list[dict]) -> dict | None:
    """LVN between origin and target HVN (navigational marker, not stop)."""
    if not target:
        return None
    if side == "long":
        between = [z for z in all_lvns
                   if z["low"] > origin["high"] and z["high"] <= target["low"]]
        return max(between, key=lambda z: z["high"]) if between else None
    between = [z for z in all_lvns
               if z["high"] < origin["low"] and z["low"] >= target["high"]]
    return min(between, key=lambda z: z["low"]) if between else None


# ── forward walk ─────────────────────────────────────────────────────────────
def _forward_outcome(bars, i, entry, side, sl, t1, t2,
                     target_hvn, atr_val, fwd_n, div_by_ts) -> dict:
    """Walk forward fwd_n bars. Priority: SL > CVD-div-EARLY-exit > T2 > T1.
    EARLY: confirmed-pivot CVD div opposing trade fires while price ≥ t1 (in
    target HVN). Realized R uses outcome priority."""
    long = side == "long"
    risk = abs(entry - sl) or 1e-9
    mfe = mae = 0.0
    sl_hit = early = False
    hit_t1 = hit_t2 = False
    bars_to_t1 = bars_to_t2 = None
    early_price = None
    early_bar = None
    fwd = bars[i + 1:i + 1 + fwd_n]
    want_div = "bear" if long else "bull"

    for j, b in enumerate(fwd):
        if long:
            mfe = max(mfe, b.ohlc.h - entry)
            mae = max(mae, entry - b.ohlc.l)
            if b.ohlc.l <= sl:
                sl_hit = True
                break
            if t1 is not None and b.ohlc.h >= t1 and not hit_t1:
                hit_t1 = True
                bars_to_t1 = j + 1
            if t2 is not None and b.ohlc.h >= t2 and not hit_t2:
                hit_t2 = True
                bars_to_t2 = j + 1
        else:
            mfe = max(mfe, entry - b.ohlc.l)
            mae = max(mae, b.ohlc.h - entry)
            if b.ohlc.h >= sl:
                sl_hit = True
                break
            if t1 is not None and b.ohlc.l <= t1 and not hit_t1:
                hit_t1 = True
                bars_to_t1 = j + 1
            if t2 is not None and b.ohlc.l <= t2 and not hit_t2:
                hit_t2 = True
                bars_to_t2 = j + 1

        # CVD-div EARLY exit — only fire AFTER price has touched target HVN
        divs = div_by_ts.get(b.close_ts, [])
        if hit_t1 and not hit_t2 and any(d["type"] == want_div for d in divs):
            early = True
            early_price = b.ohlc.c
            early_bar = j + 1
            break

    outcome = ("SL"   if sl_hit else
               "T2"   if hit_t2 else
               "EARLY" if early else
               "T1"   if hit_t1 else
               "TIME")

    # realized R per outcome priority
    if sl_hit:
        realized_r = -1.0
        exit_price = sl
    elif hit_t2:
        realized_r = abs(t2 - entry) / risk
        exit_price = t2
    elif early:
        realized_r = (early_price - entry) / risk if long else (entry - early_price) / risk
        exit_price = early_price
    elif hit_t1:
        realized_r = abs(t1 - entry) / risk
        exit_price = t1
    else:
        realized_r = 0.0
        exit_price = bars[i + len(fwd)].ohlc.c if fwd else entry

    return {
        "sl_hit": sl_hit, "hit_t1": hit_t1, "hit_t2": hit_t2,
        "early_div_exit": early, "early_price": early_price, "early_bar": early_bar,
        "bars_to_t1": bars_to_t1, "bars_to_t2": bars_to_t2,
        "mfe_atr": round(mfe / atr_val, 2) if atr_val > 0 else None,
        "mae_atr": round(mae / atr_val, 2) if atr_val > 0 else None,
        "mfe_r": round(mfe / risk, 2),
        "mae_r": round(mae / risk, 2),
        "outcome": outcome,
        "realized_r": round(realized_r, 3),
        "exit_price": round(exit_price, 4) if exit_price is not None else None,
    }


# ── scan ─────────────────────────────────────────────────────────────────────
def scan(symbol: str, tf: str) -> list[dict]:
    cfg = CFG[tf]
    bars = load_bars(symbol, tf)
    if len(bars) < cfg["vp_win"] + cfg["fwd_n"] + 5:
        return []

    bin_size = DEFAULT_BIN_SIZE.get(symbol)
    div_by_ts: dict[int, list[dict]] = {}
    for d in scan_divergences(bars, lookback=cfg["div_lookback"]):
        div_by_ts.setdefault(d["ts"], []).append(d)

    # running CVD for the live-div check at entry
    cum = [0.0] * len(bars)
    run = 0.0
    for k, b in enumerate(bars):
        run += b.delta or 0.0
        cum[k] = run

    prior_zones = _prior_session_zones(bars, symbol)

    rows: list[dict] = []
    for i in range(cfg["vp_win"], len(bars) - cfg["fwd_n"]):
        b = bars[i]
        prev_b = bars[i - 1]
        atr_val = atr(bars[max(0, i - 50):i + 1], ATR_PERIOD) or 0.0
        if atr_val <= 0:
            continue
        zones = prior_zones.get(i)
        if not zones:
            continue
        hvns, lvns = zones
        if not hvns:
            continue

        cands = (_sweep_candidates(b, hvns, atr_val)
                 + _continuation_candidates(b, prev_b, hvns))

        for c in cands:
            origin = c["origin_hvn"]
            target = _target_hvn(c["side"], origin, hvns)
            if not target:
                continue

            # SL = wider of (origin HVN far edge − buffer) AND (sweep wick − buffer).
            # For sweep entries the wick can dip way below the HVN edge — using the
            # HVN edge alone gets stopped on the very next pullback that retests
            # the wick. Take the wider stop in both directions.
            entry = b.ohlc.c
            sweep_px = c.get("sweep_price")
            if c["side"] == "long":
                sl_hvn  = origin["low"] - SL_BUFFER_ATR * atr_val
                sl_wick = (sweep_px - SL_WICK_BUFFER * atr_val) if sweep_px is not None else sl_hvn
                sl = min(sl_hvn, sl_wick)       # wider = lower for long
                t1 = target["low"]
                t2 = target["high"]
            else:
                sl_hvn  = origin["high"] + SL_BUFFER_ATR * atr_val
                sl_wick = (sweep_px + SL_WICK_BUFFER * atr_val) if sweep_px is not None else sl_hvn
                sl = max(sl_hvn, sl_wick)       # wider = higher for short
                t1 = target["high"]
                t2 = target["low"]

            risk = abs(entry - sl)
            if risk <= 0:
                continue
            reward_t2 = abs(t2 - entry)
            rr = reward_t2 / risk if risk > 0 else 0.0
            # Reject setups with absurd RR (target so far it can't be reached in fwd
            # window) and stops so tight risk_atr is negligible.
            if rr > RR_MAX or rr < RR_MIN:
                continue
            if risk / atr_val < 0.15:           # less than 0.15 ATR of room = noise
                continue

            # delta tier on the trigger bar (sweep variant computes here)
            tier, fav = _delta_tier(b, c["side"])
            cvd_div_match = [d for d in div_by_ts.get(b.close_ts, [])
                             if d["type"] == ("bull" if c["side"] == "long" else "bear")]
            cvd_div_entry = bool(cvd_div_match)
            div_strength = max((d["strength"] for d in cvd_div_match), default=0.0)

            mid_lvn = _intermediate_lvn(c["side"], origin, target, lvns)

            out = _forward_outcome(bars, i, entry, c["side"], sl, t1, t2,
                                   target, atr_val, cfg["fwd_n"], div_by_ts)

            rows.append({
                "symbol": symbol, "tf": tf, "ts": b.close_ts, "ist": _ist(b.close_ts),
                "side": c["side"], "entry_kind": c["entry_kind"],
                "origin_low": round(origin["low"], 4),
                "origin_high": round(origin["high"], 4),
                "target_low": round(target["low"], 4),
                "target_high": round(target["high"], 4),
                "mid_lvn_low": round(mid_lvn["low"], 4) if mid_lvn else None,
                "mid_lvn_high": round(mid_lvn["high"], 4) if mid_lvn else None,
                "sweep_price": round(c["sweep_price"], 4) if c.get("sweep_price") else None,
                "penetration_atr": round(c.get("penetration_atr", 0.0), 3),
                "entry": round(entry, 4),
                "sl": round(sl, 4), "t1": round(t1, 4), "t2": round(t2, 4),
                "atr": round(atr_val, 4),
                "rr": round(rr, 2),
                "delta_tier": tier,
                "favorable_delta_ratio": round(fav, 3),
                "cvd_div_at_entry": cvd_div_entry,
                "div_strength": round(div_strength, 3),
                **out,
            })
    return rows


# ── report ───────────────────────────────────────────────────────────────────
def _agg(rows: list[dict]) -> str:
    if not rows:
        return "n=   0"
    n = len(rows)
    sl = sum(1 for r in rows if r["outcome"] == "SL")
    t1 = sum(1 for r in rows if r["hit_t1"])
    t2 = sum(1 for r in rows if r["hit_t2"])
    eo = sum(1 for r in rows if r["outcome"] == "EARLY")
    ti = sum(1 for r in rows if r["outcome"] == "TIME")
    wins = sum(1 for r in rows if r["realized_r"] > 0)
    losses = sum(1 for r in rows if r["realized_r"] < 0)
    gross_win = sum(r["realized_r"] for r in rows if r["realized_r"] > 0)
    gross_loss = abs(sum(r["realized_r"] for r in rows if r["realized_r"] < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    avgR = statistics.mean(r["realized_r"] for r in rows)
    avgRR = statistics.mean(r["rr"] for r in rows)
    return (f"n={n:>4d}  T1={100*t1/n:>3.0f}%  T2={100*t2/n:>3.0f}%  "
            f"early={100*eo/n:>3.0f}%  SL={100*sl/n:>3.0f}%  time={100*ti/n:>3.0f}%  "
            f"win={100*wins/n:>3.0f}%  PF={pf:>4.2f}  avgR={avgR:>+5.2f}  avgRR={avgRR:>4.2f}")


def _section(out: list[str], title: str, rows: list[dict], splits: list[tuple]):
    out.append(f"\n### {title}")
    out.append(f"- ALL — {_agg(rows)}")
    for label, pred in splits:
        sub = [r for r in rows if pred(r)]
        out.append(f"- {label:38s} — {_agg(sub)}")


def build_report(allrows: list[dict]) -> str:
    out: list[str] = [
        "# HVN→HVN Study — capture next-HVN moves; HVN edges support, LVN is path",
        "",
        "**Thesis** — trade from ORIGIN HVN to TARGET HVN. Entry via sweep (A)",
        "or continuation break (B). Stop at ORIGIN HVN far edge (HVN = support).",
        "Target = TARGET HVN far edge. Early exit on opposing CVD div AFTER",
        "price has touched the target HVN.",
        "",
        f"SL  = max(origin HVN far edge − {SL_BUFFER_ATR:.2f}×ATR, sweep wick − {SL_WICK_BUFFER:.2f}×ATR) — wider of the two",
        "T1  = target HVN near edge (first contact, scale-out candidate)",
        "T2  = target HVN far edge  (full traversal)",
        "",
        f"Entry A (sweep)        : pen ≥ {SWEEP_PEN_FRAC*100:.0f}% of bar range, reclaim ≥ {RECLAIM_FRAC*100:.0f}% of pen",
        f"Entry B (continuation) : close beyond HVN edge + favorable Δ ≥ {DELTA_HIGH} + CVD agrees",
        "EARLY                  : opposing confirmed-pivot CVD div after price reached T1",
        "Realized R per outcome : SL=-1; T2=+|t2-entry|/risk; EARLY=close/risk; T1=+|t1-entry|/risk; TIME=0",
        "",
        f"Total setups logged: **{len(allrows)}**",
        "",
        "## Overview",
        "",
        "| symbol | tf | sweep | continuation | total |",
        "|---|---|---|---|---|",
    ]
    syms = sorted({r["symbol"] for r in allrows})
    tfs = sorted({r["tf"] for r in allrows})
    for s in syms:
        for tf in tfs:
            sw = sum(1 for r in allrows if r["symbol"] == s and r["tf"] == tf and r["entry_kind"] == "sweep")
            ct = sum(1 for r in allrows if r["symbol"] == s and r["tf"] == tf and r["entry_kind"] == "continuation")
            if sw or ct:
                out.append(f"| {s} | {tf} | {sw} | {ct} | {sw+ct} |")

    for s in syms:
        for tf in tfs:
            rows = [r for r in allrows if r["symbol"] == s and r["tf"] == tf]
            if not rows:
                continue
            out.append(f"\n## {s} {tf}")
            _section(out, "by entry variant", rows, [
                ("A sweep",        lambda r: r["entry_kind"] == "sweep"),
                ("B continuation", lambda r: r["entry_kind"] == "continuation"),
            ])
            _section(out, "by side", rows, [
                ("LONG",  lambda r: r["side"] == "long"),
                ("SHORT", lambda r: r["side"] == "short"),
            ])
            _section(out, "by delta tier on trigger bar", rows, [
                ("HIGH Δ", lambda r: r["delta_tier"] == "high"),
                ("WEAK Δ", lambda r: r["delta_tier"] == "weak"),
                ("NONE Δ", lambda r: r["delta_tier"] == "none"),
            ])
            _section(out, "sweep × delta-tier", rows, [
                ("A + HIGH Δ", lambda r: r["entry_kind"] == "sweep" and r["delta_tier"] == "high"),
                ("A + WEAK Δ", lambda r: r["entry_kind"] == "sweep" and r["delta_tier"] == "weak"),
                ("A + NONE Δ", lambda r: r["entry_kind"] == "sweep" and r["delta_tier"] == "none"),
            ])
            _section(out, "entry vs CVD div confluence", rows, [
                ("CVD div at entry",         lambda r: r["cvd_div_at_entry"]),
                ("A sweep + CVD div",        lambda r: r["entry_kind"] == "sweep" and r["cvd_div_at_entry"]),
                ("B continuation + CVD div", lambda r: r["entry_kind"] == "continuation" and r["cvd_div_at_entry"]),
                ("no CVD div",               lambda r: not r["cvd_div_at_entry"]),
            ])
            _section(out, "by RR class (reward_T2 / risk)", rows, [
                ("RR ≥ 3.0", lambda r: r["rr"] >= 3.0),
                ("RR 2-3",   lambda r: 2.0 <= r["rr"] < 3.0),
                ("RR 1-2",   lambda r: 1.0 <= r["rr"] < 2.0),
                ("RR < 1",   lambda r: r["rr"] < 1.0),
            ])
            _section(out, "by outcome", rows, [
                ("T2 full HVN→HVN", lambda r: r["outcome"] == "T2"),
                ("EARLY (CVD-div exit at target)", lambda r: r["outcome"] == "EARLY"),
                ("T1 only (touch + reverse)", lambda r: r["outcome"] == "T1"),
                ("SL", lambda r: r["outcome"] == "SL"),
                ("TIME", lambda r: r["outcome"] == "TIME"),
            ])

            # notable examples
            best = sorted(rows, key=lambda r: r["realized_r"], reverse=True)[:8]
            if best:
                out.append("\n**Top realized-R setups:**")
                for r in best:
                    out.append(
                        f"- {r['ist']} {r['side'].upper():5s} {r['entry_kind']:12s} "
                        f"orig[{r['origin_low']:.2f}-{r['origin_high']:.2f}] → "
                        f"tgt[{r['target_low']:.2f}-{r['target_high']:.2f}] "
                        f"Δ={r['delta_tier']:4s} div={'Y' if r['cvd_div_at_entry'] else 'N'} "
                        f"RR={r['rr']:.1f} → {r['outcome']:5s} "
                        f"R={r['realized_r']:+.2f} MFE={r['mfe_r']:.2f}"
                    )
    return "\n".join(out) + "\n"


# ── main ─────────────────────────────────────────────────────────────────────
def main(argv: list[str]):
    symbols = SYMBOLS
    tfs = TFS
    if "--symbols" in argv:
        symbols = argv[argv.index("--symbols") + 1].split(",")
    if "--tf" in argv:
        tfs = argv[argv.index("--tf") + 1].split(",")

    allrows: list[dict] = []
    for s in symbols:
        for tf in tfs:
            rows = scan(s, tf)
            allrows.extend(rows)
            sw = sum(1 for r in rows if r["entry_kind"] == "sweep")
            ct = sum(1 for r in rows if r["entry_kind"] == "continuation")
            print(f"  {s} {tf}: {len(rows)} setups (sweep={sw}, cont={ct})")
            out = ROOT / "data" / "reports" / f"hvn2hvn_{s}_{tf}.jsonl"
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("w") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")

    REPORT_OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT_OUT.write_text(build_report(allrows))
    print(f"\nlogged {len(allrows)} setups → data/reports/hvn2hvn_*.jsonl")
    print(f"report → {REPORT_OUT}")


if __name__ == "__main__":
    main(sys.argv[1:])
