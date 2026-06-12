#!/usr/bin/env python3
"""Entry-quality model — can causal pre-trade features predict a good entry?

Trains on data/reports/decision_dataset.csv (one row per closed position).

TARGET (default 'followthrough'):
  followthrough = mfe_r >= 0.30R  — did price run in our favor enough to matter,
                                     independent of where the exit landed. This is
                                     the ENTRY-quality signal (the 52%-noise lever).
  win           = realized_r > 0   — depends on exits/TP too (secondary).

FEATURES — strictly CAUSAL (known at/before entry). NO outcome leakage: realized_r,
mfe_r, mae_r, capture, exit_reason, duration, win are all withheld from X.

Two feature sets:
  A) full        — incl. strategy/symbol/tp_source (which-strategy can itself be a filter)
  B) conditions  — market conditions only (generalizable, no strategy identity)

Honesty: small n (~544), paper data. We report BOTH random StratifiedKFold AUC
(optimistic — leaks regime across time) and TimeSeriesSplit AUC (the real test:
train past → predict future). A shallow tree gives human-readable rules, and an
out-of-fold "filter lift" shows the book IF we'd only taken model-approved entries.

    PYTHONPATH=. .venv/bin/python -u scripts/entry_model.py [followthrough|win]
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.linear_model import LogisticRegression
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import StratifiedKFold, TimeSeriesSplit, cross_val_predict
from sklearn.metrics import roc_auc_score
from sklearn.inspection import permutation_importance

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "reports" / "decision_dataset.csv"
OUT = ROOT / "data" / "reports" / "entry_model.md"

CAT_FULL = ["strategy", "symbol", "side", "regime", "session", "cvd_div_last",
            "tp_source", "with_trend", "cvd_div_aligned", "bias_strength"]
CAT_COND = ["side", "regime", "session", "cvd_div_last",
            "with_trend", "cvd_div_aligned", "bias_strength"]
NUM = ["slope_atr", "atr_pct", "utc_hour", "entry_delta_sign", "range_pos"]


def make_pre(cats):
    return ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore"), cats),
        ("num", Pipeline([("imp", SimpleImputer(strategy="median")),
                          ("sc", StandardScaler())]), NUM),
    ])


def models(cats):
    return {
        "logreg": Pipeline([("pre", make_pre(cats)),
                            ("clf", LogisticRegression(max_iter=2000, C=0.5))]),
        "tree(d3)": Pipeline([("pre", make_pre(cats)),
                              ("clf", DecisionTreeClassifier(max_depth=3, min_samples_leaf=20,
                                                            random_state=0))]),
        "gbm": Pipeline([("pre", make_pre(cats)),
                         ("clf", GradientBoostingClassifier(n_estimators=120, max_depth=2,
                                                           learning_rate=0.05, random_state=0))]),
    }


def cv_auc(model, X, y, splitter):
    aucs = []
    for tr, te in splitter.split(X, y):
        if len(np.unique(y.iloc[tr])) < 2 or len(np.unique(y.iloc[te])) < 2:
            continue
        model.fit(X.iloc[tr], y.iloc[tr])
        p = model.predict_proba(X.iloc[te])[:, 1]
        aucs.append(roc_auc_score(y.iloc[te], p))
    return (np.mean(aucs), np.std(aucs)) if aucs else (float("nan"), float("nan"))


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "followthrough"
    df = pd.read_csv(CSV)
    df = df.sort_values("opened_ts").reset_index(drop=True)   # temporal order for TS split

    if target == "followthrough":
        y = (df["mfe_r"] >= 0.30).astype(int)
    elif target == "win":
        y = (df["realized_r"] > 0).astype(int)
    else:
        raise SystemExit("target must be followthrough|win")

    # categoricals → string (NaN→'na'); keep only present columns
    for c in set(CAT_FULL):
        if c in df.columns:
            df[c] = df[c].astype("object").where(df[c].notna(), "na").astype(str)

    base = y.mean()
    lines = []
    def out(s=""):
        print(s); lines.append(s)

    out("=" * 78)
    out(f"ENTRY-QUALITY MODEL — target='{target}'  (1 = "
        f"{'mfe_r>=0.30R' if target=='followthrough' else 'realized_r>0'})")
    out("=" * 78)
    out(f"n={len(df)}  positive rate (base)={base:.1%}  "
        f"→ a model must beat AUC 0.50 / accuracy {max(base,1-base):.1%}")
    out("")

    skf = StratifiedKFold(5, shuffle=True, random_state=0)
    tss = TimeSeriesSplit(5)

    for setname, cats in [("A: full (+strategy/symbol)", CAT_FULL),
                          ("B: conditions-only", CAT_COND)]:
        cats = [c for c in cats if c in df.columns]
        X = df[cats + NUM]
        out(f"── feature set {setname} ──")
        out(f"  {'model':<10} {'randomCV AUC':>16} {'timeseriesCV AUC':>20}")
        for name, m in models(cats).items():
            r_m, r_s = cv_auc(m, X, y, skf)
            t_m, t_s = cv_auc(m, X, y, tss)
            out(f"  {name:<10} {r_m:>8.3f}±{r_s:<5.3f}   {t_m:>8.3f}±{t_s:<5.3f}")
        out("")

    # ── interpretable rules: shallow tree on conditions-only ─────────────────
    cats = [c for c in CAT_COND if c in df.columns]
    X = df[cats + NUM]
    pre = make_pre(cats)
    Xt = pre.fit_transform(X)
    names = list(pre.named_transformers_["cat"].get_feature_names_out(cats)) + NUM
    tree = DecisionTreeClassifier(max_depth=3, min_samples_leaf=25, random_state=0).fit(Xt, y)
    out("── decision rules (depth-3 tree, conditions-only) ──")
    for ln in export_text(tree, feature_names=names).splitlines():
        out("  " + ln)
    out("")

    # ── permutation importance (GBM, full set) ───────────────────────────────
    cats = [c for c in CAT_FULL if c in df.columns]
    X = df[cats + NUM]
    gbm = models(cats)["gbm"].fit(X, y)
    pi = permutation_importance(gbm, X, y, n_repeats=10, random_state=0, scoring="roc_auc")
    imp = sorted(zip(cats + NUM, pi.importances_mean), key=lambda t: -t[1])
    out("── feature importance (permutation, GBM full set, Δ AUC) ──")
    for n, v in imp[:12]:
        bar = "█" * int(max(v, 0) * 200)
        out(f"  {n:<18} {v:+.4f}  {bar}")
    out("")

    # ── filter lift: out-of-fold proba → keep top entries, show the book ─────
    oof = cross_val_predict(gbm, X, y, cv=skf, method="predict_proba")[:, 1]
    df["_p"] = oof
    out("── FILTER LIFT (out-of-fold; book IF we only took model-approved entries) ──")
    out(f"  {'threshold':<22} {'kept':>6} {'WR':>5} {'sumR':>8} {'avgR':>7} {'pos-rate':>9}")
    full_sum = df["realized_r"].sum()
    for label, mask in [
        ("ALL (no filter)", pd.Series(True, index=df.index)),
        ("proba >= 0.50", df["_p"] >= 0.50),
        ("proba >= 0.60", df["_p"] >= 0.60),
        ("top 50% by proba", df["_p"] >= df["_p"].median()),
        ("top 33% by proba", df["_p"] >= df["_p"].quantile(0.67)),
    ]:
        sub = df[mask]
        if len(sub) == 0:
            continue
        wr = (sub["realized_r"] > 0).mean()
        sr = sub["realized_r"].sum()
        pr = y[sub.index].mean()
        out(f"  {label:<22} {len(sub):>6} {wr:>4.0%} {sr:>+8.1f} "
            f"{sub['realized_r'].mean():>+7.2f} {pr:>8.0%}")
    out(f"\n  (full book = {full_sum:+.1f}R over {len(df)} trades; "
        f"a useful filter keeps most sumR with far fewer trades)")

    OUT.write_text("\n".join(lines))
    out(f"\n[written] {OUT}")


if __name__ == "__main__":
    main()
