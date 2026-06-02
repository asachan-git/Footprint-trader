#!/usr/bin/env python3
"""Reversal study — what the footprint / volume / delta / CVD / VP level looked
like at every confirmed price reversal in BTC + XAU history.

Pipeline per (symbol, tf):
  1. DETECT  swing pivots via wave.detect_swing_points (price h/l, ±lookback).
  2. CONFIRM each pivot is a real reversal: from the pivot, price must move the
     OPPOSITE way by >= REV_ATR x ATR within FWD_N bars BEFORE the pivot extreme
     is exceeded (look-forward only). Rejected pivots are counted for accuracy.
  3. CHARACTERIZE each confirmed reversal: footprint (delta, vol, POC, trapped-
     extreme bid/ask), volume-vs-median (climax), CVD level + slope + whether a
     CVD divergence (cvd_candlestick.scan_divergences) coincided, and the nearest
     volume-profile level (POC/VAH/VAL/HVN/LVN) with distance in ATR.

Output:
  data/reports/reversals_<SYM>_<TF>.jsonl   (one row per confirmed reversal)
  data/reports/reversal_study.md            (sectioned aggregate report)

Usage: .venv/bin/python scripts/reversal_study.py
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
from pipeline.features.wave import detect_swing_points
from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE
from pipeline.features.cvd_candlestick import scan_divergences

FP_DIR = ROOT / "data" / "footprint"
REPORT_OUT = ROOT / "data" / "reports" / "reversal_study.md"

SYMBOLS = ["BTCUSDT", "XAUTUSDT"]
TFS = ["15m", "5m"]

# per-tf knobs: pivot half-window, forward confirm window, VP lookback (~24h)
CFG = {
    "15m": {"lookback": 3, "fwd_n": 12, "vp_win": 96},
    "5m":  {"lookback": 4, "fwd_n": 18, "vp_win": 288},
}
REV_ATR = 1.5          # opposite move (in ATR) required to confirm a reversal
ATR_PERIOD = 14
VOL_LB = 20            # trailing median window for the climax ratio
CVD_SLOPE_LB = 6       # bars for the CVD slope read
WINDOW_K = 3           # bars captured before AND after the pivot (the reversal sequence)


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


def _confirm_reversal(bars, i, side, atr_val, fwd_n) -> bool:
    """side='high' (top→down) or 'low' (bottom→up). Opposite move >= REV_ATR*ATR
    within fwd_n bars before the pivot extreme is exceeded."""
    if atr_val <= 0:
        return False
    pivot = bars[i]
    thr = REV_ATR * atr_val
    if side == "high":
        target = pivot.ohlc.h - thr
        for b in bars[i + 1:i + 1 + fwd_n]:
            if b.ohlc.h > pivot.ohlc.h:        # pivot broken up first → not a top
                return False
            if b.ohlc.l <= target:             # fell far enough → confirmed top
                return True
    else:
        target = pivot.ohlc.l + thr
        for b in bars[i + 1:i + 1 + fwd_n]:
            if b.ohlc.l < pivot.ohlc.l:        # pivot broken down first → not a bottom
                return False
            if b.ohlc.h >= target:             # rose far enough → confirmed bottom
                return True
    return False


def _extreme_split(bar, side) -> dict:
    """Aggressive buy(ask)/sell(bid) volume in the reversing extreme 10% zone."""
    h, l = bar.ohlc.h, bar.ohlc.l
    rng = max(h - l, 1e-9)
    zone = rng * 0.10
    if side == "high":                          # top: who fought at the high
        thr = h - zone
        ask = sum(v.vol for v in bar.ask_ladder if v.price >= thr)
        bid = sum(v.vol for v in bar.bid_ladder if v.price >= thr)
        wick = (h - max(bar.ohlc.o, bar.ohlc.c)) / rng
    else:
        thr = l + zone
        ask = sum(v.vol for v in bar.ask_ladder if v.price <= thr)
        bid = sum(v.vol for v in bar.bid_ladder if v.price <= thr)
        wick = (min(bar.ohlc.o, bar.ohlc.c) - l) / rng
    tot = ask + bid
    return {
        "zone_ask": round(ask, 2), "zone_bid": round(bid, 2),
        "zone_aggressor": ("buy" if ask >= bid else "sell"),
        "zone_buy_frac": round(ask / tot, 3) if tot > 0 else None,
        "extreme_wick_frac": round(wick, 3),
    }


def _bar_read(bars, j, cum, side) -> dict:
    """Reversal-aligned read of one bar in the window. side='high' (top→down
    reversal) or 'low' (bottom→up). rev_delta>0 = order flow pushing the
    REVERSAL way; closed_with_rev = candle closed the reversal way; the zone is
    read at the pivot's reversing extreme (high for tops, low for bottoms)."""
    bar = bars[j]
    fp = build_fp(bar)
    vol = fp.total_bid + fp.total_ask
    d = fp.delta
    rev_delta = -d if side == "high" else d
    closed_with_rev = (bar.ohlc.c < bar.ohlc.o) if side == "high" else (bar.ohlc.c > bar.ohlc.o)
    prior = [_tot_vol(b) for b in bars[max(0, j - VOL_LB):j] if _tot_vol(b) > 0]
    vr = vol / statistics.median(prior) if prior else None
    es = _extreme_split(bar, side)
    # "into-extreme" aggressor = the side that pushed the pivot extreme (buy at a
    # top, sell at a bottom) — the side that gets trapped if it reverses.
    into_extreme = (es["zone_aggressor"] == "buy") if side == "high" else (es["zone_aggressor"] == "sell")
    return {
        "off": j,
        "close": round(bar.ohlc.c, 4),
        "delta": round(d, 2),
        "rev_delta": round(rev_delta, 2),
        "rev_delta_pos": rev_delta > 0,
        "cvd": round(cum[j], 2),
        "vol_ratio": round(vr, 2) if vr else None,
        "closed_with_rev": closed_with_rev,
        "zone_aggressor": es["zone_aggressor"],
        "into_extreme_aggressor": into_extreme,
        "extreme_wick_frac": es["extreme_wick_frac"],
    }


