"""A bash guard fallback written fail-open "until a consumer lands" goes stale.

Comments and string literals under `bash_guards/` may not carry the phrases
below unless the (path, phrase) pair sits in `_KNOWN_DEFERRED_FAIL_OPEN`, whose
value names the guard-chain entry that must consume the path. The ledger only
shrinks. Rule and worked cases: docs/wiki/checks-that-cannot-fail.md.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path
from typing import Dict, List, Set, Tuple

_GUARDS_DIR = Path(__file__).resolve().parents[1]

_PHRASES = ("no consumer yet", "a future guard will decide", "deferred fail-open")

#: (path relative to bash_guards/, phrase) -> guard-chain entry name that
#: consumes the path once it lands. Empty: no such comment exists today.
_KNOWN_DEFERRED_FAIL_OPEN: Dict[Tuple[str, str], str] = {}


def _prose_of(source: str) -> str:
    """Comment and string-literal text of `source`, whitespace-collapsed and
    lower-cased, so a phrase wrapped across comment lines still matches."""
    chunks: List[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            chunks.append(tok.string.lstrip("#"))
        elif tok.type == tokenize.STRING:
            chunks.append(tok.string)
    return re.sub(r"\s+", " ", " ".join(chunks)).lower()


def deferred_phrases_in(source: str) -> Set[str]:
    prose = _prose_of(source)
    return {phrase for phrase in _PHRASES if phrase in prose}


def _guard_sources() -> List[Path]:
    return sorted(
        p
        for p in _GUARDS_DIR.rglob("*.py")
        if "__pycache__" not in p.parts and p.relative_to(_GUARDS_DIR).parts[0] != "tests"
    )


def _measured() -> Set[Tuple[str, str]]:
    found: Set[Tuple[str, str]] = set()
    for path in _guard_sources():
        rel = path.relative_to(_GUARDS_DIR).as_posix()
        for phrase in deferred_phrases_in(path.read_text(encoding="utf-8")):
            found.add((rel, phrase))
    return found


def test_a_planted_deferral_comment_is_detected():
    assert deferred_phrases_in("x = 1  # fail open: no consumer yet\n") == {"no consumer yet"}
    assert deferred_phrases_in("# no consumer\n# yet, so allow\n") == {"no consumer yet"}
    assert deferred_phrases_in('"""Deferred fail-open until wired."""\n') == {"deferred fail-open"}
    assert deferred_phrases_in("x = 'no consumer'\n") == set()


def test_the_scan_reads_the_guard_sources():
    assert len(_guard_sources()) > 50


def test_no_unledgered_deferred_fail_open_comment():
    unledgered = _measured() - set(_KNOWN_DEFERRED_FAIL_OPEN)
    assert not unledgered, (
        "fail-open path justified by a deferred consumer; decide fail-open vs "
        f"fail-loud now, or ledger it with the chain entry it waits on: {sorted(unledgered)}"
    )


def test_the_ledger_only_shrinks():
    stale = set(_KNOWN_DEFERRED_FAIL_OPEN) - _measured()
    assert not stale, f"ledger entries with no matching comment; delete them: {sorted(stale)}"
    assert all(_KNOWN_DEFERRED_FAIL_OPEN.values()), "each entry names the chain entry it waits on"
