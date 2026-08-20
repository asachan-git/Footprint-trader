"""The sentinel bar: one poisoned row, three silent failures.

A forming-bar placeholder is stored with a max close_ts (9999999999) so it
sorts last. That single property broke three unrelated things at once, and
none of them raised: 5m/15m aggregation froze for ~2h while 1m kept flowing,
ATR read a 96pt phantom range off a stale price, and both fed the grid sizing
math. Guard at the write chokepoint, and again on each read path.
"""
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipeline.state_store import StateStore
from pipeline.types import Bar, OHLC

SENTINEL = 9_999_999_999


def _bar(ts, bar_id, tf="1m", o=4000.0, h=4001.0, lo=3999.0, c=4000.5):
    return Bar(bar_id=bar_id, symbol="XAUTUSDT", tf=tf, close_ts=ts,
               source="test", ohlc=OHLC(o=o, h=h, l=lo, c=c),
               bid_ladder=(), ask_ladder=())


def _isolated_store(tmp_path, monkeypatch):
    """StateStore persists to a module-level PERSIST_DIR; point it at tmp so a
    test never reads or writes the live footprint files."""
    import pipeline.state_store as ss
    monkeypatch.setattr(ss, "PERSIST_DIR", tmp_path)
    return ss.StateStore()


def test_put_refuses_a_sentinel_ts_bar(tmp_path, monkeypatch):
    """The chokepoint: it must never reach the file. The real one came from a
    leaked count-VP test fixture that put() happily persisted to live data."""
    st = _isolated_store(tmp_path, monkeypatch)
    assert st.put(_bar(1_780_000_000, "real|1")) is True
    assert st.put(_bar(SENTINEL, "XAUTUSDT|1m|9999999999|cnttest")) is False


def test_put_refuses_a_nonpositive_ts(tmp_path, monkeypatch):
    st = _isolated_store(tmp_path, monkeypatch)
    assert st.put(_bar(0, "zero|1")) is False


def test_a_rejected_bar_never_becomes_the_newest(tmp_path, monkeypatch):
    """The whole reason the sentinel was destructive: max ts sorts LAST, so it
    became `latest()` for every consumer."""
    st = _isolated_store(tmp_path, monkeypatch)
    st.put(_bar(1_780_000_000, "real|1"))
    st.put(_bar(SENTINEL, "sentinel|1"))
    assert st.recent("XAUTUSDT", "1m", 5)[-1].close_ts == 1_780_000_000


def test_atr_ignores_a_sentinel_bar(monkeypatch):
    """ATR sizes the grid step. A sentinel at 4130 while price is 4034 is a 96pt
    fake true-range -> oversized ladder -> TP lands inside it -> legs with tp=0."""
    from pipeline.features import atr as atr_mod
    real = [_bar(1_780_000_000 + i * 60, f"b{i}", o=4000, h=4001, lo=3999, c=4000)
            for i in range(20)]
    poisoned = real + [_bar(SENTINEL, "sentinel", o=4130, h=4130, lo=4130, c=4130)]

    class _S:
        def recent(self, *_a, **_k):
            return poisoned

    monkeypatch.setattr(atr_mod, "store", lambda: _S(), raising=False)
    monkeypatch.setitem(sys.modules, "pipeline.state_store",
                        types.SimpleNamespace(store=lambda: _S()))
    got = atr_mod.atr_from_store("XAUTUSDT", "1m", period=14)
    clean = atr_mod.atr(real[-15:], period=14)
    assert abs(got - clean) < 1e-6, "a sentinel bar must not reach the true-range"


def test_deserializer_tolerates_a_field_this_build_does_not_know():
    """Footprint files are append-only and shared across branches. Level.cnt was
    added 2026-07-07; every older tree then crashed on load with a bare TypeError
    and could not boot at all. A newer writer plus an older reader must degrade,
    not brick the store."""
    from pipeline.state_store import _level
    lvl = _level({"price": 4000.0, "vol": 1.5, "cnt": 9.0, "some_future_field": "x"})
    assert lvl.price == 4000.0 and lvl.vol == 1.5
    assert getattr(lvl, "cnt", 0.0) == 9.0


def test_deserializer_defaults_a_field_an_older_file_lacks():
    from pipeline.state_store import _level
    assert _level({"price": 4000.0, "vol": 1.5}).cnt == 0.0
