# FootprintBiot — Strategy Plan

Last updated: 2026-05-31

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

### Scale-out / partial booking — BUILT behind flag (2026-05-30, default OFF)

`cycle.scale_out.enabled` (settings.yaml). On first bounce ≥`profit_buf_atr`×ATR beyond avg_entry: bank the deep large-lot legs (filled cheap → in profit), keep `keep_near`(=2) nearest legs, move them to break-even. Converts bounce-then-die cycles into scratches.

Validated on `scripts/grid_sim.py` (faithful multi-leg sim, real M2 signals): vs hold-all → WR 74%→89%, escapes 50→25, timeouts 49→12, exposure ~halved, peak inventory down; total R −33% (caps runners). Risk-reducer, not return-improver — matches "consistent profit + avoid adverse markets."

Code: `position_store.scale_out_position()` (+ `scale_out` event, `scaled_out` flag, replay-tested), `cycle_manager._check_scale_out()` (in `on_bar_close`). Partial R = cycle denom (avg−SL) × banked-leg fraction, additive with close R. **TODO before live enable:** MetaAPI broker partial-close sync (current path = store/paper only). A/B on paper first.

Other 2026-05-30 findings: footprint stacked-imbalance has no edge (`stacked_edge_scan.py`); adverse-regime gate built then REFUTED on real signals (counter-trend = best M2 trades) and disabled (`ADVERSE_GATE_ENABLED=False`, `gate_test.py`).

---

## Key Learnings (from M1 vs M2 A/B, 2026-05-28)

### What the data showed (bar-verified, sentinel bug fixed 2026-05-30)

**Previous -13R XAUT figure was a data bug** — every `sl_hit` stored as -1.0R sentinel regardless of
trailing SL position. `bar_verify_outcomes.py` walked 1m bars to recompute actual exit R.

| | BTCUSDT | XAUTUSDT | Combined |
|---|---|---|---|
| M1 bar-verified total R | +1.39 | +4.41 | **+5.80** |
| M1 bar-verified WR | 94% | 96% | **95%** |
| M1 avg R/trade | +0.045 | +0.056 | **+0.053** |
| M2 synthetic total R (same period) | +11.21 | +16.38 | **+27.59** |
| M2 synthetic WR | 74% | 87% | **79%** |
| M2 avg R/trade | +0.078 | +0.155 | **+0.110** |

Note: M2 synthetic has 67/250 trades `hit=open` (valued at bar-data cutoff close — slight upward bias).
M2 fired 250 signals vs M1's 110 in the same 3-day window (May 28–30).

### Root causes of apparent M1 underperformance (now known to be data artifact)
1. **Sentinel bug** — `sl_hit` events wrote -1.0R flat; 40 positions affected; actual avg = +0.026R
2. **Post-fix period only** — data covers May 28–30 after disaster-floor SL + TP fixes applied
3. **Grid never activated** — 93.5% single-leg; grid architecture is dead weight

### Remaining real problems (from MAE/MFE analysis)
- **Entry quality weak** — avg MFE 0.124R; 71% of trades peak below 0.15R (noise entries)
- **TP capture low** — 54% capture ratio on wins; avg MFE 0.125R but avg realized 0.068R
- **BTC worst** — avg MFE 0.079R, 68% noise entries
- **M2 avg R 2.1× higher** than M1 — M2 direction + TP targeting meaningfully better

### Implications for USC strategy
- Sentinel data bug masked real performance — M1 post-fix is actually +95% WR, +5.80R/3 days
- M2 direction + TP resolution still better per-trade; switch XAUT to M2 paper → live is the right path
- Entry filter is now the biggest lever: 71% noise entries → Tier 1.6 / 2B.1 first
- Grid is dead (93.5% single-leg) → Tier 6 architecture review after entry filter lands

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

## Post-fix M1 live (15 trades, 2026-05-29 → 2026-05-30)

