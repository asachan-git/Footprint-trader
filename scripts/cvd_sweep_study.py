#!/usr/bin/env python3
"""CVD Sweep Study — HVN-extreme sweeps + CVD divergence + delta-tier outcome.

Tests the hypothesis: at HVN extremes, a wick-sweep beyond the extreme with
intact CVD divergence (price makes a new extreme but CVD does not) is a high-RR
reversal trigger. Outcome scales with the delta-confirmation tier on the sweep
candle:

  - HIGH favorable delta → full continuation toward NEXT HVN extreme (T2)
  - WEAK / no delta confirm → partial move (opposite HVN edge T1 or LVN extreme
    T3); CVD divergence often remains intact and the setup can re-fire

Pipeline per (symbol, tf):
  1. LOAD footprint bars; compute running CVD; pre-compute swing-pivot CVD
     divergences (cvd_candlestick.scan_divergences).
  2. PER BAR  compute daily-window VP, extract hvn_zones / lvn_zones.
  3. SWEEP    bar's wick must penetrate an HVN edge by >= SWEEP_PEN_FRAC of bar
              range AND close back inside the HVN by >= RECLAIM_FRAC of the
              penetration (i.e. reject the sweep).
  4. CVD-DIV  intact iff (a) a confirmed swing-pivot divergence marker lands on
              this bar's close_ts OR (b) live check: bar made a new low/high vs
              the trailing 20 bars but CVD at this bar > CVD at prior extreme
              (bull div) / CVD < CVD at prior extreme (bear div).
  5. TIER     favorable_delta = bar.delta / total_vol (signed toward setup side).
              HIGH if >= DELTA_HIGH, WEAK if >= DELTA_WEAK, else NONE.
  6. TARGETS  T1 = opposite edge of swept HVN; T2 = far extreme of next HVN in
              setup direction; T3 = nearest LVN extreme between the two HVNs.
              SL = the sweep wick extreme (tight). Entry = bar close.
  7. WALK     forward fwd_n bars; record SL hit, T1/T2/T3 hits, MFE/MAE in R.

Output:
  data/reports/cvd_sweep_<SYM>_<TF>.jsonl   one row per sweep setup
  data/reports/cvd_sweep_study.md           sectioned aggregate report

Usage:
  .venv/bin/python scripts/cvd_sweep_study.py
  .venv/bin/python scripts/cvd_sweep_study.py --symbols BTCUSDT --tf 5m
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
REPORT_OUT = ROOT / "data" / "reports" / "cvd_sweep_study.md"

SYMBOLS = ["BTCUSDT", "XAUTUSDT"]
TFS = ["15m", "5m"]

# per-tf knobs: VP lookback (~24h), forward outcome window, swing-pivot half-window
CFG = {
    "15m": {"vp_win": 96,  "fwd_n": 20, "div_lookback": 3},
    "5m":  {"vp_win": 288, "fwd_n": 36, "div_lookback": 4},
}

ATR_PERIOD = 14
SWEEP_PEN_FRAC = 0.05    # wick must pierce HVN edge by >= 5% of bar range
RECLAIM_FRAC = 0.20      # close must come back inside by >= 20% of penetration
DELTA_HIGH = 0.35        # favorable bar.delta/total_vol ratio → HIGH tier
DELTA_WEAK = 0.15        # ... → WEAK tier; below → NONE
PRIOR_LOOKBACK = 20      # bars used to fix the "prior extreme" for live-div check


# ── data load ────────────────────────────────────────────────────────────────
def load_bars(symbol: str, tf: str) -> list:
    """Load + dedupe (by close_ts) all footprint files for symbol/tf."""
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


# ── sweep detection ──────────────────────────────────────────────────────────
def _sweep_candidates(bar, hvn_zones: list[dict], atr_val: float) -> list[dict]:
    """Return sweep candidates for this bar at HVN extremes.

    LONG sweep:  bar.l pierces hvn.low, close reclaims back inside.
    SHORT sweep: bar.h pierces hvn.high, close reclaims back inside.
    """
    if not hvn_zones or atr_val <= 0:
        return []
    h, l, c = bar.ohlc.h, bar.ohlc.l, bar.ohlc.c
    rng = max(h - l, 1e-9)
    out: list[dict] = []
    for z in hvn_zones:
        z_low, z_high = z["low"], z["high"]
        if z_high - z_low <= 0:
            continue
        # LONG sweep — wick below z_low, close reclaims
        pen = z_low - l
        if pen > 0 and pen >= SWEEP_PEN_FRAC * rng and c >= z_low - RECLAIM_FRAC * pen:
            out.append({
                "side": "long", "hvn": z, "sweep_price": l, "hvn_edge": z_low,
                "penetration": pen, "penetration_atr": pen / atr_val,
            })
        # SHORT sweep — wick above z_high, close reclaims
        pen = h - z_high
        if pen > 0 and pen >= SWEEP_PEN_FRAC * rng and c <= z_high + RECLAIM_FRAC * pen:
            out.append({
                "side": "short", "hvn": z, "sweep_price": h, "hvn_edge": z_high,
                "penetration": pen, "penetration_atr": pen / atr_val,
            })
    return out


# ── delta tier ───────────────────────────────────────────────────────────────
def _delta_tier(bar, side: str) -> tuple[str, float]:
    """Classify favorable delta strength on the sweep candle.

    side='long' wants positive delta (buyers absorbing the dip);
    side='short' wants negative delta (sellers absorbing the rip).
    """
    fp = build_fp(bar)
    total = fp.total_bid + fp.total_ask
    if total <= 0:
        return "none", 0.0
    d = bar.delta or 0.0
    ratio = d / total            # signed
    favorable = ratio if side == "long" else -ratio
    if favorable >= DELTA_HIGH:
        return "high", favorable
    if favorable >= DELTA_WEAK:
        return "weak", favorable
    return "none", favorable


# ── cvd divergence (live) ────────────────────────────────────────────────────
def _live_cvd_div(bars: list, cum: list[float], i: int, side: str) -> bool:
    """Live-style CVD div at bar i: bar made new low/high vs the trailing
    PRIOR_LOOKBACK bars while CVD at the bar close did NOT confirm.
    """
    lo = max(0, i - PRIOR_LOOKBACK)
    if lo >= i:
        return False
    bar = bars[i]
    if side == "long":
        # idx of prior bar with the lowest low
        k_prior = min(range(lo, i), key=lambda k: bars[k].ohlc.l)
        return bar.ohlc.l < bars[k_prior].ohlc.l and cum[i] > cum[k_prior]
    else:
        k_prior = max(range(lo, i), key=lambda k: bars[k].ohlc.h)
        return bar.ohlc.h > bars[k_prior].ohlc.h and cum[i] < cum[k_prior]


# ── targets ──────────────────────────────────────────────────────────────────
def _target_levels(side: str, hvn: dict, all_hvns: list[dict], all_lvns: list[dict]) -> dict:
    """T1 = opposite edge of swept HVN. T2 = far extreme of next HVN in
    setup direction. T3 = nearest LVN extreme between the two HVNs."""
    if side == "long":
        t1 = hvn["high"]
        outer = [z for z in all_hvns if z["low"] > hvn["high"]]
        next_hvn = min(outer, key=lambda z: z["low"]) if outer else None
        t2 = next_hvn["high"] if next_hvn else None
        between = [z for z in all_lvns
                   if z["low"] > hvn["high"] and (t2 is None or z["high"] <= t2)]
        t3 = max(between, key=lambda z: z["high"])["high"] if between else None
    else:
        t1 = hvn["low"]
        outer = [z for z in all_hvns if z["high"] < hvn["low"]]
        next_hvn = max(outer, key=lambda z: z["high"]) if outer else None
        t2 = next_hvn["low"] if next_hvn else None
        between = [z for z in all_lvns
                   if z["high"] < hvn["low"] and (t2 is None or z["low"] >= t2)]
        t3 = min(between, key=lambda z: z["low"])["low"] if between else None
    return {"t1": t1, "t2": t2, "t3": t3}


# ── forward outcome ──────────────────────────────────────────────────────────
def _forward_outcome(bars, i, entry, side, sweep_extreme, targets, atr_val, fwd_n) -> dict:
    """Walk forward fwd_n bars; record SL/target hits and MFE/MAE.

    SL = sweep wick extreme (no buffer; tight). Risk = |entry - SL|.
    Targets prioritized highest reached: T2 > T3 > T1 > SL > TIME.
    """
    sl = sweep_extreme
    long = side == "long"
    risk = abs(entry - sl) or 1e-9
    mfe = mae = 0.0
    hit_t1 = hit_t2 = hit_t3 = False
    sl_hit = False
    bars_to_t1 = None
    fwd = bars[i + 1:i + 1 + fwd_n]
    for j, b in enumerate(fwd):
        if long:
            mfe = max(mfe, b.ohlc.h - entry)
            mae = max(mae, entry - b.ohlc.l)
            if b.ohlc.l <= sl:
                sl_hit = True
                break
            if targets["t1"] is not None and b.ohlc.h >= targets["t1"] and not hit_t1:
                hit_t1 = True
                bars_to_t1 = j + 1
            if targets["t3"] is not None and b.ohlc.h >= targets["t3"]:
                hit_t3 = True
            if targets["t2"] is not None and b.ohlc.h >= targets["t2"]:
                hit_t2 = True
        else:
            mfe = max(mfe, entry - b.ohlc.l)
            mae = max(mae, b.ohlc.h - entry)
            if b.ohlc.h >= sl:
                sl_hit = True
                break
            if targets["t1"] is not None and b.ohlc.l <= targets["t1"] and not hit_t1:
                hit_t1 = True
                bars_to_t1 = j + 1
            if targets["t3"] is not None and b.ohlc.l <= targets["t3"]:
                hit_t3 = True
            if targets["t2"] is not None and b.ohlc.l <= targets["t2"]:
                hit_t2 = True

    outcome = ("T2" if hit_t2 else
               "T3" if hit_t3 else
               "T1" if hit_t1 else
               "SL" if sl_hit else "TIME")
    return {
        "sl_hit": sl_hit,
        "hit_t1": hit_t1, "hit_t2": hit_t2, "hit_t3": hit_t3,
        "bars_to_t1": bars_to_t1,
        "mfe_atr": round(mfe / atr_val, 2) if atr_val > 0 else None,
        "mae_atr": round(mae / atr_val, 2) if atr_val > 0 else None,
        "mfe_r": round(mfe / risk, 2),
        "mae_r": round(mae / risk, 2),
        "outcome": outcome,
    }


# ── scan ─────────────────────────────────────────────────────────────────────
def scan(symbol: str, tf: str) -> list[dict]:
    cfg = CFG[tf]
    bars = load_bars(symbol, tf)
    if len(bars) < cfg["vp_win"] + cfg["fwd_n"] + 5:
        return []

    bin_size = DEFAULT_BIN_SIZE.get(symbol)

    # confirmed swing-pivot CVD divergences indexed by ts
    div_by_ts: dict[int, list[dict]] = {}
    for d in scan_divergences(bars, lookback=cfg["div_lookback"]):
        div_by_ts.setdefault(d["ts"], []).append(d)

    # running CVD (raw cumulative delta; for live-div compare against prior extreme idx)
    cum = [0.0] * len(bars)
    run = 0.0
    for k, b in enumerate(bars):
        run += b.delta or 0.0
        cum[k] = run

    rows: list[dict] = []
    for i in range(cfg["vp_win"], len(bars) - cfg["fwd_n"]):
        b = bars[i]
        atr_val = atr(bars[max(0, i - 50):i + 1], ATR_PERIOD) or 0.0
        if atr_val <= 0:
            continue
        vp = vp_compute(
            bars[i - cfg["vp_win"] + 1:i + 1], "daily", b.ohlc.c, bin_size=bin_size
        )
        hvns = vp.hvn_zones or []
        lvns = vp.lvn_zones or []
        cands = _sweep_candidates(b, hvns, atr_val)
        if not cands:
            continue

        for c in cands:
            tier, fav_ratio = _delta_tier(b, c["side"])

            # CVD divergence — confirmed marker on this ts (correct direction) OR live
            want = "bull" if c["side"] == "long" else "bear"
            div_confirmed = [d for d in div_by_ts.get(b.close_ts, []) if d["type"] == want]
            live_div = _live_cvd_div(bars, cum, i, c["side"]) and not div_confirmed
            cvd_div_intact = bool(div_confirmed) or live_div

            entry = b.ohlc.c
            targets = _target_levels(c["side"], c["hvn"], hvns, lvns)
            out = _forward_outcome(bars, i, entry, c["side"], c["sweep_price"],
                                   targets, atr_val, cfg["fwd_n"])

            rows.append({
                "symbol": symbol, "tf": tf, "ts": b.close_ts, "ist": _ist(b.close_ts),
                "side": c["side"],
                "hvn_low": round(c["hvn"]["low"], 4),
                "hvn_high": round(c["hvn"]["high"], 4),
                "hvn_edge": round(c["hvn_edge"], 4),
                "sweep_price": round(c["sweep_price"], 4),
                "penetration_atr": round(c["penetration_atr"], 3),
                "entry": round(entry, 4),
                "atr": round(atr_val, 4),
                "delta_tier": tier,
                "favorable_delta_ratio": round(fav_ratio, 3),
                "cvd_div_intact": cvd_div_intact,
                "cvd_div_confirmed": bool(div_confirmed),
                "cvd_div_live": live_div,
                "div_strength": round(max((d["strength"] for d in div_confirmed), default=0.0), 3),
                "t1": round(targets["t1"], 4) if targets["t1"] is not None else None,
                "t2": round(targets["t2"], 4) if targets["t2"] is not None else None,
                "t3": round(targets["t3"], 4) if targets["t3"] is not None else None,
                **out,
            })
    return rows


# ── report ───────────────────────────────────────────────────────────────────
def _agg(rows: list[dict]) -> str:
    if not rows:
        return "n=  0"
    n = len(rows)
    sl = sum(1 for r in rows if r["sl_hit"])
    t1 = sum(1 for r in rows if r["hit_t1"])
    t2 = sum(1 for r in rows if r["hit_t2"])
    t3 = sum(1 for r in rows if r["hit_t3"])
    mfe = statistics.mean(r["mfe_r"] for r in rows)
    mae = statistics.mean(r["mae_r"] for r in rows)
    # net realized R with priority T2 > T3 > T1; SL = -1R; TIME = 0
    realized = []
    for r in rows:
        risk = abs(r["entry"] - r["sweep_price"]) or 1e-9
        if r["hit_t2"] and r["t2"] is not None:
            realized.append(abs(r["t2"] - r["entry"]) / risk)
        elif r["hit_t3"] and r["t3"] is not None:
            realized.append(abs(r["t3"] - r["entry"]) / risk)
        elif r["hit_t1"] and r["t1"] is not None:
            realized.append(abs(r["t1"] - r["entry"]) / risk)
        elif r["sl_hit"]:
            realized.append(-1.0)
        else:
            realized.append(0.0)
    avgR = statistics.mean(realized)
    wins = sum(1 for x in realized if x > 0)
    return (f"n={n:>4d}  T1={100*t1/n:>3.0f}%  T2={100*t2/n:>3.0f}%  T3={100*t3/n:>3.0f}%  "
            f"SL={100*sl/n:>3.0f}%  win={100*wins/n:>3.0f}%  "
            f"avgR={avgR:>+5.2f}  MFE={mfe:>4.2f}R  MAE={mae:>4.2f}R")


def _section(out: list[str], title: str, rows: list[dict], splits: list[tuple]):
    out.append(f"\n### {title}")
    out.append(f"- ALL — {_agg(rows)}")
    for label, pred in splits:
        sub = [r for r in rows if pred(r)]
        out.append(f"- {label:38s} — {_agg(sub)}")


def build_report(allrows: list[dict]) -> str:
    out: list[str] = [
        "# CVD Sweep Study — HVN-extreme sweeps + CVD divergence + delta tier",
        "",
        "**Hypothesis** — a wick-sweep beyond an HVN extreme with intact CVD",
        "divergence is a high-RR reversal trigger; outcome scales with the",
        "delta-confirmation tier on the sweep candle.",
        "",
        f"Setup gate: bar wick penetrates HVN edge by ≥{SWEEP_PEN_FRAC*100:.0f}% of bar range; "
        f"close reclaims ≥{RECLAIM_FRAC*100:.0f}% of that penetration.",
        f"Delta tier: HIGH = favorable Δ/total ≥{DELTA_HIGH}; WEAK ≥{DELTA_WEAK}; else NONE.",
        "CVD div intact = confirmed swing-pivot div on this bar's ts OR live new-extreme + "
        f"CVD-held vs trailing {PRIOR_LOOKBACK}-bar prior extreme.",
        "",
        "Targets — T1: opposite edge of swept HVN.  T2: far extreme of next HVN in setup "
        "direction.  T3: nearest LVN extreme between the two HVNs.",
        "SL = sweep wick extreme (tight, no buffer).  Risk = |entry − SL|.  "
        "Outcome priority T2 > T3 > T1 > SL > TIME; avgR = realized R using that priority.",
        "",
        f"Total setups logged: **{len(allrows)}**",
        "",
        "## Overview",
        "",
        "| symbol | tf | count |",
        "|---|---|---|",
    ]
    syms = sorted({r["symbol"] for r in allrows})
    tfs = sorted({r["tf"] for r in allrows})
    for s in syms:
        for tf in tfs:
            c = sum(1 for r in allrows if r["symbol"] == s and r["tf"] == tf)
            if c:
                out.append(f"| {s} | {tf} | {c} |")

    for s in syms:
        for tf in tfs:
            rows = [r for r in allrows if r["symbol"] == s and r["tf"] == tf]
            if not rows:
                continue
            out.append(f"\n## {s} {tf}")
            _section(out, "by side", rows, [
                ("LONG sweep (HVN low pierced)",  lambda r: r["side"] == "long"),
                ("SHORT sweep (HVN high pierced)", lambda r: r["side"] == "short"),
            ])
            _section(out, "by delta tier", rows, [
                ("HIGH favorable Δ", lambda r: r["delta_tier"] == "high"),
                ("WEAK favorable Δ", lambda r: r["delta_tier"] == "weak"),
                ("NONE / opposing Δ", lambda r: r["delta_tier"] == "none"),
            ])
            _section(out, "by CVD divergence × delta tier (thesis)", rows, [
                ("CVD div intact",                  lambda r: r["cvd_div_intact"]),
                ("CVD div intact + HIGH Δ",         lambda r: r["cvd_div_intact"] and r["delta_tier"] == "high"),
                ("CVD div intact + WEAK Δ",         lambda r: r["cvd_div_intact"] and r["delta_tier"] == "weak"),
                ("CVD div intact + NONE Δ",         lambda r: r["cvd_div_intact"] and r["delta_tier"] == "none"),
                ("no CVD div + HIGH Δ",             lambda r: not r["cvd_div_intact"] and r["delta_tier"] == "high"),
                ("no CVD div",                      lambda r: not r["cvd_div_intact"]),
            ])
            _section(out, "by CVD div source", rows, [
                ("confirmed swing-pivot div", lambda r: r["cvd_div_confirmed"]),
                ("live new-extreme div only", lambda r: r["cvd_div_live"]),
            ])
            _section(out, "by penetration depth (sweep wick / ATR)", rows, [
                ("shallow  <0.25 ATR",      lambda r: r["penetration_atr"] < 0.25),
                ("medium  0.25–0.75 ATR",   lambda r: 0.25 <= r["penetration_atr"] < 0.75),
                ("deep    ≥0.75 ATR",       lambda r: r["penetration_atr"] >= 0.75),
            ])
            _section(out, "by outcome label", rows, [
                ("reached T2 (full HVN→HVN)", lambda r: r["outcome"] == "T2"),
                ("reached T3 (LVN extreme)",  lambda r: r["outcome"] == "T3"),
                ("reached T1 (opp HVN edge)", lambda r: r["outcome"] == "T1"),
                ("SL hit",                    lambda r: r["outcome"] == "SL"),
                ("TIME stop",                 lambda r: r["outcome"] == "TIME"),
            ])

            # notable examples
            best = sorted(rows, key=lambda r: r["mfe_r"], reverse=True)[:8]
            if best:
                out.append("\n**Top MFE-R setups:**")
                for r in best:
                    out.append(
                        f"- {r['ist']} {r['side'].upper():5s} "
                        f"HVN[{r['hvn_low']:.2f}–{r['hvn_high']:.2f}] "
                        f"sweep={r['sweep_price']:.2f} ({r['penetration_atr']:.2f}ATR) "
                        f"Δ-tier={r['delta_tier']:4s} div={'Y' if r['cvd_div_intact'] else 'N'} "
                        f"→ {r['outcome']:4s} MFE={r['mfe_r']:.2f}R MAE={r['mae_r']:.2f}R"
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
            print(f"  {s} {tf}: {len(rows)} sweep setups")
            out = ROOT / "data" / "reports" / f"cvd_sweep_{s}_{tf}.jsonl"
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("w") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")

    REPORT_OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT_OUT.write_text(build_report(allrows))
    print(f"\nlogged {len(allrows)} setups → data/reports/cvd_sweep_*.jsonl")
    print(f"report → {REPORT_OUT}")


if __name__ == "__main__":
    main(sys.argv[1:])
