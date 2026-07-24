"""hvn_inside_touch wick-tap buffer.

A wick that stops just short of the node edge should still register as a tap when a
buffer is configured, and must NOT when the buffer is 0 (Jun22-native exact touch).
"""

import execution.zone_triggers as zt


class _O:
    def __init__(self, o, h, l, c):
        self.o, self.h, self.l, self.c = o, h, l, c


class _B:
    def __init__(self, o, h, l, c, ts=1_000_000):
        self.ohlc = _O(o, h, l, c)
        self.close_ts = ts
        self.bar_id = "b"


def _setup(monkeypatch, buffer, buf_pct=0.0):
    # one node 4020-4030; a candle that closes inside (4026) with a HIGH of 4029.85 —
    # 0.15 short of the 4030 top edge. Exact touch misses; a >=0.15 buffer catches it.
    monkeypatch.setattr(zt, "_zone_cfg",
                        lambda: {"hvn_touch_buffer": buffer, "hvn_touch_buffer_pct": buf_pct})
    monkeypatch.setattr(zt, "_session_hvn_zones",
                        lambda s, tf, bars: ([(4020.0, 4030.0)], "NY"))
    import pipeline.state_store as ss
    bars = [_B(4025, 4029.85, 4024, 4026)] * 3   # >=2 bars; last is the tap candle
    monkeypatch.setattr(ss.store(), "recent", lambda s, tf, n: bars)


def test_exact_touch_misses_the_near_wick(monkeypatch):
    _setup(monkeypatch, buffer=0.0)
    assert zt._t_hvn_inside_touch("SYM", "15m", 4026.0) is None   # 0.15 short, no buffer


def test_buffer_catches_the_near_wick(monkeypatch):
    _setup(monkeypatch, buffer=0.2)
    t = zt._t_hvn_inside_touch("SYM", "15m", 4026.0)
    assert t is not None
    assert t.kind == "hvn_inside_touch"
    assert t.context["edge"] == "top"
    assert abs(t.fulcrum_price - 4030.0) < 1e-9        # fulcrum is still the edge, not the wick


def test_width_relative_floor(monkeypatch):
    # flat buffer 0.05 is too small (< 0.15 gap), but width×pct = 10 × 0.10 = 1.0 catches it.
    _setup(monkeypatch, buffer=0.05, buf_pct=0.10)
    assert zt._t_hvn_inside_touch("SYM", "15m", 4026.0) is not None


def test_close_beyond_edge_is_still_a_breakout(monkeypatch):
    # buffer must not turn a genuine breakout (close OUTSIDE the node) into a touch.
    monkeypatch.setattr(zt, "_zone_cfg", lambda: {"hvn_touch_buffer": 0.2})
    monkeypatch.setattr(zt, "_session_hvn_zones",
                        lambda s, tf, bars: ([(4020.0, 4030.0)], "NY"))
    import pipeline.state_store as ss
    bars = [_B(4029, 4032, 4028, 4031)] * 3          # closes 4031 — ABOVE the node
    monkeypatch.setattr(ss.store(), "recent", lambda s, tf, n: bars)
    assert zt._t_hvn_inside_touch("SYM", "15m", 4031.0) is None
