"""Audience pins for the dispatch_checks family guards that return an AlternativeRecord.

Each case drives the production `check_*` entry point. The SUBAGENT render must
carry an escalation route and no override text; the EM render of an overridable
guard must carry the Override member; a non-matching command must still allow.

These pin `render`, not the dispatcher seam: `dispatch._render_record` resolves
the audience only for a record that carries an Override, so a record with no
Override (runaway-find, destructive-rm) reaches every receiver as its EM render.
"""
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.guard_messages.alternative_record import (
    AlternativeRecord,
    EscalationRoute,
    Override,
)
from coordinator_core.guard_messages.testing import envelope_of

_CASES = [
    ("runaway-find", lambda: dc.check_runaway_find("find / -name x", "s"), "deny", None),
    ("destructive-rm", lambda: dc.check_destructive_rm("rm -rf $(echo x)", "s"), "deny", None),
    ("no-verify", lambda: dc.check_no_verify("git commit --no-verify -m x", "s"), "deny", "COORDINATOR_OVERRIDE_NO_VERIFY"),
    ("destructive-git-orphan", lambda: dc.check_destructive_git_orphan("git push --force origin main", "s"), "deny", "COORDINATOR_ALLOW_ORPHAN"),
    ("destructive-git-revert", lambda: dc.check_destructive_git_revert("git reset --hard", "s"), "deny", "COORDINATOR_OVERRIDE_GIT_REVERT"),
    ("sed-range-read-advise", lambda: dc.check_sed_range_read_advise("sed -n '1,5p' f.txt", "s"), "advisory", "COORDINATOR_ALLOW_SED_RANGE"),
]

_NON_MATCHING = [
    lambda: dc.check_runaway_find("find . -maxdepth 2 -name x", "s"),
    lambda: dc.check_no_verify("git commit -m x", "s"),
    lambda: dc.check_sed_range_read_advise("echo hi", "s"),
]


def _text(envelope) -> str:
    hso = envelope["hookSpecificOutput"]
    return hso.get("permissionDecisionReason") or hso.get("additionalContext") or ""


@pytest.mark.parametrize("name, fire, kind, env", _CASES, ids=[c[0] for c in _CASES])
def test_message_is_a_record_with_a_route(name, fire, kind, env):
    record = fire()
    assert isinstance(record, AlternativeRecord)
    assert record.guard_name == name
    assert record.verdict_kind == kind
    assert any(isinstance(m, EscalationRoute) for m in record.members)
    overrides = [m for m in record.members if isinstance(m, Override)]
    assert [o.env_var for o in overrides] == ([env] if env else [])


@pytest.mark.parametrize("name, fire, kind, env", _CASES, ids=[c[0] for c in _CASES])
def test_subagent_render_has_a_route_and_no_override(name, fire, kind, env):
    rendered = _text(envelope_of(fire(), "subagent"))
    assert "Report " in rendered
    assert "COORDINATOR_" not in rendered


@pytest.mark.parametrize("fire", _NON_MATCHING)
def test_non_matching_command_still_allows(fire):
    assert fire() is None


@pytest.mark.parametrize("name, fire, kind, env", _CASES, ids=[c[0] for c in _CASES])
def test_em_render_is_the_legacy_text_verbatim(name, fire, kind, env):
    record = fire()
    assert record.em_text
    assert _text(envelope_of(record, "em")) == record.em_text


def test_em_text_keeps_the_legacy_use_instead_block():
    record = dc.check_runaway_find("find / -name x", "s")
    assert record.em_text.endswith("Use instead:\n  find . -maxdepth 3 -name '<pattern>'")
    assert "Use instead" not in _text(envelope_of(record, "subagent"))


def test_grep_rewrite_keeps_its_verdict_and_updated_input():
    record = dc.check_grep_via_bash_rewrite("grep -rn foo .", "s")
    assert record.verdict_kind == "rewrite" and record.rewrite_allow is True
    assert " -c " in record.updated_input["command"]
    em = envelope_of(record, "em")["hookSpecificOutput"]
    assert em["permissionDecision"] == "allow"
    assert em["updatedInput"] == record.updated_input
    assert em["additionalContext"] == record.em_text
    sub = envelope_of(record, "subagent")["hookSpecificOutput"]
    assert sub["permissionDecision"] == "allow"
    assert "Report " in sub["additionalContext"]


def test_grep_rewrite_still_declines_a_piped_grep():
    assert dc.check_grep_via_bash_rewrite("cat f | grep foo", "s") is None
