from prompts.builder import cached_prefix, active_version


def test_active_version_present():
    v = active_version()
    assert v.startswith("v")


def test_cached_prefix_includes_system_and_fewshot():
    prefix = cached_prefix(few_shot_count=2)
    assert "FootprintBiot" in prefix or "orderflow" in prefix.lower()
    assert "FEW-SHOT" in prefix.upper() or "examples" in prefix.lower()
