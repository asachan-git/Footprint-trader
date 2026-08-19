"""Boot guard on the risk configuration.

Every control here is one the Jun22-26 tree did not have. The regression test
that matters is the last one: feed the preflight the config that was live on
14 July 2026 — the day the account lost 126,891 USC — and it must refuse to
start, naming each reason.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipeline.startup_check import _check_risk_config

GOOD = {"grid_levels": {"disaster_sl_usd": 20.0, "cycle_max_loss_usd": 2500.0,
                        "base_lot": 0.25, "max_base_lot_guard": 0.5,
                        "bias_trail_activate_usd": 125.0, "bias_book_frac": 0.5}}


def _run(cfg):
    fails, warns = [], []
    _check_risk_config(cfg, fails, warns)
    return fails, warns


def test_the_measured_profile_boots():
    assert _run(GOOD) == ([], [])


def test_refuses_to_boot_without_a_broker_stop():
    cfg = {"grid_levels": {**GOOD["grid_levels"], "disaster_sl_usd": 0}}
    fails, _ = _run(cfg)
    assert any("disaster_sl_usd" in f for f in fails)


def test_refuses_to_boot_without_a_loss_exit():
    cfg = {"grid_levels": {**GOOD["grid_levels"], "cycle_max_loss_usd": 0}}
    fails, _ = _run(cfg)
    assert any("bounded outcome" in f for f in fails)


def test_refuses_an_oversized_base_lot():
    cfg = {"grid_levels": {**GOOD["grid_levels"], "base_lot": 1.5}}
    fails, _ = _run(cfg)
    assert any("max_base_lot_guard" in f for f in fails)


def test_refuses_the_book_frac_sentinel():
    """100 meant 'close the entire side' — it removed the breakeven runner that
    is the whole point of the trail."""
    cfg = {"grid_levels": {**GOOD["grid_levels"], "bias_book_frac": 100}}
    fails, _ = _run(cfg)
    assert any("bias_book_frac" in f for f in fails)


def test_warns_when_the_trail_is_loose_for_the_lot_size():
    """Normalised per unit of base_lot, the measured June profile ran ~500."""
    cfg = {"grid_levels": {**GOOD["grid_levels"], "bias_trail_activate_usd": 1000.0,
                           "base_lot": 0.25}}
    fails, warns = _run(cfg)
    assert fails == [] and any("per unit of base_lot" in w for w in warns)


def test_rejects_an_unknown_setup_from_the_env(monkeypatch):
    """FB_SETUPS_* is not version-controlled — it is how hvn_edge went live two
    days before it existed in any setup list, and how a typo trades silently."""
    monkeypatch.setenv("FB_SETUPS_5M", "hvn_inside_touch hvn_egde")
    fails, _ = _run(GOOD)
    assert any("hvn_egde" in f for f in fails)


def test_the_2026_07_14_config_is_refused():
    """base_lot 1.5, no stop, no loss cap — the -126,891 day."""
    cfg = {"grid_levels": {"disaster_sl_usd": 0, "cycle_max_loss_usd": 0,
                           "base_lot": 1.5, "bias_book_frac": 0.5}}
    fails, _ = _run(cfg)
    assert len(fails) >= 3
