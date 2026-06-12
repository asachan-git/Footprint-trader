#!/usr/bin/env python3
"""EqHL-failure signal + anchor-bar exit study — each split with/without a CVD-div
filter, on the clean real-delta footprint.

PART 1 — EqHL failure as an ENTRY signal (both arms, classified):
  An equal-high/low cluster (≥2 swing pivots within EQ_TOL) is resting liquidity.
  When a bar breaks it, classify over the next CLASSIFY_N bars:
    FAILED  break  — price closes back through the level → breakout failed → FADE
                     (break above EqH then reject → short; below EqL then reclaim → long)
    ACCEPTED break — price holds beyond the level → level failed to hold → CONTINUATION
                     (hold above EqH → long; hold below EqL → short)
  Signal fires at the classification bar (causal). Each arm measured ±CVD-div
  (side aligned with the last confirmed 15m CVD-div).

PART 2 — Anchor-bar EXIT (both triggers), on simple anchor entries:
  Entry: a high-delta anchor bar → enter in its delta direction at its close.
  Baseline exit: fixed 2R target / 1R stop / time(fwd_n).
  Test exits (first to fire, else baseline):
    BREAK_AGAINST   — price breaks through an opposing anchor's high/low.
    OPP_ANCHOR      — a fresh anchor bar prints with delta AGAINST the position.
  Each test exit ±CVD-div confirm (a fresh opposing CVD-div on the exit bar).
  (Fleet-entry baseline = follow-up; this isolates the exit on a clean entry set.)

Metric: realized R (1:1 ATR unit for signals; R vs entry/stop for exits), per arm/filter.
Output: data/reports/eqhl_anchor_study.md

Usage:
  .venv/bin/python scripts/eqhl_anchor_study.py --symbols XAUTUSDT BTCUSDT --tf 15m
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.state_store import store
from pipeline.features.atr import atr
from pipeline.features.cvd_candlestick import scan_divergences
from pipeline.features.wave import detect_swing_points

REPORT = ROOT / "data" / "reports" / "eqhl_anchor_study.md"

EQ_TOL = 0.0015        # two pivots within 0.15% = an equal level (stop cluster)
SWING_LB = 3           # ± bars for swing pivots
CLASSIFY_N = 3         # bars to classify a break as failed vs accepted
FWD_N = 16             # forward horizon (bars) for signal MFE/MAE + exit sim
SCALP_TGT = 1.0        # ATR (signal scalp target)
SCALP_STOP = 1.0       # ATR
TP_R = 2.0             # exit-study baseline target (R)
SL_R = 1.0             # exit-study baseline stop (R) — = entry-anchor risk
ATR_PERIOD = 14
# anchor-bar criteria (mirror pipeline/features/anchor_bar.py)
ANCHOR_VOL_Z = 2.0
ANCHOR_DELTA_Z = 2.0
ANCHOR_DIR_MIN = 0.30
ANCHOR_AVG_N = 60


def _vol(b):
    return sum(l.vol for l in (b.bid_ladder or [])) + sum(l.vol for l in (b.ask_ladder or []))


def _atr_at(bars, i):
    return atr(bars[max(0, i - ATR_PERIOD):i + 1]) or 0.0


def _cvd_dir_at(bars, i):
    """Most recent confirmed/live CVD-div direction as of bar i (causal): +1 bull,
    −1 bear, 0 none. Uses a trailing window."""
    seg = bars[max(0, i - 120):i + 1]
    if len(seg) < 10:
        return 0
    divs = scan_divergences(seg, lookback=3, include_live=True)
    if not divs:
        return 0
    last = max(divs, key=lambda d: d["ts"])
    return 1 if last["type"] == "bull" else -1


def _is_anchor(bars, i):
    """(is_anchor, delta_sign) for bar i per anchor_bar.py criteria."""
    if i < ANCHOR_AVG_N:
        return False, 0
    win = bars[i - ANCHOR_AVG_N:i]
    avg_vol = statistics.mean(_vol(b) for b in win) or 1e-9
    avg_ad = statistics.mean(abs(b.delta or 0) for b in win) or 1e-9
    v, d = _vol(bars[i]), (bars[i].delta or 0.0)
    if v > ANCHOR_VOL_Z * avg_vol and abs(d) > ANCHOR_DELTA_Z * avg_ad \
            and abs(d) / max(v, 1e-9) > ANCHOR_DIR_MIN:
        return True, (1 if d > 0 else -1)
    return False, 0


def _fwd(bars, i, direction, atr_i):
    """Signal forward MFE/MAE (ATR) + 1:1 scalp hit over FWD_N bars."""
    if atr_i <= 0:
        return None
    entry = bars[i].ohlc.c
    mfe = mae = 0.0
    hit = None
    for b in bars[i + 1:i + 1 + FWD_N]:
        fav = (b.ohlc.h - entry) if direction > 0 else (entry - b.ohlc.l)
        adv = (entry - b.ohlc.l) if direction > 0 else (b.ohlc.h - entry)
        mfe = max(mfe, fav / atr_i); mae = max(mae, adv / atr_i)
        if hit is None:
            if fav >= SCALP_TGT * atr_i:
                hit = "tgt"
            elif adv >= SCALP_STOP * atr_i:
                hit = "stop"
    return {"hit": hit, "mfe": mfe, "mae": mae}


# ── equal-level detection ──────────────────────────────────────────────────────
def _equal_levels(prices):
    """Prices with ≥1 near-duplicate within EQ_TOL → equal-cluster levels."""
    out = []
    for i, p in enumerate(prices):
        if any(i != j and abs(p - q) <= p * EQ_TOL for j, q in enumerate(prices)):
            out.append(p)
    return out


# ── PART 1: EqHL failure signal ────────────────────────────────────────────────
def part1(bars):
    rec = defaultdict(list)   # (arm, filt) -> [fwd]
    n = len(bars)
    for i in range(ANCHOR_AVG_N, n - FWD_N - CLASSIFY_N):
        win = bars[max(0, i - 200):i + 1]
        highs, lows = detect_swing_points(win, lookback=SWING_LB)
        eq_h = _equal_levels([p for _, p in highs])
        eq_l = _equal_levels([p for _, p in lows])
        b = bars[i]
        atr_i = _atr_at(bars, i)
        if atr_i <= 0:
            continue
        # break of an EqH (up) — nearest below the bar high
        for lvl in [x for x in eq_h if b.ohlc.h > x >= b.ohlc.o]:
            post = bars[i + 1:i + 1 + CLASSIFY_N]
            if len(post) < CLASSIFY_N:
                break
            held = all(pb.ohlc.c > lvl for pb in post)        # accepted (holds above)
            failed = post[-1].ohlc.c < lvl                    # closed back below
            j = i + CLASSIFY_N
            if held:
                _emit(rec, "accept_EqH_long", bars, j, +1)    # continuation long
            elif failed:
                _emit(rec, "fail_EqH_short", bars, j, -1)     # fade short
            break
        for lvl in [x for x in eq_l if b.ohlc.l < x <= b.ohlc.o]:
            post = bars[i + 1:i + 1 + CLASSIFY_N]
            if len(post) < CLASSIFY_N:
                break
            held = all(pb.ohlc.c < lvl for pb in post)
            failed = post[-1].ohlc.c > lvl
            j = i + CLASSIFY_N
            if held:
                _emit(rec, "accept_EqL_short", bars, j, -1)   # continuation short
            elif failed:
                _emit(rec, "fail_EqL_long", bars, j, +1)      # fade long
            break
    return rec


def _emit(rec, arm, bars, j, direction):
    if j >= len(bars) - FWD_N:
        return
    f = _fwd(bars, j, direction, _atr_at(bars, j))
    if f is None:
        return
    cvd = _cvd_dir_at(bars, j)
    aligned = (cvd == direction)
    rec[(arm, "ALL")].append(f)
    rec[(arm, "+CVDdiv" if aligned else "−CVDdiv")].append(f)


# ── PART 2: anchor-bar exit on simple anchor entries ───────────────────────────
def part2(bars):
    """Enter in an anchor's delta direction at its close; compare exit policies."""
    rec = defaultdict(list)   # policy -> [realized_R]
    n = len(bars)
    for i in range(ANCHOR_AVG_N, n - FWD_N - 1):
        is_a, dsign = _is_anchor(bars, i)
        if not is_a:
            continue
        atr_i = _atr_at(bars, i)
        if atr_i <= 0:
            continue
        entry = bars[i].ohlc.c
        risk = SL_R * atr_i
        sl = entry - risk if dsign > 0 else entry + risk
        tp = entry + TP_R * risk if dsign > 0 else entry - TP_R * risk
        fut = bars[i + 1:i + 1 + FWD_N]
        rec["baseline_2R"].append(_sim_exit(entry, sl, tp, dsign, risk, fut, bars, i, None))
        for trig in ("break_against", "opp_anchor"):
            rec[f"{trig}"].append(_sim_exit(entry, sl, tp, dsign, risk, fut, bars, i, (trig, False)))
            rec[f"{trig}+CVD"].append(_sim_exit(entry, sl, tp, dsign, risk, fut, bars, i, (trig, True)))
    return rec


