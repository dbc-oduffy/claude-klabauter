"""Behavioral tests for
coordinator_core.write_guards.block_fleet_delegation_write -- the
Write-tool-channel guard that denies any hand-authored
Write/Edit/MultiEdit/NotebookEdit against
``<settings_home()>/fleet-delegation.json``, for every caller class
(chunk C3, docs/plans/2026-08-28-the-ask-the-pm-step-gets-an-artifact-to-
check.md).

Pinned here:

  - Deny fires across all four MATCHERS tools, WITH an ``agent_id`` present
    (subagent) AND without one (EM-inline) -- the deny is unconditional,
    unlike the sibling ``block_subagent_grant_record_write`` guard whose
    allow leg (1) exempts the no-``agent_id`` case. This module has no such
    leg.
  - Scope-equals-enforcement: a sibling file in the same settings-home
    directory is allowed (filename-anchored, not directory-anchored); a
    same-named file OUTSIDE settings_home() is allowed (path-anchored, not
    filename-alone).
  - A ``..`` traversal segment that lexically resolves onto the target
    still denies; one that resolves elsewhere allows.
  - A differently-cased candidate that resolves onto the same on-disk
    target (case-insensitive filesystem) still denies.
  - A non-MATCHERS ``tool_name`` (e.g. ``Read``) is allowed -- defense-in-
    depth pin matching every sibling write_guards module's convention.
  - Reachable through the auto-discovery dispatcher
    (``coordinator_core.write_guards.engine.evaluate``), not just this
    module's own ``check()`` directly.

Payload/assertion shape follows
``test_block_subagent_grant_record_write.py``'s conventions (a ``_payload``
builder, ``_deny``/``_allow`` helpers), with ``settings_home()`` patched via
``COORDINATOR_SETTINGS_HOME`` (monkeypatch env var) rather than a git-repo
fixture -- this guard's target has no repo-relative component.
"""

from __future__ import annotations

from coordinator_core.write_guards import block_fleet_delegation_write as guard
from coordinator_core.write_guards import engine


def _payload(
    file_path: str,
    *,
    agent_id: str = "",
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


def _notebook_payload(notebook_path: str, *, agent_id: str = "", cwd: str) -> dict:
    payload = {
        "tool_name": "NotebookEdit",
        "tool_input": {"notebook_path": notebook_path},
        "cwd": cwd,
    }
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


def _deny(file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is not None, f"expected DENY for: {file_path!r}"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result


def _allow(file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is None, f"expected ALLOW for: {file_path!r}, got {result!r}"


def _target_path(monkeypatch, tmp_path):
    """Point settings_home() at a fresh tmp dir and return the resolved
    grant-file path under it."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    return str(tmp_path / "fleet-delegation.json")


# ---------------------------------------------------------------------------
# Base cases -- unconditional deny (no agent_id gate), all four MATCHERS.
# ---------------------------------------------------------------------------


class TestBaseCases:
    def test_em_inline_write_denied_no_agent_id(self, monkeypatch, tmp_path):
        target = _target_path(monkeypatch, tmp_path)
        _deny(target, agent_id="", tool_name="Write", cwd=str(tmp_path))

    def test_subagent_write_denied(self, monkeypatch, tmp_path):
        target = _target_path(monkeypatch, tmp_path)
        _deny(
            target,
            agent_id="aexecutor-teammate-1234567890abcdef",
            tool_name="Write",
            cwd=str(tmp_path),
        )

    def test_subagent_edit_denied(self, monkeypatch, tmp_path):
        target = _target_path(monkeypatch, tmp_path)
        _deny(
            target,
            agent_id="aexecutor-teammate-1234567890abcdef",
            tool_name="Edit",
            cwd=str(tmp_path),
        )

    def test_subagent_multiedit_denied(self, monkeypatch, tmp_path):
        target = _target_path(monkeypatch, tmp_path)
        _deny(
            target,
            agent_id="aexecutor-teammate-1234567890abcdef",
            tool_name="MultiEdit",
            cwd=str(tmp_path),
        )

    def test_subagent_notebookedit_denied(self, monkeypatch, tmp_path):
        target = _target_path(monkeypatch, tmp_path)
        result = guard.check(
            _notebook_payload(
                target,
                agent_id="aexecutor-teammate-1234567890abcdef",
                cwd=str(tmp_path),
            )
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_sibling_file_in_settings_home_allowed(self, monkeypatch, tmp_path):
        """Filename-anchored, not directory-anchored: a different file in
        the SAME settings-home directory is allowed."""
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
        sibling = str(tmp_path / "machine-local" / "some-other-record.json")
        _allow(sibling, cwd=str(tmp_path))

    def test_similarly_named_file_outside_settings_home_allowed(
        self, monkeypatch, tmp_path
    ):
        """Path-anchored, not filename-alone: a same-named file OUTSIDE
        settings_home() is allowed."""
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
        outside = str(tmp_path / "elsewhere" / "fleet-delegation.json")
        _allow(outside, cwd=str(tmp_path))

    def test_non_matcher_tool_name_allowed(self, monkeypatch, tmp_path):
        target = _target_path(monkeypatch, tmp_path)
        _allow(target, tool_name="Read", cwd=str(tmp_path))


# ---------------------------------------------------------------------------
# Traversal and case-fold bypasses -- same shapes pinned for the sibling
# grant-record guard.
# ---------------------------------------------------------------------------


class TestTraversalAndCaseFoldBypasses:
    def test_unrelated_traversal_path_allowed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
        unrelated = str(tmp_path / "some" / "dir" / ".." / "other" / "file.py")
        _allow(unrelated, cwd=str(tmp_path))

    def test_traversal_path_resolving_onto_target_denied(self, monkeypatch, tmp_path):
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
        traversal_path = str(
            tmp_path / "sub" / ".." / "fleet-delegation.json"
        )
        _deny(traversal_path, cwd=str(tmp_path))

    def test_differently_cased_candidate_denied(self, monkeypatch, tmp_path):
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
        cased_path = str(tmp_path / "FLEET-DELEGATION.JSON")
        _deny(cased_path, cwd=str(tmp_path))

    def test_relative_candidate_resolved_against_cwd_denied(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
        _deny("fleet-delegation.json", cwd=str(tmp_path))


# ---------------------------------------------------------------------------
# Reachable through the auto-discovery dispatcher.
# ---------------------------------------------------------------------------


class TestReachableThroughEngine:
    def test_engine_evaluate_denies_fleet_delegation_write(self, monkeypatch, tmp_path):
        target = _target_path(monkeypatch, tmp_path)
        payload = _payload(target, tool_name="Write", cwd=str(tmp_path))
        result = engine.evaluate(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
