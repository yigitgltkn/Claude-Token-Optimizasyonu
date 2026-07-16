import pytest

from rules import pricing


def test_canonical_exact_and_dated_ids():
    assert pricing.canonical("claude-sonnet-5") == "claude-sonnet-5"
    assert pricing.canonical("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert pricing.canonical("claude-opus-4-8") == "claude-opus-4-8"
    assert pricing.canonical("<synthetic>") is None
    assert pricing.canonical(None) is None
    assert pricing.canonical("") is None


def test_lookup_returns_pricing_or_none():
    p = pricing.lookup("claude-sonnet-5")
    assert p.input_per_mtok == 3.0
    assert p.output_per_mtok == 15.0
    assert pricing.lookup("unknown-model") is None


def test_turn_cost_per_component():
    m = "claude-sonnet-5"  # $3 in / $15 out per MTok
    assert pricing.turn_cost_usd(m, input_tokens=1_000_000) == pytest.approx(3.0)
    assert pricing.turn_cost_usd(m, output_tokens=1_000_000) == pytest.approx(15.0)
    assert pricing.turn_cost_usd(m, cache_read=1_000_000) == pytest.approx(0.3)
    assert pricing.turn_cost_usd(m, cache_5m=1_000_000) == pytest.approx(3.75)
    assert pricing.turn_cost_usd(m, cache_1h=1_000_000) == pytest.approx(6.0)


def test_turn_cost_unsplit_cache_creation_priced_as_1h():
    usd = pricing.turn_cost_usd("claude-sonnet-5", cache_creation=1_000_000)
    assert usd == pytest.approx(6.0)


def test_turn_cost_split_takes_precedence_over_unsplit_total():
    usd = pricing.turn_cost_usd(
        "claude-sonnet-5", cache_creation=1_000_000, cache_5m=1_000_000
    )
    assert usd == pytest.approx(3.75)  # split known -> unsplit total ignored


def test_turn_cost_unknown_model_returns_none():
    assert pricing.turn_cost_usd("<synthetic>", input_tokens=100) is None
    assert pricing.turn_cost_usd(None, input_tokens=100) is None