After applying disaster-floor SL + confluence-only grid + R-math fix + trail disable:

| Metric | Value |
|---|---|
| Total R | **+2.48** |
| Win rate | 100 % (15 / 15) |
| Avg R | +0.17 |
| BTC | +1.79 R / 7 trades |
| XAU | +0.70 R / 8 trades |
| Single-leg | 14 / 15 |
| Exit reasons | 10 tp_hit + 5 sl_hit (all positive R; trailed-SL profit locks) |

**Remaining gap:** avg +0.17R = tiny. TP placed too close (1.5×ATR). Action: bumped to 2.0×ATR (range) / 2.5×ATR (directional) / 1.0×ATR (cautious). Confluence loosened (n×3 candidates, min_gap 0.05×ATR) — first XAU short post-fix queued 3 legs vs prior 1.

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

## Roadmap — Consolidated Priority (2026-05-29)

**Cardinal rule:** One feature at a time. Replay-validate first → paper-validate second → promote third. Measure expectancy delta at every step. Boring discipline > velocity.

**Data foundation status:** Binance native aggressor validated for BTC + XAU end-to-end (ingest, storage, dashboard). Bybit/Exness pipelines dormant. No data-integrity unknowns blocking work.

---

### TIER 0 — Validation (do BEFORE building anything)

Existing data only. Answers which problem is real before any new code.

- [~] **0.1** M2 counterfactual query — `scripts/m2_counterfactual.py` scaffolded; not run yet (0.5d)
- [~] **0.2** MAE/MFE backfill — `scripts/mae_mfe_backfill.py` scaffolded; not run yet (0.5d)
- [~] **0.3** `cycles.jsonl` aggregate — `scripts/cycles_stats.py` scaffolded; not run yet (0.5h)
- [ ] **0.4** `decisions.jsonl` rationale clustering on losing trades — what patterns Claude consistently misreads (0.5d)

**Decision gates after Tier 0:**
- M2 disagrees > 60% on M1 losses → switch XAU to M2 now, skip more paper
- MFE >> realized R → TP problem, branch to Tier 2A
- MFE ≈ realized R → direction problem, branch to Tier 2B
- 90%+ single-leg cycles → drop grid, accelerate Tier 6

---

### TIER 1 — Defense + Infrastructure

Protects everything downstream. Unblocks fast iteration. Required before more features.

- [x] **1.1** Hard equity kill switch in `router.py` — refuse dispatch if today's sum_R < −5R, manual reset only (1h)
  — implemented router.py:219-241, floor=-4R from settings.yaml risk.daily.max_dd_r
- [x] **1.2** Pending order JSONL persistence — `data/pending_orders.jsonl` writes confirmed (2h)
- [x] **1.3** Disable cycle_tp shrinker for single-leg trades — gated `cycle_manager.py:89` (`leg_count >= 2`) (2h)
- [~] **1.4** `scripts/replay.py` — scaffolded; not run yet (2-3d) — **unblocks all future feature work**
- [x] **1.5** `/health` route — `server/routes/health.py` live (1h)
- [x] **1.6** Cost-aware pre-filter on `/decide` — `execution/pre_filter.py` wired in `decide.py:121` + `grid_tick.py:103` (2h)

---

### TIER 2 — Branch on Tier 0 result

#### Tier 2A — If TP problem dominant

- [x] **2A.1** POC-trail TP in `tp_resolver.py` — trail TP toward POC as cycle ages instead of shrink-on-fill (1d)
  — extended poc_trail in cycle_manager.py: now trails both directions (tighten if POC closer, extend if POC drifted farther, capped at poc_trail_max_ext_atr_mult×ATR=3.0)
- [ ] **2A.2** Re-measure 1 week, target avg win ≥ 0.8R (passive)
- [~] **2A.3** Partial profit-booking / scale-out — SIMULATED, decision pending (2026-05-30)
  — `scripts/grid_sim.py` (grid-aware multi-leg cycle sim: Fib legs avg-down, disaster SL, progressive TP, trend-escape; walks real 1m paths) + `scripts/partial_tp_test.py` (single-entry proxy). Tested on real M2 signals (mode_compare, 268 cycles). Partial booking is a **risk-reducer, not a return-improver**:
    - baseline (hold all): WR74% avgR+0.115 totR+30.9 avgLoss−0.152 peakLots6.7 bars335 | tp169 esc50 to49
    - conservative 33% (book frac of every leg on bounce): WR76% avgR+0.100 totR+26.9 — barely changes outcomes, not worth it.
    - **aggressive keep2** (dump deep large-lot legs on bounce + remainder to break-even): WR89% avgR+0.077 totR+20.5 avgLoss−0.187 peakLots5.8 bars162 | tp99 esc25 to12 be132. Converts adverse-market deaths→break-even (escape 50→25, timeout 49→12), halves exposure time, cuts inventory. Cost: caps runners → −33% total R.
  - Tradeoff: baseline = max money/fattest losses/most blowups; aggressive keep2 = max consistency+survival at ⅓ less return. Matches stated aim (consistent profit + avoid adverse markets). Caveats: BE-stop assumes clean fills; one favorable epoch (May 28-30); idealized one-shot bounce timing. NOT yet wired into `cycle_manager` — decision pending.

#### Tier 2B — If direction problem dominant

- [x] **2B.1** `pipeline/features/stacked_imbalance.py` — walk ladder, N≥10 consecutive levels with ask/bid ≥ 2.5, proximity ≤1×ATR, span ≥0.15×ATR
  — initial hard gate broken (wrong side labels). Demoted to vote (weight 0.60) after replay showed hard gate blocked 47% signals with no WR discrimination.
- [x] **2B.2** Validate on replay tester (hours)
  — combined_gates replay + M2 signal cross-reference done. Hard gate: pass=WR68% vs veto=WR68% (useless). Vote (tightened): pass=WR70%/+0.067R vs influenced=WR64%/+0.036R. BTC: influenced avgR=−0.027R (negative — real signal).
- [x] **2B.3** `pipeline/features/delta_divergence.py` — price breaks 15-bar window high/low, delta fails to confirm; weight 0.50 vote in direction_engine
- [x] **2B.4** `pipeline/features/unfinished_auction.py` — single-print at bar extreme, min_ratio=8; weight 0.40 vote in direction_engine (up→bullish, down→bearish)
- [ ] **2B.5** `pipeline/features/poc_drift.py` — intraday per-bar POC migration. Replaces static `vp_shape` vote (1d)
- [ ] **2B.6** Volume confirmation gate in `decision_validator.py` — entry zone vol ≥ 1.5× 10-bar avg (0.5d)
- [~] **2B.7** Adverse-regime gate in `direction_engine.py` — BUILT then DISABLED, REFUTED on real signals (2026-05-30)
  — `_trend_regime()` + counter-trend veto coded behind `ADVERSE_GATE_ENABLED` (now False). Synthetic all-bars scan said counter-trend loses; `scripts/gate_test.py` on REAL M2 signals (mode_compare, 263 trades) showed the opposite — counter-trend = best trades (WR82% +0.123R vs kept +0.038R), gate made avgR WORSE (+0.049→+0.038). M2 already handles regime via votes; `grid_modes.py` has an explicit counter-trend-exhaustion mode. Lesson: validate gates on real signals, not all-bars. Do NOT re-enable without real-signal re-validation.

