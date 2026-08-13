"""Behavioral tests for
coordinator_core.write_guards.block_subagent_guard_grant_write -- the
Write-tool-channel leg that denies a dispatched subagent's direct
Write/Edit/MultiEdit/NotebookEdit against EITHER EM-guard-grant artifact
(chunk C4, docs/plans/2026-08-13-em-exercisable-in-band-grant-route.md).

Two artifacts, both covered:
  - the durable grant record,
    ``.git/coordinator-sessions/<sid>/em-guard-grant.json``
  - the DR-260 unlock sentinel,
    ``<platform-temp-dir>/coordinator-guard-unlock-<sid>.<guard>``

Any materialized sentinel fixture uses a per-test unique sid
(``uuid4``-suffixed) and is cleaned up in a ``finally:`` block swallowing
``OSError`` -- this machine runs many concurrent sessions sharing the real
platform temp dir, and a leaked sentinel would silently grant an unrelated
later call.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from coordinator_core.session.guard_unlock_sentinel import sentinel_path
from coordinator_core.write_guards import block_subagent_guard_grant_write as guard
from coordinator_core.write_guards import engine

_GRANT_FILENAME = "em-guard-grant.json"


def _payload(
    file_path: str,
    *,
    agent_id: str = "aexecutor-teammate-1234567890abcdef",
    tool_name: str = "Edit",
    cwd: str = "/repo",
) -> dict:
    payload = {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path, "old_string": "x", "new_string": "y"},
        "cwd": cwd,
    }
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


def _notebook_payload(notebook_path: str, *, agent_id: str, cwd: str) -> dict:
    return {
        "tool_name": "NotebookEdit",
        "tool_input": {"notebook_path": notebook_path},
        "cwd": cwd,
        "agent_id": agent_id,
    }


def _deny(file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is not None, f"expected DENY for: {file_path!r}"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result


def _allow(file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is None, f"expected ALLOW for: {file_path!r}, got {result!r}"


def _make_plain_clone(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _grant_path(repo_root, sid="sess-123"):
    return str(repo_root / ".git" / "coordinator-sessions" / sid / _GRANT_FILENAME)


def _unique_sid(prefix="sess"):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Grant-record leg (AC-4a).
# ---------------------------------------------------------------------------


class TestGrantRecordLeg:
    def test_em_inline_write_allowed_no_agent_id(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _allow(_grant_path(repo_root), agent_id="", cwd=str(repo_root))

    def test_subagent_write_denied(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _deny(_grant_path(repo_root), tool_name="Write", cwd=str(repo_root))

    def test_subagent_edit_denied(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _deny(_grant_path(repo_root), tool_name="Edit", cwd=str(repo_root))

    def test_subagent_multiedit_denied(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _deny(_grant_path(repo_root), tool_name="MultiEdit", cwd=str(repo_root))

    def test_subagent_notebookedit_denied(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        result = guard.check(
            _notebook_payload(
                _grant_path(repo_root),
                agent_id="aexecutor-teammate-1234567890abcdef",
                cwd=str(repo_root),
            )
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_sibling_path_in_same_session_dir_allowed(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        sibling = str(
            repo_root
            / ".git"
            / "coordinator-sessions"
            / "sess-123"
            / "plan-body-write-block.log"
        )
        _allow(sibling, cwd=str(repo_root))

    def test_similarly_named_file_outside_coordinator_sessions_allowed(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        outside = str(repo_root / "somewhere" / "else" / _GRANT_FILENAME)
        _allow(outside, cwd=str(repo_root))

    def test_non_matcher_tool_name_allowed(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _allow(_grant_path(repo_root), tool_name="Read", cwd=str(repo_root))

    def test_denies_regardless_of_specific_session_id_value(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        for sid in ("sess-abc", "another-session-id", "0123456789abcdef"):
            _deny(_grant_path(repo_root, sid=sid), cwd=str(repo_root))

    def test_denies_exactly_the_grant_record_not_other_files_in_dir(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _deny(_grant_path(repo_root, sid="sess-xyz"), cwd=str(repo_root))
        _allow(
            str(
                repo_root
                / ".git"
                / "coordinator-sessions"
                / "sess-xyz"
                / "some-other-record.json"
            ),
            cwd=str(repo_root),
        )


# ---------------------------------------------------------------------------
# Sentinel leg (AC-4b) -- the more important of the two per the dispatch
# brief: the sentinel is what actually clears the guard.
# ---------------------------------------------------------------------------


class TestSentinelLeg:
    def _real_sentinel_path(self, sid, guard_name="bump-foreign-repo-write"):
        return sentinel_path(sid, guard_name)

    def test_subagent_write_to_real_sentinel_path_denied(self, tmp_path):
        sid = _unique_sid()
        path = self._real_sentinel_path(sid)
        try:
            _deny(str(path), tool_name="Write", cwd=str(tmp_path))
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def test_subagent_edit_to_real_sentinel_path_denied(self, tmp_path):
        sid = _unique_sid()
        path = self._real_sentinel_path(sid)
        try:
            _deny(str(path), tool_name="Edit", cwd=str(tmp_path))
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def test_subagent_multiedit_to_real_sentinel_path_denied(self, tmp_path):
        sid = _unique_sid()
        path = self._real_sentinel_path(sid)
        try:
            _deny(str(path), tool_name="MultiEdit", cwd=str(tmp_path))
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def test_subagent_notebookedit_to_real_sentinel_path_denied(self, tmp_path):
        sid = _unique_sid()
        path = self._real_sentinel_path(sid)
        try:
            result = guard.check(
                _notebook_payload(
                    str(path),
                    agent_id="aexecutor-teammate-1234567890abcdef",
                    cwd=str(tmp_path),
                )
            )
            assert result is not None
            assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def test_em_inline_write_to_sentinel_path_allowed(self, tmp_path):
        sid = _unique_sid()
        path = self._real_sentinel_path(sid)
        _allow(str(path), agent_id="", cwd=str(tmp_path))

    def test_denies_regardless_of_sid_or_guard_name_pair(self, tmp_path):
        for sid, guard_name in (
            (_unique_sid(), "bump-foreign-repo-write"),
            (_unique_sid(), "bump-outside-repo-write"),
            (_unique_sid(), "some-future-grantable-guard"),
        ):
            path = self._real_sentinel_path(sid, guard_name)
            _deny(str(path), cwd=str(tmp_path))

    def test_non_sentinel_file_in_temp_dir_allowed(self, tmp_path):
        unrelated = str(Path(tempfile.gettempdir()) / "some-unrelated-file.json")
        _allow(unrelated, cwd=str(tmp_path))

    def test_sentinel_shaped_name_outside_temp_dir_allowed(self, tmp_path):
        """Path-anchored, not filename-alone: a sentinel-prefixed basename
        OUTSIDE the resolved platform temp dir is allowed."""
        outside = str(tmp_path / "coordinator-guard-unlock-sess-1.some-guard")
        _allow(outside, cwd=str(tmp_path))

    def test_non_matcher_tool_name_allowed(self, tmp_path):
        sid = _unique_sid()
        path = self._real_sentinel_path(sid)
        _allow(str(path), tool_name="Read", cwd=str(tmp_path))


# ---------------------------------------------------------------------------
# Registration reachability -- confirms the leg is auto-discovered through
# the dispatcher entrypoint, not just directly importable.
# ---------------------------------------------------------------------------


class TestReachableThroughEngine:
    def test_reachable_through_engine_for_grant_record(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        payload = _payload(_grant_path(repo_root), cwd=str(repo_root))
        result = engine.evaluate(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_reachable_through_engine_for_sentinel(self, tmp_path):
        sid = _unique_sid()
        path = sentinel_path(sid, "bump-foreign-repo-write")
        try:
            payload = _payload(str(path), cwd=str(tmp_path))
            result = engine.evaluate(payload)
            assert result is not None
            assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        finally:
            try:
                path.unlink()
            except OSError:
                pass
