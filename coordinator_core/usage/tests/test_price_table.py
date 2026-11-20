"""Pins every R1 price number in coordinator_core/usage/price_table.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

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


def _json_table():
    return json.loads((_PATH.with_name("price_table.json")).read_text(encoding="utf-8"))


def test_json_matches_python_table(pt):
    data = _json_table()
    assert isinstance(data["schema_version"], int)
    assert data["cache_read_mult"] == pt.CACHE_READ_MULT
    assert data["cache_write_mult"] == pt.CACHE_WRITE_MULT
    assert set(data["models"]) == set(pt.PRICES)
    approx = pytest.approx
    for key, p in pt.PRICES.items():
        m = data["models"][key]
        assert m["input"] == approx(p["input"])
        assert m["output"] == approx(p["output"])
        assert m["cache_read"] == approx(p["input"] * p.get("cache_read_mult", pt.CACHE_READ_MULT))
        assert m["cache_creation"] == approx(p["input"] * p.get("cache_write_mult", pt.CACHE_WRITE_MULT))
        assert m.get("fast_multiplier") == p.get("fast_multiplier")
        tier = m.get("long_prompt_tier")
        if "tier_prompt_tokens" in p:
            assert tier["prompt_tokens_over"] == p["tier_prompt_tokens"]
            assert tier["input"] == approx(p["tier_input"])
            assert tier["output"] == approx(p["tier_output"])
            assert tier["cache_read"] == approx(p["tier_input"] * p.get("cache_read_mult", pt.CACHE_READ_MULT))
            assert tier["cache_creation"] == approx(p["tier_input"] * pt.CACHE_WRITE_MULT)
        else:
            assert tier is None


def test_gemini_flash_prices_at_list_and_has_provenance(pt):
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert pt.estimate_usd("Gemini 3.8 Flash (Medium)", u) == pytest.approx(4.5)
    assert pt.estimate_usd("gemini-3.8-flash", {"cache_read_input_tokens": 1_000_000}) == pytest.approx(0.075)
    assert pt.estimate_usd("gemini-3.8-flash", {"cache_creation_input_tokens": 1_000_000}) == pytest.approx(0.75)
    prov = pt.RATE_PROVENANCE["gemini-3.8-flash"]
    assert prov["source"].startswith("https://ai.google.dev/gemini-api/docs/pricing")
    assert prov["verified_at"]


def test_gemini_family_resolves_effort_and_display_forms_but_not_other_models(pt):
    for ok in ("Gemini 3.8 Flash (Medium)", "gemini-3.8-flash-high", "gemini-3.8-flash", "gemini-3.8-flash-2026-11-01"):
        assert pt.model_family(ok) == "gemini-3.8-flash"
    for other in ("Gemini 3.8 Flash TTS", "gemini-3.8-flash-lite", "Gemini 3.1 Pro", "gemini-4-argon-high"):
        assert pt.model_family(other) is None
        assert pt.estimate_usd(other, {"input_tokens": 5}) is None


def test_gpt_62_sol_prices_cached_input_at_list_and_has_provenance(pt):
    assert pt.model_family("gpt-6.2-sol") == "gpt-6.2-sol"
    assert pt.model_family("gpt-6.2-sol-2026-11-01") == "gpt-6.2-sol"
    assert pt.model_family("gpt-6.2-sol-mini") is None
    u = {"input_tokens": 100_000, "cache_read_input_tokens": 100_000, "output_tokens": 100_000}
    assert pt.estimate_usd("gpt-6.2-sol", u) == pytest.approx(0.2 + 0.01 + 1.0)
    assert pt.estimate_usd("gpt-6.2-sol", {"input_tokens": 300_000}) == pytest.approx(1.2)
    assert pt.estimate_usd("gpt-6.2-sol", {"input_tokens": 200_000}, speed="fast") == pytest.approx(0.8)
    prov = pt.RATE_PROVENANCE["gpt-6.2-sol"]
    assert prov["source"] == "https://developers.openai.com/api/docs/pricing"
    assert prov["verified_at"] == "2026-11-19"
    assert _json_table()["models"]["gpt-6.2-sol"]["provenance"]["verified_at"] == prov["verified_at"]


def test_sonnet_prices_at_list(pt):
    assert pt.estimate_usd("claude-sonnet-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == 12.0
