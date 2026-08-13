"""
coordinator_core.cartography._skip_dirs — shared vendor/build/VCS directory-name set.

Purpose: single implementation of the "skip these directory NAMES during a
tree walk" convention that was living as a duplicated frozenset literal in
``coordinator_core/ops/cartography_stack.py`` and
``coordinator_core/ops/detect_primary_languages.py`` (found duplicated during
the ``cartography.chunk_table`` build — see
``cross-repo/inbox/2026-08-06-coordinator-claude-em-cartography-chunk-table-producer-
seam.md``). Both call sites now import ``SKIP_DIR_NAMES`` from here instead
of each declaring their own copy.

This is a directory-NAME set, not a file classifier — it prunes an entire
subtree by directory basename during a walk (``.venv``, ``node_modules``,
``dist``, ...). It says nothing about whether an individual FILE (e.g. a test
module) is a build/test artifact; that is a different predicate
(``coordinator_core.cartography.chunk_table.is_test_artifact``), deliberately
not folded into this set.

Negative-spec:
  - Does NOT walk anything itself — pure data, no I/O.
  - Does NOT enumerate test-framework directories (``tests/``, ``__tests__/``,
    ``spec/``, ``test/``) — those are legitimate tracked-source containers in
    many repos, not vendor/build noise; test-artifact exclusion is a
    filename/path-shaped predicate elsewhere, not a directory-name skip.

Spec backlink: cross-repo/inbox/2026-08-06-coordinator-claude-em-cartography-chunk-table-producer-seam.md
"""

from __future__ import annotations

#: Directory basenames pruned during a repo-wide walk — vendor/build/VCS
#: noise that would otherwise dominate walk time/results without changing
#: the answer to "what source exists here."
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
    }
)
