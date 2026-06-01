# System Overview — How FootprintBiot Works

A plain-language guide to the whole system, top to bottom. For the detailed roadmap and
experiment log see [PLAN.md](PLAN.md); for the layered change-history see [README.md](README.md).

---

## 1. What it is, in one paragraph

FootprintBiot reads live order-flow (every trade tick), builds **footprint bars** (how much
was bought vs sold at each price level), and from those decides a trade **direction** for
BTC and gold (XAU). When it wants a position it does **not** fire one order with a stop —
it places a **grid** of limit orders that average into the move, holds through drawdown
(nano lot sizing absorbs it), and exits the whole cycle at a volume-profile target. There
is **no hard stop loss**; a cycle only dies on a disaster floor or a structure break.

---

## 2. The big picture (data flow)

```
 Bybit trade ticks
        │  (bybit/footprint_builder.py — bucket ticks by price level)
        ▼
 Footprint bar  ──POST /ingest──►  server/routes/ingest.py
        │                              │
        │                              ├─ store bar (pipeline/state_store.py)
        │                              ├─ check OPEN cycles for exits (cycle_manager)
        │                              └─ on 15m bar close → trigger a decision
        ▼
 Decision  ──► two "brains" (run in parallel, compared):
        │
        ├─ M1  /decide      → Claude LLM reads the chart context, returns a call
        └─ M2  /grid_tick   → mechanical rules engine (execution/direction_engine.py)
                                   │
                                   ▼
                          plan a grid (execution/grid_placer.py + grid_modes.py)
                                   │
                                   ▼
                          place legs (paper or broker)  →  manage cycle
```

Everything is a Flask app: [server/app.py](server/app.py) wires the routes
(`/ingest`, `/decide`, `/grid_tick`, dashboard, etc.). A live dashboard
([dashboard/](dashboard/)) shows candles + footprint + grid state over SSE.

---

## 3. Two brains (M1 vs M2)

The system runs **two independent direction engines** and compares them on paper:

- **M1 — Claude (LLM).** `/decide` builds a text prompt of the current chart/footprint
  situation and asks Claude for a call. Good on BTC, weak on gold (over-traded chop).
- **M2 — Rules engine.** `execution/direction_engine.py`. Pure code, no LLM. A set of
  **votes** combine into one score. Better on gold. This is the focus of recent work.

Live, both produce a decision every 15m bar; M2's is logged dry-run to
`data/mode_compare.jsonl` so the two can be back-tested against real price paths.

---

## 4. How M2 decides direction (the vote system)

`collect_votes()` runs a handful of small detectors. Each returns a **Vote**: a direction
(+1 long … −1 short) and a **strength** (weight). The votes:

| Vote | What it reads |
|------|----------------|
| `cvd` | cumulative delta momentum (net buying vs selling over 20 bars) |
| `vp_shape` | volume-profile shape (P / b / D = top-heavy / bottom-heavy / balanced) |
| `vp_position` | price above value-area-high (bullish) / below value-area-low (bearish) |
| `fvg` | fair-value gaps (imbalanced candles price tends to revisit) |
| `sweep` | liquidity sweeps, interpreted by regime (reversal in range, continuation in trend) |
| `confirmation` | absorption / exhaustion at a level |
| `stacked_imbalance` | stacked buy/sell pressure zones *(weak — see §8)* |
| `delta_divergence` | new price high but delta fails to confirm = exhaustion *(weak)* |
| `unfinished_auction` | one-sided volume at a bar extreme = price will return *(weak)* |

**Score** = Σ(direction × strength) ÷ Σ(strength), then multiplied by a VP-shape × regime
agreement factor. If |score| < **0.35** → **flat** (no trade). Otherwise score sign = side,
and |score| maps to a **bias_strength** 1–5 that scales the grid lot size.

---

## 5. How the grid is built (execution)

Once M2 says "long" or "short", `grid_placer.plan_grid()` builds the order grid:

- **Entries land on STRONG ZONES, not fixed ATR steps.** `zone_collector.collect()` gathers
  confluence levels — VP POC, VAH/VAL, naked POC, HVN/LVN, FVG, swings — and ranks them by
  strength. Legs are placed at the nearest strong zones on the correct side of price.
  (ATR-spacing is only a last-resort fallback when not enough zones exist.)
- **Lot ladder = Fibonacci [1,1,2,3,5] × base_lot × (bias_strength/5).** The nearest leg is
  smallest; the **deepest legs are biggest** — so the further price moves against you, the
  more you average in (recovery math).
- **Two regime modes** (`grid_modes.py`, picked by `day_type`):
  - **Mean-reversion** (range, or counter-trend exhaustion): up to 5 legs, wider.
  - **Trend-continuation** (with the trend): fewer legs, tighter spacing.

