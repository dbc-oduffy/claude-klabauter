"""
coordinator_core.ops.ceremony.tests.test_real_git_fixture_boundary

Guards the mock-vs-real git boundary in this test package: enumerates every
``test_*.py`` module that imports ``fixtures.real_git`` and asserts the set
equals a hardcoded allowlist. Most suites in this directory deliberately
mock git (see e.g. test_wsc_commit.py's "Patched in tests -- do NOT call
real git in test assertions"); a new import of the real-git fixture module
should be a deliberate, reviewed allowlist edit, not a silent accident that
hands real git to a suite that never meant to see it.

Spec backlink: docs/plans/2026-07-27-computed-commit-mechanism-selection.md
chunk C2.
"""

from __future__ import annotations

import re
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent

# test_commit_scoped.py (C3, docs/plans/2026-07-27-computed-commit-
# mechanism-selection.md) is the first deliberate, reviewed real-git
# crossing: index/worktree divergence -- the thing the commit-mechanism
# selector has to tell apart -- cannot be exhibited by a mocked git.
# test_wsc_tail_trailer_divergence.py (C10, same plan) is the second: it
# proves wsc_tail._derive_trailers() no longer clobbers a deliberately
# partial-hunk-staged path's content when pre-staging for `commit.anchors`.
# test_commit_scoped_trailer_replay.py (C10-remainder / AC18, same plan) is
# the third: it proves the diverged (commit-tree) branch replays the
# prepare-commit-msg hook's Session-Id/Deliverable-Id trailers, which
# real git hooks (installed into a real .git/hooks/) are needed to exhibit.
# test_consumed_handoff_stamp.py and test_post_commit_tail.py (route-tail-
# commits-through-commit_scoped fix) are the fourth and fifth: each proves
# its own post-commit follow-up commit (`_commit_and_push_follow_up` /
# `_commit_and_push_origin_stub_close`) now routes through
# `git_native.commit_scoped` rather than a raw `add_paths` +
# `commit_with_message_file` pair, so a peer's deliberately-staged
# partial-hunk content on a path in the follow-up commit's own path set
# survives -- the same divergence real git alone can exhibit.
# Extend this set deliberately when a further suite adds real-git coverage
# -- see fixtures/real_git.py's module docstring for why that's a reviewed
# crossing, not a convenience import.
_ALLOWED_REAL_GIT_IMPORTERS: frozenset[str] = frozenset({
    "test_commit_scoped.py",
    "test_commit_scoped_trailer_replay.py",
    "test_consumed_handoff_stamp.py",
    "test_post_commit_tail.py",
    "test_wsc_tail_trailer_divergence.py",
})

_IMPORT_PATTERN = re.compile(
    r"from\s+\.fixtures\.real_git\s+import"
    r"|from\s+fixtures\.real_git\s+import"
    r"|from\s+\.fixtures\s+import\s+real_git"
    r"|import\s+.*fixtures\.real_git"
)


def _importers_of_real_git() -> set[str]:
    importers: set[str] = set()
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if _IMPORT_PATTERN.search(text):
            importers.add(path.name)
    return importers


def test_real_git_importers_match_allowlist() -> None:
    actual = _importers_of_real_git()
    expected = set(_ALLOWED_REAL_GIT_IMPORTERS)
    assert actual == expected, (
        "The set of test modules importing fixtures.real_git changed. This "
        "directory deliberately mocks git in its suites; importing real "
        "git is a reviewed crossing (see fixtures/real_git.py's module "
        "docstring), not a convenience.\n"
        f"  currently importing: {sorted(actual)}\n"
        f"  allowlisted:         {sorted(expected)}\n"
        "If this is a deliberate, reviewed addition, add the module name "
        "to _ALLOWED_REAL_GIT_IMPORTERS in this file. If it is not "
        "deliberate, stop importing real git into that suite."
    )
