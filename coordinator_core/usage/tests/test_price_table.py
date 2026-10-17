"""Pins every R1 price number in coordinator_core/usage/price_table.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "price_table.py"


@pytest.fixture(scope="module")
def pt():
    spec = importlib.util.spec_from_file_location("price_table_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stdlib_only_imports():
    src = _PATH.read_text(encoding="utf-8")
    assert "coordinator_core" not in src.replace("coordinator_core/usage", "")
    assert "subprocess" not in src
    assert "open(" not in src


def test_token_fields(pt):
    assert pt.TOKEN_FIELDS == (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )


def test_opus_list_price(pt):
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert pt.estimate_usd("claude-opus-5-5", u, None) == pytest.approx(24.0)


def test_opus_fast_doubles(pt):
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert pt.estimate_usd("claude-opus-5-5", u, "fast") == pytest.approx(48.0)
    assert pt.estimate_usd("claude-opus-5-5", u, "standard") == pytest.approx(24.0)


def test_cache_multipliers(pt):
    assert pt.estimate_usd(
        "claude-opus-5-5", {"cache_read_input_tokens": 1_000_000}, None
    ) == pytest.approx(0.4)
    assert pt.estimate_usd(
        "claude-opus-5-5", {"cache_creation_input_tokens": 1_000_000}, None
    ) == pytest.approx(5.0)


def test_haiku_base_and_tier(pt):
    base = {"input_tokens": 100_000, "output_tokens": 1_000_000}
    assert pt.estimate_usd("claude-haiku-5-5", base, None) == pytest.approx(0.01 + 0.5)
    over = {"input_tokens": 100_001, "output_tokens": 1_000_000}
    assert pt.estimate_usd("claude-haiku-5-5", over, None) == pytest.approx(
        100_001 * 0.5 / 1e6 + 2.5
    )


def test_haiku_tier_counts_cache_in_prompt(pt):
    u = {"input_tokens": 10, "cache_read_input_tokens": 100_000}
    assert pt.estimate_usd("claude-haiku-5-5", u, None) == pytest.approx(
        (10 * 0.5 + 100_000 * 0.5 * 0.1) / 1e6
    )


def test_dated_suffix_normalised(pt):
    assert pt.model_family("claude-opus-5-5-20260801") == "claude-opus-5-5"
    assert pt.model_family("claude-haiku-5-5") == "claude-haiku-5-5"


def test_unknown_model_is_none_not_zero(pt):
    assert pt.estimate_usd("claude-sonnet-4-6", {"input_tokens": 5}, None) is None
    assert pt.estimate_usd("", {"input_tokens": 5}, None) is None
    assert pt.model_family(None) is None


def test_opus_costs_more_than_three_haiku(pt):
    opus = pt.estimate_usd(
        "claude-opus-5-5", {"input_tokens": 4_000, "output_tokens": 600, "cache_read_input_tokens": 40_000}, None
    )
    haiku = 3 * pt.estimate_usd(
        "claude-haiku-5-5", {"input_tokens": 20_000, "output_tokens": 2_000, "cache_read_input_tokens": 40_000}, None
    )
    assert opus > haiku


def test_sonnet_prices_at_list(pt):
    assert pt.estimate_usd("claude-sonnet-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == 12.0
