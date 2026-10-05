"""Glob validity for the declared-pair census.

`PathMatcher` answers "does any tracked path match this pathspec" by bisecting
the sorted tracked set on the pattern's literal prefix, then matching only that
range. Matching is `fnmatch.fnmatchcase` semantics (case-sensitive on every OS,
`*` crosses `/`). The predicates carry DR-305's three rejections.
"""

from __future__ import annotations

import fnmatch
import re
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from functools import lru_cache

_WILDCARD_CHARS = "*?["
_PREFIX_SENTINEL = "\U0010ffff"
RUNTIME_LEDGER_PREFIXES = ("state/", ".coordinator-local/")


@lru_cache(maxsize=None)
def _compiled(pattern: str) -> "re.Pattern[str]":
    return re.compile(fnmatch.translate(pattern))


def _literal_prefix(pattern: str) -> str:
    for index, ch in enumerate(pattern):
        if ch in _WILDCARD_CHARS:
            return pattern[:index]
    return pattern


class PathMatcher:
    """Matcher over a tracked path set. `paths` need not be sorted or unique."""

    def __init__(self, paths: Iterable[str]) -> None:
        self._paths: list[str] = sorted(set(paths))

    def candidates(self, pattern: str) -> Sequence[str]:
        prefix = _literal_prefix(pattern)
        if not prefix:
            return self._paths
        lo = bisect_left(self._paths, prefix)
        hi = bisect_left(self._paths, prefix + _PREFIX_SENTINEL, lo)
        return self._paths[lo:hi]

    def matches(self, pattern: str) -> bool:
        match = _compiled(pattern).match
        return any(match(path) for path in self.candidates(pattern))

    def matches_any(self, patterns: Iterable[str]) -> bool:
        return any(self.matches(pattern) for pattern in patterns)


def has_wildcard(pattern: str) -> bool:
    return any(ch in pattern for ch in _WILDCARD_CHARS)


def is_catch_all(pattern: str) -> bool:
    """True when the pattern has no literal path segment and no literal extension
    on its final segment (`*`, `**`, `**/*`, `*/*`)."""
    segments = pattern.split("/")
    if any(seg and not has_wildcard(seg) for seg in segments):
        return False
    last = segments[-1]
    if "." in last:
        suffix = last.rsplit(".", 1)[1]
        if suffix and not has_wildcard(suffix):
            return False
    return True


def is_runtime_ledger(patterns: Sequence[str]) -> bool:
    """True when every pattern sits under a runtime-ledger root."""
    return all(pattern.startswith(RUNTIME_LEDGER_PREFIXES) for pattern in patterns)