**Stacked-imbalance investigation — CLOSED (2026-05-30, `scripts/stacked_edge_scan.py`):**
Re-bucketed stored $0.1 ladders to candidate cell widths (no re-ingest), 11d agg 15m, both symbols.
- **Granularity hypothesis REFUTED.** BTC footprint already $10 cells (not 0.1 — only XAU is $0.1). Coarsening (BTC $25 / XAU $1) made discrimination *worse*; native already best.
- **Acceptance filter (close-beyond-zone) shows NO edge.** Rejected ≥ accepted in every granularity AND every regime cell. On BTC, acceptance is a consistent *anti*-signal. The observed "reaction" was a ~0.08 ATR scalp skew that washes out / flips sign under regime slicing → noise.
- **Only stable edge = regime alignment** (with-trend vs counter-trend, n=40–65/cell):
  - with-trend: BTC +0.014R (WR74%) · XAU +0.082R (WR80%)
  - counter-trend: BTC −0.052R · XAU −0.040R  ← the adverse market to gate
- Action: stacked/delta/unfinished votes stay demoted (already weak weights); built 2B.7 adverse gate instead. Caveat: 11d, one regime epoch, n thin — counter-trend=bad is the robust (negative) finding; act by *avoiding*, confirm with-trend magnitude on more live data.

---

### TIER 3 — VP/Sweep upgrades

- [x] **3.1** `pipeline/features/naked_poc.py` — file created; not wired as TP magnet yet (0.5d)
  — file exists, wired in tp_resolver.py:26-35 as TP candidate source
- [x] **3.2** Upgrade `sweep.py` classification — `reversal_high/low` / `liquidity_grab` / `failed_sweep` / `stop_run`. Each gets different downstream action (1d)
  — reversal/liquidity_grab/stop_run/failed_sweep implemented in sweep.py
- [ ] **3.3** VA width tracking in `vp_cache.py` — 3-bar expansion/contraction = regime classifier (0.5d)
- [ ] **3.4** `pipeline/features/lvn_trade.py` — LVN traverse + same-side delta + vol > avg = continuation entry (1d)
- [ ] **3.5** Combo gate layer above feature layer — patterns from VP playbook (continuation, exhaustion, reversion, breakout, trap) as gates, single-pattern hits get vetoed (2-3d)

---

### TIER 4 — Adaptive / self-tuning

Requires Tier 0-2 baseline + replay tester + sufficient closed positions (≥ 200).

- [ ] **4.1** `eval/feature_attribution.py` — per-position realized_R attribution to active features. Rolling 100-trade window re-weights M2 votes (1-2d)
- [ ] **4.2** Per-symbol M2 weight configs (`config/m2_weights_BTCUSDT.yaml`, `_XAUTUSDT.yaml`). Don't share weights across symbols (0.5d)
- [ ] **4.3** Time-of-day edge map — per-session feature reliability from outcomes per session tag (1d)
- [ ] **4.4** Data drift detector in `ab_analysis.py` — 30d vs 90d expectancy delta, alert on divergence (0.5d)
- [ ] **4.5** Weight grid-search over historical positions — validate hardcoded voter weights aren't guesses (1d)

---

### TIER 5 — Cuts (parallel, free wins)

- [x] **5.1** Cut M1 Claude on XAUT — `settings.yaml: decide_filter.disabled_symbols: [XAUTUSDT]` (5 min)
- [ ] **5.2** Remove `wave.py` from M2 voter — subjective, no WR evidence (30 min)
- [x] **5.3** Audit `big_trade.py` — wired to `confirmation.py` + `tp_participation.py`; `classify_outcomes` runs per bar in `ingest.py` (1h)
- [~] **5.4** Collapse `trend_escape` — disaster-floor gate added (`cycle_manager.py:275`); full merge with ChoCh pending (1h)
- [ ] **5.5** Defer `hedge_manager.py` — hedging single-leg = second coin flip (0)
- [x] **5.6** Drop Bybit linear XAUUSDT migration — Binance Futures `--venue futures` per `scripts/start.sh` (0)
- [ ] **5.7** Footprint JSONL rotation — rolling 90-day window, gzip older (1h)

---

### TIER 6 — V2 architecture: drop the grid

After Tier 0-3 prove the signal.

