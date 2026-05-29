# FootprintBiot — Strategy Plan

Last updated: 2026-05-28

---

## Account

**Vantage USC** — nano lot sizing, $99k balance, 500× leverage.  
Edge: nano lots allow holding drawdown through chop and exiting on recovery. No hard SL needed.

---

## Core Strategy

Grid recovery. Entry → 5-leg Fib grid. No hard SL.

1. Direction signal fires (M2 rule engine or M1 Claude)
2. Leg 1 fills at signal bar close
3. Legs 2–5 stored as pending limits (ATR-step spacing)
4. Price moves against → legs fill, averaging cost down/up
5. Price recovers to TP → close all legs, book net profit
6. ChoCh confirmed → close cycle at loss, re-enter fresh

**One cycle per symbol at a time. Never stack same-direction grids.**

---

## Direction Engine

| Symbol | Engine | Status | Reasoning |
|--------|--------|--------|-----------|
| XAUTUSDT | M2 rules (CVD + structure + FVG + sweep) | Paper (A/B) | 73% WR vs M1's 42% in backtest |
| BTCUSDT | M1 Claude | Live | 75% WR, acceptable |

Decision: after 2 weeks of M2 paper data, compare `data/positions_m2.jsonl` vs `data/positions.jsonl`.  
If M2 outperforms on XAUT → switch XAUT to M2 live.

---

## Grid Parameters

```
Step:      0.7 × ATR_15m     (wider than 0.5× — XAUT chop depth ~3.2×ATR)
Legs:      5                  (Fib ladder [1,1,2,3,5] × base_lot × bias/5)
Hard SL:   NONE               (nano lots — hold and recover)
Soft stop: ChoCh confirmation  (market structure invalidation)
TP:        VP POC primary → VAH/VAL → 2×ATR_15m fallback
Max open:  1 cycle / symbol
```

---

## Entry Filter

- Rule score: `|score| ≥ 0.5`, `bias_strength ≥ 2`
- Session: active hours only (skip Asia dead zone 00:00–06:00 UTC for XAUT)
- Cooldown: 3 bars after any close before same-direction re-entry

---

## Cycle Management

| Condition | Action |
|-----------|--------|
| Drawdown > 2×ATR_15m | Tighten TP to POC only (no extension) |
| ChoCh confirmed | Close full cycle, wait 3 bars, re-assess |
| Leg 1 TP hit | Book partial (~0.3R), trail legs 2–5 to next VP level |
| All legs filled, no ChoCh | Hold. Recovery grid is working as intended. |

---

## Key Learnings (from M1 vs M2 A/B, 2026-05-28)

### What the data showed

| | BTCUSDT | XAUTUSDT |
|---|---|---|
| M1 total RR | +2.68 | **-13.02** |
| M1 win rate | 75% | 42% |
| M2 sim total RR | +7.02 | +1.81 |
| M2 sim win rate | 73% | **73%** |

### Root causes of M1 XAUT failure
1. **Direction quality** — Claude called wrong direction 58% of the time on XAUT
2. **Over-trading** — 57 entries during chop where M2 stayed flat
3. **Fixed ATR TP too tight** — 16 timeout trades (stalled between entry and TP for 5hrs)
4. **Wide SL** — 24 SL hits at -1.0R each; 5×ATR_15m SL correct but direction was wrong

### Implications for USC strategy
- Direction quality dominates. If you enter the wrong way, grid recovery just defers the loss.
- With no hard SL: the question shifts to "how many wrong-direction entries before ChoCh?"
- VP-anchored TP is essential — price gravitates to POC, not to fixed ATR distances.
- XAUT needs M2 direction. M1 Claude lacks the structural pattern recognition for gold.

---

## Metrics to Track

Run weekly: `python scripts/ab_analysis.py`

| Metric | Target |
|--------|--------|
| M2 XAUT win rate | ≥ 65% |
| Avg RR capture | ≥ 50% of shown RR |
| Timeout rate | ≤ 15% of active signals |
| Max cycle drawdown | ≤ 3×ATR_15m before recovery |
| ChoCh false positives | ≤ 20% (ChoCh fires but price recovers after) |

---

## Post-restart M1 live (72 trades, 2026-05-29)

| Metric | Value |
|---|---|
| Total R | **−14.61** |
| Win rate | 58 % (42 W / 30 L) |
| Avg win | +0.37 R |
| Avg loss | −1.00 R |
| Expectancy | −0.20 R / trade |
| Single-leg cycles | 66 / 72 (grid never DCA'd) |

Per symbol/side: BTC long −8.62 R (worst), BTC short −2.64 R, XAU long −3.03 R, XAU short −0.32 R.

### Implications

- **Grid never activated**: 66/72 single-leg → legs 2-5 placed via ATR-step at non-S/R prices, price never retraced to fill.
- **Hard SL still firing**: ~30 sl_hit exits at exactly −1.00 R. Plan said no-SL but code kept it tight.
- **TP shrinker truncating wins**: avg win 0.37 R << target 1.5 R. cycle_manager shrinks TP after every fill regardless.

## Roadmap

### Immediate (this week) — applied 2026-05-29

- [x] Drop ATR-step fallback in `grid_placer.py`. Legs must land on confluence zones (VP/HVN/swings/FVG/sweep/CVD) returned by `zone_collector.collect`. Request 2× legs from collector, raise `min_gap` to 0.10×ATR. Fewer legs ok if zones sparse.
- [x] SL → **disaster floor only**: SL distance ≥ 3 % BTC / 1.5 % XAU. Closer SLs ignored by `ingest._sl_hit` check; placement floor in `grid_placer.safety_sl`.
- [ ] Disable / gate cycle_tp shrinker for single-leg trades — let leg-1 winners reach 1.5×ATR.
- [ ] Restart stack → M2 paper mode active (`data/positions_m2.jsonl`)
- [ ] Complete Bybit linear XAUUSDT switch (`--symbol XAUUSDT --symbol-as XAUTUSDT --category linear`)

### Next (after 1 week M2 paper data)
- [ ] Compare M1 vs M2 paper on XAUT using `ab_analysis.py`
- [ ] If M2 XAUT paper WR ≥ 65%: switch XAUT direction engine to M2 live
- [ ] ChoCh cycle invalidation: wire `direction_engine.py` ChoCh signal to `cycle_manager.py`

### Deferred
- [ ] VP redesign (VA collapse + HVN/LVN) — see `project_vp_redesign.md`
- [ ] COMEX GC data ($30/mo) for real gold futures footprint
- [ ] Elliott Wave for M2 (low ROI per memory notes)