def _sim_exit(entry, sl, tp, dsign, risk, fut, bars, i, policy):
    """Realized R. Baseline = first of TP/SL/time. With policy = also exit on the
    anchor trigger (optionally CVD-confirmed) at that bar's close, whichever first."""
    for k, b in enumerate(fut):
        # hard SL / TP first-touch (SL-first)
        if (dsign > 0 and b.ohlc.l <= sl) or (dsign < 0 and b.ohlc.h >= sl):
            return -SL_R
        if (dsign > 0 and b.ohlc.h >= tp) or (dsign < 0 and b.ohlc.l <= tp):
            return TP_R
        if policy is not None:
            trig, need_cvd = policy
            bi = i + 1 + k
            fired = False
            if trig == "break_against":
                # price breaks an opposing anchor's high/low (against the trade)
                fired = _broke_opposing_anchor(bars, bi, dsign)
            else:  # opp_anchor — a fresh anchor with delta against us
                is_a, ds = _is_anchor(bars, bi)
                fired = is_a and ds == -dsign
            if fired and (not need_cvd or _cvd_dir_at(bars, bi) == -dsign):
                pnl = (b.ohlc.c - entry) if dsign > 0 else (entry - b.ohlc.c)
                return pnl / risk
    last = fut[-1].ohlc.c if fut else entry
    pnl = (last - entry) if dsign > 0 else (entry - last)
    return pnl / risk