- [ ] **6.1** Drop grid for V2 — workaround for distrust of own signal
- [ ] **6.2** Position size scales by `bias_strength` from features — replaces grid recovery
- [ ] **6.3** Single-entry, single-exit, sized by confidence — clean attribution per trade

---

### TIER 7 — Sunset Claude entirely

**Vision:** M2 becomes smart enough that M1 (Claude) is dead weight. Sub-second latency. Zero per-decision API cost. Fully deterministic and auditable.

**Gates before Claude can be killed (all must hold ≥ 4 consecutive weeks):**
- M2 live expectancy ≥ +0.5R/trade across both symbols
- M2 live WR ≥ 60% on each symbol independently (not blended)
- M2 drawdown ≤ 1.5× M1's historical worst-week DD
- Tier 4 self-tuning loop closed — weights adapting from outcomes, not hardcoded
- Replay tester (1.4) green on full 90-day history with current M2 config
- No catastrophic-regime gap visible in `ab_analysis.py` drift detector (4.4)

**Then:**
- [ ] **7.1** Cut M1 from XAU (will happen earlier via 5.1, dry-run for full sunset)
- [ ] **7.2** Cut M1 from BTC after gate criteria met for 4 weeks
- [ ] **7.3** Archive `llm/` + `prompts/` + `decide.py` Claude path — keep code, disable route
- [ ] **7.4** Optional: retain Claude as **monthly auditor** — review last 30d of M2 decisions, flag systematic blind spots. Cheap (one batch call), preserves outside-view signal without per-decision dependency

**Risks of sunsetting too early:**
- M2 weights overfit to recent regime → looks great until regime shifts
- Loss of qualitative pattern recognition (Claude catches setups deterministic rules miss — black swan event, news context, multi-frame structure)
- No fallback when M2 score is ambiguous → either fires bad trade or skips good one

**Mitigation if sunset:** preserve Claude as breaker-glass option (`config/settings.yaml` toggle, ready to re-enable in 1 command if M2 expectancy degrades > 0.3R over rolling 30d).

---

### TIER 8 — Multi-strategy manager (built 2026-05-31, PR #1 merged)

Framework to deploy many strategies on one shared data feed with isolated
per-strategy results. See `STRATEGIES.md`. Shipped: `StrategyManager` (in-process
fan-out, manage→enter→record), `scope()` store redirection via contextvars,
`Strategy.adjust_plan`/`settings_override` hooks, routes (`/strategies`,
`/strategies/<name>/results`, `/strategies/tick`), wired into ingest bar-close.

- [x] **8.1** StrategyManager + Strategy/StrategyContext + per-strategy stores under `data/strategies/<name>/`
- [x] **8.2** `democracy` — wraps M2 weighted-vote engine (disaster-floor SL, existing behavior)
- [x] **8.3** `republic` — democracy's signal + hard 1.5×ATR SL (opt-in `cycle.hard_sl_exit`); A/B for WR-vs-RR
- [x] **8.4** SYSTEM_REPORT §1b — true-RR analysis (R vs disaster floor ≈ ×12 the TP distance → headline understates edge ~12×)
- [ ] **8.5** Run democracy vs republic live ≥ 2 weeks, then decide the WR-vs-RR tradeoff. **Gate:** if republic avg-R/trade > democracy AND republic max-DD ≤ 1.5× democracy → promote a real hard SL to the core grid. Else keep disaster-floor. (passive — needs live data)
- [ ] **8.6** Wire true-RR into `results.py` / `scripts/gen_system_report.py` — report R against a configurable real-risk denominator (TP-distance or tight %SL), not only the disaster floor. §1b is static prose today; recompute it. (0.5d)
- [ ] **8.7** Per-strategy equity-curve view in dashboard — read `data/strategies/<name>/equity.jsonl` (0.5d)

### Recommended next 10 days

