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

#: `diff_scoped_tests._TEST_FILE_RE`.
_TEST_FILE_RE = re.compile(r"^test_(.+)\.py$")


def _basename_index(repo_root: str) -> dict:
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
    stem = Path(source_path).stem
    return stem.replace("-", "_")


def map_source_to_tests(source_path: str, repo_root: Optional[str] = None) -> list:
    root = repo_root if repo_root is not None else "."
    if not source_path or not source_path.strip():
        return []
    stem = _source_stem(source_path)
    if not stem:
        return []
    index = _basename_index(root)
    return list(index.get(stem, []))


def map_changed_sources(paths: Sequence[str], repo_root: Optional[str] = None):
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
