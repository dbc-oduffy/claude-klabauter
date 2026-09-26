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
      an exact-basename `fnmatch` glob, portable to any repo (not DoE-hardcoded).
    - Does NOT invoke `subprocess` — `discover()` never runs a suite, only finds
      and classifies it.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

FAMILY_GLOBS: dict[str, str] = {
    "js-prefix": "test-*.js",
    "js-suffix": "*.test.js",
    "py-native": "test_*.py",
    "py-nonnative": "*.test.py",
}

FAMILY_RUNNER_KIND: dict[str, str] = {
    "js-prefix": "node",
    "js-suffix": "node",
    "py-native": "pytest",
    "py-nonnative": "python3",
}

ALL_FAMILIES: frozenset[str] = frozenset(FAMILY_GLOBS)

# DEC-2: exact-basename frozenset, matched against directory BASENAMES only
EXCLUDED_DIRNAMES: frozenset[str] = frozenset(
    {".git", "node_modules", ".venv", "site-packages", ".coordinator-venv"}
)


@dataclass(frozen=True)
class Suite:

    family: str
    path: Path
    runner_kind: str


def _classify(filename: str, families: frozenset[str]) -> str | None:
    for family in families:
        if fnmatch.fnmatch(filename, FAMILY_GLOBS[family]):
            return family
    return None


def discover(repo_root: str | Path, families: Iterable[str] = ALL_FAMILIES) -> list[Suite]:
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
