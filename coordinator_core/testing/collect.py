"""
coordinator_core.testing.collect — pure discovery/classification engine for
Claude-klabauter's cross-repo full-test runner.

Purpose: walks a repo tree, classifies every file into one of four test-family
conventions via exact-basename globs (DEC-3..6), and excludes bundled-venv/
site-packages subtrees in-place during the walk (DEC-2). Read-only — no
subprocess invocation (that lives in `run.py`, C2).

Port source: none — net-new (DR-059 harness authoring).
Spec backlink: pln-claude-klabauter-python-full-test-runner-f8ca5a § C1 (DEC-2..6)

Negative-spec:
    - Does NOT filter excluded directories via a path glob or a post-collection
      filter — pruning happens IN-PLACE on `os.walk`'s `dirs` list, so an
      excluded subtree (e.g. a newly-added `.venv/`) is never descended into in
      the first place, not merely filtered out of the result after the fact.
    - Does NOT classify by directory location or full path — family matching is
      an exact-basename `fnmatch` glob, portable to any repo (not coordinator-claude-hardcoded).
    - Does NOT invoke `subprocess` — `discover()` never runs a suite, only finds
      and classifies it.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# DEC-3/4/5/6: exact-basename glob per family. Two JS conventions (prefix +
# suffix) and two Python conventions (pytest-native + per-file-run).
FAMILY_GLOBS: dict[str, str] = {
    "js-prefix": "test-*.js",
    "js-suffix": "*.test.js",
    "py-native": "test_*.py",
    "py-nonnative": "*.test.py",
}

# Maps each family to the interpreter/runner C2 (run.py) will invoke it with.
FAMILY_RUNNER_KIND: dict[str, str] = {
    "js-prefix": "node",
    "js-suffix": "node",
    "py-native": "pytest",
    "py-nonnative": "python3",
}

ALL_FAMILIES: frozenset[str] = frozenset(FAMILY_GLOBS)

# DEC-2: exact-basename frozenset, matched against directory BASENAMES only
# (never a path glob), pruned in-place during os.walk so excluded subtrees are
# never descended. Forward-safe against a newly-added venv and portable to
# other repos (not a coordinator-claude-specific two-path hardcode).
EXCLUDED_DIRNAMES: frozenset[str] = frozenset(
    {".git", "node_modules", ".venv", "site-packages", ".coordinator-venv"}
)


@dataclass(frozen=True)
class Suite:
    """One discovered test suite: a single file matched to exactly one family.

    `runner_kind` carries enough for C2 (run.py) to pick the invocation without
    re-deriving it from `family`.
    """

    family: str
    path: Path
    runner_kind: str


def _classify(filename: str, families: frozenset[str]) -> str | None:
    """Return the first family in `families` whose glob matches `filename`.

    The four family globs are mutually exclusive for filenames following a
    single convention, so match order does not usually affect the result —
    this is a plain first-match scan, not a priority ladder. A pathological
    filename combining two conventions at once (e.g. `test_x.test.py`, which
    satisfies both `test_*.py` and `*.test.py`) is an unspecified edge case:
    classification falls back to `frozenset` iteration order, which is not
    guaranteed deterministic.
    """
    for family in families:
        if fnmatch.fnmatch(filename, FAMILY_GLOBS[family]):
            return family
    return None


def discover(repo_root: str | Path, families: Iterable[str] = ALL_FAMILIES) -> list[Suite]:
    """Walk `repo_root` and return every classified `Suite`, venv-excluded.

    `families` restricts classification to a subset (defaults to all four).
    Directory pruning (DEC-2) happens in-place on `os.walk`'s `dirs` list
    before it descends, so an excluded subtree's contents are never even
    listed — not a post-hoc filter applied to already-discovered paths.

    Results are sorted by (family, path) for deterministic output ordering.
    """
    root = Path(repo_root)
    wanted = frozenset(families)
    suites: list[Suite] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRNAMES]
        for filename in filenames:
            family = _classify(filename, wanted)
            if family is None:
                continue
            path = Path(dirpath) / filename
            suites.append(
                Suite(
                    family=family,
                    path=path,
                    runner_kind=FAMILY_RUNNER_KIND[family],
                )
            )

    suites.sort(key=lambda s: (s.family, str(s.path)))
    return suites