```
Updated 2026-05-30 (post Tier 0 analysis):
  Done  : 1.1 kill switch, 1.2 pending persistence, 1.3 TP shrink gate,
           1.5 /health, 1.6 pre-filter, 3.1 naked_poc, 3.2 sweep classification
  Next  : 5.2 remove wave.py (30 min)
           2B.1 stacked_imbalance hard gate in direction_engine (1d)
           2A.1 POC-trail TP in tp_resolver (1d)
           1.4 replay.py — run against footprint data (2-3d, unblocks feature dev)
  Skip  : M2 switch not warranted yet — M1 bar-verified +5.80R same period
```

End of 10 days: validated foundation, can't blow up, iterating features in hours, one feature shipped with empirical justification.

---

### Deferred indefinitely

- VP redesign (VA collapse + HVN/LVN) — see `project_vp_redesign.md`. Superseded by Tier 3 incremental approach.
- COMEX GC data ($30/mo) — only if Binance XAU venue-edge fails to scale
- Elliott Wave for M2 — low ROI, dropped (Tier 5.2)

---

## Footprint Feature Expansion (added 2026-05-29)

Ladder data ingested but unused in decisions. M2 collapses 2D orderflow into 7 scalar votes — discards spatial structure. Build hierarchical features, not more votes.

### New features to add (`pipeline/features/`)

1. **`stacked_imbalance.py`** — walk ladder, count consecutive levels where `ask_vol / bid_vol ≥ 2.5` (or inverse). `N ≥ 3` = imbalance column. Direction = column side. Feeds `direction_engine` as **hard gate**, not vote.
2. **`delta_divergence.py`** — bar-level delta vs swing structure. Price HH + delta LH = bearish divergence. Price LL + delta HL = bullish. Single most reliable footprint signal.
3. **`unfinished_auction.py`** — bar close with single-print at extreme (only bid or only ask traded at top/bottom level). Signals return-to-level. Complements `sweep.py`.
4. **`poc_drift.py`** — intraday POC position per bar. 3-bar rising POC = demand absorption; falling = supply. Replaces static `vp_shape` vote with dynamic signal.
5. **Volume confirmation gate** — pre-entry: bar volume at entry zone ≥ 1.5× 10-bar avg at that zone. Filter low-volume fakes. Add to `decision_validator.py`.

### Wiring

- `direction_engine.py`: stacked imbalance + delta divergence = **gates** (entry blocked if conflicting). Sweep/CVD/FVG/session remain weighted votes.
- `vp_cache.py`: per-bar POC drift update, not just session-boundary rebuild.
- `tp_resolver.py`: bias toward POC if intraday POC drift aligns with cycle direction; toward VAH/VAL if drift opposite (mean-reversion target).

### Cuts

- [ ] Remove `wave.py` from M2 voter (subjective, no WR evidence)
- [ ] Cut M1 Claude on XAUT (−13R confirmed, stop API spend)
- [ ] Audit `big_trade.py` — log-only, no decision wiring. Either gate decisions on outcome or delete.
- [ ] Collapse `trend_escape` force-close into ChoCh + disaster floor (two overlapping exits = ambiguous attribution)
- [ ] Defer `hedge_manager.py` until legs 2–5 actually fill. Hedging single-leg cycles = second coin flip.

### Priority order

1. `stacked_imbalance.py` (highest signal/effort ratio)
2. `delta_divergence.py`
3. POC drift in `vp_cache.py`
4. `unfinished_auction.py`
5. Volume confirmation gate
6. Cuts (wave, M1-XAUT, big_trade audit)

---

## VP / LVN / HVN / Sweep Strategy Playbook (added 2026-05-29)

How to actually trade footprint structure. Not features in isolation — combinations.

### Volume Profile zones — role per regime

