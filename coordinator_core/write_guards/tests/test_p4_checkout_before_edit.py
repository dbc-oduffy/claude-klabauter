"""
test_p4_checkout_before_edit.py — pytest coverage for
coordinator_core.write_guards.p4_checkout_before_edit.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md
§ C5, § D5. The cockpit pvcs-02 acceptance cases (a) and (c) are exercised
here as the guard's own test cases: (a) a writable/git-only target pays zero
p4 spawns; (c) a locked/binary/refused target denies, never allow-through.

Spawn budget under test: zero ``runner.run`` calls for a git-only repo or a
writable target; at most two (fstat, then edit) for a read-only target in a
marker repo.
"""

from __future__ import annotations

import os
import stat

import pytest

import coordinator_core.write_guards.p4_checkout_before_edit as guard
from coordinator_core.p4 import runner, workspace
from coordinator_core.p4.session_change import P4SessionChangeError


@pytest.fixture
def identity():
    return workspace.P4Identity(
        port="p4.example.com:1666", user="bob", client="bob-ws", client_root="/root"
    )


def _payload(tool_name, file_path, cwd, session_id="sid-1"):
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "cwd": cwd,
        "session_id": session_id,
    }


def _make_spy():
    calls = []

    def _run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("runner.run must not be called in this scenario")

    return calls, _run


class TestZeroSpawnAllowPaths:
    def test_non_matcher_tool_allows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd: str(tmp_path))
        payload = _payload("Read", str(tmp_path / "x.txt"), str(tmp_path))
        assert guard.check(payload) is None

    def test_git_only_repo_allows_zero_spawns(self, monkeypatch, tmp_path):
        monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd: str(tmp_path))
        monkeypatch.setattr(workspace, "is_p4_repo", lambda root: False)
        _, spy = _make_spy()
        monkeypatch.setattr(runner, "run", spy)

        target = tmp_path / "file.txt"
        target.write_text("hi", encoding="utf-8")
        payload = _payload("Edit", str(target), str(tmp_path))

        assert guard.check(payload) is None

    def test_writable_target_allows_zero_spawns(self, monkeypatch, tmp_path):
        monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd: str(tmp_path))
        monkeypatch.setattr(workspace, "is_p4_repo", lambda root: True)
        _, spy = _make_spy()
        monkeypatch.setattr(runner, "run", spy)

        target = tmp_path / "file.txt"
        target.write_text("hi", encoding="utf-8")
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        payload = _payload("Edit", str(target), str(tmp_path))

        assert guard.check(payload) is None

    def test_nonexistent_target_allows_zero_spawns(self, monkeypatch, tmp_path):
        monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd: str(tmp_path))
        monkeypatch.setattr(workspace, "is_p4_repo", lambda root: True)
        _, spy = _make_spy()
        monkeypatch.setattr(runner, "run", spy)

        target = tmp_path / "brand-new.txt"
        payload = _payload("Write", str(target), str(tmp_path))

        assert guard.check(payload) is None

    def test_unresolvable_repo_root_allows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd: None)
        payload = _payload("Edit", str(tmp_path / "x.txt"), str(tmp_path))
        assert guard.check(payload) is None


class _ReadOnlyFixture:
    """Shared setup for the read-only-target arm: a real read-only file plus
    a registered p4 identity, so only ``runner.run`` needs faking per test."""

    def setup(self, monkeypatch, tmp_path, identity):
        target = tmp_path / "locked.txt"
        target.write_text("hi", encoding="utf-8")
        os.chmod(target, stat.S_IREAD)
        monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd: str(tmp_path))
        monkeypatch.setattr(workspace, "is_p4_repo", lambda root: True)
        monkeypatch.setattr(guard, "_resolve_repo_key", lambda root: "p4-studio/fifa-main")
        monkeypatch.setattr(workspace, "identity", lambda repo_key: identity)
        monkeypatch.setattr(guard, "ensure_session_change", lambda root, sid: 101)
        return target


