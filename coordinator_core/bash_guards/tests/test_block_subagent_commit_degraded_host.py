"""C6 (2026-09-27): the R21 degraded-host commit-phase sentinel leg is
retired. `emit.py`'s DAG-form scripts (C12) no longer compose a
`coordinator:git-commit-agent` commit-phase prompt of any kind -- degraded
host or not -- and the grind route's commit stage never carried the
sentinel, so the leg guarded nothing after C12 lands: recognising a
degraded-host `general-purpose` dispatch as an exempt commit-agent by a
transcript sentinel is deleted outright, not widened or reshaped.

This file now pins two things instead:

1. The guard denies a subagent's own `coordinator-invoke
   dispatch.terminal_commit '<json>'` call -- the new op is a member of
   `_COMMITTING_OP_NAMES`, same as `ceremony.commit_v2`.
2. A general-purpose subagent whose transcript happens to still carry the
   old sentinel text is denied like any other subagent: the sentinel leg no
   longer exists to read it, so it is inert data, not an admission signal.

Pure Python -- no shell spawns. Identity/ownership seams are monkeypatched
directly onto the guard module object, mirroring test_block_subagent_
commit.py's own pattern.
"""

from __future__ import annotations

import sys

import pytest

from typing import Any, Dict, List

from coordinator_core.bash_guards import block_subagent_commit as guard

_GIT_COMMIT_AGENT_TYPE = guard._GIT_COMMIT_AGENT_TYPE
_FAKE_REPO_ROOT = "/repo"


@pytest.fixture(autouse=True)
def _repo_root_under_tmp_path(tmp_path, monkeypatch):
    """A stand-down here writes real audit logs under the resolved root."""
    monkeypatch.setattr(sys.modules[__name__], "_FAKE_REPO_ROOT", str(tmp_path / "repo"))

_SCOPED_COMMIT_CMD = 'git commit -m "msg" -- src/foo.py'

# The literal string the old degraded-host leg used to look for. Retained
# here only as inert fixture text -- the guard no longer imports or reads
# `emit._COMMIT_PHASE_PROMPT_SENTINEL` at all (that import is deleted).
_OLD_SENTINEL_TEXT = "coordinator:terminal-commit-request"


def _payload(command, agent_type, transcript_path=None, agent_id="deadbeef0123"):
    p: Dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": None,
        "agent_id": agent_id,
    }
    if agent_type is not None:
        p["agent_type"] = agent_type
    if transcript_path is not None:
        p["transcript_path"] = transcript_path
    return p


def _git_commit_agent_setup(monkeypatch, *, scope_result=(True, "")):
    """Same shape as test_block_subagent_commit.py's own fixture -- wires
    identity resolution and the lazily-imported ownership-scope helper so
    `_git_commit_agent_may_commit`'s LEG 2/LEG 3 can run without a real git
    repo or backpointer chain on disk."""
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: _FAKE_REPO_ROOT)
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: ""
    )

    def _spy(session_id, paths, cwd=None, *, allow_orphans=False):
        calls.append(
            {"session_id": session_id, "paths": paths, "cwd": cwd, "allow_orphans": allow_orphans}
        )
        return scope_result

    monkeypatch.setattr(guard, "_import_assert_paths_in_session_scope", lambda: _spy)
    return calls


def _write_transcript(tmp_path, first_line):
    path = tmp_path / "transcript.jsonl"
    path.write_text(first_line + "\n", encoding="utf-8")
    return str(path)


# --- Deleted-leg negative specs ---


def test_leg_symbols_no_longer_exist_on_the_guard_module():
    """The degraded-host sentinel leg's constant, import and reader
    function are gone entirely -- not renamed, not stubbed."""
    for name in (
        "_DEGRADED_HOST_NATIVE_AGENT_TYPE",
        "_COMMIT_PHASE_PROMPT_SENTINEL",
        "_TRANSCRIPT_SENTINEL_READ_CAP_BYTES",
        "_transcript_carries_commit_phase_sentinel",
    ):
        assert not hasattr(guard, name), f"{name} should have been deleted"


def test_degraded_general_purpose_with_old_sentinel_text_still_denies(monkeypatch, tmp_path):
    """A general-purpose subagent whose transcript carries the old sentinel
    text is denied like any other subagent -- the leg that used to read it
    is gone, so the text is inert."""
    _git_commit_agent_setup(monkeypatch)
    transcript = _write_transcript(
        tmp_path, '{"role": "user", "content": "' + _OLD_SENTINEL_TEXT + '"}'
    )
    result = guard.check(
        _payload(
            _SCOPED_COMMIT_CMD,
            agent_type="general-purpose",
            transcript_path=transcript,
        )
    )
    assert result is not None, "expected DENY for a general-purpose agent"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_ordinary_coordinator_agent_type_still_works_unchanged(monkeypatch, tmp_path):
    """The pre-existing, non-degraded route (agent_type ==
    coordinator:git-commit-agent) keeps working unchanged."""
    _git_commit_agent_setup(monkeypatch)
    result = guard.check(
        _payload(
            _SCOPED_COMMIT_CMD,
            agent_type=_GIT_COMMIT_AGENT_TYPE,
        )
    )
    assert result is None, f"expected ALLOW, got {result!r}"


# --- AC19: dispatch.terminal_commit is denied to subagents ---


def test_subagent_invoking_terminal_commit_op_is_denied(monkeypatch):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: _FAKE_REPO_ROOT)
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: ""
    )
    result = guard.check(
        _payload(
            "coordinator-invoke dispatch.terminal_commit '{\"script_path\": \"x\"}'",
            agent_type="general-purpose",
        )
    )
    assert result is not None, "expected DENY for a subagent dispatch.terminal_commit invoke"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_terminal_commit_op_name_is_a_committing_op():
    assert "dispatch.terminal_commit" in guard._COMMITTING_OP_NAMES
