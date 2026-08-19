"""Analysis frame vs venue frame.

Detection compares Binance OHLC bars against zone boundaries. Fetching those
boundaries through the offset-applying face tested a Binance bar against a
Vantage-shifted edge, so the offset error suppressed valid touches outright.
The offset belongs at the drawing boundary and nowhere else.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipeline.features import vp_cache as vc

FAKE = {"XAUTUSDT": {
    "venue_price_offset": -5.21,
    "session_start_utc": 22,
    "daily": {"2026-06-25": {"poc": 4000.0, "vah": 4010.0, "val": 3990.0,
                             "hvn_zones": [{"low": 3995.0, "high": 4005.0}]}},
}}


def test_raw_is_unshifted_and_get_is_shifted(monkeypatch):
    monkeypatch.setattr(vc, "_load", lambda: FAKE)
    monkeypatch.setattr(vc, "_session_day_key", lambda ts, anchor: "2026-06-25")
    raw = vc.get_raw("XAUTUSDT", "daily")
    ven = vc.get("XAUTUSDT", "daily")
    assert raw["poc"] == 4000.0, "detection must see the stored analysis price"
    assert ven["poc"] != raw["poc"], "the drawing face must carry the venue offset"
    assert abs(ven["poc"] - (4000.0 - 5.21)) < 1e-6


def test_get_offset_is_public_for_the_drawing_boundary(monkeypatch):
    monkeypatch.setattr(vc, "_load", lambda: FAKE)
    assert vc.get_offset("XAUTUSDT") == -5.21
    assert vc.get_offset("NOTHING") == 0.0


def test_history_raw_is_unshifted(monkeypatch):
    monkeypatch.setattr(vc, "_load", lambda: FAKE)
    hist = vc.get_history_raw("XAUTUSDT", "daily", n=5)
    assert hist and hist[-1]["poc"] == 4000.0


def test_detection_modules_never_import_the_shifted_face():
    """A regression guard on the import surface itself: the grid detection path
    must not reach for the offset-applying getters."""
    root = pathlib.Path(__file__).resolve().parent.parent
    for mod in ("execution/zone_triggers.py", "execution/grid_planner.py"):
        src = (root / mod).read_text()
        assert "vp_cache import get as " not in src, f"{mod} fetches venue-frame VP"
        assert "vp_cache import get_history\n" not in src, f"{mod} fetches venue-frame history"
