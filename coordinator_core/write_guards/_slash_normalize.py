"""Shared helper: normalize a caller-supplied path string to forward slashes
with no doubled separator, before a guard pattern-matches it.

Distinct from `_case_fold_path.py`,
which solves a different problem (case-insensitive-filesystem and
extended-length-prefix desync) and does not collapse doubled separators.

The underscore prefix is load-bearing: `engine.py::_discover_guards()` skips
modules whose name starts with `_`, so this stays shared plumbing rather than
a registered guard of its own.

Negative-spec: does not strip a trailing slash, does not casefold, does not
resolve `.`/`..` segments — callers that need those do them separately, as
each did before this hoist.
"""

from __future__ import annotations


def collapse_slashes(value: str) -> str:
    normalized = value.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized
