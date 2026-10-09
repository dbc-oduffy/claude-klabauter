"""A peer's claim on a path whose content equals HEAD lapses; a path with
uncommitted state still contests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import core, scope, touch_record

# _git and the repo fixture run real git; needs a real process.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root, check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _claim(root: Path, sid: str, path: str) -> None:
    sdir = root / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    touch_record.append_event(
        touch_record.sink_path(sdir), session_id=sid, agent_id=None,
        verb=touch_record.VERB_TOUCH, path=path,
    )


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("one\n")
    (tmp_path / "b.py").write_text("one\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
    for sid in ("mine", "peer"):
        core.init(sid, cwd=str(tmp_path))
    _claim(tmp_path, "peer", "a.py")
    _claim(tmp_path, "peer", "b.py")
    return tmp_path


def test_reverted_path_no_longer_blocks_but_edited_path_does(repo):
    (repo / "a.py").write_text("two\n")
    (repo / "a.py").write_text("one\n")
    (repo / "b.py").write_text("real edit\n")
    got = scope.contested_by_live_peers(["a.py", "b.py"], "mine", str(repo))
    assert got == {"b.py": ["peer"]}


def test_staged_change_and_untracked_still_block(repo):
    (repo / "a.py").write_text("two\n")
    _git(repo, "add", "a.py")
    (repo / "a.py").write_text("one\n")  # worktree == HEAD, index differs
    new = repo / "new.py"
    new.write_text("x\n")
    _claim(repo, "peer", "new.py")
    got = scope.contested_by_live_peers(["a.py", "new.py"], "mine", str(repo))
    assert set(got) == {"a.py", "new.py"}


def test_git_failure_retains_every_claim(repo, monkeypatch):
    monkeypatch.setattr(scope, "_git_output", lambda *a, **k: None)
    got = scope.contested_by_live_peers(["a.py", "b.py"], "mine", str(repo))
    assert set(got) == {"a.py", "b.py"}


def test_batch_is_one_git_call(repo, monkeypatch):
    calls = []
    real = scope._git_output
    monkeypatch.setattr(scope, "_git_output", lambda a, c=None: calls.append(a) or real(a, c))
    scope.contested_by_live_peers(["a.py", "b.py"], "mine", str(repo))
    assert len(calls) == 1
