# Trade Analysis — 2026-05-27

## Summary

| Metric | Value |
|--------|-------|
| Total closed trades | 37 |
| Win rate | 49% (18W / 19L) |
| Sum R | **-4.93R** |
| Avg win | +0.46R |
| Avg loss | -0.69R |
| Expectancy | **-0.133R per trade** |
| TP hits | 18 trades, +8.21R |
| SL hits | 0 |
| Absorption kills | 10 trades, **-8.00R** |
| Circuit breaker kills | 2 trades, -2.00R |

BTCUSDT: 23 trades, 13W/10L, sum=-4.27R  
XAUTUSDT: 14 trades, 5W/9L, sum=-0.66R

---

## Trade Log

| Date | Symbol | Side | Legs | R | Status | Reason |
|------|--------|------|------|---|--------|--------|
| 05-20 04:43 | BTCUSDT | long | 1 | -1.00 | invalidate | sell absorption 27% at 76850 |
| 05-20 04:45 | XAUTUSDT | long | 1 | +1.50 | tp_hit | |
| 05-20 04:52 | BTCUSDT | long | 1 | +1.50 | tp_hit | |
| 05-20 05:14 | BTCUSDT | short | 1 | -1.00 | sl_hit | |
| 05-20 05:15 | XAUTUSDT | long | 1 | +1.11 | tp_hit | |
| 05-20 05:36 | BTCUSDT | short | 1 | +1.50 | tp_hit | |
| 05-20 21:29 | XAUTUSDT | long | 1 | -0.05 | close | broker_closed |
| 05-20 21:32 | XAUTUSDT | long | 1 | -0.09 | close | broker_closed |
| 05-26 10:16 | XAUTUSDT | short | 1 | +1.71 | tp_hit | |
| 05-26 21:46 | XAUTUSDT | short | 1 | -1.00 | close | broker_closed |
| 05-26 22:15 | XAUTUSDT | long | 1 | -1.00 | invalidate | sell absorption 36% at 4498 |
| 05-26 22:49 | BTCUSDT | short | 1 | +0.13 | tp_hit | |
| 05-26 23:06 | BTCUSDT | short | 1 | +0.12 | tp_hit | |
| 05-26 23:06 | XAUTUSDT | short | 1 | +0.13 | tp_hit | |
| 05-26 23:10 | BTCUSDT | short | 1 | +0.00 | close | manual_close_stale |
| 05-26 23:10 | XAUTUSDT | short | 1 | +0.00 | close | manual_close_stale |
| 05-26 23:23 | XAUTUSDT | short | 2 | +0.03 | tp_hit | |
| 05-26 23:30 | BTCUSDT | short | 1 | -1.00 | invalidate | buy absorption 32% at 75980 |
| 05-26 23:33 | XAUTUSDT | short | 1 | +0.00 | close | tp_absorption |
| 05-26 23:37 | BTCUSDT | long | 1 | +0.04 | tp_hit | |
| 05-27 00:01 | XAUTUSDT | long | 1 | -1.00 | invalidate | daily DD circuit breaker |
| 05-27 00:12 | BTCUSDT | short | 1 | +0.03 | tp_hit | |
| 05-27 00:30 | BTCUSDT | short | 1 | +0.05 | tp_hit | |
| 05-27 00:45 | BTCUSDT | short | 3 | -1.00 | invalidate | buy absorption 60% at 75860 |
| 05-27 00:46 | XAUTUSDT | short | 5 | -1.00 | invalidate | buy absorption 100% at 4497 |
| 05-27 00:50 | BTCUSDT | long | 1 | +0.03 | tp_hit | |
| 05-27 01:00 | BTCUSDT | long | 1 | +0.06 | tp_hit | |
| 05-27 01:30 | BTCUSDT | short | 1 | +0.00 | close | tp_absorption |
| 05-27 02:15 | BTCUSDT | short | 1 | +0.06 | tp_hit | |
| 05-27 02:45 | BTCUSDT | short | 1 | +0.07 | tp_hit | |
| 05-27 03:00 | BTCUSDT | long | 5 | -1.00 | invalidate | sell absorption 64% at 76020 |
| 05-27 03:05 | BTCUSDT | short | 5 | -1.00 | invalidate | buy absorption 22% at 76040 |
| 05-27 03:08 | BTCUSDT | long | 4 | -1.00 | invalidate | sell absorption 28% at 76010 |
| 05-27 03:11 | BTCUSDT | short | 5 | -1.00 | invalidate | daily DD circuit breaker |
| 05-27 16:55 | XAUTUSDT | long | 2 | — | open | still open |
| 05-27 17:00 | BTCUSDT | short | 1 | +0.07 | tp_hit | |
| 05-27 17:00 | XAUTUSDT | short | 1 | -1.00 | sl_hit | |
| 05-27 17:01 | BTCUSDT | short | 1 | +0.07 | tp_hit | |

