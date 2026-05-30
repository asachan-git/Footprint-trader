#!/usr/bin/env python3
"""Tier 0.1 — M2 counterfactual analysis.

For every M1 (Claude) decision that produced a closed cycle, look up what M2
(rules engine) said at the same bar_id. Answer:

  - When M1 lost, did M2 agree (same wrong direction) or disagree (would've
    avoided)?
  - When M1 won, did M2 also fire same direction?

Decision matrix per closed cycle:
    M1 side vs M2 side  ×  M1 outcome (win/loss)
    → agreement_win / agreement_loss / disagreement_win / disagreement_loss

If disagreement_loss / total_loss > 60% → M2 is real edge, switch
If agreement_loss / total_loss > 60% → M2 has same blindness, switch is trap
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = ROOT / "data" / "decisions.jsonl"
POSITIONS = ROOT / "data" / "positions.jsonl"
CYCLES = ROOT / "data" / "cycles.jsonl"
MODE_COMPARE = ROOT / "data" / "mode_compare.jsonl"


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.open() if l.strip()] if p.exists() else []


def main() -> None:
    decisions = load_jsonl(DECISIONS)
    positions = load_jsonl(POSITIONS)
    cycles = load_jsonl(CYCLES)
    m2 = load_jsonl(MODE_COMPARE)

    # M1 decisions by bar_id (last decision wins if duplicate)
    m1_by_bar: dict[str, dict] = {}
    for d in decisions:
        bar_id = d.get("bar_id")
        if not bar_id:
            continue
        side = (d.get("decision") or {}).get("side")
        if side not in ("long", "short"):
            continue
        m1_by_bar[bar_id] = {
            "side": side,
            "confidence": (d.get("decision") or {}).get("confidence", 0),
            "symbol": d.get("symbol"),
            "ts": d.get("ts"),
        }

    # M2 votes by bar_id
    m2_by_bar: dict[str, dict] = {}
    for m in m2:
        bar_id = m.get("bar_id")
        if not bar_id:
            continue
        m2_by_bar[bar_id] = {
            "side": m.get("side"),
            "score": m.get("score", 0),
            "bias": m.get("bias_strength", 0),
            "symbol": m.get("symbol"),
        }

    # Cycle PnL: cycle_id -> realized_pnl
    cycle_pnl: dict[str, float] = {}
    cycle_pos: dict[str, str] = {}  # cycle_id -> position_id
    for ev in cycles:
        cid = ev.get("cycle_id")
        if ev.get("type") == "open":
            cycle_pos[cid] = ev.get("position_id")
        elif ev.get("type") == "close" and ev.get("realized_pnl") is not None:
            cycle_pnl[cid] = ev["realized_pnl"]

    # Position open by position_id → bar_id (link to M1 decision)
    pos_bar: dict[str, str] = {}
    pos_symbol: dict[str, str] = {}
    pos_side: dict[str, str] = {}
    for ev in positions:
        if ev.get("type") == "open":
            pos_bar[ev["position_id"]] = ev.get("bar_id")
            pos_symbol[ev["position_id"]] = ev.get("symbol")
            pos_side[ev["position_id"]] = ev.get("side")

    # Build joined records
    rows = []
    for cid, pnl in cycle_pnl.items():
        pid = cycle_pos.get(cid)
        if not pid:
            continue
        bar_id = pos_bar.get(pid)
        if not bar_id:
            continue
        m1 = m1_by_bar.get(bar_id)
        m2v = m2_by_bar.get(bar_id)
        if not m1 or not m2v:
            continue
        rows.append({
            "cycle_id": cid,
            "symbol": pos_symbol.get(pid),
            "bar_id": bar_id,
            "m1_side": m1["side"],
            "m1_conf": m1["confidence"],
            "m2_side": m2v["side"],
            "m2_score": m2v["score"],
            "pnl": pnl,
        })

    if not rows:
        print("No joined records. Check that decisions.jsonl, positions.jsonl, cycles.jsonl, mode_compare.jsonl all overlap in time.")
        return

    # Aggregate
    matrix = defaultdict(lambda: {"win": 0, "loss": 0, "sum_r": 0.0})
    per_sym = defaultdict(lambda: defaultdict(lambda: {"win": 0, "loss": 0, "sum_r": 0.0}))

    for r in rows:
        agree = (r["m1_side"] == r["m2_side"])
        m2_flat = r["m2_side"] == "flat"
        if m2_flat:
            cat = "m2_flat"
        elif agree:
            cat = "agree"
        else:
            cat = "disagree"
        outcome = "win" if r["pnl"] > 0 else "loss"
        matrix[cat][outcome] += 1
        matrix[cat]["sum_r"] += r["pnl"]
        per_sym[r["symbol"]][cat][outcome] += 1
        per_sym[r["symbol"]][cat]["sum_r"] += r["pnl"]

    # Output
    print("=" * 70)
    print("M2 COUNTERFACTUAL ANALYSIS")
    print("=" * 70)
    print(f"Joined records: {len(rows)} closed cycles with M1 + M2 votes")
    print()

    print("Overall matrix:")
    print(f"  {'category':<20} {'wins':>5} {'losses':>7} {'WR':>6} {'sum R':>8}")
    for cat in ("agree", "disagree", "m2_flat"):
        m = matrix[cat]
        total = m["win"] + m["loss"]
        wr = m["win"] / total * 100 if total else 0
        print(f"  {cat:<20} {m['win']:>5} {m['loss']:>7} {wr:>5.1f}% {m['sum_r']:>+8.2f}")
    print()

    # The decision metric
    total_losses = sum(matrix[c]["loss"] for c in matrix)
    total_wins = sum(matrix[c]["win"] for c in matrix)
    if total_losses > 0:
        disagree_loss_pct = matrix["disagree"]["loss"] / total_losses * 100
        agree_loss_pct = matrix["agree"]["loss"] / total_losses * 100
        flat_loss_pct = matrix["m2_flat"]["loss"] / total_losses * 100
        print("LOSS ATTRIBUTION (would M2 have saved us?):")
        print(f"  M2 agreed (would've also lost) : {matrix['agree']['loss']:>3}/{total_losses}  ({agree_loss_pct:.1f}%)")
        print(f"  M2 disagreed (would've avoided) : {matrix['disagree']['loss']:>3}/{total_losses}  ({disagree_loss_pct:.1f}%)")
        print(f"  M2 flat (would've skipped)      : {matrix['m2_flat']['loss']:>3}/{total_losses}  ({flat_loss_pct:.1f}%)")
        print()

    if total_wins > 0:
        agree_win_pct = matrix["agree"]["win"] / total_wins * 100
        disagree_win_pct = matrix["disagree"]["win"] / total_wins * 100
        flat_win_pct = matrix["m2_flat"]["win"] / total_wins * 100
        print("WIN ATTRIBUTION (would M2 have missed?):")
        print(f"  M2 agreed (would've also won)   : {matrix['agree']['win']:>3}/{total_wins}  ({agree_win_pct:.1f}%)")
        print(f"  M2 disagreed (would've missed)  : {matrix['disagree']['win']:>3}/{total_wins}  ({disagree_win_pct:.1f}%)")
        print(f"  M2 flat (would've skipped)      : {matrix['m2_flat']['win']:>3}/{total_wins}  ({flat_win_pct:.1f}%)")
        print()

    print("Per symbol:")
    for sym in sorted(per_sym):
        print(f"\n  {sym}:")
        for cat in ("agree", "disagree", "m2_flat"):
            m = per_sym[sym][cat]
            total = m["win"] + m["loss"]
            wr = m["win"] / total * 100 if total else 0
            print(f"    {cat:<12} W={m['win']:>3} L={m['loss']:>3} WR={wr:>5.1f}% sumR={m['sum_r']:+6.2f}")

    # Counterfactual "if we had only fired M2 trades"
    print()
    print("=" * 70)
    print("COUNTERFACTUAL: if M2 was the gate (M1 still chose side)")
    print("=" * 70)
    cf_r = 0.0
    cf_n = 0
    for r in rows:
        if r["m1_side"] == r["m2_side"]:  # M2 agreed → trade would've fired
            cf_r += r["pnl"]
            cf_n += 1
    print(f"Trades that would have fired (M1 == M2): {cf_n}")
    print(f"Counterfactual sum R: {cf_r:+.2f}")
    actual_r = sum(r["pnl"] for r in rows)
    print(f"Actual sum R         : {actual_r:+.2f}")
    delta = cf_r - actual_r
    print(f"Delta (M2-gate vs all): {delta:+.2f}R  ({'BETTER' if delta > 0 else 'WORSE'} with M2 as gate)")


if __name__ == "__main__":
    main()
