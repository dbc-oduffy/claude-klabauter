"""
coordinator_core.ops.ceremony.tests.test_consumed_handoff_stamp_claim_release

Purpose: per-caller claim-release coverage for C3d (docs/plans/2026-08-11-
claim-release-and-the-gate-that-cannot-clear.md), the second of
`git_native.commit_scoped`'s two remaining uninstrumented callers (see that
function's own comment, landed in C3a c034cb87a). Asserts the claim on a
stamped-handoff follow-up commit clears once
`consumed_handoff_stamp._commit_and_push_follow_up` lands it -- the same
property `test_common_claim_release.py` (C3a) already covers for its two
routes.

Calls `_commit_and_push_follow_up` directly (not the full R1-R4
`post_commit_stamp_and_ship` pass) -- this is a pure git-commit-then-release
unit, and the surrounding stamp/ship machinery is exercised elsewhere
(`test_wsc_tail_parity.py` et al.). `push_mode="none"` throughout: no remote
exists in the fixture repo, and the release call under test sits BEFORE the
push-mode branch in the function body regardless.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.commit_pipeline import PUSH_MODE_NONE
from coordinator_core.ops.ceremony.consumed_handoff_stamp import (
    _commit_and_push_follow_up,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope

# `_commit_and_push_follow_up` lands a real commit and reads real touched.txt
# claim-release events through `session_scope` -- a mocked git would not
# exercise the actual commit->release ordering this file is asserting. The
# `repo` fixture is per-test (not module scope) because the test mutates
# (commits into) it.
# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _sdir(repo: Path, sid: str) -> Path:
    return Path(repo) / ".git" / "coordinator-sessions" / sid


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _released_paths(repo: Path, sid: str) -> set:
    touched = _sdir(repo, sid) / "touched.txt"
    if not touched.exists():
        return set()
    released = set()
    for line in touched.read_text(encoding="utf-8").splitlines():
        verb, _ts, path = session_scope.parse_touch_event(line)
        if verb == "R":
            released.add(path)
    return released


def test_commit_and_push_follow_up_releases_claim_on_stamped_path(repo):
    """AC1 (consumed_handoff_stamp route): a claim on the stamped handoff
    path clears once its follow-up commit lands, using the caller's own
    `session_id` (the WSC session running the ceremony) -- never a
    reflexively-resolved `resolve_session_id(cwd)`."""
    sid = "consumed-handoff-stamp-claim-test"

    rel = "state/handoffs/a.md"
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / rel).write_text("stamped: true\n", encoding="utf-8")

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    follow_up_sha, pushed, push_status, error = _commit_and_push_follow_up(
        repo, [rel], "deadbeef", PUSH_MODE_NONE, sid
    )

    assert error is None
    assert follow_up_sha is not None
    assert pushed is None

    released = _released_paths(repo, sid)
    assert rel in released or Path(rel).name in {Path(p).name for p in released}
