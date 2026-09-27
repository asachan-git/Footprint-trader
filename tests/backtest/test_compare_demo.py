"""backtest/compare_demo.py — unit conversion, cycle matching, MT5 report parsing."""
from __future__ import annotations

import pandas as pd

from backtest.compare_demo import (attach_realized, compare, load_report, replay_cfg,
                                   units)


def _settings(**g):
    base = dict(base_lot=0.01, lot_step=0.01, max_lots=0.04, contract_size=100,
                account_ccy_per_usd=1, cycle_net_target_by_tf={"15m": 66.0},
                bias_trail_activate_usd=6.67, cycle_max_loss_pts=140,
                fullfill_be_enabled=True, fullfill_cancel_opposite=True)
    base.update(g)
    return {"grid_levels": base}


def test_units_usd_account_base_001_is_one_dollar_per_point():
    assert units(_settings()["grid_levels"]) == 1.0


def test_replay_cfg_converts_cent_account_and_bigger_base_lot_to_points():
    # cent account, base 0.25: 1 pt = 0.25 × 100 × 100 = 2,500 USC
    cfg = replay_cfg(_settings(base_lot=0.25, lot_step=0.25, max_lots=1.0,
                               account_ccy_per_usd=100,
                               cycle_net_target_by_tf={"15m": 165_000.0}))
    assert cfg["target"] == 66.0
    assert cfg["lot_step"] == 0.01 and cfg["max_lots"] == 0.04
    assert cfg["disaster_pts"] == 140 and cfg["cancel_opp_on_full"] is True


def test_compare_matches_within_window_and_flags_extras():
    demo = pd.DataFrame([
        dict(strat="hvn", armed_ts=1000, pts=10.0, exit_reason="net_target"),
        dict(strat="hvn", armed_ts=9000, pts=-5.0, exit_reason="max_loss"),     # no twin
    ])
    rep = pd.DataFrame([
        dict(strat="hvn", arm_ts=1000 + 60, pnl=8.0, reason="net_target"),
        dict(strat="hvn", arm_ts=50_000, pnl=3.0, reason="leg_tp"),             # no twin
    ])
    s, P = compare(demo, rep)
    h = s["hvn"]
    assert (h["matched"], h["demo_only"], h["replay_only"]) == (1, 1, 1)
    assert h["median_abs_gap_pts"] == 2.0 and h["same_exit_rate"] == 1.0


def _mt5_html(rows):
    hdr = ["Time", "Position", "Symbol", "Type", "Comment"] + [""] * 7 + \
          ["Volume", "Price", "S / L", "T / P", "Time", "Price", "Commission", "Swap", "Profit"]
    def tr(cells):
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
    body = [tr(["Positions"] + [""] * 20), tr(hdr)] + [tr(r) for r in rows] + [tr(["Orders"] + [""] * 20)]
    return "<html><body><table>" + "".join(body) + "</table></body></html>"


def test_load_report_and_attach_realized(tmp_path):
    def row(open_s, comment, close_s, comm, swap, profit):
        return [open_s, "1", "XAUUSD+", "buy", comment] + [""] * 7 + \
               ["0.01", "2650", "", "", close_s, "2651", comm, swap, profit]
    html = _mt5_html([
        row("2026.10.01 13:00:00", "FB|hvn|15m|b1", "2026.10.01 13:30:00", "-0.06", "0", "1.50"),
        row("2026.10.01 13:05:00", "FB|hvn|15m|b2", "2026.10.01 13:30:00", "-0.06", "0", "2.00"),
        row("2026.10.01 13:10:00", "FB|lvn_edge|15m|s1", "2026.10.01 13:40:00", "-0.06", "0", "-1.00"),
        row("2026.10.01 13:10:00", "FB|hvn|5m|b1", "2026.10.01 13:40:00", "0", "0", "9.00"),  # other TF
        row("2026.10.01 13:10:00", "manual", "2026.10.01 13:40:00", "0", "0", "99.00"),     # not ours
    ])
    p = tmp_path / "r.html"
    p.write_text(html, encoding="utf-8")
    pos = load_report(str(p), utc_offset_h=3)
    t = int(pd.Timestamp("2026-10-01 10:00", tz="UTC").timestamp())       # 13:00 server = 10:00 UTC
    assert pos.open_ts.min() == t and set(pos.strat) == {"hvn", "lvn"}
    cyc = pd.DataFrame([dict(strat="hvn", armed_ts=t - 30), dict(strat="lvn", armed_ts=t)])
    out = attach_realized(cyc, pos, "15m")
    assert round(out.pnl_native.tolist()[0], 2) == 3.38
    assert round(out.pnl_native.tolist()[1], 2) == -1.06
