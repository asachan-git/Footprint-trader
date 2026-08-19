"""Weekend exclusion for session-anchored symbols.

Gold analysis runs on Binance XAUT, which keeps ticking Saturday and Sunday on
a thin one-sided book while the real gold market is shut. Nothing in the cache
knew that, so weekend prints reached live decisions three ways: as their own
cached daily node, through the prior-day vacuum fallback that walks
get_history, and inside the weekly profile hvn_edge scans alongside the daily.
22 June 2026 was a Monday and the first gold session of the profitable run.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipeline.features import vp_cache as vc


def test_weekend_session_keys_are_recognised():
    assert vc._is_weekend_key("2026-06-27")      # Saturday
    assert vc._is_weekend_key("2026-06-28")      # Sunday
    assert not vc._is_weekend_key("2026-06-26")  # Friday
    assert not vc._is_weekend_key("2026-06-22")  # Monday
    assert not vc._is_weekend_key("not-a-date")


def test_gold_is_not_treated_as_24_7_but_crypto_is():
    assert "XAUTUSDT" not in vc._ALWAYS_24_7
    assert "BTCUSDT" in vc._ALWAYS_24_7


def test_weekly_window_is_five_weekdays_for_a_session_anchored_symbol():
    """The trailing 48h of an ISO week is the weekend; hvn_edge scans the weekly
    profile for a fulcrum, so a weekend-fattened node could win it outright."""
    start, end = vc._week_bounds("2026-W26", 22)
    assert (end - start) / 86400 == 5.0


def test_weekly_window_stays_seven_days_for_crypto():
    start, end = vc._week_bounds("2026-W26", 0, always_24_7=True)
    assert (end - start) / 86400 == 7.0


def test_get_history_hides_weekend_profiles_for_gold(monkeypatch):
    """The prior-day vacuum fallback walks get_history. A Sat/Sun profile written
    before the build-time filter existed must not be borrowable."""
    fake = {"XAUTUSDT": {"daily": {
        "2026-06-25": {"poc": 4000.0}, "2026-06-26": {"poc": 4010.0},
        "2026-06-27": {"poc": 4020.0},          # Saturday
        "2026-06-28": {"poc": 4030.0},          # Sunday
    }}}
    monkeypatch.setattr(vc, "_load", lambda: fake)
    keys = [e["period_key"] for e in vc.get_history("XAUTUSDT", "daily", n=5)]
    assert keys == ["2026-06-25", "2026-06-26"]


def test_get_history_keeps_weekend_profiles_for_crypto(monkeypatch):
    fake = {"BTCUSDT": {"daily": {
        "2026-06-26": {"poc": 1.0}, "2026-06-27": {"poc": 2.0}, "2026-06-28": {"poc": 3.0},
    }}}
    monkeypatch.setattr(vc, "_load", lambda: fake)
    keys = [e["period_key"] for e in vc.get_history("BTCUSDT", "daily", n=5)]
    assert keys == ["2026-06-26", "2026-06-27", "2026-06-28"]