---

## 6. How positions exit (the no-hard-SL philosophy)

A cycle is managed every bar by `cycle_manager.on_bar_close()`. Exit paths:

1. **Take-profit (the normal exit).** When price reaches the common TP **and** the average
   entry is in profit, **all legs close together**. TP is volume-profile-anchored and
   *shrinks* as more legs fill (deeper drawdown → exit at the nearest profit zone faster).
2. **Disaster floor (the only "stop").** A safety level far out — `leg_last ± max(5×ATR,
   3% BTC / 1.5% XAU)`. Catches news gaps. Normal trades never reach it.
3. **Invalidation.** Structure break (ChoCh, or close beyond VAH/VAL against the cycle) =
   the idea was wrong → close, don't wait for the floor.
4. **Trend-escape.** If price runs hard against the cycle for several bars with no bounce,
   force-close — protects against a runaway trend mislabeled as a range.
5. **Trailing.** When a cycle is in profit, the stop trails up to the next strong zone.

**Why no fixed stop:** nano lots make each leg tiny, so drawdown is survivable and a losing
leg becomes a cheaper averaging entry rather than a realized loss. The risk that actually
kills this style is a *trend that never comes back* — hence the disaster floor + trend-escape.

---

## 7. Risk model

- **Account:** Vantage USC, nano lots, 500× leverage. Per-leg exposure is tiny by design.
- **R (risk unit):** measured against the **disaster floor** — `entry × 3%` (BTC) / `1.5%`
  (XAU). All back-test results report realized R in these units.
- **One cycle per symbol at a time.** No stacking same-direction grids.

---

## 8. What's been learned (and turned off)

Recent validation work (see PLAN.md Tier 2A/2B, and `scripts/`):

- **Footprint micro-structure votes are weak.** Stacked-imbalance / delta-divergence /
  unfinished-auction were built as hard gates, found to have no edge, and demoted to small
  weighted votes. The stacked-imbalance "close beyond the zone" setup showed **no usable
  edge** in any granularity or regime — it tracked the trend, not the footprint.
- **Adverse-regime gate: built then REFUTED.** A gate to veto counter-trend entries looked
  good on synthetic all-bar data but, tested on **real M2 signals**, counter-trend signals
  were the *best* trades (M2's reversals are deliberate). Gate is coded but **disabled**
  (`ADVERSE_GATE_ENABLED=False`). Lesson: validate filters on the real signal population.
- **Partial profit-booking: simulated, decision pending.** A grid-aware simulator
  (`scripts/grid_sim.py`) shows scale-out is a **risk-reducer, not a return-improver**.
  An "aggressive" version (bank the deep legs on a bounce + move the rest to break-even)
  raises win-rate 74%→89%, halves time-in-trade, and converts most adverse-market blowups
  into break-even exits — at the cost of ~⅓ of total return. Not yet wired in.

**Stated aim of the strategy:** consistent profits and avoiding adverse markets. High entry
prices are acceptable. (This is why survival/consistency levers are weighed against raw return.)

---

## 9. File map (where to look)

| Area | Files |
|------|-------|
| Tick → footprint bar | `bybit/footprint_builder.py`, `bybit/main.py` |
| Server / routes | `server/app.py`, `server/routes/{ingest,decide,grid_tick}.py` |
| Footprint + features | `pipeline/footprint.py`, `pipeline/features/*` (cvd, vp_cache, sweep, imbalance, day_type, …) |
| Direction (M2) | `execution/direction_engine.py` |
| Direction (M1) | `llm/`, `prompts/`, `server/routes/decide.py` |
| Grid build | `execution/grid_placer.py`, `execution/grid_modes.py`, `execution/zone_collector.py` |
| Cycle management | `execution/cycle_manager.py`, `execution/position_store.py` |
| Back-test / analysis | `scripts/` (`grid_sim.py`, `gate_test.py`, `partial_tp_test.py`, `m2_synthetic_backtest.py`, `replay.py`, `stacked_edge_scan.py`) |
| Dashboard | `dashboard/` (React + SSE) |
| Config | `config/settings.yaml` |

---

## 10. Running it

- **Feed:** `python -m bybit.main --symbol BTCUSDT --tf 1m` (and XAU) → posts bars to Flask.
- **Server:** the Flask app (`server/app.py`) ingests bars, decides, manages cycles.
- **Dashboard:** `dashboard/` dev server for live candles + footprint + grid state.
- **Back-tests:** scripts under `scripts/` read `data/footprint/*.jsonl` and
  `data/mode_compare.jsonl` — no live feed needed.

> Note: footprint cell width is the feed's `--price-step` (BTC ran at $10, XAU at $0.1).
> Volume-profile bins are separate (`vp_bin_size`: BTC $25 / XAU $1).
