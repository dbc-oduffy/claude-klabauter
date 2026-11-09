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
    if _model in {"gpt-6.1-sol", "gpt-6-astra"}:
        PRICES[_model]["ultrafast_multiplier"] = 6.0
    RATE_PROVENANCE[_model] = {
        "source": f"https://developers.openai.com/api/docs/models/{_model}",
        "verified_at": "2026-11-07", "basis": "API list prices; 30m cache writes",
        "effective_from": None,  # The source states current rates, not their start date.
    }


def model_family(model: str | None) -> str | None:
    """Return the PRICES key `model` starts with (dated suffixes drop), else None."""
    if not model:
        return None
    for key in PRICES:
        if model.startswith(key):
            if key.startswith("gpt-") and model != key and not re.fullmatch(r"-\d{4}-?\d{2}-?\d{2}", model[len(key):]):
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
        + cache_write * inp * CACHE_WRITE_MULT
    ) / 1_000_000
