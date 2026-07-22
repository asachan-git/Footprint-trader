"""Unit tests for the LOG-ONLY fractal→VP→TP study.

Covers the two things that would silently invalidate the study:
  1. fractal detection fires exactly once per pivot (dedup works)
  2. the TP cascade reproduces the planner's rules, including the outer-leg guard
     and the POC reversion override
"""

from dataclasses import dataclass

from execution import fractal_tp_study as fts


@dataclass
class _O:
    o: float
    h: float
    l: float
    c: float


@dataclass
class _B:
    bar_id: str
    close_ts: int
    ohlc: _O


def _bars(highs):
    return [_B(f"b{i}", 1000 + i, _O(h, h, h - 2.0, h)) for i, h in enumerate(highs)]


def _reset():
    fts._seen_fractals.clear()
    fts._last_bar.clear()


# ── 1. fractal detection ─────────────────────────────────────────────────────

def test_detects_pivot_at_newest_confirmable_index():
    _reset()
    # rising into a peak at idx 5, then a lower bar -> idx 5 is a 3-bar swing high and
    # sits at len-2, the newest confirmable position.
    sp = fts.newly_confirmed_fractal("X", "5m", _bars([10, 11, 12, 13, 14, 20, 15]))
    assert sp is not None
    assert sp.idx == 5
    assert sp.kind == "high"
    assert sp.price == 20


def test_same_pivot_reported_only_once():
    _reset()
    bars = _bars([10, 11, 12, 13, 14, 20, 15])
    assert fts.newly_confirmed_fractal("X", "5m", bars) is not None
    # polled again on the same bar (monitor_cycle runs ~1Hz) -> must not re-fire
    assert fts.newly_confirmed_fractal("X", "5m", bars) is None


def test_no_pivot_when_newest_index_is_not_a_swing():
    _reset()
    # monotonic rise: idx len-2 is not a local extreme (its right neighbour is higher)
    assert fts.newly_confirmed_fractal("X", "5m", _bars([10, 11, 12, 13, 14, 15, 16])) is None


def test_too_few_bars_is_safe():
    _reset()
    assert fts.newly_confirmed_fractal("X", "5m", _bars([10, 11])) is None


# ── 2. throttle ──────────────────────────────────────────────────────────────

def test_should_run_once_per_bar():
    _reset()
    assert fts.should_run("X", 7701, 5000) is True
    assert fts.should_run("X", 7701, 5000) is False   # same bar, ~1Hz poll
    assert fts.should_run("X", 7701, 5060) is True    # next bar
    assert fts.should_run("X", 7702, 5000) is True    # different magic, independent


# ── 3. TP cascade ────────────────────────────────────────────────────────────

class _VP:
    def __init__(self, hvn, poc=0.0, lvn=None):
        self.hvn_zones = [{"low": lo, "high": hi} for lo, hi in hvn]
        self.lvn_zones = lvn or []
        self.poc = poc
        self.vah = 0.0
        self.val = 0.0


def test_hvn_to_hvn_picks_nearest_node_edges_beyond_the_ladder():
    # fulcrum 100, step 1, 2 legs/side -> outer legs at 102 / 98.
    # Nodes at (104,106) above and (92,94) below both clear the ladder.
    vp = _VP([(104.0, 106.0), (92.0, 94.0)])
    r = fts.tp_cascade(vp, edge=100.0, fulcrum=100.0, step=1.0, buy_n=2, sell_n=2,
                       trigger_kind="hvn_edge", edge_side="", hvn_reversion_bias=False)
    assert r["tp_up"] == 106.0    # nearest node TOP above the edge
    assert r["tp_down"] == 92.0   # nearest node BOTTOM below the edge
    assert r["top_leg"] == 102.0 and r["bot_leg"] == 98.0


def test_target_inside_the_ladder_is_rejected():
    # node top at 101 is above the edge but INSIDE the ladder (outer leg 102) — taking it
    # would make the grid unable to profit. This is the guard from grid_planner:577-590.
    vp = _VP([(100.5, 101.0)])
    r = fts.tp_cascade(vp, edge=100.0, fulcrum=100.0, step=1.0, buy_n=2, sell_n=2,
                       trigger_kind="hvn_edge", edge_side="", hvn_reversion_bias=False)
    assert r["tp_up"] == 0.0


def test_poc_reversion_retargets_the_fade_side_on_tapped_top():
    # tapped TOP -> the fade is DOWN, so the SELL side retargets POC (which must clear
    # the inner/outer sell leg at 98).
    vp = _VP([(104.0, 106.0), (92.0, 94.0)], poc=95.0)
    r = fts.tp_cascade(vp, edge=100.0, fulcrum=100.0, step=1.0, buy_n=2, sell_n=2,
                       trigger_kind="hvn_inside_touch", edge_side="top",
                       hvn_reversion_bias=True)
    assert r["tp_down"] == 95.0
    assert r["poc_override_side"] == "sell"
    assert r["tp_up"] == 106.0          # breakout side keeps its far HVN target


def test_poc_reversion_skipped_when_flag_off():
    vp = _VP([(104.0, 106.0), (92.0, 94.0)], poc=95.0)
    r = fts.tp_cascade(vp, edge=100.0, fulcrum=100.0, step=1.0, buy_n=2, sell_n=2,
                       trigger_kind="hvn_inside_touch", edge_side="top",
                       hvn_reversion_bias=False)
    assert r["tp_down"] == 92.0
    assert r["poc_override_side"] == ""


def test_skewed_ladder_uses_the_longer_side_for_the_guard():
    # sell_n 4 -> bot_leg 96, so a node bottom at 97 no longer clears the ladder.
    vp = _VP([(97.0, 99.0)])
    r = fts.tp_cascade(vp, edge=100.0, fulcrum=100.0, step=1.0, buy_n=2, sell_n=4,
                       trigger_kind="hvn_edge", edge_side="", hvn_reversion_bias=False)
    assert r["bot_leg"] == 96.0
    assert r["tp_down"] == 0.0


def test_no_zones_yields_no_targets():
    r = fts.tp_cascade(_VP([]), edge=100.0, fulcrum=100.0, step=1.0, buy_n=2, sell_n=2,
                       trigger_kind="hvn_edge", edge_side="", hvn_reversion_bias=False)
    assert r["tp_up"] == 0.0 and r["tp_down"] == 0.0 and r["n_hvn"] == 0
