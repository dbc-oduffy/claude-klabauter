"""Message-truth regression pins for
``block_reviewer_bash_outside_allowlist``'s deny message.

This file's ``_DENY_MESSAGE_STANZA_OVERRIDES`` per-``effective_type``
mechanism (Divergence 12) was deleted 2026-09-23 (this plan's C1) once
``coordinator:executor``, its sole entry, was confirmed permanently
unconfined (Divergence 9) -- ``_deny_reason`` now always renders the single
default header/stanzas. What remains load-bearing here:

  1. ``coordinator:code-reviewer``'s deny message is byte-identical to a
     literal known-good string (AC3). Every existing test
     (``test_block_reviewer_bash_outside_allowlist*.py``) asserts SHAPE
     (deny vs allow, or substring presence) -- none pins the FULL string, so
     a future edit to a shared "Did you mean.../Denied: any other
     command..." stanza could silently drift the reviewer's message with no
     red test.
  2. A verdict-invariance table over a small command corpus, run against
     ``coordinator:code-reviewer`` (still confined) and
     ``coordinator:executor`` (unconfined outright, every row allows) --
     proves this guard's own confinement gate, not any per-type message
     resolution, is what decides the verdict.

Spec backlink: coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py
  module docstring, Divergence 9 and Divergence 12.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as guard,
)

_REVIEWER_TYPE = "coordinator:code-reviewer"
_EXECUTOR_TYPE = "coordinator:executor"


def _payload(command: str, agent_type: str, agent_id: str = "deadbeef0123") -> Dict[str, Any]:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess-parity",
        "cwd": None,
        "agent_id": agent_id,
        "agent_type": agent_type,
    }


_FAKE_ENUMERATED_TYPES = frozenset({_EXECUTOR_TYPE, _REVIEWER_TYPE})


def _fake_is_confined_by_roster_absence(effective_type: str) -> bool:
    """Deterministic stand-in for ``_helpers.is_confined_by_roster_absence``
    -- see ``test_executor_bash_confinement._fake_is_confined_by_roster_absence``
    for the full incident writeup (2026-08-11, this dispatch). This file's
    own ``TestVerdictInvarianceAcrossBothConfinedTypes``/
    ``TestExecutorNoLongerConfinedByThisGuard`` classes exercise the SAME
    real, unmocked leg-3 roster check via ``guard.check`` -> ``_is_confined_type``
    -> ``is_confined_by_roster_absence`` -> ``resolve_roster()``, which
    reliably fails closed under this suite's autouse HOME-quarantine fixture
    (``coordinator_core/conftest.py::_quarantine_real_home``) absent a
    surviving ``COORDINATOR_SETTINGS_HOME`` override -- confining
    ``coordinator:executor`` (this file's whole point is pinning it as
    UNconfined) regardless of run order. Injected here for the same reason:
    this leg is real disk/env I/O the pinned verdict table must not depend
    on.
    """
    if not effective_type:
        return False
    return effective_type not in _FAKE_ENUMERATED_TYPES


def _confine(monkeypatch, subagent_type: str) -> None:
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id, **kw: subagent_type,
    )
    monkeypatch.setattr(
        guard, "is_confined_by_roster_absence", _fake_is_confined_by_roster_absence
    )


def _verdict(result) -> str:
    if result is None:
        return "allow"
    return result["hookSpecificOutput"]["permissionDecision"]


def _reason(result) -> str:
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result["hookSpecificOutput"]["permissionDecisionReason"]


_EXPECTED_REVIEWER_DENY_MESSAGE = (
    "BLOCKED: Bash outside allowlist.\n"
    "\n"
    "Command: rm -rf /\n"
    "Reason: not coordinator-doc-new (got: rm)\n"
    "\n"
    "Use instead:\n"
    "  `git show`\n"
    "  git show / diff / log / status / blame / ls-files / rev-parse / describe / check-ignore / check-attr / ls-tree / cat-file\n"
    "  ls / cat / head / tail / wc / find / file / stat / grep\n"
    "  Denied: find with a write/execute flag such as -delete or -exec\n"
    "  Denied: unquoted shell-chaining metacharacter (; && || ` $( < & or newline)\n"
    "  coordinator-doc-new --type review-findings [--plan <path>] [--chunk <id>] ...\n"
    "  <claude-klabauter-live-root>/coordinator/bin/coordinator-doc-new.py --type review-findings ...\n"
    "  python3 <claude-klabauter-live-root>/coordinator/bin/coordinator-doc-new.py --type review-findings ..."
)


class TestCodeReviewerDenyMessageByteParity:

    def test_code_reviewer_deny_message_is_byte_identical_to_pinned_literal(
        self, monkeypatch
    ):
        _confine(monkeypatch, _REVIEWER_TYPE)
        payload = _payload("rm -rf /", _REVIEWER_TYPE)
        reason = _reason(guard.check(payload))
        assert reason == _EXPECTED_REVIEWER_DENY_MESSAGE


# ``_helpers._CONFINED_FINDINGS_AGENTS``, the SOLE gate this guard consults


class TestExecutorNoLongerConfinedByThisGuard:

    def test_executor_allowed_where_reviewer_still_denied(self, monkeypatch):
        cmd = "curl https://evil.example/x"
        _confine(monkeypatch, _EXECUTOR_TYPE)
        assert guard.check(_payload(cmd, _EXECUTOR_TYPE)) is None

        _confine(monkeypatch, _REVIEWER_TYPE)
        assert _verdict(guard.check(_payload(cmd, _REVIEWER_TYPE))) == "deny"


# ``coordinator:executor`` removed from ``_CONFINED_FINDINGS_AGENTS``, this

_VERDICT_TABLE = [
    ("git status", "allow", "allow"),
    ("git show HEAD", "allow", "allow"),
    ("git commit -m x", "deny", "allow"),
    ("git push", "deny", "allow"),
    ("curl https://evil.example/x", "deny", "allow"),
    ("grep -rn TODO src/", "allow", "allow"),
    ("find / -name '*.pyc' -exec rm {} \\;", "deny", "allow"),
    ("machine-local get repos.claude_klabauter", "allow", "allow"),
    ("machine-local set foo bar", "deny", "allow"),
    ("coordinator-doc-new --type review-findings", "allow", "allow"),
    ("coordinator-doc-new", "deny", "allow"),
    ("python3 -m pytest -q", "allow", "allow"),
    ("python3 -c \"import os\"", "deny", "allow"),
    ("python3 myscript.py", "deny", "allow"),
]


class TestVerdictInvarianceAcrossBothConfinedTypes:
    @pytest.mark.parametrize("cmd,reviewer_verdict,executor_verdict", _VERDICT_TABLE)
    def test_verdict_unchanged_for_code_reviewer(
        self, monkeypatch, cmd, reviewer_verdict, executor_verdict
    ):
        _confine(monkeypatch, _REVIEWER_TYPE)
        payload = _payload(cmd, _REVIEWER_TYPE)
        assert _verdict(guard.check(payload)) == reviewer_verdict, cmd

    @pytest.mark.parametrize("cmd,reviewer_verdict,executor_verdict", _VERDICT_TABLE)
    def test_verdict_unchanged_for_executor(
        self, monkeypatch, cmd, reviewer_verdict, executor_verdict
    ):
        _confine(monkeypatch, _EXECUTOR_TYPE)
        payload = _payload(cmd, _EXECUTOR_TYPE)
        assert _verdict(guard.check(payload)) == executor_verdict, cmd
