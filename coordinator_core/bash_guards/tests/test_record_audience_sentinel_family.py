"""The sentinel and worktree creation guards return AlternativeRecords: no Override, an
EscalationRoute, an EM render equal to the legacy text, and a subagent render that is a deny
carrying the route."""
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import (
    block_approval_sentinel_creation as approval,
    block_dev_repo_sentinel_removal as dev_repo,
    block_worktree_creation as worktree,
    block_worktree_sentinel_creation as worktree_sentinel,
)
from coordinator_core.guard_messages.alternative_record import (
    AlternativeRecord,
    Audience,
    EscalationRoute,
    Override,
)
from coordinator_core.guard_messages.render import render


def _payload(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _strict(monkeypatch):
    from coordinator_core import machine_profile

    monkeypatch.setattr(machine_profile, "guard_level", lambda name=None: "strict")


_CASES = [
    ("approval-direct", lambda: approval.check_ungated(_payload("touch .coordinator-doctrine-edit-approved"))),
    ("approval-indirection", lambda: approval.check_ungated(_payload("xargs rm"))),
    ("dev-repo-direct", lambda: dev_repo.check(_payload("rm .coordinator-dev-repo"))),
    ("worktree", lambda: worktree.check(_payload("git worktree add ../x"))),
    ("worktree-sentinel-direct", lambda: worktree_sentinel.check(_payload("touch .coordinator-override-worktree-guard"))),
    ("worktree-sentinel-indirection", lambda: worktree_sentinel.check(_payload("xargs rm"))),
]


@pytest.mark.parametrize("name,fire", _CASES, ids=[c[0] for c in _CASES])
def test_deny_is_a_record_with_a_route_and_no_override(name, fire, monkeypatch):
    _strict(monkeypatch)
    record = fire()
    assert isinstance(record, AlternativeRecord)
    assert record.verdict_kind == "deny"
    assert record.em_text
    assert not any(isinstance(m, Override) for m in record.members)
    assert any(isinstance(m, EscalationRoute) for m in record.members)


@pytest.mark.parametrize("name,fire", _CASES, ids=[c[0] for c in _CASES])
def test_em_render_is_the_legacy_text_and_subagent_render_denies_with_the_route(
    name, fire, monkeypatch
):
    _strict(monkeypatch)
    record = fire()
    em = render(record, Audience.EM)["hookSpecificOutput"]
    assert em["permissionDecisionReason"] == record.em_text
    sub = render(record, Audience.SUBAGENT)["hookSpecificOutput"]
    assert sub["permissionDecision"] == "deny"
    assert "needs authorizing" in sub["permissionDecisionReason"]


def test_gate_disable_denial_is_a_record(monkeypatch):
    _strict(monkeypatch)
    record = approval.gate_disable_denial(_payload("coordinator set doctrine_edit_gate off > x"))
    if record is None:
        pytest.skip("shape not flagged on this host")
    assert isinstance(record, AlternativeRecord)
    assert any(isinstance(m, EscalationRoute) for m in record.members)


def test_dev_repo_advisory_is_an_advisory_record():
    record = dev_repo.check_advisory(_payload("echo .coordinator-dev-repo | xargs rm"))
    assert isinstance(record, AlternativeRecord)
    assert record.verdict_kind == "advisory"
    assert any(isinstance(m, EscalationRoute) for m in record.members)
    assert render(record, Audience.EM)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_a_non_strict_level_returns_the_leveled_envelope_not_the_record(monkeypatch):
    from coordinator_core import machine_profile

    monkeypatch.setattr(machine_profile, "guard_level", lambda name=None: "warn")
    out = worktree_sentinel.check(_payload("touch .coordinator-override-worktree-guard"))
    assert isinstance(out, dict)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


@pytest.mark.parametrize(
    "fire",
    [
        lambda: approval.check_ungated(_payload("ls")),
        lambda: worktree.check(_payload("git worktree list")),
        lambda: worktree_sentinel.check(_payload("ls")),
        lambda: dev_repo.check(_payload("ls")),
    ],
)
def test_an_allowed_command_returns_none(fire):
    assert fire() is None