| Zone | Role in range | Role in trend |
|------|--------------|---------------|
| POC | Magnet (mean-revert target) | Re-test point after breakout (continuation trigger) |
| VAH/VAL | Rejection edges (fade) | Breakout level (momentum entry) |
| HVN | Barrier (price stalls, expect reversal) | Re-accumulation pause (continuation) |
| LVN | Acceleration zone (price slices through fast) | Confirms trend if filled with volume |
| prior-day POC | Strongest single magnet next session | Acts as S/R, often tested first |
| naked POC (untested) | High-probability draw — price seeks it | Same — institutional unfinished business |

**Regime gate first.** Same zone = different trade depending on `day_type`. Currently M2 ignores this split.

### LVN trade — acceleration confirmation

Setup: price approaches LVN from HVN. If LVN traverses with **rising volume + delta same-side**, trend continues. If LVN gets **rejected** (price wicks into LVN then reverses with opposite delta), failed breakout — fade back to HVN.

```
long entry: price exits HVN → LVN with +delta, vol > 1.5× avg
TP: next HVN above
SL: back inside originating HVN
```

This is the **highest expectancy footprint trade** documented. Currently unimplemented.

### HVN trade — barrier fade

Setup: price runs into HVN from below (uptrend). At HVN, watch for:
- Delta flip negative
- Stacked ask imbalance failure (asks not getting absorbed)
- Unfinished auction at HVN top

If 2 of 3 fire → short fade back to POC or VAL.

Inverse for HVN approach from above.

### Naked POC magnet

Any untested POC from prior session = high-probability draw. Track per symbol:
```
naked_pocs = {symbol: [(ts, price, session_id), ...]}
```
Remove when price trades through. Use as **TP target** if cycle direction aligns. Adds context to `tp_resolver.py`.

### Sweep — current detector is shallow

Current: wick + reclaim → boolean. Missing:
- **Sweep + delta confirmation** — wick high then close with strong negative delta = real reversal. Wick high + flat delta = stop-run only, low edge.
- **Liquidity pool identification** — equal highs/lows = liquidity. Sweep through equal high then reverse = highest-edge reversal pattern.
- **Sweep failure** — wick high, reclaim, but next bar continues up = breakout. Currently treated same as reversal.

Upgrade `sweep.py` to classify: `reversal_high` / `reversal_low` / `failed_sweep` / `liquidity_grab`. Each has different downstream action.

### Value Area dynamics (intraday)

- **VA expansion** (range widening) = trend developing → favor breakout trades
- **VA contraction** (range narrowing) = compression → favor mean-reversion to POC
- **VA migration** (POC drift) = imbalance forming → bias direction of drift

Track VA width vs prior bar. 3-bar VA expansion = regime shift signal.

### Combinations that matter (the alpha)

These are the multi-feature confluences worth firing on. Single-feature trades are weak.

| Pattern | Confluence | Trade |
|---------|-----------|-------|
| **Trend continuation** | LVN traverse + delta same-side + vol > avg + session aligned | Strong continuation, hold to next HVN |
| **Trend exhaustion** | HVN hit + delta divergence + unfinished auction + sweep | Counter-trend fade to POC |
| **Range reversion** | VAH/VAL touch + stacked opposite imbalance + day_type=range | Mean-revert to POC |
| **Breakout** | VA expansion + naked POC tested + delta directional + LVN cleared | Momentum entry, trail SL behind LVN |
| **Trap** | Sweep + reclaim + delta opposite of wick + back inside VA | Reversal, target opposite VA edge |

### Implementation order

1. Build `naked_poc.py` tracker (cheap, high-value TP context)
2. Upgrade `sweep.py` classification (reversal vs failed vs grab)
3. Add VA width tracking to `vp_cache.py` (expansion/contraction regime)
4. Build LVN-traverse detector (`pipeline/features/lvn_trade.py`)
5. Wire combinations as M2 **gates** (not votes) — single-pattern hits get veto'd

### Critical principle

Footprint edge comes from **spatial+temporal context**, not isolated signals. Current M2 votes are signal-isolated. Combinations are where edge lives. Build the combo detector layer above the feature layer.

