# combined-v1 demo runbook

Goal: two weeks on a demo account, then a cycle-by-cycle comparison against the
replay of the same dates. Go live only if the bridge behaves like the replay.

## Setup

1. Pull `feat/combined-v1`. Run `pytest tests -q` (all green; 17 of the tests cover
   the full-fill orphan guard, the disaster cap and the flatten-rest races).
2. Demo account in **USD** on `XAUUSD+` (the symbol map already points there),
   `account_ccy_per_usd: 1`. No manual trades on this account, no deposits mid-run.
3. `base_lot: 0.1`, `lot_step: 0.1`, `max_lots: 0.4` — the replay's ladder ×10 (1 pt = $10
   for leg 1), with every $ threshold scaled ×10 to match. Check the first grid in MT5: legs
   must read 0.1 / 0.2 / 0.3 / 0.4 (0.4 again for a 5th or skew leg). A full 5-leg side is
   1.4 lots; three setups can hold up to 4.2 lots at once.
   Run `python backtest/arm_smoke.py --from <date> --to <date>` first: it drives the live
   arm path on recorded bars and prints arms per day, legs and the lot ladder per setup.
4. Triggers: `hvn_inside_touch`, `lvn_edge_touch`, `hvn_edge` on `[15m]` only;
   `candle_sweep` off. `daily_target_pct: 0`, EA `InpEquityTarget = 0`.
5. Leave the server recording `data/footprint/XAUTUSDT_{15m,5m}.jsonl` — the replay
   needs those bars for the same dates.

## What to watch during the run

- `data/exec_emit.jsonl` exit rows: `fullfill_be` should be followed by `CLOSE_SIDE`
  `FB|fullfill_close_opp|…` whenever the opposite side had fills; no `leg_closed_other`
  within a few seconds of a `fullfill_be` or `bias_book_trail` on the same magic.
- No cycle below −140 pts at base lot (−$1,400 at 0.1). A `max_loss` exit is fine;
  a cycle past it by more than slippage is a bug.
- `data/cycles/cycle_outcomes_*.jsonl` — every filled cycle now ends with a final row
  (including `all_closed` for cycles that close leg by leg).

## After two weeks

Export MT5 History (Report → HTML) for the demo account, then:

```
python backtest/compare_demo.py --cycles data/cycles \
    --report ~/Downloads/ReportHistory-<acct>.html --from <start> --to <end>
```

It replays the window with the settings in `config/settings.yaml`, matches cycles by
setup and arm time (±15 min) and prints, per setup: demo vs replay cycles, net points,
PF, match rate, correlation of matched cycles, same-exit rate and the largest gaps.

## Go-live gates (per setup)

| Check | Pass |
|---|---|
| Match rate (demo cycles with a replay twin) | ≥ 70% |
| Demo PF vs replay PF on the same window | within 30% |
| Matched-cycle correlation | ≥ 0.6 |
| Worst demo cycle | not below −140 pts |
| Orphan close fired whenever needed | yes (exit log) |

A setup that fails a gate stays on demo. Go live with hvn_inside_touch and
lvn_edge_touch first; hvn_edge only after its own gates pass.

## Cent account (live)

P&L arrives in USC there. Before switching, confirm the XAUUSD.pc contract size in
the MT5 symbol spec, set `account_ccy_per_usd: 100`, and scale every other
native-currency threshold (`cycle_net_target_by_tf`, `bias_trail_activate_usd`,
`cycle_min_target_usd`) by the same factor, or the targets will be 100× tighter than
what was tested.
