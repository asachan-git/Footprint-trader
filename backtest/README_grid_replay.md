# Grid replay — 15m hvn_inside_touch, May 7 → Sep 24 2026

`backtest/grid_replay.py` replays the grid on `data/footprint/XAUTUSDT_15m.jsonl`
(signals + volume profile) and `XAUTUSDT_5m.jsonl` (fills and exits).
`backtest/run_grid_replay.py` runs baseline, proposed and every ablation.

## What is modelled

- **Zones:** rolling 96-bar 15m volume profile computed with the repo's own
  `pipeline.features.volume_profile.compute` (bin 0.4).
- **Entry:** `hvn_inside_touch` rules from `execution/zone_triggers.py`: the bar
  closes inside an HVN and taps an edge. Arms at the 15m close.
- **Grid:** width-driven n (max 5), step 0.5×ATR14, reversion skew (+1 leg on the
  fade side), lots 0.01 → 0.04. Buy stops above the fulcrum, sell stops below.
  Structural leg TP ceiling at the next HVN.
- **Exits:** `ExecBridge.monitor_cycle` from `origin/exp/pre-server-tp-8d9f546`:
  - bias trail (6.67 activation, 35% giveback, book half, break-even on the rest);
  - net target 66 with 33% hedge decay;
  - flatten-rest;
  - full hedge.
- **One cycle at a time,** as with one magic per strategy × TF.
- **Costs:** 0.25 spread and $6 per lot round-trip commission.

## Results ($ at 0.01 base lot, $1/pt for leg 1)

| Scenario | Net | PF | Max DD | Worst cycle | Cycles |
|---|---|---|---|---|---|
| Baseline (pre-server-tp) | +1,909 | 1.17 | −1,377 | −505 | 855 |
| **Proposed** (fullfill + orphan close + disaster cap) | **+6,361** | **1.78** | **−502** | **−200** | 1,062 |
| Proposed, fullfill without orphan close | −46 | 0.99 | −704 | −171 | 214 |
| Proposed, 2× costs | +4,396 | 1.48 | −689 | −204 | 1,062 |
| Proposed, 3× costs | +2,514 | 1.25 | −911 | −208 | 1,062 |
| Proposed, pessimistic intrabar path (H→L) | +3,983 | 1.45 | −791 | −200 | 1,071 |

Monthly net for the proposed rules was positive in every month:

| Month | May | Jun | Jul | Aug | Sep |
|---|---|---|---|---|---|
| Net | +1,053 | +2,520 | +1,295 | +807 | +685 |

Baseline over the same months: +51, +1,846, +706, +58, −752.

The rules were designed from the August live review, so **May–Jul is the
out-of-sample period**: proposed +4,868 vs baseline +2,603.

## Rules tested and not adopted

| Rule | Effect on top of proposed |
|---|---|
| News block by clock (08:25–08:50 and 09:55–10:20 NY every weekday) | −1,044 |
| Trapped flatten at 4 legs | ≈0 |
| Contested target decay 0.9 | ≈0 |
| Opposite-side cap at 2 legs | −445 |
| BB slope size tilt | +310 with a deeper worst cycle |

Trapped flatten and contested decay rarely fire once fullfill is on. A clock-based
news block is too blunt; it needs a real release calendar.

## Why fullfill needs the orphan guard

`fullfill_cancel_opposite` (already in base-v2) cancels the opposite pending orders.
Any opposite legs that had already filled are left with nothing resting behind
them, so flatten-rest can never fire. In replay such legs were held for weeks, and
one blocked the strategy from May 12 onward. The patch closes them together with
the cancel (`fullfill_close_opposite_filled: true`).

## Limits

- XAUTUSDT (Bybit) prices, not Vantage XAUUSD.pc.
- 5m granularity for fills and trailing, with an intrabar path heuristic.
- Rolling VP only: no cached or prev-day zones, session gating or squeeze target multiplier.
- No slippage beyond the spread figure.
- Treat as a relative comparison between rule sets, then confirm on demo.