---

## System-Wide Gaps (added 2026-05-29)

Things beyond features. Architecture-level.

### 1. Outcome feedback loop (biggest gap)

Positions logged but no module reads them to tune feature weights. M2 vote weights all hardcoded. After 200 trades you'll know `sweep` predicts 65% on XAU, 40% on BTC. Nothing uses this.

Build `eval/feature_attribution.py`: per closed position, attribute realized_R to features active at entry. Weekly: re-weight `direction_engine` votes from rolling 100-trade window. Self-tuning M2.

### 2. Time-of-day edge map

`session.py` tags session but doesn't gate features by it. Sweep at London-fix = institutional. Sweep at Asia-dead = noise. Same signal, opposite edge.

Per-session feature reliability map from `positions.jsonl` outcomes per session tag. Gate or weight votes accordingly.

### 3. Cost-aware decision gate

`/decide` fires every 60s regardless. Claude call ~$0.02. ~$28/day on idle bars. Pre-Claude footprint pre-filter: skip if no stacked imbalance + no sweep + no VA touch + delta within ±0.2 of mean. ~70% bars = noise.

### 4. MAE/MFE per setup type

Only realized_R logged. Missing max favorable excursion (how close to TP before reversing) + max adverse excursion (deepest drawdown). Avg win 0.37R but if MFE was 1.4R → TP too tight, not direction problem.

Track per-bar high-water marks in `position_store.py`. Surface in dashboard.

### 5. Footprint replay tester

Months of footprint JSONL exist. No way to test new feature ideas against history without restarting full ingest. Build `scripts/replay.py`: feed `data/footprint/*.jsonl` into feature under test, output signal/no-signal per bar. Validates `stacked_imbalance.py` against history in seconds, not 2 weeks paper.

### 6. Anti-pattern: do not LLM-tune from outcomes

Tempting: feed Claude last 50 losses, ask for prompt fixes. Don't. Overfits to recent regime. Use outcomes to weight deterministic features, not rewrite Claude prompts. Claude prompt stays stable; M2 weights adapt.

### 7. Symbol divergence

BTC footprint ≠ XAU ≠ NQ. Tick aggressor rules, ladder density, sweep patterns differ. M2 single config across symbols = guaranteed underfit. Per-symbol weight files: `config/m2_weights_BTCUSDT.yaml`, `_XAUTUSDT.yaml`. Don't share.

### 8. Hard equity kill switch

Soft cooldowns exist. No hard circuit breaker on cumulative R. SNB-style flash event = unbounded loss in current code.

One-line gate in `router.py`: read `positions.jsonl` for today's sum_R, refuse dispatch if `< -5R`. Manual reset only.

### 9. Data drift detection

XAU footprint character changed 2023→2024 (algorithmic LP changes, COMEX bar widening). 2025 backtest != 2026 live. Tag positions by month. Compare last-30-day expectancy vs last-90-day. Drift = early warning edge died. Single aggregate in `ab_analysis.py`.

### 10. Architectural punchline — invert the system

Grid was safety net for bad direction. If direction good → grid never matters (66/72 single-leg = direction worked, grid was dead weight). If direction bad → grid amplifies loss.

**Invert: fix direction, scale position size by signal strength, drop grid for V1 of new system.** Grid is workaround for not trusting signal. Build signal you trust.

### Priority order (system gaps)

1. Hard equity kill switch (`router.py` 5-line gate) — protects everything else
2. MAE/MFE tracking — tells if TP or direction is the problem
3. Footprint replay tester — unblocks all future feature dev
4. Cost-aware pre-filter on `/decide` — cuts API spend immediately
5. Outcome feedback loop (`eval/feature_attribution.py`)
6. Per-symbol M2 weight configs
7. Time-of-day edge map
8. Data drift detector in `ab_analysis.py`
9. Architectural inversion — V2 design after V1 features land
