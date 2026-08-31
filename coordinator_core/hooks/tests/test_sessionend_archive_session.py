"""
coordinator_core.hooks.tests.test_sessionend_archive_session — Tier-T test for
the SessionEnd warm-door op that replaces the `archive-session-scope.py`
subprocess spawn.

Three obligations, per `docs/reference/warm-hook-migration.md`'s runbook (none
catches the others):
  (a) the op is registered and resolvable through `warm.hook_http.op_for_path`;
  (b) it is CLASSIFIED — an explicit assertion of the `classify()` call/result,
      since routing alone never calls `_is_compute_only` for a prefixed op;
  (c) it returns the source script's shape (always the no-op envelope) for one
      real, firing payload, AND actually performs the archive side effect —
      the CLI's whole job was the `archive()` call, so a test that only checks
      the return shape without checking the move would pass on a no-op stub.
"""

from __future__ import annotations

import importlib
import os

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.warm.hook_http import HOOK_PATH, op_for_path


def test_op_registers_and_resolves_through_op_for_path() -> None:
    module = importlib.import_module(
        "coordinator_core.hooks.sessionend_archive_session"
    )
    assert hasattr(module, "_handler")

    from coordinator_core.ipc import _REGISTRY

    assert "hooks.sessionend_archive_session" in _REGISTRY

    resolved = op_for_path(HOOK_PATH + "/hooks.sessionend_archive_session")
    assert resolved == "hooks.sessionend_archive_session"


def test_op_is_classified_mutating() -> None:
    # Explicit assertion of the classify() call/result — routing alone (a
    # `hooks.` prefix match) never reaches `_is_compute_only`, so an absent
    # classification would pass every routing test and still be a dispatch-
    # time authz gap.
    result = classify("hooks.sessionend_archive_session")
    assert result is OpClass.MUTATING


def test_op_returns_no_advisory_envelope_on_missing_session_id() -> None:
    from coordinator_core.hooks.sessionend_archive_session import _handler

    assert _handler({"payload": {"session_id": "", "cwd": ""}}) == {}
    assert _handler({"payload": {}}) == {}
    assert _handler({}) == {}


def test_op_archives_the_session_dir_and_returns_no_advisory(tmp_path, monkeypatch) -> None:
    """Mirrors `_cmd_archive_session`'s own contract: call `archive(sid)` and
    always return the no-op envelope, regardless of `archive()`'s own result.

    `session.scope.archive` is exercised for real (not mocked away) so this
    test actually proves the side effect the source CLI existed to perform,
    per this module's own docstring warning against a shape-only test.
    `core.sessions_dir` is monkeypatched to a plain tmp_path directory rather
    than spawning a real `git` process — the assertion is about `archive()`'s
    own move logic, not git's own `--git-common-dir` resolution.
    """
    from coordinator_core import session as _session_pkg  # noqa: F401
    from coordinator_core.session import core as session_core
    from coordinator_core.hooks.sessionend_archive_session import _handler

    repo = tmp_path / "repo"
    repo.mkdir()
    sessions_dir = repo / "coordinator-sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(
        session_core, "sessions_dir", lambda cwd=None: str(sessions_dir)
    )

    session_id = "test-sessionend-archive-op"
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "marker.txt").write_text("hello", encoding="utf-8")

    payload = {"session_id": session_id, "cwd": str(repo)}
    result = _handler({"payload": payload})
    assert result == {}

    # The live session dir is gone (moved), not merely deleted.
    assert not session_dir.exists()
    archive_root = sessions_dir / ".archive"
    assert archive_root.is_dir()
    moved = [p for p in archive_root.iterdir() if p.name.startswith(session_id)]
    assert moved, "expected archive() to move the session dir under .archive/"
    assert (moved[0] / "marker.txt").read_text(encoding="utf-8") == "hello"


def test_op_is_non_fatal_when_archive_raises(monkeypatch) -> None:
    """Non-fatal by design (module docstring, verbatim from the source CLI's
    own `_cmd_archive_session`): an exception from `archive()` is swallowed,
    never propagated, and the op still returns the no-op envelope."""
    import coordinator_core.hooks.sessionend_archive_session as mod

    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "archive", _raise)
    result = mod._handler({"payload": {"session_id": "sid-raises", "cwd": ""}})
    assert result == {}


def test_op_reads_only_from_payload_never_os_environ(monkeypatch) -> None:
    """`session_id`/`cwd` come from params["payload"], never from os.environ or
    this process's own cwd — the payload-in contract this runbook pins."""
    import coordinator_core.hooks.sessionend_archive_session as mod

    captured = {}

    def _fake_archive(sid, cwd=None):
        captured["sid"] = sid
        captured["cwd"] = cwd
        return True

    monkeypatch.setattr(mod, "archive", _fake_archive)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "env-sid-should-be-ignored")
    payload = {"session_id": "payload-sid", "cwd": "/payload/cwd"}
    result = mod._handler({"payload": payload})
    assert result == {}
    assert captured == {"sid": "payload-sid", "cwd": "/payload/cwd"}
