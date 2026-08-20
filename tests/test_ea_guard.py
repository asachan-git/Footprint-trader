"""EA self-report guards: two failures that produce no error anywhere."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from execution.ea_guard import check_ea_version, check_magic_window


def test_current_build_passes():
    assert check_ea_version("1.11") is None
    assert check_ea_version("1.12") is None
    assert check_ea_version("2.0") is None


def test_stale_build_is_flagged():
    """Pre-1.10 wipes the SL on every pending modify, so the disaster stop the
    server places is discarded by the terminal without any error."""
    msg = check_ea_version("1.05")   # the build the main tree still compiles
    assert msg and "inert" in msg


def test_a_silent_ea_is_treated_as_stale():
    """No version reported means an old build — fail toward the warning."""
    assert check_ea_version(None) is not None
    assert check_ea_version("") is not None


def test_unparseable_version_is_flagged_not_swallowed():
    assert check_ea_version("beta") is not None


def test_magics_inside_the_window_pass():
    assert check_magic_window([770013, 770052], 770000, 770110) is None


def test_a_magic_outside_the_window_is_flagged():
    """The 2026-08-06 shape: the EA trades it but stops reporting it, so the
    server sees a flat cycle and runs no exits against a live position."""
    msg = check_magic_window([770013, 774013], 770000, 770110)
    assert msg and "774013" in msg and "NO exit logic" in msg


def test_unreported_window_is_not_a_failure():
    """An EA that predates the self-report must not trip the check."""
    assert check_magic_window([770013], 0, 0) is None
    assert check_magic_window([770013], None, None) is None


def test_no_active_magics_is_not_a_failure():
    assert check_magic_window([], 770000, 770110) is None