def _pivot_features(win) -> dict:
    """Decision-time features derived from the reversal sequence, knowable by the
    +1 (flip) bar. delta_swing = the flip magnitude (rev_delta at +1 minus at the
    pivot) — the core trigger; pivot vol×/wick; whether +1 closed the reversal way."""
    by_rel = {w["rel"]: w for w in win}
    piv = by_rel.get(0)
    nxt = by_rel.get(1)
    if not piv or not nxt:
        return {}
    return {
        "pivot_vol_ratio": piv["vol_ratio"],
        "pivot_wick_frac": piv["extreme_wick_frac"],
        "pivot_rev_delta": piv["rev_delta"],
        "delta_swing": round(nxt["rev_delta"] - piv["rev_delta"], 2),  # the flip
        "flip_closed_rev": nxt["closed_with_rev"],
        "flip_rev_delta_pos": nxt["rev_delta_pos"],
    }


def _nearest_vp_level(price, vp, atr_val) -> dict:
    """Nearest VP level to the pivot price; distance in ATR."""
    cands: list[tuple[str, float]] = []
    if vp.poc is not None: cands.append(("POC", vp.poc))
    if vp.vah is not None: cands.append(("VAH", vp.vah))
    if vp.val is not None: cands.append(("VAL", vp.val))
    for z in (vp.hvn_zones or []):
        cands.append(("HVN", (z["low"] + z["high"]) / 2))
    for z in (vp.lvn_zones or []):
        cands.append(("LVN", (z["low"] + z["high"]) / 2))
    if not cands:
        return {"nearest_level": None, "nearest_level_price": None, "dist_atr": None}
    label, lvl = min(cands, key=lambda c: abs(price - c[1]))
    return {
        "nearest_level": label,
        "nearest_level_price": round(lvl, 4),
        "dist_atr": round(abs(price - lvl) / atr_val, 3) if atr_val > 0 else None,
        "vp_position": vp.current_position,
    }


