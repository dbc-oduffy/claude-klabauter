"""
coordinator_core.ops.dispatch_emit.run_leftovers -- run-touched paths a landing set leaves dirty.

Purpose: the terminal commit refuses a half-landed run by naming these paths.

Invariant: pure and zero-spawn; callers supply the already-read dirty set.
"""

from __future__ import annotations

from typing import Collection, Mapping


def _norm(path: object) -> str:
    return str(path).replace("\\", "/")


def run_touched_left(
    changed: Collection[str],
    touched: frozenset,
    landing: Collection[str],
    contested: Mapping,
) -> list[str]:
    """Sorted posix paths in ``touched`` and ``changed`` that are in neither ``landing``
    nor ``contested`` (a peer-held path is reported elsewhere, not as this run's leftover)."""
    held = {_norm(p) for p in landing} | {_norm(p) for p in contested}
    mine = {_norm(p) for p in touched}
    return sorted({n for n in map(_norm, changed) if n in mine and n not in held})
