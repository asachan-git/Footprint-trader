#!/usr/bin/env python3
"""Calibrate P(up) from the vote dataset (plan Part 2.2).

Reads data/strategies/vote_dataset.jsonl and answers:
  1. Base rate P(up) over the horizon.
  2. Per-vote predictiveness — when each module votes a direction, how often is it
     right? (directional accuracy + sample count). Surfaces votes that DON'T predict.
  3. Existing engine calibration — does sign(score) predict direction, and does
     |score| (→ bias_strength) correlate with accuracy? i.e. is bias a real
     confidence or noise?
  4. A logistic model P(up) on the per-vote signed contributions (pure numpy),
     time-split train/test, reported with Brier score + a calibration table
     (predicted decile vs realized). Coefficients persisted to
     config/direction_model.json.

Run: .venv/bin/python scripts/calibrate_direction.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "strategies" / "vote_dataset.jsonl"
MODEL_OUT = ROOT / "config" / "direction_model.json"


def load():
    rows = [json.loads(l) for l in DATA.read_text().splitlines() if l.strip()]
    return rows


def per_vote_predictiveness(rows):
    """For each module: of the bars where it voted long/short, how often did the
    forward move agree? Reports directional accuracy + count."""
    agg = defaultdict(lambda: {"n": 0, "correct": 0, "long": 0, "short": 0})
    for r in rows:
        up = r["fwd_up"]
        for mod, d in r.get("vote_dirs", {}).items():
            if d == 0:
                continue
            a = agg[mod]
            a["n"] += 1
            a["long" if d > 0 else "short"] += 1
            pred_up = d > 0
            if pred_up == up:
                a["correct"] += 1
    return agg


def score_calibration(rows):
    """sign(score) directional accuracy + accuracy by bias bucket."""
    n = sign_correct = 0
    by_bias = defaultdict(lambda: [0, 0])  # bias -> [n, correct]
    for r in rows:
        if r["side"] == "flat":
            continue
        up = r["fwd_up"]
        pred_up = r["score"] > 0
        n += 1
        ok = pred_up == up
        sign_correct += ok
        b = r["bias"]
        by_bias[b][0] += 1
        by_bias[b][1] += ok
    return n, sign_correct, by_bias


# ── pure-numpy logistic regression ───────────────────────────────────────────
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logistic(X, y, l2=1.0, iters=4000, lr=0.1):
    n, d = X.shape
    w = np.zeros(d)
    for _ in range(iters):
        p = _sigmoid(X @ w)
        grad = X.T @ (p - y) / n + l2 * w / n
        w -= lr * grad
    return w


def brier(p, y):
    return float(np.mean((p - y) ** 2))


def calibration_table(p, y, bins=10):
    out = []
    edges = np.linspace(0, 1, bins + 1)
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        if m.sum() == 0:
            continue
        out.append((lo, hi, int(m.sum()), float(p[m].mean()), float(y[m].mean())))
    return out


def main():
    if not DATA.exists():
        print(f"no dataset at {DATA} — run build_vote_dataset.py first"); return
    rows = load()
    print(f"=== dataset: {len(rows)} rows ===")
    up = sum(1 for r in rows if r["fwd_up"])
    base = up / len(rows)
    print(f"base rate P(up) = {100*base:.1f}%\n")

    # 1. per-vote predictiveness
    print("=== per-vote directional accuracy (>50% predicts up-when-long) ===")
    agg = per_vote_predictiveness(rows)
    print(f"{'module':22s} {'n':>5s} {'acc%':>6s} {'long':>5s} {'short':>6s}  edge_vs_base")
    for mod, a in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
        acc = 100 * a["correct"] / a["n"] if a["n"] else 0
        edge = acc - 100 * base  # vs always-predict-up baseline is base; vs 50 is coin
        print(f"{mod:22s} {a['n']:>5d} {acc:>5.1f}% {a['long']:>5d} {a['short']:>6d}  {acc-50:+.1f}pp vs coin")

    # 2. existing engine calibration
    print("\n=== existing engine: sign(score) accuracy + by bias bucket ===")
    n, sc, by_bias = score_calibration(rows)
    if n:
        print(f"sign(score) directional accuracy = {100*sc/n:.1f}%  (n={n}, coin=50%)")
        print(f"{'bias':>4s} {'n':>5s} {'acc%':>6s}")
        for b in sorted(by_bias):
            bn, bc = by_bias[b]
            print(f"{b:>4d} {bn:>5d} {100*bc/bn:>5.1f}%")
        print("(if acc% does NOT rise with bias → bias_strength is not real confidence)")

    # 3. logistic model on per-vote signed contributions
    modules = sorted({m for r in rows for m in r.get("votes", {})})
    X = np.array([[r["votes"].get(m, 0.0) for m in modules] for r in rows], dtype=float)
    y = np.array([1.0 if r["fwd_up"] else 0.0 for r in rows])
    X = np.hstack([np.ones((len(X), 1)), X])  # intercept
    # time split 70/30 (rows are already in chronological order per symbol; sort by ts)
    order = np.argsort([r["ts"] for r in rows])
    X, y = X[order], y[order]
    cut = int(0.7 * len(X))
    Xtr, ytr, Xte, yte = X[:cut], y[:cut], X[cut:], y[cut:]
    w = fit_logistic(Xtr, ytr)
    ptr, pte = _sigmoid(Xtr @ w), _sigmoid(Xte @ w)
    print(f"\n=== logistic P(up) on votes (train {len(Xtr)} / test {len(Xte)}) ===")
    print(f"Brier  train={brier(ptr,ytr):.4f}  test={brier(pte,yte):.4f}  (base={brier(np.full_like(yte, base), yte):.4f})")
    acc_te = float(np.mean((pte > 0.5) == (yte > 0.5)))
    print(f"test accuracy (P>0.5)= {100*acc_te:.1f}%")
    print("\ncoefficients (|w| = influence on P(up)):")
    names = ["intercept"] + modules
    for nm, wi in sorted(zip(names, w), key=lambda x: -abs(x[1])):
        print(f"  {nm:22s} {wi:+.3f}")
    print("\ntest calibration (pred decile → realized P(up)):")
    print(f"{'lo':>5s}-{'hi':<5s} {'n':>5s} {'pred':>6s} {'real':>6s}")
    for lo, hi, cnt, pm, ym in calibration_table(pte, yte):
        print(f"{lo:>4.1f}-{hi:<4.1f} {cnt:>5d} {100*pm:>5.1f}% {100*ym:>5.1f}%")

    MODEL_OUT.write_text(json.dumps({
        "horizon_bars": 6,
        "modules": modules,
        "weights": {nm: float(wi) for nm, wi in zip(names, w)},
        "base_rate_up": base,
        "brier_test": brier(pte, yte),
    }, indent=2))
    print(f"\nwrote model → {MODEL_OUT}")


if __name__ == "__main__":
    main()
