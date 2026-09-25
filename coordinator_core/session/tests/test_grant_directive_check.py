"""
coordinator_core.session.tests.test_grant_directive_check — tests for the
`check` verb `run_grant_directive` (coordinator_core.session.grant_directive)
gained in P071-C2.

Unit-scoped against monkeypatched `check_tier_u_grant` /
`_ungranted_record_failing_gate` rather than a real git-backed session
fixture: `check`'s own contract is "delegate to the existing predicate and
name the gate on denial" — the predicate and the gate-naming diagnostic
already carry their own fixtures (`test_grant.py`,
`test_grant_deny_distinguishes_a_live_record.py`). This file's job is the
verb's argv parsing and exit/message mapping, which needs neither.

Spec backlink: docs/plans/2026-09-11-grant-and-validation-gates-fire-from-the.md (P071-C2)
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.session import grant_directive

pytestmark = [pytest.mark.cadence]


def test_parse_check_args_empty_is_legal():
    assert grant_directive.parse_check_args([]) is None


def test_parse_check_args_rejects_any_argv():
    assert grant_directive.parse_check_args(["--only-ceremony", "x"]) is grant_directive.ARGS_INVALID
    assert grant_directive.parse_check_args(["extra"]) is grant_directive.ARGS_INVALID


def test_check_with_trailing_argv_is_usage():
    code, message = grant_directive.run_grant_directive(["check", "extra"])
    assert code == grant_directive.EXIT_USAGE
    assert message


def test_check_granted_is_exit_ok_with_no_message(monkeypatch):
    monkeypatch.setattr(
        grant_directive, "check_tier_u_grant", lambda: (True, {"granted_by": "pm"})
    )
    code, message = grant_directive.run_grant_directive(["check"])
    assert code == grant_directive.EXIT_OK
    assert message == ""


def test_check_no_record_names_absence_directly(monkeypatch):
    """`check_tier_u_grant` returns `(False, None)` when no sid resolves or
    no grant file exists — the review note's case the gate-naming helper
    cannot explain (it needs a real record). Named directly, not routed
    through the helper."""
    monkeypatch.setattr(grant_directive, "check_tier_u_grant", lambda: (False, None))
    code, message = grant_directive.run_grant_directive(["check"])
    assert code == grant_directive.EXIT_FALSE
    assert "no Tier-U grant found" in message


def test_check_denied_with_record_names_the_gate(monkeypatch):
    """A denial with a real record routes through
    `_ungranted_record_failing_gate` (lazy-imported inside the deny branch
    only) so the message names WHICH gate rejected it — seed AC3."""
    record = {"granted_by": "pm", "session_id": "other-sid"}
    monkeypatch.setattr(grant_directive, "check_tier_u_grant", lambda: (False, record))
    monkeypatch.setattr(
        "coordinator_core.bash_guards.check_test_suite_invocation._ungranted_record_failing_gate",
        lambda rec, sid, cwd: "the grant was written for a different session",
    )
    code, message = grant_directive.run_grant_directive(["check"])
    assert code == grant_directive.EXIT_FALSE
    assert "the grant was written for a different session" in message


def test_check_spawns_no_subprocess(monkeypatch):
    """Zero process spawns — the same in-process shape `grant`/`revoke`
    already hold (module docstring: no `tier-u-grant-cli` subprocess is
    introduced by this plan)."""
    calls = []
    original_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or original_run(*a, **k))
    monkeypatch.setattr(grant_directive, "check_tier_u_grant", lambda: (True, {}))
    grant_directive.run_grant_directive(["check"])
    assert calls == []


def test_unknown_verb_still_usage():
    """Regression guard: adding `check` must not disturb the existing
    unknown-verb fallthrough."""
    code, message = grant_directive.run_grant_directive(["bogus"])
    assert code == grant_directive.EXIT_USAGE
    assert "bogus" in message
