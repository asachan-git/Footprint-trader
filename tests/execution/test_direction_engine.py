"""Tests for execution.direction_engine — confirmation vote + shape_regime_agreement."""
import pytest
from execution.direction_engine import (
    _shape_regime_agreement, _confirmation_vote, decide_direction, FLAT_THRESHOLD,
)


class TestShapeRegimeAgreement:
    def test_p_trend_down_boost(self):
        assert _shape_regime_agreement("P", "trend_down") == 1.2

    def test_b_trend_up_boost(self):
        assert _shape_regime_agreement("b", "trend_up") == 1.2

    def test_d_range_boost(self):
        assert _shape_regime_agreement("D", "range") == 1.1

    def test_strong_disagreement_penalty(self):
        # P (top-heavy) vs trend_up = strong disagreement
        mult = _shape_regime_agreement("P", "trend_up")
        assert mult == 0.7

    def test_b_trend_down_disagreement(self):
        mult = _shape_regime_agreement("b", "trend_down")
        assert mult == 0.7

    def test_no_data_neutral(self):
        assert _shape_regime_agreement("", "range") == 1.0

    def test_bimodal_uncertain_neutral(self):
        assert _shape_regime_agreement("B", "uncertain") == 1.0


class TestConfirmationVote:
    def test_returns_none_when_no_store_data(self):
        # Fresh process — no state_store bars → should return None, not raise
        from unittest.mock import patch
        with patch("pipeline.state_store.StateStore.recent", return_value=[]):
            result = _confirmation_vote("XAUTUSDT", "1m")
        assert result is None

    def test_returns_none_on_import_error(self):
        from unittest.mock import patch
        with patch("pipeline.features.confirmation.from_store",
                   side_effect=ImportError("missing")):
            result = _confirmation_vote("XAUTUSDT", "1m")
        assert result is None

    def test_absorption_long_produces_long_vote(self):
        from unittest.mock import patch, MagicMock
        from pipeline.features.confirmation import PatternResult
        long_abs = PatternResult(
            pattern="absorption", side="long", weight_total=0.75,
            fired=True, confidence=0.75, conditions=[],
        )
        no_exh = PatternResult(
            pattern="exhaustion", side="neutral", weight_total=0.0,
            fired=False, confidence=0.0, conditions=[],
        )
        with patch("pipeline.features.confirmation.from_store", return_value=(long_abs, no_exh)):
            vote = _confirmation_vote("XAUTUSDT", "1m")
        assert vote is not None
        assert vote.direction == 1.0
        assert vote.strength <= 0.85

    def test_exhaustion_short_produces_short_vote(self):
        from unittest.mock import patch
        from pipeline.features.confirmation import PatternResult
        no_abs = PatternResult(pattern="absorption", side="neutral", weight_total=0.0,
                               fired=False, confidence=0.0, conditions=[])
        short_exh = PatternResult(pattern="exhaustion", side="short", weight_total=0.80,
                                  fired=True, confidence=0.80, conditions=[])
        with patch("pipeline.features.confirmation.from_store", return_value=(no_abs, short_exh)):
            vote = _confirmation_vote("XAUTUSDT", "1m")
        assert vote is not None
        assert vote.direction == -1.0


class TestDecideDirection:
    def test_no_bars_returns_flat(self):
        from unittest.mock import patch
        with patch("pipeline.state_store.StateStore.recent", return_value=[]):
            result = decide_direction("XAUTUSDT", "1m")
        assert result.side == "flat"

    def test_flat_below_threshold(self):
        from unittest.mock import patch
        from execution.direction_engine import collect_votes, Vote
        zero_votes = [Vote("cvd", 0.1, 0.01, "near zero")]
        with patch("execution.direction_engine.collect_votes", return_value=zero_votes):
            result = decide_direction("XAUTUSDT", "1m")
        assert result.side == "flat"
        assert abs(result.score) < FLAT_THRESHOLD

    def test_strong_long_votes_return_long(self):
        from unittest.mock import patch
        from execution.direction_engine import Vote
        strong_votes = [
            Vote("cvd", 1.0, 0.65, "strong cvd"),
            Vote("wave", 1.0, 0.90, "impulse"),
            Vote("confirmation", 1.0, 0.85, "continuation"),
        ]
        with patch("execution.direction_engine.collect_votes", return_value=strong_votes), \
             patch("execution.direction_engine._shape_regime_agreement", return_value=1.0):
            result = decide_direction("XAUTUSDT", "1m")
        assert result.side == "long"
        assert result.bias_strength >= 3
