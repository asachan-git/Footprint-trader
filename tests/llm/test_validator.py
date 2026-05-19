from llm.schema import Decision
from llm.validator import validate


def test_flat_passes():
    d = Decision(side="flat", confidence=0.5, rationale="no edge")
    assert validate(d) is None


def test_long_rejects_sl_above_entry():
    d = Decision(side="long", entry=100, stop_loss=101, take_profit=110, confidence=0.7, rationale="x")
    assert validate(d) is not None


def test_long_rejects_low_rr():
    d = Decision(side="long", entry=100, stop_loss=99, take_profit=100.5, confidence=0.7, rationale="x")
    assert validate(d) is not None  # RR = 0.5


def test_short_passes_clean():
    d = Decision(side="short", entry=100, stop_loss=102, take_profit=96, confidence=0.7, rationale="Sell absorption at resistance with negative delta")
    assert validate(d) is None
