"""The one engine-owned model-to-list-price table and per-message USD estimate.

Stdlib only, no I/O, no spawn: consumers outside this package (DoE's
session_spend.py) load it by file path via importlib.util.spec_from_file_location.
price_table.json beside this file publishes the same table for non-Python
consumers; test_price_table.py fails when the two disagree, so edit both.
List prices stand in for subscription-limit weight. A model missing here prices
as None (unpriced), never as a guess.

Sonnet 5.5's $2/$10 comes from third-party aggregators (2026-10-17), and one of
them reports a scheduled rise to $3/$15. Re-check it against Anthropic's pricing
page when it next moves.
"""

from __future__ import annotations

from typing import Mapping
import re

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25

# USD per 1M tokens, keyed by model-id prefix. tier_* apply when the message
# prompt (input + cache_read + cache_creation) exceeds tier_prompt_tokens.
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "fast_multiplier": 2.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-5-5": {
        "input": 0.10,
        "output": 0.50,
        "tier_prompt_tokens": 100_000,
        "tier_input": 0.50,
        "tier_output": 2.50,
    },
}

# First-party model pages checked 2026-11-07. These are API-equivalent list
# prices, not a claim about a subscription invoice. Cache writes use 30m retention.
RATE_PROVENANCE: dict[str, dict[str, str | None]] = {}
for _model, _input, _output, _cache_mult in (
    ("gpt-6.2-sol", 2.0, 10.0, 0.05),
    ("gpt-6.1-sol", 2.0, 10.0, 0.05),
    ("gpt-6.1-terra", 1.0, 5.0, 0.05),
    ("gpt-6-astra", 10.0, 50.0, 0.1),
    ("gpt-6-luna", 0.1, 0.5, 0.1),
):
    PRICES[_model] = {
        "input": _input, "output": _output, "cache_read_mult": _cache_mult,
        "tier_prompt_tokens": 272_000, "tier_input": _input * 2, "tier_output": _output * 1.5,
        "fast_multiplier": 2.0, "flex_multiplier": 0.5, "batch_multiplier": 0.5,
    }
    if _model in {"gpt-6.2-sol", "gpt-6.1-sol", "gpt-6-astra"}:
        PRICES[_model]["ultrafast_multiplier"] = 6.0  # List rate; 6.2 promo is 3x through 2027-02-28.
    RATE_PROVENANCE[_model] = {
        "source": f"https://developers.openai.com/api/docs/models/{_model}",
        "verified_at": "2026-11-07", "basis": "API list prices; 30m cache writes",
        "effective_from": None,  # The source states current rates, not their start date.
    }

# GPT-6.2 Sol read from the OpenAI pricing page 2026-11-19: Standard $2 in / $0.10 cached / $2.50
# cache write / $10 out per 1M; Fast 2x, Batch 0.5x, Ultrafast list 6x; >272K prompt 2x in / 1.5x out.
RATE_PROVENANCE["gpt-6.2-sol"].update(
    source="https://developers.openai.com/api/docs/pricing", verified_at="2026-11-19",
)

PRICES["gpt-6.2-astra"] = {
    "input": 8.0, "output": 40.0, "cache_read_mult": 0.05,
    "tier_prompt_tokens": 272_000, "tier_input": 16.0, "tier_output": 60.0,
    "fast_multiplier": 2.0, "ultrafast_multiplier": 3.0,
}
RATE_PROVENANCE["gpt-6.2-astra"] = {
    "source": "https://developers.openai.com/api/docs/pricing",
    "verified_at": "2026-11-20", "basis": "API-equivalent estimate; current Ultrafast rate; not an invoice",
    "effective_from": None,
}

# Gemini 3.8 Flash standard paid tier through 2026-12-31; Google schedules $1.50/$7.50 (cache read
# $0.15) from 2027-01-01. Re-check the pricing page then. Cache writes carry no surcharge.
PRICES["gemini-3.8-flash"] = {"input": 0.75, "output": 3.75, "cache_read_mult": 0.1, "cache_write_mult": 1.0}
RATE_PROVENANCE["gemini-3.8-flash"] = {
    "source": "https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash",
    "verified_at": "2026-11-16", "basis": "API list prices, Standard paid tier, per 1M tokens; output includes thinking",
    "effective_from": None,  # The page states the rate for a window ending 2026-12-31, not its start.
}

