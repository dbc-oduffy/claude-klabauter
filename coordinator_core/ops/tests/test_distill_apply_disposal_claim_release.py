
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.distill_apply_disposal import _delete_tracked_and_append_log
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

# ordering. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
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


def _run(coro):
    return asyncio.run(coro)


def _own_sid(monkeypatch, sid: str) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _claim_cleared(repo: Path, sid: str, rel_path: str) -> bool:
    result = claim_index.lookup([rel_path], cwd=str(repo))
    return sid not in result.get(rel_path, [])


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


def test_delete_tracked_and_append_log_releases_claim_on_reaped_path(repo, monkeypatch):
    sid = "distill-apply-disposal-claim-test"
    _own_sid(monkeypatch, sid)

    rel = "archive/handoffs/gone.md"
    tracked = repo / rel
    tracked.parent.mkdir(parents=True)
    tracked.write_text("body\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed gone.md"], repo)

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    reaped, denorm_written, denorm_write_failed, failed = _run(
        _delete_tracked_and_append_log(
            worktree_root=repo,
            tracked_paths=[tracked],
            log_path=repo / "state" / "distillation-log.md",
            log_rows=[],
            subject="test: dispose gone.md",
        )
    )

    assert failed == []
    assert reaped == [rel]

    assert _claim_cleared(repo, sid, rel)
