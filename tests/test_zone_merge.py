"""_merge_zone_tuples: rolling and cached spans arrive concatenated, unsorted."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from execution.zone_triggers import _merge_zone_tuples


def test_lowest_span_survives_when_input_is_unsorted():
    """The bug: seeding from zones[0] and walking the SORTED list merged every
    span below the seed into it, so the lowest real node was swallowed — price
    could sit inside a zone that no longer existed and nothing armed."""
    zones = [(4050.0, 4060.0), (4010.0, 4020.0), (4030.0, 4035.0)]
    assert _merge_zone_tuples(zones) == [(4010.0, 4020.0), (4030.0, 4035.0), (4050.0, 4060.0)]


def test_overlapping_spans_still_collapse():
    """The actual job: one node reported twice at slightly offset prices must
    become one fulcrum, not two near-duplicate edges."""
    assert _merge_zone_tuples([(4050.0, 4060.0), (4058.0, 4065.0)]) == [(4050.0, 4065.0)]


def test_touching_spans_collapse():
    assert _merge_zone_tuples([(4050.0, 4060.0), (4060.0, 4070.0)]) == [(4050.0, 4070.0)]


def test_a_span_fully_inside_another_is_absorbed():
    assert _merge_zone_tuples([(4000.0, 4100.0), (4030.0, 4040.0)]) == [(4000.0, 4100.0)]


def test_empty_and_single():
    assert _merge_zone_tuples([]) == []
    assert _merge_zone_tuples([(4010.0, 4020.0)]) == [(4010.0, 4020.0)]
