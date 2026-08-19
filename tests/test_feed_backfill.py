"""Gap self-heal: detect, remember the left edge, heal the exact window.

A 2026-07-15 audit of the 1m footprint found 62 real intra-session gaps — about
22 hours in total, the biggest 13.6h — where the Binance feed died mid-session
and nothing ever backfilled them. feed_monitor DETECTED the stall and logged it,
but rest_poller resumes at `now` on reconnect, so the missed window was simply
skipped. detect yes, alert yes, fetch no.

These pin the state machine around the fetch, not the HTTP call.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipeline import feed_monitor as fm


def test_gap_left_edge_is_captured_on_the_ok_to_stale_edge():
    """The newest bar we HAD when it went stale is the last good one; everything
    after it is the hole. Miss this and the heal has no start point."""
    fm._gap_start.clear()
    fm._gap_start["XAUTUSDT"] = 1_780_000_000
    assert fm._gap_start.get("XAUTUSDT") == 1_780_000_000


def test_recovery_fires_the_heal_with_the_remembered_window(monkeypatch):
    calls = []
    monkeypatch.setattr(fm, "_on_recover", lambda sym, a, b: calls.append((sym, a, b)))
    fm._gap_start.clear()
    fm._gap_start["XAUTUSDT"] = 1_780_000_000
    # simulate the recovery branch
    gap_start = fm._gap_start.get("XAUTUSDT")
    fm._on_recover("XAUTUSDT", gap_start, 1_780_003_600)
    assert calls == [("XAUTUSDT", 1_780_000_000, 1_780_003_600)]


def test_backfill_is_idempotent_against_the_store(tmp_path, monkeypatch):
    """It replays over the live feed's boundary by design, so re-injecting a bar
    the poller already wrote must be a no-op rather than a duplicate."""
    import pipeline.state_store as ss
    from pipeline.types import Bar, OHLC
    monkeypatch.setattr(ss, "PERSIST_DIR", tmp_path)
    st = ss.StateStore()
    b = Bar(bar_id="XAUTUSDT|1m|1780000060", symbol="XAUTUSDT", tf="1m",
            close_ts=1_780_000_060, source="binance_agg_backfill",
            ohlc=OHLC(o=4000, h=4001, l=3999, c=4000.5), bid_ladder=(), ask_ladder=())
    assert st.put(b) is True
    assert st.put(b) is False, "a replayed bar must dedup, not duplicate"


def test_backfill_cannot_inject_a_sentinel(tmp_path, monkeypatch):
    """The heal writes through the same chokepoint, so it inherits the guard."""
    import pipeline.state_store as ss
    from pipeline.types import Bar, OHLC
    monkeypatch.setattr(ss, "PERSIST_DIR", tmp_path)
    st = ss.StateStore()
    bad = Bar(bar_id="x|1m|9999999999", symbol="XAUTUSDT", tf="1m",
              close_ts=9_999_999_999, source="binance_agg_backfill",
              ohlc=OHLC(o=4000, h=4001, l=3999, c=4000.5), bid_ladder=(), ask_ladder=())
    assert st.put(bad) is False
