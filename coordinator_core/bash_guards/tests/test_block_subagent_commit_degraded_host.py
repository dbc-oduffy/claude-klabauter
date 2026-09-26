"""Tests for R21 (2026-09-26, IBMFR item 21): the `coordinator:git-commit-
agent` C3 commit exemption must reach a DEGRADED-host dispatch of that same
row -- one whose harness-supplied `payload["agent_type"]` is the host-native
`general-purpose` roster substitution (`emit.py::_HOST_NATIVE_AGENT_TYPE_
ROSTER`), never the coordinator:* literal `_git_commit_agent_may_commit`'s
own LEG 1 was written against.

The fix recognises this case by a SECOND signal alongside the degraded
`agent_type`: whether the subagent's own transcript's first (harness-
authored, pre-tool-call) line carries the fixed sentinel substring
`_commit_agent_call` in `coordinator_core/ops/dispatch_emit/emit.py` bakes
into every commit-phase prompt it composes, degraded or not -- an EXISTING
marker, not a new stamp. `agent_type == "general-purpose"` ALONE must never
be sufficient: that would exempt every general-purpose agent's commits, not
only a degraded git-commit-agent's.

Pure Python -- no shell spawns. Identity/ownership seams are monkeypatched
directly onto the guard module object, mirroring test_block_subagent_
commit.py's own pattern; the transcript file is a real tmp_path file (the
one piece of real I/O `_transcript_carries_commit_phase_sentinel` performs).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard

_GIT_COMMIT_AGENT_TYPE = guard._GIT_COMMIT_AGENT_TYPE
_DEGRADED_HOST_NATIVE_AGENT_TYPE = guard._DEGRADED_HOST_NATIVE_AGENT_TYPE
_SENTINEL = guard._COMMIT_PHASE_PROMPT_SENTINEL
_FAKE_REPO_ROOT = "/repo"

_SCOPED_COMMIT_CMD = 'git commit -m "msg" -- src/foo.py'


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


def _sentinel_transcript_line():
    # A JSON-string-encoded line, the same shape a real transcript entry
    # takes -- the sentinel needs no escaping (plain ASCII, no quotes/
    # backslashes), so it survives verbatim inside the JSON string.
    return (
        '{"role": "user", "content": "Commit wave 1\'s work. '
        + _SENTINEL
        + '"}'
    )


# --- The degraded-host exemption fires only with BOTH signals present ---


def test_degraded_general_purpose_with_sentinel_allows(monkeypatch, tmp_path):
    _git_commit_agent_setup(monkeypatch)
    transcript = _write_transcript(tmp_path, _sentinel_transcript_line())
    result = guard.check(
        _payload(
            _SCOPED_COMMIT_CMD,
            agent_type=_DEGRADED_HOST_NATIVE_AGENT_TYPE,
            transcript_path=transcript,
        )
    )
    assert result is None, f"expected ALLOW, got {result!r}"


def test_bare_general_purpose_without_sentinel_still_denies(monkeypatch, tmp_path):
    """The negative spec this row's body names explicitly: `general-
    purpose` alone must never be sufficient."""
    _git_commit_agent_setup(monkeypatch)
    transcript = _write_transcript(tmp_path, '{"role": "user", "content": "do some work"}')
    result = guard.check(
        _payload(
            _SCOPED_COMMIT_CMD,
            agent_type=_DEGRADED_HOST_NATIVE_AGENT_TYPE,
            transcript_path=transcript,
        )
    )
    assert result is not None, "expected DENY for an ordinary general-purpose agent"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_general_purpose_with_no_transcript_path_still_denies(monkeypatch):
    """No transcript at all (absent path) must fail closed, not open."""
    _git_commit_agent_setup(monkeypatch)
    result = guard.check(
        _payload(
            _SCOPED_COMMIT_CMD,
            agent_type=_DEGRADED_HOST_NATIVE_AGENT_TYPE,
            transcript_path=None,
        )
    )
    assert result is not None, "expected DENY with no transcript_path"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_general_purpose_with_unreadable_transcript_still_denies(monkeypatch, tmp_path):
    """A transcript_path that does not resolve to a readable file must
    fail closed (never assume the sentinel is present)."""
    _git_commit_agent_setup(monkeypatch)
    missing = str(tmp_path / "does-not-exist.jsonl")
    result = guard.check(
        _payload(
            _SCOPED_COMMIT_CMD,
            agent_type=_DEGRADED_HOST_NATIVE_AGENT_TYPE,
            transcript_path=missing,
        )
    )
    assert result is not None, "expected DENY on an unreadable transcript"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_ordinary_coordinator_agent_type_ignores_transcript_path(monkeypatch, tmp_path):
    """The pre-existing, non-degraded route (agent_type ==
    coordinator:git-commit-agent) must keep working unchanged, regardless
    of what the transcript does or doesn't say."""
    _git_commit_agent_setup(monkeypatch)
    transcript = _write_transcript(tmp_path, '{"role": "user", "content": "irrelevant"}')
    result = guard.check(
        _payload(
            _SCOPED_COMMIT_CMD,
            agent_type=_GIT_COMMIT_AGENT_TYPE,
            transcript_path=transcript,
        )
    )
    assert result is None, f"expected ALLOW, got {result!r}"


def test_degraded_exemption_still_honors_ownership_scope_leg(monkeypatch, tmp_path):
    """The degraded route must not skip LEG 3 -- an ownership-scope denial
    still denies even with the sentinel present and agent_type degraded."""
    _git_commit_agent_setup(monkeypatch, scope_result=(False, "claimed by peer session x"))
    transcript = _write_transcript(tmp_path, _sentinel_transcript_line())
    result = guard.check(
        _payload(
            _SCOPED_COMMIT_CMD,
            agent_type=_DEGRADED_HOST_NATIVE_AGENT_TYPE,
            transcript_path=transcript,
        )
    )
    assert result is not None, "expected DENY on an ownership-scope refusal"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_degraded_exemption_still_denies_a_sweeping_pathspec(monkeypatch, tmp_path):
    """LEG 3's sweeping-pathspec rejection must still run unconditionally
    on the degraded route -- the sentinel is not a blanket bypass."""
    _git_commit_agent_setup(monkeypatch)
    transcript = _write_transcript(tmp_path, _sentinel_transcript_line())
    result = guard.check(
        _payload(
            "git commit -A -m msg",
            agent_type=_DEGRADED_HOST_NATIVE_AGENT_TYPE,
            transcript_path=transcript,
        )
    )
    assert result is not None, "expected DENY on a sweeping pathspec"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- Unit coverage of the sentinel-reading helper itself ---


def test_sentinel_helper_true_on_matching_first_line(tmp_path):
    path = _write_transcript(tmp_path, _sentinel_transcript_line())
    assert guard._transcript_carries_commit_phase_sentinel(path) is True


def test_sentinel_helper_false_on_non_matching_first_line(tmp_path):
    path = _write_transcript(tmp_path, '{"role": "user", "content": "hi"}')
    assert guard._transcript_carries_commit_phase_sentinel(path) is False


def test_sentinel_helper_false_on_missing_file(tmp_path):
    missing = str(tmp_path / "nope.jsonl")
    assert guard._transcript_carries_commit_phase_sentinel(missing) is False


def test_sentinel_helper_false_on_empty_path():
    assert guard._transcript_carries_commit_phase_sentinel("") is False
    assert guard._transcript_carries_commit_phase_sentinel(None) is False


def test_sentinel_helper_false_when_sentinel_only_on_second_line(tmp_path):
    """Only the FIRST line is trusted -- a later line (which a subagent's
    own tool use could in principle append to) must not count."""
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        '{"role": "user", "content": "hi"}\n' + _sentinel_transcript_line() + "\n",
        encoding="utf-8",
    )
    assert guard._transcript_carries_commit_phase_sentinel(str(path)) is False
