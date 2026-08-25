"""
coordinator_core.hooks.tests.test_track_touched_files_fresh_dir — guards the
mkdir precondition C1 kept when session bootstrap was stripped out of
``track_touched_files._handler``.

``_ensure_session_dir``/``_needs_session_init``/``_bootstrap_session`` were
deleted (they cost a `session.core.init` call and two cold `git rev-parse`
spawns on the first edit of every session — see docs/plans/2026-08-22-track-
touched-files-pays-only-for-the-append.md § C1). A bare
``os.makedirs(session_dir, exist_ok=True)`` was kept immediately before the
touched.txt handling.

**Narrow, true rationale** (corrected 2026-08-23 — the original claim below
was falsified by code-review slice P3-test-surface): on the PRIMARY append
path, this mkdir is redundant. ``locked_write.py::locked_rmw`` creates the
target file's parent directory itself (``target_dir.mkdir(parents=True,
exist_ok=True)``) immediately before it ``mkstemp``s, unconditionally, so a
fresh session dir gets created by ``locked_rmw`` regardless of whether the
handler's own mkdir ran. The handler's mkdir is load-bearing ONLY on the
FALLBACK path: ``_append_locked`` catches any non-(``LockTimeout``,
``MutateAbort``) exception from ``locked_rmw`` (non-POSIX locking, a
non-git fixture, a lock-backend failure) and falls through to ``_append``,
which opens the target with ``"a"`` — that mode does NOT create parent
directories. On a session whose first tool call is an Edit, hitting that
fallback without the handler's mkdir raises ``FileNotFoundError``, swallowed
by the module's silent-failure contract: the T event is lost with NO signal.
(A ``_touch_if_absent`` pre-create helper also sat on this path until C9
removed it as dead weight; it never created parents either, so it never
covered this case.)

This module has two cases:

- ``test_t_event_lands_when_session_dir_did_not_exist`` drives the PRIMARY
  path (real ``locked_rmw``) into a session dir that did not exist
  beforehand. This is cheap coverage that the primary path works end to end,
  but it is NOT evidence for the handler's mkdir — ``locked_rmw``'s own
  self-creation would carry this case even with the handler's mkdir deleted.
- ``test_t_event_lands_via_fallback_when_session_dir_did_not_exist`` is the
  actual guard: it monkeypatches ``locked_rmw`` to raise a plain
  ``RuntimeError`` (neither ``LockTimeout`` nor ``MutateAbort``), forcing
  ``_append_locked`` through the ``_append`` fallback — the one branch that
  genuinely lacks a self-creating mkdir. Delete the handler's
  ``os.makedirs(session_dir, exist_ok=True)`` and THIS test fails: `_append`
  opens "a" against a directory that was never created and raises
  ``FileNotFoundError``, swallowed silently, so ``touched.txt`` never
  appears.

Negative-spec: does NOT assert anything about meta.json, started_at, or
head_at_start — those were part of the deleted bootstrap and are no longer
this hook's concern (liveness now stamps only through the claiming ceremony
or `session/scope.py::cs_touch`, see the plan § C1 rationale).

Spec backlink: docs/plans/2026-08-22-track-touched-files-pays-only-for-the-append.md § C1
Review backlink: state/subagent-share/26c961e1-b1da-43f7-a851-3dce6fd60700/2026-08-23-codereview-sliceP3-test-surface-track-touched-files-fresh-dir.md
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from coordinator_core.hooks import track_touched_files as ttf
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.session import scope as touch_scope

# Spawns a real external process (git init fixture); runs at cadence gates,
# not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


class TestHandlerWritesIntoFreshSessionDir:
    """Drives `_handler` with a session_id whose session dir does not exist
    beforehand — the shape an earlier draft of C1 broke by dropping the mkdir
    that guards the append's fallback precondition. Two cases: the primary
    path (cheap smoke coverage, NOT a guard — see module docstring) and the
    forced fallback path (the actual guard for the handler's own mkdir)."""

    def test_t_event_lands_when_session_dir_did_not_exist(self, tmp_path):
        """Primary path (real `locked_rmw`): a session dir absent before the
        call still ends up with the T event, because `locked_rmw` self-creates
        the parent directory. NOT evidence for the handler's own mkdir —
        `locked_rmw` would carry this case even if that mkdir were deleted.
        Kept as cheap end-to-end coverage of the common case."""
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")

        common_dir = git_common_dir(repo)  # production shape: <repo>/.git
        session_id = "freshdirfeed0001"
        session_dir = common_dir / "coordinator-sessions" / session_id

        assert not session_dir.exists(), (
            "test fixture leaked a pre-existing session dir; the point of "
            "this test is a session whose first tool call is an Edit"
        )

        params = {
            "session_id": session_id,
            "tool_name": "Edit",
            "file_path": str(target),
        }
        asyncio.run(ttf._handler(params, repo_root=common_dir))

        touched_file = session_dir / "touched.txt"
        assert touched_file.exists(), (
            "handler did not create touched.txt in a session dir that did "
            "not exist beforehand (primary path)"
        )
        lines = [
            line
            for line in touched_file.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert lines, (
            "touched.txt exists but carries no T event — the append itself "
            "silently failed against the fresh session dir"
        )
        entries = [touch_scope.parse_touch_event(line)[2] for line in lines]
        assert "src/new.py" in entries

    def test_t_event_lands_via_fallback_when_session_dir_did_not_exist(
        self, tmp_path, monkeypatch
    ):
        """Fallback path (`locked_rmw` forced to raise): the actual guard for
        the handler's `os.makedirs(session_dir, exist_ok=True)`. Delete that
        mkdir and this test fails — `_append_locked` catches the plain
        `RuntimeError` below (neither `LockTimeout` nor `MutateAbort`) and
        falls through to `_append`, which opens the target with `"a"` and does
        NOT create parent directories, so a missing session dir raises
        `FileNotFoundError`, swallowed by the module's silent-failure
        contract — `touched.txt` never appears."""

        def _raise(*args, **kwargs):
            raise RuntimeError("forced: simulate locked_rmw unavailable")

        monkeypatch.setattr(ttf, "locked_rmw", _raise)

        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")

        common_dir = git_common_dir(repo)
        session_id = "freshdirfallback01"
        session_dir = common_dir / "coordinator-sessions" / session_id

        assert not session_dir.exists(), (
            "test fixture leaked a pre-existing session dir; the point of "
            "this test is a session whose first tool call is an Edit"
        )

        params = {
            "session_id": session_id,
            "tool_name": "Edit",
            "file_path": str(target),
        }
        asyncio.run(ttf._handler(params, repo_root=common_dir))

        touched_file = session_dir / "touched.txt"
        assert touched_file.exists(), (
            "handler did not create touched.txt via the _append fallback in "
            "a session dir that did not exist beforehand — the handler's own "
            "mkdir precondition for the fallback path was dropped"
        )
        lines = [
            line
            for line in touched_file.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert lines, (
            "touched.txt exists but carries no T event — the _append "
            "fallback silently failed against the fresh session dir"
        )
        entries = [touch_scope.parse_touch_event(line)[2] for line in lines]
        assert "src/new.py" in entries
