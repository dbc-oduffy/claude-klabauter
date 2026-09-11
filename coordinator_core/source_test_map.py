"""
coordinator_core.source_test_map — layout-aware source-to-covering-test map.

Purpose: `coordinator_core.diff_scoped_tests` narrows a routine ceremony gate
to the test files a diff CHANGED DIRECTLY. It has no way to see that a
changed SOURCE file (not itself a test file) is covered by tests living
somewhere else in the tree -- so a source-only diff, the majority of commits,
narrowed to nothing. This module is that missing signal: given a changed
source path, return every test file this repo's own basename convention says
covers it.

Spec backlink: docs/plans/2026-07-30-diff-scoped-ceremony-gates-elegant.md
(C2, Design decision 3). Branch B / scout measurement found EIGHT distinct
test-file layout shapes across this repo's configured testpaths, not the two
the handoff spec assumed (46% coverage) -- see the plan body's own table.
Building eight hand-coded per-layout rules would be brittle and incomplete
by construction; instead this module indexes every `test_*.py` file actually
present under the configured testpaths roots (via
`coordinator_core.diff_scoped_tests._read_testpaths` -- NOT re-derived here)
and matches purely by BASENAME convention: a source file `<mod>/foo.py`
(or `coordinator/bin/some-cli.py`, dash-normalized to `some_cli`) is covered
by every `test_foo.py` found ANYWHERE under any configured testpaths root,
regardless of which of the eight directory shapes it lives under. This is
layout-agnostic by construction, not layout-enumerating.

Negative-spec (do not build any of this here):
  - Does NOT pick a single "best" candidate -- see `map_source_to_tests`'s
    own docstring: returning only one silently under-runs the gate when a
    source file has covering tests in more than one testpaths root (the
    `workday-complete-step1-validate.py` two-root case this module's own
    tests pin).
  - Does NOT perform any I/O beyond filesystem existence/directory-listing
    checks -- no git invocation, no subprocess, no coverage instrumentation
    (anti-scope, plan § Anti-scope).
  - Does NOT decide what a caller does with an unmapped/not-fully-mapped
    result -- `map_changed_sources`'s `fully_mapped` bool is a signal, not
    an action; `coordinator_core.diff_scoped_tests` (C3) is the ONLY module
    that reacts to it by falling back to the full tier.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.diff_scoped_tests import _read_testpaths

#: This repo's test-file convention (basename only), matching
#: `diff_scoped_tests._TEST_FILE_RE`.
_TEST_FILE_RE = re.compile(r"^test_(.+)\.py$")


def _basename_index(repo_root: str) -> dict:
    """Build a ``{source_stem: sorted [test file paths]}`` index by walking
    every configured testpaths root and recording each ``test_*.py`` file
    found under it, keyed by the stem AFTER the ``test_`` prefix (e.g.
    ``test_foo.py`` -> key ``foo``).

    Re-derived on every call rather than cached at module scope -- the
    working tree can change between calls within one process (a repo-root
    argument makes this testable across many `tmp_path` fixtures in the
    same test run), and this module's own anti-scope (no I/O beyond
    filesystem checks) makes a fresh, cheap directory walk the honest
    choice over a staleness-prone cache.
    """
    index: dict = {}
    root_path = Path(repo_root)
    for testpath in _read_testpaths(repo_root):
        base = root_path / testpath
        if not base.exists():
            continue
        if base.is_file():
            candidates = [base] if _TEST_FILE_RE.match(base.name) else []
        else:
            candidates = list(base.rglob("test_*.py"))
        for cand in candidates:
            m = _TEST_FILE_RE.match(cand.name)
            if not m:
                continue
            stem = m.group(1)
            rel = cand.relative_to(root_path).as_posix()
            index.setdefault(stem, set()).add(rel)
    return {stem: sorted(paths) for stem, paths in index.items()}


def _source_stem(source_path: str) -> str:
    """The basename convention's lookup key for a changed SOURCE file.

    Strips the ``.py`` extension and applies dash->underscore normalization
    -- ``coordinator/bin`` CLIs are hyphenated on disk (``some-cli.py``) but
    their test files follow Python's underscore convention
    (``test_some_cli.py``), so the raw stem never matches without this
    normalization.
    """
    stem = Path(source_path).stem
    return stem.replace("-", "_")


def map_source_to_tests(source_path: str, repo_root: Optional[str] = None) -> list:
    """Every test file this repo's basename convention says covers
    ``source_path``, sorted, repo-root-relative, POSIX-separated.

    Returns ``[]`` for an unknown/unmappable source file -- a source stem
    with no ``test_<stem>.py`` anywhere under the configured testpaths is
    not evidence of "no coverage exists" vs. "coverage exists under a name
    this convention cannot derive" (Design decision 3); this function makes
    no claim either way, it only reports what the convention found. Callers
    combining this across many files (`map_changed_sources`) are the ones
    that must treat an empty result as the dangerous case.

    Returns EVERY candidate across every testpaths root, never a single
    "best" pick -- a source file's tests can legitimately live in more than
    one root (the canonical `workday-complete-step1-validate.py` case: it
    has covering tests in both `coordinator/tests/` and `coordinator/bin/
    tests/`), and a map that silently drops one under-runs the gate.
    """
    root = repo_root if repo_root is not None else "."
    if not source_path or not source_path.strip():
        return []
    stem = _source_stem(source_path)
    if not stem:
        return []
    index = _basename_index(root)
    return list(index.get(stem, []))


def map_changed_sources(paths: Sequence[str], repo_root: Optional[str] = None):
    """Map every changed SOURCE file in ``paths`` to its covering tests, and
    report whether the whole set was fully mapped.

    Returns ``(candidates, fully_mapped)``:
      - ``candidates`` — the sorted UNION of every test file
        `map_source_to_tests` found for any path in ``paths`` (additive
        over the whole set, never per-file).
      - ``fully_mapped`` — ``False`` the moment ANY path in ``paths`` maps
        to zero candidates (conjunctive fail-safe, AC9): the rule cannot
        distinguish "no coverage exists" from "coverage exists under a name
        it cannot derive", and those demand opposite behaviours, so one
        un-mappable file forces the caller toward the safe (run-more)
        direction for the WHOLE set, not just that one file.

    ``([], True)`` for an EMPTY ``paths`` is NOT a narrowing licence on its
    own -- it means "nothing to map", and callers MUST treat "no source
    files changed" as a distinct case from "source files changed and none
    mapped" (the latter is `([], False)` whenever ``paths`` is non-empty and
    the first entry is unmappable, never silently coerced to the former).
    """
    root = repo_root if repo_root is not None else "."
    if not paths:
        return ([], True)

    fully_mapped = True
    candidates: set = set()
    for source_path in paths:
        matches = map_source_to_tests(source_path, root)
        if not matches:
            fully_mapped = False
        candidates.update(matches)

    return (sorted(candidates), fully_mapped)
