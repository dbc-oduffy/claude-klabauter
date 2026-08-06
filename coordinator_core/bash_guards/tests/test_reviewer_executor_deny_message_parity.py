"""Message-truth regression pins for
``block_reviewer_bash_outside_allowlist``'s per-``effective_type`` deny
message (Divergence 12, ``_DENY_MESSAGE_STANZA_OVERRIDES``).

This file adds the two proofs the guard module's own docstring negative-spec
promises but which no existing test file pins directly:

  1. ``coordinator:code-reviewer``'s deny message is byte-identical to a
     literal known-good string -- the AC3 invariant that message-truth work
     for ``coordinator:executor`` must never touch. Every existing test
     (``test_block_reviewer_bash_outside_allowlist*.py``) asserts SHAPE
     (deny vs allow, or substring presence) -- none pins the FULL string, so
     a future edit to a shared "Did you mean.../Denied: any other
     command..." stanza (which this guard's own docstring says stays
     type-agnostic) could silently drift the reviewer's message with no red
     test.
  2. A verdict-invariance table over a small command corpus, fired against
     BOTH confined types -- proves that resolving deny-message TEXT
     per-``effective_type`` (this module's ``_DENY_MESSAGE_STANZA_OVERRIDES``
     dict) never itself changes the underlying allow/deny VERDICT for either
     type. Message-truth work operating on prose alone must never widen or
     narrow the allowlist as a side effect.

Spec backlink: coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py
  module docstring, Divergence 12 and its "Negative spec, binding on any
  future third confined type."
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


def _confine(monkeypatch, subagent_type: str) -> None:
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id: subagent_type,
    )


def _verdict(result) -> str:
    if result is None:
        return "allow"
    return result["hookSpecificOutput"]["permissionDecision"]


def _reason(result) -> str:
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# AC3 -- code-reviewer deny message, byte-identical to a pinned literal.
# ---------------------------------------------------------------------------

_EXPECTED_REVIEWER_DENY_MESSAGE = (
    "BLOCKED: confined findings-agent Bash outside allowlist.\n"
    "\n"
    "Command: rm -rf /\n"
    "Reason: first command token is not coordinator-doc-new (got: rm)\n"
    "\n"
    "Use instead:\n"
    "  `git show`\n"
    "  git show / diff / log / status / blame / ls-files / rev-parse / describe\n"
    "  ls / cat / head / tail / wc / find / file / stat / grep\n"
    "  Denied: find with a write/execute flag such as -delete or -exec\n"
    "  Denied: unquoted shell-chaining metacharacter (; && || ` $( < & or newline)\n"
    "  coordinator-doc-new --type review-findings [--plan <path>] [--chunk <id>] ...\n"
    "  <claude-klabauter-root>/coordinator/bin/coordinator-doc-new --type review-findings ...\n"
    "  python3 <claude-klabauter-root>/coordinator/bin/coordinator-doc-new --type review-findings ..."
)


class TestCodeReviewerDenyMessageByteParity:
    """AC3 -- ``coordinator:code-reviewer``'s deny message must never drift,
    byte-for-byte, regardless of any per-``effective_type`` message work done
    for a different confined type."""

    def test_code_reviewer_deny_message_is_byte_identical_to_pinned_literal(
        self, monkeypatch
    ):
        _confine(monkeypatch, _REVIEWER_TYPE)
        payload = _payload("rm -rf /", _REVIEWER_TYPE)
        reason = _reason(guard.check(payload))
        # Fixed via a live capture against this module at the time this test
        # was authored, not hand-transcribed -- see this file's own module
        # docstring and the guard's Divergence 12 negative spec.
        assert reason == _EXPECTED_REVIEWER_DENY_MESSAGE


# ---------------------------------------------------------------------------
# Executor deny-message content -- RETIRED 2026-08-03 (DR-125,
# docs/plans/2026-08-03-narrow-subagent-commit-confinement-two-classes.md,
# chunk C2). ``coordinator:executor`` was removed from
# ``_helpers._CONFINED_FINDINGS_AGENTS``, the SOLE gate this guard consults
# to decide whether to evaluate a payload at all -- ``guard.check`` now
# returns ``None`` (allow) unconditionally for any ``coordinator:executor``
# payload, so the executor-framed deny-message content this class used to
# pin (no "review-findings" pin, no findings-agent framing, no "dispatch a
# separate executor" advice, names what it can run) can never render again
# through this guard: there is no longer a deny envelope to read a reason
# off. ``TestCodeReviewerDenyMessageByteParity`` above is unaffected and
# remains the byte-identical pin for the type that stays confined.
# ---------------------------------------------------------------------------


class TestExecutorNoLongerConfinedByThisGuard:
    """Bucket-3 inversion (three-way rule, chunk C2): 'executor IS confined'
    becomes 'executor is NOT confined while coordinator:code-reviewer still
    IS' -- proven directly rather than by inverting the old message-content
    assertions, which have no reason string to inspect once the verdict is
    allow."""

    def test_executor_allowed_where_reviewer_still_denied(self, monkeypatch):
        cmd = "curl https://evil.example/x"
        _confine(monkeypatch, _EXECUTOR_TYPE)
        assert guard.check(_payload(cmd, _EXECUTOR_TYPE)) is None

        _confine(monkeypatch, _REVIEWER_TYPE)
        assert _verdict(guard.check(_payload(cmd, _REVIEWER_TYPE))) == "deny"


# ---------------------------------------------------------------------------
# Verdict-invariance table -- resolving deny-message TEXT per-effective_type
# must never itself change the allow/deny VERDICT for either confined type.
#
# Executor column updated 2026-08-03 (DR-125, chunk C2): with
# ``coordinator:executor`` removed from ``_CONFINED_FINDINGS_AGENTS``, this
# guard (the ONLY thing this table calls -- ``guard.check``, not the full
# multi-guard pipeline) no-ops unconditionally for executor payloads, so
# every row that used to deny for executor solely via THIS guard's own
# confinement now allows. The code-reviewer column is the AC4 load-bearing
# check: byte-identical to its pre-edit values, proving code-reviewer's own
# confinement behaviour is untouched by this narrowing.
# ---------------------------------------------------------------------------

_VERDICT_TABLE = [
    # (command, expected verdict for code-reviewer, expected verdict for executor)
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
    # (Amendment 2, 2026-08-03) both confined types now share the pytest
    # module allowance -- see block_reviewer_bash_outside_allowlist's own
    # Amendment 2 docstring entry.
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
