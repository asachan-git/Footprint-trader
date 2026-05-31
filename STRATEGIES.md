# Strategy Manager

Deploy **multiple trading strategies** on the same live data feed. Market data,
settings and logs are **shared**; results (positions, cycles, decisions, equity)
are **isolated per strategy** so PnL and win-rate never cross-contaminate.

First strategy deployed: **`democracy`**.

---

## 1. Concepts

| Thing | What it is |
|---|---|
| **Strategy** | A `decide(symbol, tf, bar, settings) → Decision\|None`. Pure signal + execution policy. |
| **StrategyContext** | One strategy's isolated stores + result files + a `scope()`. |
| **StrategyManager** | Loops enabled strategies each bar: manage exits → maybe enter → record. |
| **scope()** | Redirects the shared `position_store()` / `cycle_store()` getters at a strategy's own stores, so the existing exit engine (`cycle_manager`) runs unchanged on per-strategy data. |

### Shared vs per-strategy

```
COMMON (all strategies read the same world)
  data/footprint/*          market data / footprint bars
  config/*.yaml             settings
  logs/                     process logs

PER-STRATEGY  data/strategies/<name>/
  positions.jsonl           leg-level fills
  cycles.jsonl              cycle open/close (drives results)
  decisions.jsonl           every entry signal emitted
  equity.jsonl              cumulative-R points (one per realized close)
```

---

## 2. The flow (per bar close)

`StrategyManager.tick(symbol, tf, bar, settings)` — for each enabled strategy
trading `symbol`, inside `ctx.scope()`:

1. **MANAGE** — `cycle_manager.on_bar_close(...)` resolves exits on the
   strategy's open cycles: VP-anchored TP, disaster floor, ChoCh invalidation,
   scale-out. Reused as-is; the scope makes it operate on per-strategy stores.
2. **ENTER** — if no open position on the symbol, call `strategy.decide()`. On a
   non-flat `Decision`, build the mechanical 5-leg grid (`_build_grid_plan`) and
   paper-fill it into the strategy's stores.
3. **RECORD** — append a `decisions.jsonl` row; append an `equity.jsonl` point
   whenever cumulative realized R changes.

One cycle per symbol per strategy at a time (matches the core grid rule).

---

## 3. `democracy` — first strategy

Weighted-vote direction engine. A panel of footprint/VP detectors (CVD, VP shape,
VP position, FVG, sweep, confirmation, + demoted micro-structure votes) each cast
a weighted ballot; the weighted majority sets side + conviction (`bias_strength`).
Wraps the existing `execution/direction_engine.decide_direction` — no new signal
logic, just adapted to the `Strategy` interface.

Config (`config/strategies.yaml`):

```yaml
strategies:
  - name: democracy
    enabled: true
    config:
      symbols: [BTCUSDT, XAUTUSDT]
      vote_tf: 15m          # TF the vote panel evaluates structure on
```

### `republic` — democracy's signal + a constitution (hard SL)

Subclasses `democracy`, so the **signal is identical**. Only the execution policy
differs: instead of the disaster floor (~5×ATR, which fired 0× in 108 trades and
makes the R-denominator ~4× the TP distance — see SYSTEM_REPORT §1b), `republic`
clamps the grid `safety_sl` to `sl_atr_mult × ATR_15m` from the anchor (default
1.5×ATR) via the `adjust_plan` hook.

This is a deliberate **A/B for the WR-vs-RR tradeoff** on the same live signal:

| | democracy | republic |
|---|---|---|
| Signal | weighted vote | same |
| Stop | disaster floor (~5×ATR) | hard 1.5×ATR |
| Expected WR | high (~94%) | lower (stop gets hit) |
| Expected realized RR | tiny per win | larger per win |

```yaml
  - name: republic
    enabled: true
    config:
      symbols: [BTCUSDT, XAUTUSDT]
      vote_tf: 15m
      sl_atr_mult: 1.5      # hard SL distance from anchor, in ATR_15m units
```

Compare `data/strategies/democracy/` vs `data/strategies/republic/` after both
have run to see which side of the tradeoff wins on real data.

> **`adjust_plan` hook:** any strategy may override `adjust_plan(plan, bar,
> settings)` to mutate the built `GridPlan` before fill (SL, TP, legs) without
> touching the shared grid placer. Default is a no-op.

---

## 4. API

| Method | Route | Purpose |
|---|---|---|
| GET | `/strategies` | List deployed strategies + headline results |
| GET | `/strategies/<name>/results` | Full stats + ASCII equity curve |
| POST | `/strategies/tick` | Run one manager tick. Body: `{symbols?, tf?}` |

`results` payload: `overall` / `by_symbol` / `by_direction` (buy-sell split) /
`exit_reasons` / `equity` (final R, max drawdown) / `equity_ascii`.

---

## 5. Add a new strategy

1. Subclass `Strategy` in `strategies/<name>.py`, implement `decide()`.
2. Register it in `strategies/registry.py` → `REGISTRY`.
3. Add an entry to `config/strategies.yaml` with `enabled: true`.

That's it — the manager creates its isolated data dir and starts reporting
results on first close. No changes to shared execution code.

---

## 6. Wiring it into the live loop

The manager is a request-global singleton in `server/routes/strategies.py`.
To run it automatically each bar (alongside / instead of `/grid_tick`), call
`/strategies/tick` from the same place the bar-close trigger fires
(`server/routes/ingest.py` mtf hook or the `start.sh` wall-clock loop).

> Status: framework + `democracy` deployed and import-validated. Entry path
> paper-fills into per-strategy stores; exits reuse `cycle_manager`. Not yet
> auto-wired into the bar-close trigger — call `/strategies/tick` to drive it.