# Gemini 4 family verified 2026-11-23: Carbon $4/$20 standard ($2/$10 intro thru 2026-11-30, 95% cache read
# discount), Neon $3/$15 standard (95% cache read discount), Hydrogen $1.50/$7.50 flat across 1M context.
for _gmodel, _ginput, _goutput, _gcache_mult, _gbasis in (
    ("gemini-4-carbon", 4.0, 20.0, 0.05,
     "API list prices, Standard paid tier ($4/$20; $2/$10 intro thru 2026-11-30), per 1M tokens; 95% cache read discount; output includes thinking"),
    ("gemini-4-neon", 3.0, 15.0, 0.05,
     "API list prices, Standard paid tier, per 1M tokens; 95% cache read discount; output includes thinking"),
    ("gemini-4-hydrogen", 1.5, 7.5, 0.1,
     "API list prices, Standard paid tier flat across 1M context, per 1M tokens; output includes thinking"),
):
    PRICES[_gmodel] = {
        "input": _ginput, "output": _goutput,
        "cache_read_mult": _gcache_mult, "cache_write_mult": 1.0,
    }
    RATE_PROVENANCE[_gmodel] = {
        "source": f"https://ai.google.dev/gemini-api/docs/pricing#{_gmodel}",
        "verified_at": "2026-11-23", "basis": _gbasis, "effective_from": None,
    }

_GEMINI_EFFORT = r"-(?:minimal|low|medium|high|xhigh|max)"


def model_family(model: str | None) -> str | None:
    """Return the PRICES key `model` starts with (dated suffixes drop), else None.

    Gemini ids are also accepted in agy's display form (`Gemini 3.8 Flash (Medium)`); an effort
    suffix or date resolves to the family, any other suffix (`-lite`, `-tts`) is a different model.
    """
    if not model:
        return None
    if model.lower().startswith("gemini"):
        model = re.sub(r"\s*\([^)]*\)\s*$", "", model).strip().lower().replace(" ", "-")
    for key in PRICES:
        if model.startswith(key):
            rest = model[len(key):]
            if key.startswith("gpt-") and rest and not re.fullmatch(r"-\d{4}-?\d{2}-?\d{2}", rest):
                continue
            if key.startswith("gemini-") and rest and not re.fullmatch(rf"({_GEMINI_EFFORT})?(-\d{{4}}-?\d{{2}}-?\d{{2}})?", rest):
                continue
            return key
    return None


def _n(usage: Mapping, field: str) -> int:
    v = usage.get(field)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def estimate_usd(model: str, usage: Mapping, speed: str | None = None) -> float | None:
    """List-price USD for ONE message; None for an unknown model, never 0.

    Pricing is per message because the Haiku tier keys on a single request's
    prompt size; sum the results, never price an aggregate.
    """
    family = model_family(model)
    if family is None:
        return None
    p = PRICES[family]
    inp, out = p["input"], p["output"]
    cache_read = _n(usage, "cache_read_input_tokens")
    cache_write = _n(usage, "cache_creation_input_tokens")
    prompt = _n(usage, "input_tokens") + cache_read + cache_write
    if "tier_prompt_tokens" in p and prompt > p["tier_prompt_tokens"]:
        inp, out = p["tier_input"], p["tier_output"]
    selected_speed = "fast" if speed == "priority" else speed
    if family.startswith("gpt-") and selected_speed not in {None, "standard", "default", "fast", "ultrafast", "flex", "batch"}:
        return None
    if selected_speed in {"fast", "ultrafast", "flex", "batch"}:
        if family.startswith("gpt-") and f"{selected_speed}_multiplier" not in p:
            return None
        mult = p.get(f"{selected_speed}_multiplier", 1.0)
        inp, out = inp * mult, out * mult
    return (
        _n(usage, "input_tokens") * inp
        + _n(usage, "output_tokens") * out
        + cache_read * inp * p.get("cache_read_mult", CACHE_READ_MULT)
        + cache_write * inp * p.get("cache_write_mult", CACHE_WRITE_MULT)
    ) / 1_000_000
