"""Byte attribution for a composed ask script: ranked component sizes and the cap-refusal text naming the largest."""

from __future__ import annotations

from typing import Mapping, Sequence


def component_sizes(parts: Mapping[str, str]) -> list[tuple[str, int]]:
    """`(name, utf-8 byte count)` per part, largest first; ties keep name order."""
    return sorted(((name, len(text.encode("utf-8"))) for name, text in parts.items()), key=lambda p: (-p[1], p[0]))


def cap_refusal(total: int, cap: int, components: Sequence[tuple[str, int]]) -> str:
    """The refusal text for a script of `total` bytes over `cap`, naming the largest of `components` (ranked or not). Component sizes may be pre-hoist, so they need not sum to `total`."""
    head = f"composed ask script is over the Workflow runner's {cap}-byte cap: {total} bytes"
    if not components:
        return head
    name, n = max(components, key=lambda c: c[1])
    return f"{head}; largest inlined component {name} ({n} bytes)"
