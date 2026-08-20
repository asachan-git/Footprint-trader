"""ratchet_candle_sl: the expansion candle-close SL trail must only ever tighten —
buy SL only moves up, sell SL only moves down — never the reverse.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from execution.exec_bridge import ExecBridge

ACCT, SYM, MAGIC = "SLTRAIL", "XAUUSD.pc", 770301


def test_buy_sl_only_moves_up():
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, MAGIC, "buy", 4500.0) is True
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, MAGIC, "buy", 4505.0) is True   # improves
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, MAGIC, "buy", 4502.0) is False  # would loosen
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, MAGIC, "buy", 4505.0) is False  # no-op, not better


def test_sell_sl_only_moves_down():
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, 770302, "sell", 4500.0) is True
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, 770302, "sell", 4495.0) is True   # improves
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, 770302, "sell", 4498.0) is False  # would loosen


def test_invalid_candidate_is_rejected():
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, 770303, "buy", 0.0) is False
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, 770303, "buy", -5.0) is False


def test_buy_and_sell_ratchets_are_independent():
    """Same magic, opposite sides — must not share state."""
    magic = 770304
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, magic, "buy", 4500.0) is True
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, magic, "sell", 4600.0) is True
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, magic, "buy", 4501.0) is True
    assert ExecBridge.ratchet_candle_sl(ACCT, SYM, magic, "sell", 4599.0) is True
