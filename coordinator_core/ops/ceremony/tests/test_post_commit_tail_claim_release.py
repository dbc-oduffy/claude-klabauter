
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.push import PUSH_MODE_NONE
from coordinator_core.ops.ceremony.post_commit_tail import (
    _commit_and_push_origin_stub_close,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


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
    """Reads the C4 ``touch-record.jsonl`` seam directly (`touch_record.
    _read_stream_claims`'s own last-verb-wins fold), not a legacy
    ``touched.txt`` -- that compat-union read was deleted 2026-08-26 (see
    `session_scope._read_touch_record_as_legacy_lines`'s own docstring),
    and `release_committed_claims` has written only the jsonl sink since.
    A path whose last recorded event is a release (`touch_record.
    VERB_RELEASE`) is released; anything else (still-`T`, or never
    touched at all) is not."""
    sink = _sdir(repo, sid) / touch_record.RECORD_FILENAME
    if not sink.exists():
        return set()
    claims, _degraded, _reasons = touch_record._read_stream_claims(sink)
    return {
        path
        for path, event in claims.items()
        if event.verb == touch_record.VERB_RELEASE
    }


def test_commit_and_push_origin_stub_close_releases_claim_on_closed_stub(repo):
    sid = "post-commit-tail-claim-test"

    rel = "state/handoffs/origin-stub.md"
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / rel).write_text("deployment_state: shipped\n", encoding="utf-8")

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    follow_up_sha, pushed, push_status, error = _commit_and_push_origin_stub_close(
        repo, [rel], "deadbeef", PUSH_MODE_NONE, sid
    )

    assert error is None
    assert follow_up_sha is not None
    assert pushed is None

    released = _released_paths(repo, sid)
    assert rel in released or Path(rel).name in {Path(p).name for p in released}
