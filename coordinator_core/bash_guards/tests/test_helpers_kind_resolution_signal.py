"""Direct branch coverage for ``_helpers.emit_kind_resolution_failure_signal``.

The function derives its reported disposition from the verdict the caller is
about to emit; a message that misreports the guard's decision is worse than no
message, so the 3x3 leg/outcome matrix and the unrecognized-shape fallback are
pinned here independent of any calling guard.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _helpers
from coordinator_core.bash_guards._verdict import collecting

_GUARD = "block_example"

_LEG_AGENT_ID = "agent_id-canonicalization"
_LEG_GIT_ROOT = "backpointer-read (git_root empty/unresolvable)"
_LEG_CHAIN = "backpointer-read (missing, unreadable, or malformed chain)"

_OUT_ALLOW = "this guard ALLOWS it (verdict: allow)"
_OUT_DENY = "this guard CONFINES it (denies) (verdict: deny)"
_OUT_UNKNOWN = "disposition not reported (unrecognized verdict shape)"

_DENY = {"hookSpecificOutput": {"permissionDecision": "deny"}}

# (agent_id, git_root, expected leg fragment, expected git_root rendering)
_LEGS = [
    pytest.param("", "/repo", _LEG_AGENT_ID, "present", id="agent_id-empty"),
    pytest.param("a1", "", _LEG_GIT_ROOT, "empty", id="git_root-empty"),
    pytest.param("a1", None, _LEG_GIT_ROOT, "empty", id="git_root-none"),
    pytest.param("a1", "/repo", _LEG_CHAIN, "present", id="chain-unreadable"),
]

_OUTCOMES = [
    pytest.param(None, _OUT_ALLOW, id="allow"),
    pytest.param(_DENY, _OUT_DENY, id="deny"),
    pytest.param({"hookSpecificOutput": {"permissionDecision": "allow"}}, _OUT_UNKNOWN, id="non-deny-decision"),
]


def _emit(capsys, agent_id, git_root, verdict):
    _helpers.emit_kind_resolution_failure_signal(_GUARD, agent_id, git_root, verdict)
    return capsys.readouterr()


@pytest.mark.parametrize("agent_id,git_root,leg,root_word", _LEGS)
@pytest.mark.parametrize("verdict,outcome", _OUTCOMES)
def test_leg_outcome_matrix(capsys, agent_id, git_root, leg, root_word, verdict, outcome):
    captured = _emit(capsys, agent_id, git_root, verdict)
    assert captured.out == ""
    line = captured.err
    assert line.startswith("%s: kind-resolution-failed -- leg=" % _GUARD)
    assert "leg=%s" % leg in line
    assert "git_root=%s." % root_word in line
    assert line.rstrip("\n").endswith("%s." % outcome)
    assert line.count("\n") == 1


def test_empty_agent_id_wins_over_empty_git_root(capsys):
    # First elif branch order: agent_id canonicalization is checked before git_root.
    line = _emit(capsys, "", "", None).err
    assert "leg=%s" % _LEG_AGENT_ID in line
    assert _LEG_GIT_ROOT not in line
    assert "git_root=empty" in line


@pytest.mark.parametrize(
    "verdict",
    [
        {},
        {"hookSpecificOutput": "deny"},
        {"hookSpecificOutput": None},
        {"hookSpecificOutput": []},
        {"hookSpecificOutput": {}},
        {"permissionDecision": "deny"},
        "deny",
        ["deny"],
    ],
    ids=lambda v: repr(v),
)
def test_malformed_verdict_shape_reports_unrecognized(capsys, verdict):
    line = _emit(capsys, "a1", "/repo", verdict).err
    assert line.rstrip("\n").endswith("%s." % _OUT_UNKNOWN)
    assert "ALLOWS" not in line
    assert "CONFINES" not in line


def test_signal_carries_no_payload_contents(capsys):
    line = _emit(capsys, "secret-agent-id-123", "/secret/cwd", None).err
    assert "secret-agent-id-123" not in line
    assert "/secret/cwd" not in line


def test_allow_records_silent_declaration(capsys):
    with collecting() as silences:
        _emit(capsys, "a1", "", None)
    assert len(silences) == 1
    assert silences[0].guard_name == _GUARD
    assert "kind-resolution-failed" in silences[0].reason
    assert _LEG_GIT_ROOT in silences[0].reason
    assert "git_root=empty" in silences[0].reason


@pytest.mark.parametrize(
    "verdict", [_DENY, {}, {"hookSpecificOutput": {"permissionDecision": "allow"}}]
)
def test_non_allow_records_no_silent_declaration(capsys, verdict):
    with collecting() as silences:
        _emit(capsys, "a1", "/repo", verdict)
    assert silences == []


def test_allow_outside_collection_is_a_noop_for_recording(capsys):
    assert _emit(capsys, "a1", "/repo", None).err != ""