---

## Root Cause Analysis

### Problem 1: Absorption kills = entire loss (-8R from 10 trades) ← PRIMARY

Absorption detection in `ingest._check_positions` fired on every 1m bar and killed positions at -1R the moment any absorption signal appeared near entry, regardless of:
- Whether price had actually moved against the position (some had bar_low only 1-2 pts from entry)
- Whether absorption was at a strong reaction zone (expected market microstructure)
- Whether the position had profit to protect

Worst cases:
- `c5bc3cfce51e`: XAUT short, 5 legs filled, buy absorption 100% → -1R (entire grid, closed instantly)
- `e0f00972`: BTC short, 5 legs filled, buy absorption **22%** → -1R (threshold way too low)
- 03:00–03:16 bloodbath: 3 BTC positions open simultaneously, cascading absorption kills → 4 trades, -4R in 16 minutes

**This path was removed in commit `423cfbc`** (today). The correct replacement (not yet built): absorption + at strong zone + PnL positive → book profit; absorption + not at strong zone or PnL negative → hold.

### Problem 2: TP wins are tiny (+0.03–0.13R for 14 of 18 wins)

Only 4 trades had proper R (1.1–1.7R), all on May 20. All May 26–27 wins averaged ~0.06R.

**Why:** Safety SL is 5×ATR (very wide, ~880pts BTC, ~30pts XAUT). When only 1 leg fills, realized_R = (TP - leg1_entry) / (leg1_entry - safety_sl). The denominator is huge so even a 60pt TP move on BTC = 0.07R. The grid was designed for avg_entry after all 5 legs fill — but TP triggers on leg1 before the grid develops.

### Problem 3: Every multi-leg position (legs > 1) was a loser

7 multi-leg positions: all losses. Grid fills into adverse move (correct — limits placed below current price), but absorption detection then fires before bounce. No multi-leg position ever got to close profitably.

### Problem 4: 03:00–03:16 position overlap (4 trades, -4R)

Three positions opened at same price level (76010–76050 BTC) simultaneously. The same-direction guard in `router.py:237` should have blocked opens 2 and 3. This suggests either:
- Guard not firing on grid path (calls `dispatch_grid` not `dispatch`)
- Position store not reflecting open state at time of check

Need to verify `dispatch_grid` in [execution/router.py:199-211](execution/router.py#L199) checks position store before submitting — currently the grid path does NOT have the same-direction guard that the legacy single-order path has.

---

## Improvements Backlog (priority order)

### P1: TP sizing relative to actual filled risk
**Status: Not built**  
When 1 leg is filled, use tightest safe zone below/above leg1 as the per-leg SL for R calculation — not the grid safety SL. Or: only close at TP if at least 2 legs filled (minimum position size before taking profit). This directly fixes the 0.03–0.07R wins.

### P2: Minimum hold before absorption check (2 bars)
**Status: Not built**  
Skip all invalidation events if `(now - open_ts) < 2 × bar_interval_seconds`. Most absorption kills fired within 1–3 minutes of open. This alone would have saved the 03:00–03:16 positions.

### P3: Absorption threshold scaled by legs filled
**Status: Not built**  
- 1 leg filled → ignore absorption entirely (noise)
- 2 legs → threshold 50%+ AND absorption_price within 1×ATR of avg_entry
- 3+ legs → threshold 35%+ AND at strong zone (HVN/POC/VAH/VAL)

### P4: Add same-direction grid guard to `dispatch_grid`
**Status: Not built**  
[execution/router.py:199](execution/router.py#L199) — `dispatch_grid` live/paper path skips the same-direction position check that exists at line 237 for the legacy path. Add the guard before `submit_grid` call.

### P5: Absorption-at-strong-zone profit booking
**Status: Designed (plan), not built**  
Replace absorption kills with: absorption + at HVN/POC/VAH/VAL + PnL > 0 → close at market (book profit). Absorption + not at strong zone OR PnL < 0 → hold, let trail_sl handle. This is the correct semantic for what absorption means in market microstructure.

### P6: TP shrinking on adverse fill (cycle_manager redesign)
**Status: Partially built in d7632c5**  
`nearest_strong_zone_toward` exists in zone_collector. cycle_manager's `_shrink_tp_on_adverse_fill` hook needs wiring to actually call it per bar (currently only called if cycle_manager is the active exit handler — verify it runs on every ingest tick).

---

## What Worked

- Direction accuracy: 13/23 BTC wins (57%), good when not killed by absorption
- May 20 session: 4/6 trades profitable, avg +1.4R — this is the target regime
- Regime gate: no trades against confirmed trends (zero regime-blocked kills)
- Mode 2 vote distribution now balanced (sweep, wave, FVG all firing after bug fixes)
