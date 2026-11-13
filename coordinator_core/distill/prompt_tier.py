"""Shared long-prompt tier arithmetic: the Haiku 100k-token boundary and a greedy packer under it.

Token proxy is bytes/4, the same proxy `benchmarks/distill_run_cost.py` prices with. Pure; no I/O.
"""

from __future__ import annotations

from typing import Callable, TypeVar

__all__ = ["LONG_PROMPT_TIER_TOKENS", "BYTES_PER_TOKEN", "estimate_tokens", "over_tier", "split_to_tier"]

LONG_PROMPT_TIER_TOKENS = 100_000
BYTES_PER_TOKEN = 4

T = TypeVar("T")


def estimate_tokens(n_bytes: int) -> int:
    """Token estimate for `n_bytes` of prompt input (ceil of bytes / BYTES_PER_TOKEN); negative counts as 0."""
    return -(-max(0, int(n_bytes)) // BYTES_PER_TOKEN)


def over_tier(tokens: int) -> bool:
    """True when `tokens` exceeds LONG_PROMPT_TIER_TOKENS (exactly at the tier is under it)."""
    return tokens > LONG_PROMPT_TIER_TOKENS


def split_to_tier(items: list[T], size_of: Callable[[T], int]) -> list[list[T]]:
    """Greedily pack `items` in order into groups whose summed `size_of` tokens stay <= the tier.

    `size_of(item)` returns BYTES. An item alone over the tier gets a group of its own and is
    never dropped or split; callers detect it with `over_tier(estimate_tokens(size_of(item)))`.
    """
    groups: list[list[T]] = []
    current: list[T] = []
    current_tokens = 0
    for item in items:
        tokens = estimate_tokens(size_of(item))
        if current and current_tokens + tokens > LONG_PROMPT_TIER_TOKENS:
            groups.append(current)
            current, current_tokens = [], 0
        current.append(item)
        current_tokens += tokens
    if current:
        groups.append(current)
    return groups
