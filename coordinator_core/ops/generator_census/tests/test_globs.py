"""Tests for the census glob matcher and DR-305 rejection predicates."""

from __future__ import annotations

import fnmatch
import itertools
import json
from pathlib import Path

import pytest

from coordinator_core.git.git_index import parse_index_identity
from coordinator_core.ops.generator_census.globs import (
    PathMatcher,
    has_wildcard,
    is_catch_all,
    is_runtime_ledger,
)

_REPO = Path(__file__).resolve().parents[4]
_ORACLE = _REPO / "coordinator_core/ops/generator_census/tests/fixtures/census_oracle.json"

_PATHS = [
    "a.py",
    "a/b.py",
    "a/b/c.py",
    "a/b/c.yaml",
    "ab",
    "abc/d.md",
    "docs/plans/x.md",
    "docs/plans/x-check.md",
    "docs/Plans/Upper.md",
    "state/goals/g1.yaml",
    "state/goals/sub/g2.yaml",
    "state/[odd]/f.txt",
    "state/q?/f.txt",
    "tasks/audits/z.md",
    "uni/café.md",
    "uni/日本/x.md",
    "zz/last.py",
]

_PATTERNS = [
    "",
    "*",
    "**",
    "**/*",
    "*/*",
    "*.py",
    "**/*.py",
    "a*",
    "a/*",
    "a/**",
    "a/b/*.yaml",
    "ab?",
    "a?",
    "abc/*",
    "nomatch/*",
    "nomatch",
    "docs/plans/*.md",
    "docs/plans/*-check.md",
    "docs/plans/x.md",
    "docs/plans/X.md",
    "docs/plans/[a-z].md",
    "docs/plans/[!x].md",
    "docs/*/Upper.md",
    "state/[odd]/f.txt",
    "state/[[]odd]/f.txt",
    "state/q?/f.txt",
    "state/q[?]/f.txt",
    "state/**/*.yaml",
    "state/goals/*.yaml",
    "state/",
    "uni/café.md",
    "uni/*.md",
    "uni/日*",
    "uni/*/x.md",
    "zz/*",
    "zz/l*y",
    "[",
    "a[",
    "[ab]*",
    "*/b*",
]


def _brute(pattern: str, paths: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(p, pattern) for p in paths)


@pytest.mark.parametrize("pattern", _PATTERNS)
def test_matches_equals_bruteforce(pattern):
    shuffled = list(reversed(_PATHS))
    assert PathMatcher(shuffled).matches(pattern) == _brute(pattern, _PATHS)


def test_matches_equals_bruteforce_on_subsets():
    for size in (0, 1, 2):
        for subset in itertools.combinations(_PATHS, size):
            matcher = PathMatcher(subset)
            for pattern in _PATTERNS:
                assert matcher.matches(pattern) == _brute(pattern, list(subset)), (pattern, subset)


def test_case_sensitive_everywhere():
    matcher = PathMatcher(["docs/Plans/Upper.md"])
    assert not matcher.matches("docs/plans/*.md")
    assert matcher.matches("docs/Plans/*.md")


def test_star_crosses_slash():
    assert PathMatcher(["a/b/c.py"]).matches("a/*.py")


def test_empty_set_and_matches_any():
    assert not PathMatcher([]).matches("*")
    matcher = PathMatcher(_PATHS)
    assert matcher.matches_any(["nomatch/*", "zz/*"])
    assert not matcher.matches_any(["nomatch/*", "none*"])
    assert not matcher.matches_any([])


def test_candidates_bisect_narrows_to_prefix():
    matcher = PathMatcher(_PATHS)
    assert list(matcher.candidates("zz/*")) == ["zz/last.py"]
    assert list(matcher.candidates("nomatch/*")) == []
    assert len(matcher.candidates("*.py")) == len(_PATHS)


@pytest.mark.parametrize(
    "pattern,expected",
    [("*", True), ("a/b?", True), ("[x]", True), ("docs/exec-summary.md", False), ("", False)],
)
def test_has_wildcard(pattern, expected):
    assert has_wildcard(pattern) is expected


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("*", True),
        ("**", True),
        ("**/*", True),
        ("*/*", True),
        ("*/*.*", True),
        ("**/*.py", False),
        ("*.py", False),
        ("state/**/*.yaml", False),
        ("state/*", False),
        ("docs/plans/*.md", False),
        ("a/b/c", False),
        ("**/*.p?", True),
    ],
)
def test_is_catch_all(pattern, expected):
    assert is_catch_all(pattern) is expected


@pytest.mark.parametrize(
    "patterns,expected",
    [
        (["state/goals/*.yaml"], True),
        ([".coordinator-local/memo-outbox/*.md", "state/x/*"], True),
        (["state/*.yaml", "docs/*.md"], False),
        (["docs/*.md"], False),
        (["statefoo/*"], False),
    ],
)
def test_is_runtime_ledger(patterns, expected):
    assert is_runtime_ledger(patterns) is expected


def test_oracle_patterns_match_bruteforce_on_live_tracked_set():
    tracked = sorted(parse_index_identity(_REPO))
    assert tracked, "index read returned no tracked paths"
    records = json.loads(_ORACLE.read_text(encoding="utf-8"))
    patterns = sorted(
        {m for r in records if r["verdict"] in _MUTATOR_VERDICTS for m in r["mutates"]}
    )
    assert patterns, "oracle fixture carries no mutator patterns"
    matcher = PathMatcher(tracked)
    for pattern in patterns:
        assert matcher.matches(pattern) == _brute(pattern, tracked), pattern


_MUTATOR_VERDICTS = {"MUTATES_DECLARED", "MUTATES_APPEND_DECLARED", "UNSTAMPED_BY_DESIGN"}
