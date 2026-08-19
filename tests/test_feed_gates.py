"""Refuse to arm on data we do not trust.

Both gates fail OPEN by design: a hiccup inside a guard must never itself
freeze arming. Live cycles are never affected — their exits ride the EA's
venue-price poll, which is independent of the analysis feed.
"""
import pathlib
import sys
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from server.routes.exec_bridge import _feed_is_stale, _zone_shift_too_far

CFG = {"feed_max_age_s": 180, "zone_shift_max_usd": 6.0}


def _stub_store(monkeypatch, bars):
    monkeypatch.setitem(sys.modules, "pipeline.state_store",
                        types.SimpleNamespace(store=lambda: types.SimpleNamespace(
                            recent=lambda *a, **k: bars)))


def _bar(ts):
    return types.SimpleNamespace(close_ts=ts, ohlc=types.SimpleNamespace(c=4000.0))


def test_fresh_feed_allows_arming(monkeypatch):
    _stub_store(monkeypatch, [_bar(time.time() - 30)])
    assert _feed_is_stale("XAUTUSDT", CFG) is None


def test_dead_feed_suppresses_arming(monkeypatch):
    """2026-07-15: the Binance process died, bars stopped, and nothing stopped
    the grid arming on frozen zones."""
    _stub_store(monkeypatch, [_bar(time.time() - 900)])
    age = _feed_is_stale("XAUTUSDT", CFG)
    assert age is not None and age > 180


def test_a_sentinel_bar_cannot_pass_as_fresh(monkeypatch):
    """The frozen placeholder sorts last; it must not read as liveness."""
    _stub_store(monkeypatch, [_bar(time.time() - 900), _bar(9_999_999_999)])
    assert _feed_is_stale("XAUTUSDT", CFG) is not None


def test_no_real_bars_is_maximally_stale(monkeypatch):
    _stub_store(monkeypatch, [])
    assert _feed_is_stale("XAUTUSDT", CFG) == 1e9


def test_gate_is_disableable(monkeypatch):
    _stub_store(monkeypatch, [_bar(time.time() - 9000)])
    assert _feed_is_stale("XAUTUSDT", {"feed_max_age_s": 0}) is None


def test_normal_basis_passes():
    assert _zone_shift_too_far(4003.0, 4000.0, CFG) is None


def test_blown_out_basis_skips_the_arm():
    """One feed lagged or stalled — an absolutely priced grid built off that
    basis lands at the wrong price."""
    assert _zone_shift_too_far(4020.0, 4000.0, CFG) == 20.0


def test_pct_cap_overrides_usd_so_it_scales_to_btc():
    cfg = {"zone_shift_max_usd": 6.0, "zone_shift_max_pct": 0.0015}
    assert _zone_shift_too_far(100_000.0, 99_950.0, cfg) is None      # 50 < 150
    assert _zone_shift_too_far(100_000.0, 99_700.0, cfg) == 300.0     # 300 > 150


def test_both_gates_fail_open_on_bad_input():
    assert _zone_shift_too_far(0.0, 4000.0, CFG) is None
    assert _zone_shift_too_far(4000.0, 4000.0, {}) is None
