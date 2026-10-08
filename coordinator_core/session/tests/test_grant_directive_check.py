
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
    monkeypatch.setattr(grant_directive, "check_tier_u_grant", lambda: (False, None))
    code, message = grant_directive.run_grant_directive(["check"])
    assert code == grant_directive.EXIT_FALSE
    assert "no Tier-U grant found" in message


def test_check_denied_with_record_names_the_gate(monkeypatch):
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
    calls = []
    original_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or original_run(*a, **k))
    monkeypatch.setattr(grant_directive, "check_tier_u_grant", lambda: (True, {}))
    grant_directive.run_grant_directive(["check"])
    assert calls == []


def test_unknown_verb_still_usage():
    code, message = grant_directive.run_grant_directive(["bogus"])
    assert code == grant_directive.EXIT_USAGE
    assert "bogus" in message
