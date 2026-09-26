
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.push import PUSH_MODE_NONE
from coordinator_core.ops.ceremony.consumed_handoff_stamp import (
    _commit_and_push_follow_up,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags())


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
    record = _sdir(repo, sid) / session_scope._TOUCH_RECORD_FILENAME
    if not record.exists():
        return set()
    legacy_lines, _truncated = session_scope._read_touch_record_as_legacy_lines(record)
    released = set()
    for line in legacy_lines:
        verb, _ts, path = session_scope.parse_touch_event(line)
        if verb == "R":
            released.add(path)
    return released


def test_commit_and_push_follow_up_releases_claim_on_stamped_path(repo):
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
