"""Regression tests for `_crash_deny`'s remediation text
(coordinator_core.bash_guards.dispatch).

Bug: the message told the operator to "Re-run, or invoke the standalone
check function to see the underlying error" -- but `_crash_deny` is only
ever reached from a hard-deny guard crash, and this dispatcher denies EVERY
subsequent Bash command in the session once that happens (`echo probe`,
`git status`, committing already-finished work -- all of it). "Re-run"/
"invoke the standalone check function" both require the exact tool the
crash just disabled, so the advice could never be followed from inside the
failure. Confirmed 2026-07-28 (example-game-repo-em cross-repo report,
`cross-repo/inbox/2026-07-28-example-game-repo-em-sentinel-guard-fails-closed-and-
bricked-bash.md`): a whole session was lost to this, unable even to
hand-deliver its own bug report.

Fix: the message is rewritten to be actionable WITHOUT Bash -- it states
plainly that this is a guard bug (not a policy verdict on the command), and
names a next step a human/PM can carry out from their OWN shell (open the
guard file under `coordinator_core/bash_guards/` in claude-klabauter, fix it,
re-run) rather than one that depends on the disabled tool.

Message-only fix: the fail-closed behavior itself (denying every Bash
command for the rest of the session) is UNCHANGED and deliberately not
covered by these tests -- that is a separate decision, not this fix's
concern.
"""

from __future__ import annotations

import inspect

from coordinator_core.bash_guards.dispatch import _crash_deny, evaluate_payload_json


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


class TestCrashDenyMessageIsActionableWithoutBash:
    def test_still_denies(self):
        result = _crash_deny("no-verify", ValueError("boom"))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_names_the_crashing_guard_and_exception(self):
        result = _crash_deny("destructive-git-orphan", KeyError("missing"))
        reason = _reason(result)
        assert "destructive-git-orphan" in reason
        assert "KeyError" in reason
        assert "missing" in reason

    def test_states_this_is_a_guard_bug_not_a_policy_verdict(self):
        reason = _reason(_crash_deny("no-verify", ValueError("boom")))
        assert "bug" in reason.lower()
        assert "not a policy verdict" in reason.lower()

    def test_does_not_tell_the_operator_to_re_run_or_use_bash(self):
        # The old text -- "Re-run, or invoke the standalone check function
        # to see the underlying error" -- required the exact tool the crash
        # just disabled. Neither phrase may survive.
        reason = _reason(_crash_deny("no-verify", ValueError("boom")))
        assert "invoke the standalone check function" not in reason
        assert "Re-run, or" not in reason

    def test_names_a_next_step_that_does_not_require_bash(self):
        reason = _reason(_crash_deny("no-verify", ValueError("boom")))
        # Actionable from outside the session: a human/PM with their own
        # shell, pointed at the file to open.
        assert "coordinator_core/bash_guards/" in reason
        assert "human" in reason.lower() or "PM" in reason

    def test_explains_every_bash_command_is_now_denied(self):
        reason = _reason(_crash_deny("no-verify", ValueError("boom")))
        assert "every" in reason.lower() and "bash" in reason.lower()


class TestCrashDenyResolutionClass:
    """`resolution_class` (2026-08-05): threads example-doctrine-repo's opaque engine-
    resolution signal into the crash-deny envelope so it names WHICH ENGINE
    crashed. Must be backward compatible byte-for-byte when unsupplied or
    unrecognized -- see `_crash_deny`'s own docstring section."""

    def test_omitted_is_byte_identical_to_none(self):
        omitted = _crash_deny("no-verify", ValueError("boom"))
        explicit_none = _crash_deny("no-verify", ValueError("boom"), resolution_class=None)
        assert omitted == explicit_none

    def test_unrecognized_class_degrades_to_omitted_text(self):
        omitted = _reason(_crash_deny("no-verify", ValueError("boom")))
        unrecognized = _reason(
            _crash_deny("no-verify", ValueError("boom"), resolution_class="some-future-value")
        )
        assert omitted == unrecognized

    def test_never_emits_a_placeholder_phrase_for_none(self):
        reason = _reason(_crash_deny("no-verify", ValueError("boom")))
        assert "engine: None" not in reason
        assert "engine: none" not in reason.lower()

    def test_resolved_engine_names_the_engine(self):
        reason = _reason(
            _crash_deny("no-verify", ValueError("boom"), resolution_class="resolved-engine")
        )
        assert "resolved engine" in reason

    def test_live_working_tree_names_the_engine_not_as_an_error(self):
        reason = _reason(
            _crash_deny("no-verify", ValueError("boom"), resolution_class="live-working-tree")
        )
        assert "live working tree" in reason

    def test_unresolved_names_the_engine(self):
        reason = _reason(
            _crash_deny("no-verify", ValueError("boom"), resolution_class="unresolved")
        )
        assert "unresolved" in reason

    def test_three_classes_produce_distinct_reasons(self):
        reasons = {
            cls: _reason(_crash_deny("no-verify", ValueError("boom"), resolution_class=cls))
            for cls in ("resolved-engine", "live-working-tree", "unresolved")
        }
        assert len(set(reasons.values())) == 3


class TestEvaluatePayloadJsonFeatureDetection:
    """example-doctrine-repo's `preuse-bash-dispatch.py` feature-detects `resolution_class` via
    `inspect.signature(evaluate_payload_json).parameters` and passes the
    kwarg ONLY if present -- this is the whole contract this change exists
    to satisfy."""

    def test_signature_declares_resolution_class(self):
        params = inspect.signature(evaluate_payload_json).parameters
        assert "resolution_class" in params
        assert params["resolution_class"].default is None

    def test_call_without_resolution_class_is_unaffected(self):
        # Same payload, called both ways -- must be byte-identical, since
        # every pre-existing caller (and example-doctrine-repo's own feature-detect miss path)
        # omits this kwarg entirely.
        payload = '{"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "session_id": "s", "cwd": "/tmp"}'
        omitted = evaluate_payload_json(payload)
        explicit_none = evaluate_payload_json(payload, resolution_class=None)
        assert omitted == explicit_none