def scan(symbol: str, tf: str) -> tuple[list[dict], list[dict], dict]:
    cfg = CFG[tf]
    bars = load_bars(symbol, tf)
    stats = {"bars": len(bars), "pivots": 0, "confirmed": 0, "rejected": 0}
    if len(bars) < cfg["vp_win"] + cfg["fwd_n"] + 5:
        return [], [], stats

    highs, lows = detect_swing_points(bars, lookback=cfg["lookback"])
    pivots = [(i, p, "high") for i, p in highs] + [(i, p, "low") for i, p in lows]
    pivots.sort(key=lambda x: x[0])
    stats["pivots"] = len(pivots)

    bin_size = DEFAULT_BIN_SIZE.get(symbol)
    div_ts = {d["ts"]: d for d in scan_divergences(bars, lookback=cfg["lookback"])}

    rows: list[dict] = []
    n = len(bars)
    cum = [0.0] * n
    run = 0.0
    for k, b in enumerate(bars):
        run += b.delta or 0.0
        cum[k] = run

    rejected: list[dict] = []
    for i, price, side in pivots:
        # need VP history behind + forward confirm window + the after-window ahead
        if i < cfg["vp_win"] + WINDOW_K or i + max(cfg["fwd_n"], WINDOW_K) >= n:
            continue
        atr_val = atr(bars[max(0, i - 50):i + 1], ATR_PERIOD)
        win = [{**_bar_read(bars, j, cum, side), "rel": j - i}
               for j in range(i - WINDOW_K, i + WINDOW_K + 1)]
        bar = bars[i]
        vp = vp_compute(bars[i - cfg["vp_win"] + 1:i + 1], "daily", bar.ohlc.c,
                        bin_size=bin_size)
        pfeat = _pivot_features(win)
        nvp = _nearest_vp_level(price, vp, atr_val)
        if not _confirm_reversal(bars, i, side, atr_val, cfg["fwd_n"]):
            stats["rejected"] += 1
            rejected.append({"symbol": symbol, "tf": tf, "idx": i,
                             "pivot_side": side, "window": win,
                             "dist_atr": nvp["dist_atr"], "nearest_level": nvp["nearest_level"],
                             **pfeat})
            continue
        stats["confirmed"] += 1

        fp = build_fp(bar)
        vol = fp.total_bid + fp.total_ask
        prior_vols = [_tot_vol(b) for b in bars[i - VOL_LB:i] if _tot_vol(b) > 0]
        vol_ratio = vol / statistics.median(prior_vols) if prior_vols else None

        # CVD slope over trailing window (continuous cum delta)
        cvd_now = cum[i]
        cvd_prev = cum[max(0, i - CVD_SLOPE_LB)]
        cvd_slope = cvd_now - cvd_prev

        rev_dir = "down" if side == "high" else "up"
        delta = fp.delta
        # does delta oppose the (exhausted) prior move = classic reversal tell?
        # top → expect waning/negative delta; bottom → waning/positive delta
        delta_confirms_reversal = (delta < 0) if side == "high" else (delta > 0)

        row = {
            "symbol": symbol, "tf": tf, "idx": i, "ts": bar.close_ts,
            "pivot_side": side, "reversal_dir": rev_dir, "price": round(price, 4),
            "atr": round(atr_val, 4),
            "delta": round(delta, 2),
            "delta_confirms_reversal": delta_confirms_reversal,
            "total_vol": round(vol, 2),
            "vol_ratio_med": round(vol_ratio, 2) if vol_ratio else None,
            "is_climax": (vol_ratio is not None and vol_ratio >= 3.0),
            "poc_price": round(fp.poc_price, 4) if fp.poc_price else None,
            "cvd": round(cvd_now, 2),
            "cvd_slope": round(cvd_slope, 2),
            "cvd_divergence_here": bar.close_ts in div_ts,
            "cvd_div_type": div_ts.get(bar.close_ts, {}).get("type"),
            **_extreme_split(bar, side),
            **nvp,
            **pfeat,
            # reversal sequence: K bars before … pivot(rel=0) … K bars after,
            # all reversal-aligned (rev_delta>0 = flow toward the reversal).
            "window": win,
        }
        rows.append(row)

    return rows, rejected, stats


