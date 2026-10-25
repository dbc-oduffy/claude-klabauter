"""memo.transition refuses to commit while HEAD is a default branch (no write, no commit)."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.memo_transition import _handler
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MEMO = """---
title: "t"
from: "a"
to: "b"
created: 2026-10-17
status: open
summary: "s"
kind: "bug"
---

body
"""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    )
    return out.stdout.strip()


_REPO_TEMPLATES: dict = {}


def _repo(tmp_path: Path, branch: str) -> Path:
    """Copy of a once-built per-branch repo; the init/commit spawns run once per branch."""
    import shutil
    import tempfile

    if branch not in _REPO_TEMPLATES:
        tpl = Path(tempfile.mkdtemp(prefix="memo-tpl-")) / "repo"
        tpl.mkdir()
        _git(tpl, "init", "-b", branch)
        inbox = tpl / "state" / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "m.md").write_text(_MEMO, encoding="utf-8")
        _git(tpl, "add", "-A")
        _git(tpl, "commit", "-m", "init")
        _REPO_TEMPLATES[branch] = tpl
    repo = tmp_path / "repo"
    shutil.copytree(_REPO_TEMPLATES[branch], repo)
    return repo / "state" / "cross-repo" / "inbox" / "m.md"


def _claim(memo: Path) -> dict:
    return asyncio.run(_handler({
        "verb": "claim", "memo": str(memo), "session_id": "s1", "at": "2026-10-20T00:00:00Z",
    }))


@pytest.mark.parametrize("branch", ["main", "master"])
def test_default_branch_refuses_without_write_or_commit(tmp_path, branch):
    memo = _repo(tmp_path, branch)
    head = _git(memo.parents[3], "rev-parse", "HEAD")
    r = _claim(memo)
    assert r["exit_code"] == 1 and "default branch" in r["error"], r
    assert memo.read_text(encoding="utf-8") == _MEMO
    assert _git(memo.parents[3], "rev-parse", "HEAD") == head


def test_work_branch_still_commits(tmp_path):
    memo = _repo(tmp_path, "work/x")
    r = _claim(memo)
    assert r["exit_code"] == 0 and r["applied"], r