class TestReadOnlyTargetDenies(_ReadOnlyFixture):
    def test_binary_head_type_denies_no_edit_spawn(self, monkeypatch, tmp_path, identity):
        target = self.setup(monkeypatch, tmp_path, identity)
        calls = []

        def fake_run(port, user, client, args, *, cwd=None, timeout=None, spec_input=None):
            calls.append(args)
            return runner.P4Result(ok=True, stdout="... headType binary\n")

        monkeypatch.setattr(runner, "run", fake_run)

        result = guard.check(_payload("Edit", str(target), str(tmp_path)))

        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "binary file" in reason
        assert "checkout" in reason.lower()
        assert len(calls) == 1  # fstat only, never reaches edit

    def test_exclusive_open_with_other_open_denies_naming_holder(self, monkeypatch, tmp_path, identity):
        target = self.setup(monkeypatch, tmp_path, identity)

        def fake_run(port, user, client, args, *, cwd=None, timeout=None, spec_input=None):
            return runner.P4Result(
                ok=True,
                stdout="... headType text+l\n... otherOpen 1\n... otherOpen0 alice@alice-ws\n",
            )

        monkeypatch.setattr(runner, "run", fake_run)

        result = guard.check(_payload("Edit", str(target), str(tmp_path)))

        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "alice@alice-ws" in reason

    def test_other_lock_denies_naming_holder(self, monkeypatch, tmp_path, identity):
        target = self.setup(monkeypatch, tmp_path, identity)

        def fake_run(port, user, client, args, *, cwd=None, timeout=None, spec_input=None):
            return runner.P4Result(
                ok=True,
                stdout="... headType text\n... otherLock \n... otherLock0 carol@carol-ws\n",
            )

        monkeypatch.setattr(runner, "run", fake_run)

        result = guard.check(_payload("Edit", str(target), str(tmp_path)))

        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "carol@carol-ws" in reason

    def test_fstat_ticket_expired_denies_never_allow_through(self, monkeypatch, tmp_path, identity):
        target = self.setup(monkeypatch, tmp_path, identity)

        def fake_run(port, user, client, args, *, cwd=None, timeout=None, spec_input=None):
            return runner.P4Result(
                ok=False, stdout="", error=runner.P4Error(kind="ticket_expired", raw="not logged in")
            )

        monkeypatch.setattr(runner, "run", fake_run)

        result = guard.check(_payload("Edit", str(target), str(tmp_path)))

        assert result is not None
        assert "ticket_expired" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_fstat_runner_timeout_denies(self, monkeypatch, tmp_path, identity):
        target = self.setup(monkeypatch, tmp_path, identity)

        def fake_run(port, user, client, args, *, cwd=None, timeout=None, spec_input=None):
            return runner.P4Result(ok=False, stdout="", error=runner.P4Error(kind="refused", raw="timeout"))

        monkeypatch.setattr(runner, "run", fake_run)

        result = guard.check(_payload("Edit", str(target), str(tmp_path)))

        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "refused" in reason
        assert "timeout" in reason

    def test_edit_spawn_refused_denies_after_two_spawns(self, monkeypatch, tmp_path, identity):
        target = self.setup(monkeypatch, tmp_path, identity)
        calls = []

        def fake_run(port, user, client, args, *, cwd=None, timeout=None, spec_input=None):
            calls.append(args)
            if args[0] == "-ztag":
                return runner.P4Result(ok=True, stdout="... headType text\n")
            return runner.P4Result(
                ok=False, stdout="", error=runner.P4Error(kind="refused", raw="no such changelist")
            )

        monkeypatch.setattr(runner, "run", fake_run)

        result = guard.check(_payload("Edit", str(target), str(tmp_path)))

        assert result is not None
        assert len(calls) == 2
        assert calls[1][:2] == ["edit", "-c"]

    def test_session_changelist_unavailable_denies(self, monkeypatch, tmp_path, identity):
        target = self.setup(monkeypatch, tmp_path, identity)

        def fake_run(port, user, client, args, *, cwd=None, timeout=None, spec_input=None):
            return runner.P4Result(ok=True, stdout="... headType text\n")

        monkeypatch.setattr(runner, "run", fake_run)

        def fail_ensure(root, sid):
            raise P4SessionChangeError("no registered p4 workspace")

        monkeypatch.setattr(guard, "ensure_session_change", fail_ensure)

        result = guard.check(_payload("Edit", str(target), str(tmp_path)))

        assert result is not None
        assert "session changelist" in result["hookSpecificOutput"]["permissionDecisionReason"]


class TestReadOnlyTargetAllows(_ReadOnlyFixture):
    def test_clean_read_only_file_opens_into_session_cl(self, monkeypatch, tmp_path, identity):
        target = self.setup(monkeypatch, tmp_path, identity)
        calls = []

        def fake_run(port, user, client, args, *, cwd=None, timeout=None, spec_input=None):
            calls.append(args)
            if args[0] == "-ztag":
                return runner.P4Result(ok=True, stdout="... headType text\n")
            assert args == ["edit", "-c", "101", str(target)]
            return runner.P4Result(ok=True, stdout="opened for edit\n")

        monkeypatch.setattr(runner, "run", fake_run)

        result = guard.check(_payload("Edit", str(target), str(tmp_path)))

        assert result is None
        assert len(calls) == 2


class TestUnregisteredWorkspace(_ReadOnlyFixture):
    def test_unregistered_workspace_denies_no_spawn(self, monkeypatch, tmp_path):
        target = tmp_path / "locked.txt"
        target.write_text("hi", encoding="utf-8")
        os.chmod(target, stat.S_IREAD)
        monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd: str(tmp_path))
        monkeypatch.setattr(workspace, "is_p4_repo", lambda root: True)

        def fail_resolve(root):
            raise P4SessionChangeError("no registered p4 workspace")

        monkeypatch.setattr(guard, "_resolve_repo_key", fail_resolve)
        _, spy = _make_spy()
        monkeypatch.setattr(runner, "run", spy)

        result = guard.check(_payload("Edit", str(target), str(tmp_path)))

        assert result is not None
        assert "unregistered" in result["hookSpecificOutput"]["permissionDecisionReason"]