# ── report ────────────────────────────────────────────────────────────────────
def _pct(num, den) -> str:
    return f"{100*num/den:.0f}%" if den else "—"


def _section(title) -> str:
    return f"\n## {title}\n"


def build_report(all_rows, all_stats, all_rej=None) -> str:
    L = ["# Reversal Study — footprint / volume / delta / CVD / VP at confirmed reversals",
         "",
         f"Confirm rule: opposite move ≥ {REV_ATR}×ATR within the forward window before "
         f"the pivot extreme is exceeded. Pivots from wave.detect_swing_points.",
         ""]

    # ── accuracy ──
    L.append(_section("1. Detector accuracy (confirmed vs rejected pivots)"))
    L.append("| set | bars | pivots | confirmed | rejected | confirm-rate |")
    L.append("|---|---|---|---|---|---|")
    for key, s in all_stats.items():
        L.append(f"| {key} | {s['bars']} | {s['pivots']} | {s['confirmed']} | "
                 f"{s['rejected']} | {_pct(s['confirmed'], s['pivots'])} |")

    rows = [r for rs in all_rows.values() for r in rs]
    if not rows:
        L.append("\n_No confirmed reversals._")
        return "\n".join(L)

    def agg(subset, name):
        if not subset:
            return f"| {name} | 0 | — | — | — | — | — | — |"
        n = len(subset)
        clx = sum(1 for r in subset if r["is_climax"])
        dcr = sum(1 for r in subset if r["delta_confirms_reversal"])
        cdv = sum(1 for r in subset if r["cvd_divergence_here"])
        dists = [r["dist_atr"] for r in subset if r["dist_atr"] is not None]
        med_d = statistics.median(dists) if dists else None
        near = [r["dist_atr"] for r in subset if r["dist_atr"] is not None and r["dist_atr"] <= 0.5]
        vr = [r["vol_ratio_med"] for r in subset if r["vol_ratio_med"] is not None]
        med_vr = statistics.median(vr) if vr else None
        return (f"| {name} | {n} | {_pct(dcr, n)} | {_pct(cdv, n)} | {_pct(clx, n)} | "
                f"{med_vr:.2f} | {med_d:.2f} | {_pct(len(near), len(dists))} |")

    L.append(_section("2. Microstructure signature of confirmed reversals"))
    L.append("Cols: n · delta-opposes-move · CVD-divergence-here · climax(≥3×) · "
             "median vol-ratio · median dist-to-nearest-VP (ATR) · within-0.5ATR-of-VP")
    L.append("| set | n | Δ-confirms | CVD-div | climax | med vol× | med VP-dist | ≤0.5ATR |")
    L.append("|---|---|---|---|---|---|---|---|")
    for key in all_rows:
        L.append(agg(all_rows[key], key))
    L.append(agg([r for r in rows if r["pivot_side"] == "high"], "ALL tops"))
    L.append(agg([r for r in rows if r["pivot_side"] == "low"],  "ALL bottoms"))
    L.append(agg(rows, "ALL"))

    # ── nearest-VP-level breakdown ──
    L.append(_section("3. Which VP level do reversals happen at?"))
    L.append("| nearest level | count | share | within 0.5ATR |")
    L.append("|---|---|---|---|")
    levels = {}
    for r in rows:
        lv = r["nearest_level"] or "none"
        levels.setdefault(lv, []).append(r)
    for lv, sub in sorted(levels.items(), key=lambda x: -len(x[1])):
        near = sum(1 for r in sub if r["dist_atr"] is not None and r["dist_atr"] <= 0.5)
        L.append(f"| {lv} | {len(sub)} | {_pct(len(sub), len(rows))} | {_pct(near, len(sub))} |")

    # ── trapped-side read ──
    L.append(_section("4. Trapped-side read at the reversing extreme"))
    L.append("At a confirmed TOP the high-zone aggressor should be BUYers (trapped); "
             "at a BOTTOM, SELLers (trapped).")
    tops = [r for r in rows if r["pivot_side"] == "high"]
    bots = [r for r in rows if r["pivot_side"] == "low"]
    top_buy = sum(1 for r in tops if r["zone_aggressor"] == "buy")
    bot_sell = sum(1 for r in bots if r["zone_aggressor"] == "sell")
    L.append(f"- Tops with BUY aggressor trapped at high: **{_pct(top_buy, len(tops))}** "
             f"({top_buy}/{len(tops)})")
    L.append(f"- Bottoms with SELL aggressor trapped at low: **{_pct(bot_sell, len(bots))}** "
             f"({bot_sell}/{len(bots)})")

    # ── reversal sequence (before → pivot → after) ──
    L.append(_section("5. Reversal sequence — rev-aligned trajectory across the window"))
    L.append("All metrics reversal-aligned: rev_delta>0 = order flow toward the reversal "
             "direction. rel=0 is the pivot bar; −K before, +K after. "
             "`flow→rev` = mean rev_delta (raw); `closed→rev` = % bars that closed the "
             "reversal way; `into-extreme` = % bars whose extreme-zone aggressor is the "
             "side that gets trapped (buyers at a top / sellers at a bottom).")

    def seq_table(subset, title):
        if not subset:
            return
        L.append(f"\n**{title}** (n={len(subset)})")
        L.append("| rel | flow→rev (mean Δ) | closed→rev | into-extreme aggr | mean vol× | mean CVD |")
        L.append("|---|---|---|---|---|---|")
        by_rel: dict[int, list[dict]] = {}
        for r in subset:
            for w in r.get("window", []):
                by_rel.setdefault(w["rel"], []).append(w)
        for rel in sorted(by_rel):
            ws = by_rel[rel]
            md = statistics.mean(w["rev_delta"] for w in ws)
            cw = sum(1 for w in ws if w["closed_with_rev"])
            ie = sum(1 for w in ws if w["into_extreme_aggressor"])
            vrs = [w["vol_ratio"] for w in ws if w["vol_ratio"] is not None]
            mvr = statistics.mean(vrs) if vrs else 0
            mcvd = statistics.mean(w["cvd"] for w in ws)
            tag = " ← pivot" if rel == 0 else ""
            L.append(f"| {rel:+d}{tag} | {md:+.1f} | {_pct(cw, len(ws))} | "
                     f"{_pct(ie, len(ws))} | {mvr:.2f} | {mcvd:+.0f} |")

    seq_table(rows, "ALL reversals")
    seq_table(tops, "TOPS (→ down)")
    seq_table(bots, "BOTTOMS (→ up)")

    # ── 6. confirmed vs rejected at DECISION TIME (rel ≤ 0) ──
    rej = [r for rs in (all_rej or {}).values() for r in rs]
    if rej:
        L.append(_section("6. Confirmed vs FAILED pivots — decision-time features"))
        L.append("Features knowable by the +1 (flip) bar. If they separate confirmed "
                 "reversals from failed pivots, the pattern is PREDICTIVE. NOTE the "
                 "confirm rule (1.5×ATR retrace) is lenient, so the base rate of "
                 "'confirmed' is high — read the SEPARATION, not the absolute share.")
        L.append(f"\nConfirmed n={len(rows)} · Failed n={len(rej)}")

        def mean_of(subset, key):
            vals = [r[key] for r in subset if r.get(key) is not None]
            return statistics.mean(vals) if vals else None

        def frac(subset, pred):
            sub = [r for r in subset if r.get("delta_swing") is not None]
            return (sum(1 for r in sub if pred(r)) / len(sub)) if sub else None

        def line(metric, cval, fval, fmt="{:+.1f}", pp=False):
            if cval is None or fval is None:
                return
            if pp:
                L.append(f"| {metric} | {100*cval:.0f}% | {100*fval:.0f}% | {100*(cval-fval):+.0f}pp |")
            else:
                L.append(f"| {metric} | {fmt.format(cval)} | {fmt.format(fval)} | {fmt.format(cval-fval)} |")

        L.append("| feature | confirmed | failed | edge |")
        L.append("|---|---|---|---|")
        line("pivot vol× median", mean_of(rows, "pivot_vol_ratio"), mean_of(rej, "pivot_vol_ratio"), "{:.2f}")
        line("pivot rev_delta (into-trap)", mean_of(rows, "pivot_rev_delta"), mean_of(rej, "pivot_rev_delta"))
        line("delta_swing (the flip)", mean_of(rows, "delta_swing"), mean_of(rej, "delta_swing"))
        line("pivot wick frac", mean_of(rows, "pivot_wick_frac"), mean_of(rej, "pivot_wick_frac"), "{:.3f}")
        line("dist to nearest VP (ATR)", mean_of(rows, "dist_atr"), mean_of(rej, "dist_atr"), "{:.2f}")
        line("+1 closed reversal way", frac(rows, lambda r: r["flip_closed_rev"]),
             frac(rej, lambda r: r["flip_closed_rev"]), pp=True)
        line("+1 delta flipped to rev", frac(rows, lambda r: r["flip_rev_delta_pos"]),
             frac(rej, lambda r: r["flip_rev_delta_pos"]), pp=True)

        # combined rule sweep: pivot climax + the +1 flip
        L.append(_section("7. Combined rule — climax pivot + next-bar flip (precision)"))
        L.append("Among ALL pivots (confirmed+failed) passing each rule, what % became "
                 "real reversals? Tests the derived pattern as a tradeable filter.")
        L.append("| rule | matched | → confirmed | precision | base-rate lift |")
        L.append("|---|---|---|---|---|")
        allp = [("C", r) for r in rows] + [("F", r) for r in rej]
        base = len(rows) / len(allp) if allp else 0

        def rule_row(name, pred):
            m = [(lab, r) for lab, r in allp if r.get("delta_swing") is not None and pred(r)]
            if not m:
                L.append(f"| {name} | 0 | — | — | — |")
                return
            conf = sum(1 for lab, _ in m if lab == "C")
            prec = conf / len(m)
            L.append(f"| {name} | {len(m)} | {conf} | {100*prec:.0f}% | {prec-base:+.2f} |")

        L.append(f"| (base rate) | {len(allp)} | {len(rows)} | {100*base:.0f}% | — |")
        rule_row("vol×≥2.0", lambda r: (r.get("pivot_vol_ratio") or 0) >= 2.0)
        rule_row("+1 flip (closed rev + Δ>0)", lambda r: r["flip_closed_rev"] and r["flip_rev_delta_pos"])
        rule_row("vol×≥2.0 + +1 flip",
                 lambda r: (r.get("pivot_vol_ratio") or 0) >= 2.0 and r["flip_closed_rev"] and r["flip_rev_delta_pos"])
        rule_row("vol×≥2.0 + flip + Δswing≥80",
                 lambda r: (r.get("pivot_vol_ratio") or 0) >= 2.0 and r["flip_closed_rev"]
                           and r["flip_rev_delta_pos"] and (r.get("delta_swing") or 0) >= 80)

    return "\n".join(L)


def main():
    all_rows: dict[str, list[dict]] = {}
    all_rej: dict[str, list[dict]] = {}
    all_stats: dict[str, dict] = {}
    for sym in SYMBOLS:
        for tf in TFS:
            key = f"{sym} {tf}"
            rows, rejected, stats = scan(sym, tf)
            all_rows[key] = rows
            all_rej[key] = rejected
            all_stats[key] = stats
            out = ROOT / "data" / "reports" / f"reversals_{sym}_{tf}.jsonl"
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            print(f"{key}: {stats['bars']} bars, {stats['pivots']} pivots, "
                  f"{stats['confirmed']} confirmed reversals → {out.name}")

    report = build_report(all_rows, all_stats, all_rej)
    REPORT_OUT.write_text(report)
    print(f"\nReport → {REPORT_OUT}")
    print(report)


if __name__ == "__main__":
    main()
