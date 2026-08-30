"""
coordinator_core.ops.ceremony.tests.test_no_interpreter_on_commit

Regression pin for C6 (state/dispatch-briefs/2026-08-30-who-pushes-and-
when/C6.md, docs/plans/2026-08-30-who-pushes-and-when.md): the engine's own
commit path must never spawn a Python interpreter to replay a post-commit
auto-push on itself. `git_native._replay_post_commit_auto_push` and its
call sites in `commit_scoped()` and `commit_authored_content()` were
deleted -- this test is the falsifier's leg 1 acceptance check, a spawn
assertion, not a code read: it patches `subprocess.Popen` around a REAL
commit through both entrypoints and asserts no Python-interpreter child was
ever spawned.

Negative-spec: does NOT assert anything about the installed `post-commit`
git hook shim (`ensure_post_commit_hook`) -- that is a SEPARATE route to
the same `hooks/auto_push.py` module, out of this chunk's scope (C6 touches
only the engine's own in-process replay call, not the on-disk hook script).
A real `git commit` in this test would still fire that hook if one were
installed; the fixtures below never install one, so the only spawn path
this test can observe is the engine's own (now-deleted) replay call.

Spec backlink: docs/plans/2026-08-30-who-pushes-and-when.md chunk C6.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

from coordinator_core.ops.ceremony import git_native

from .fixtures.real_git import real_git_repo

# Real-git spawn is load-bearing: a real commit through both entrypoints is
# the only way to prove no interpreter child is spawned as a SIDE EFFECT of
# landing it -- a mocked git would only prove the mock itself does nothing.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _write_msg(tmp_path: Path, text: str) -> Path:
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text(text, encoding="utf-8")
    return msg_file


class _PopenSpy:
    """Wraps the real `subprocess.Popen`, recording every argv[0] spawned
    during the test so a Python-interpreter child (the shape
    `hooks/auto_push._detach_and_run` used to `Popen`) can be asserted
    absent -- not merely "the mocked replay function was never called",
    which would pass even if a regression re-wired the same spawn through a
    different call path.
    """

    def __init__(self, real_popen):
        self._real_popen = real_popen
        self.argvs: List[list] = []

    def __call__(self, args, *a, **kw):
        self.argvs.append(list(args) if isinstance(args, (list, tuple)) else [args])
        return self._real_popen(args, *a, **kw)


def _assert_no_interpreter_spawn(spy: "_PopenSpy") -> None:
    python_exe_name = Path(sys.executable).name.lower()
    for argv in spy.argvs:
        first = str(argv[0]).lower() if argv else ""
        assert python_exe_name not in first and "python" not in Path(first).name.lower(), (
            f"a Python-interpreter child was spawned during a commit: {argv!r} "
            f"(full spawn list: {spy.argvs!r})"
        )


def test_commit_scoped_spawns_no_interpreter(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)

    (repo / "second.txt").write_text("second\n", encoding="utf-8")
    _git(["add", "second.txt"], repo)

    msg_file = _write_msg(tmp_path, "commit_scoped: no interpreter replay\n")

    spy = _PopenSpy(subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", spy)

    result = git_native.commit_scoped(["second.txt"], msg_file, repo)

    assert result.ok, result
    _assert_no_interpreter_spawn(spy)


def test_commit_authored_content_spawns_no_interpreter(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)

    msg_file = _write_msg(tmp_path, "commit_authored_content: no interpreter replay\n")

    spy = _PopenSpy(subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", spy)

    result = git_native.commit_authored_content(
        "seed.txt", "authored content\n", msg_file, repo
    )

    assert result.ok, result
    _assert_no_interpreter_spawn(spy)


def test_commit_scoped_diverged_path_spawns_no_interpreter(tmp_path, monkeypatch):
    """The DIVERGED branch (`commit_scoped`'s second `_replay_post_commit_
    auto_push` call site, deleted alongside the agree-branch one) is a
    separate code path from the first test above -- pin it independently so
    a regression that only removed one of the two call sites still fails
    here.
    """
    repo = real_git_repo(tmp_path)

    staged_path = repo / "diverged.txt"
    staged_path.write_text("staged content\n", encoding="utf-8")
    _git(["add", "diverged.txt"], repo)
    staged_path.write_text("worktree content\n", encoding="utf-8")

    msg_file = _write_msg(tmp_path, "commit_scoped diverged: no interpreter replay\n")

    spy = _PopenSpy(subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", spy)

    result = git_native.commit_scoped(
        ["diverged.txt"],
        msg_file,
        repo,
        known_diverged={"diverged.txt"},
    )

    assert result.ok, result
    _assert_no_interpreter_spawn(spy)
