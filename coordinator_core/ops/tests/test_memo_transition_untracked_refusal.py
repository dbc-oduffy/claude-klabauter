"""A memo untracked at HEAD is refused before any frontmatter write.

Real git is load-bearing: the check reads HEAD's tree of a real repo.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.memo_transition import _handler
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_OPEN_MEMO = """\
---
kind: fyi
status: open
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    flags = no_console_creationflags()
    subprocess.run(["git", "init", "-b", "work/test", str(repo)], check=True, capture_output=True, **flags)
    (repo / ".gitkeep").touch()
    subprocess.run(["git", "-C", str(repo), "add", ".gitkeep"], check=True, capture_output=True, **flags)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, env=_ENV, **flags)
    (repo / "cross-repo" / "inbox").mkdir(parents=True)
    return repo


def _resolve(memo: Path, **extra) -> dict:
    return asyncio.run(_handler({
        "verb": "resolve", "memo": str(memo), "session_id": "s1",
        "at": "2026-06-02T00:00:00Z", "decision": "accepted", "realized_by": "abc1234",
        **extra,
    }))


def test_untracked_memo_is_refused_and_bytes_unchanged(tmp_path):
    repo = _repo(tmp_path)
    memo = repo / "cross-repo" / "inbox" / "memo.md"
    memo.write_text(_OPEN_MEMO, encoding="utf-8")
    before = memo.read_bytes()

    result = _resolve(memo)

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "not tracked at HEAD" in result["error"]
    assert "git add" in result["error"]
    assert memo.read_bytes() == before


def test_tracked_memo_still_resolves(tmp_path):
    repo = _repo(tmp_path)
    memo = repo / "cross-repo" / "inbox" / "memo.md"
    memo.write_text(_OPEN_MEMO, encoding="utf-8")
    flags = no_console_creationflags()
    rel = "cross-repo/inbox/memo.md"
    subprocess.run(["git", "-C", str(repo), "add", "--", rel], check=True, capture_output=True, **flags)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "memo", "--", rel],
                   check=True, capture_output=True, env=_ENV, **flags)

    result = _resolve(memo)

    assert result["exit_code"] == 0, result
    assert "status: actioned" in memo.read_text(encoding="utf-8")


def test_deferred_batch_resolve_is_not_refused(tmp_path):
    repo = _repo(tmp_path)
    memo = repo / "cross-repo" / "inbox" / "memo.md"
    memo.write_text(_OPEN_MEMO, encoding="utf-8")

    result = _resolve(memo, defer_commit=True)

    assert result["exit_code"] == 0, result
