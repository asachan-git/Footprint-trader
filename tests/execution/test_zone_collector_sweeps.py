"""Tests for zone_collector sweep wick zones."""
import pytest
from unittest.mock import patch, MagicMock
from execution.zone_collector import collect, Zone


def _make_sweep_event(sweep_type, wick_extreme, swept_level, delta_confirms=True):
    ev = MagicMock()
    ev.sweep_type = sweep_type
    ev.wick_extreme = wick_extreme
    ev.swept_level = swept_level
    ev.delta_confirms = delta_confirms
    return ev


def test_confirmed_sweep_low_added_as_zone():
    sweep = _make_sweep_event("sweep_low", wick_extreme=4440.0, swept_level=4442.0)
    with patch("pipeline.features.sweep.active_sweeps", return_value=[sweep]), \
         patch("pipeline.features.vp_cache.get", return_value=None), \
         patch("pipeline.features.swing.get", return_value=[]):
        zones = collect("XAUTUSDT", direction="long", anchor=4460.0,
                        htf_bars=None, min_gap=0)
    sources = [z.source for z in zones]
    assert any("sweep_low" in s for s in sources), f"No sweep_low zone found. zones: {zones}"


def test_unconfirmed_sweep_excluded():
    sweep = _make_sweep_event("sweep_low", wick_extreme=4440.0, swept_level=4442.0,
                              delta_confirms=False)
    with patch("pipeline.features.sweep.active_sweeps", return_value=[sweep]), \
         patch("pipeline.features.vp_cache.get", return_value=None), \
         patch("pipeline.features.swing.get", return_value=[]):
        zones = collect("XAUTUSDT", direction="long", anchor=4460.0,
                        htf_bars=None, min_gap=0)
    assert not any("sweep" in z.source for z in zones)


def test_sweep_zone_strength_is_085():
    sweep = _make_sweep_event("sweep_high", wick_extreme=4480.0, swept_level=4478.0)
    with patch("pipeline.features.sweep.active_sweeps", return_value=[sweep]), \
         patch("pipeline.features.vp_cache.get", return_value=None), \
         patch("pipeline.features.swing.get", return_value=[]):
        zones = collect("XAUTUSDT", direction="short", anchor=4460.0,
                        htf_bars=None, min_gap=0)
    sweep_zones = [z for z in zones if "sweep" in z.source]
    assert sweep_zones, "No sweep zone returned"
    assert sweep_zones[0].strength == pytest.approx(0.85)


def test_sweep_uses_wick_extreme_price():
    sweep = _make_sweep_event("sweep_low", wick_extreme=4437.5, swept_level=4442.0)
    with patch("pipeline.features.sweep.active_sweeps", return_value=[sweep]), \
         patch("pipeline.features.vp_cache.get", return_value=None), \
         patch("pipeline.features.swing.get", return_value=[]):
        zones = collect("XAUTUSDT", direction="long", anchor=4460.0,
                        htf_bars=None, min_gap=0)
    sweep_zones = [z for z in zones if "sweep" in z.source]
    assert sweep_zones
    assert sweep_zones[0].price == pytest.approx(4437.5)


def test_sweep_fallback_to_swept_level_when_wick_zero():
    sweep = _make_sweep_event("sweep_low", wick_extreme=0.0, swept_level=4442.0)
    with patch("pipeline.features.sweep.active_sweeps", return_value=[sweep]), \
         patch("pipeline.features.vp_cache.get", return_value=None), \
         patch("pipeline.features.swing.get", return_value=[]):
        zones = collect("XAUTUSDT", direction="long", anchor=4460.0,
                        htf_bars=None, min_gap=0)
    sweep_zones = [z for z in zones if "sweep" in z.source]
    assert sweep_zones
    assert sweep_zones[0].price == pytest.approx(4442.0)