def _broke_opposing_anchor(bars, bi, dsign, lookback=20):
    """Did bar bi break through a recent opposing-delta anchor's extreme, against us?"""
    for k in range(max(0, bi - lookback), bi):
        is_a, ds = _is_anchor(bars, k)
        if not is_a or ds != -dsign:
            continue
        # long trade trapped: break BELOW a bearish anchor's low; short: break ABOVE bull anchor high
        if dsign > 0 and bars[bi].ohlc.c < bars[k].ohlc.l:
            return True
        if dsign < 0 and bars[bi].ohlc.c > bars[k].ohlc.h:
            return True
    return False


def _stat_sig(fwds):
    n = len(fwds)
    if not n:
        return None
    h = sum(1 for f in fwds if f["hit"] == "tgt"); s = sum(1 for f in fwds if f["hit"] == "stop")
    res = h + s
    return f"n={n:<4} scalpWR={100*h/res if res else 0:>3.0f}% medMFE={statistics.median(f['mfe'] for f in fwds):.2f} expR={(h-s)/n:+.2f}"


def _stat_r(rs):
    n = len(rs)
    if not n:
        return None
    w = sum(1 for r in rs if r > 0)
    return f"n={n:<4} WR={100*w/n:>3.0f}% sumR={sum(rs):>7.2f} avgR={statistics.mean(rs):+.3f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["XAUTUSDT", "BTCUSDT"])
    ap.add_argument("--tf", default="15m")
    a = ap.parse_args()
    L = ["# EqHL-failure signal + anchor-bar exit study", "",
         f"tf={a.tf}, classify_n={CLASSIFY_N}, fwd_n={FWD_N}, scalp {SCALP_TGT}:{SCALP_STOP} ATR, "
         f"exit baseline {TP_R}R/{SL_R}R. Clean real-delta footprint.", ""]
    for sym in a.symbols:
        bars = list(store()._bars.get((sym, a.tf), []))
        L.append(f"\n## {sym}  ({len(bars)} {a.tf} bars)")
        if len(bars) < 300:
            L.append("insufficient bars"); continue
        L.append("\n### Part 1 — EqHL failure as signal (fade failed / continue accepted)")
        r1 = part1(bars)
        for arm in ("fail_EqH_short", "fail_EqL_long", "accept_EqH_long", "accept_EqL_short"):
            for filt in ("ALL", "+CVDdiv", "−CVDdiv"):
                st = _stat_sig(r1.get((arm, filt), []))
                if st:
                    L.append(f"  {arm:<18}/{filt:<8} {st}")
        L.append("\n### Part 2 — anchor-bar exit vs 2R baseline (simple anchor entries)")
        r2 = part2(bars)
        for pol in ("baseline_2R", "break_against", "break_against+CVD", "opp_anchor", "opp_anchor+CVD"):
            st = _stat_r(r2.get(pol, []))
            if st:
                L.append(f"  {pol:<22} {st}")
        L.append("")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(L))
    print("\n".join(L))
    print(f"\n[written] {REPORT}")


if __name__ == "__main__":
    main()
