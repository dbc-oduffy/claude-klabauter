"""block_cross_repo_agent_write: an agent's Write/Edit into another registered repo needs a grant."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core import machine_resolver
from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
    check_cross_repo_writes,
    grant_file,
)
from coordinator_core.write_guards import block_cross_repo_agent_write as guard

SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    run, sibling, stranger = (tmp_path / n for n in ("run-repo", "sib-repo", "stranger"))
    for repo in (run, sibling, stranger):
        (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        machine_resolver,
        "merged_flat_registry",
        lambda: {"repos.run": str(run), "repos.sib": str(sibling)},
    )
    return SimpleNamespace(run=run, sib=sibling, stranger=stranger)


def _payload(fleet, target, **over):
    p = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
        "agent_id": "agent-1",
        "session_id": SESSION,
        "cwd": str(fleet.run),
    }
    p.update(over)
    return p


def test_agent_write_into_registered_sibling_is_denied(fleet):
    out = guard.check(_payload(fleet, fleet.sib / "src" / "merge_band_parts.py"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "run-repo" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_write_tool_and_relative_path_are_denied_too(fleet):
    rel = "../sib-repo/new_test.py"
    p = _payload(fleet, rel, tool_name="Write")
    assert guard.check(p)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_write_inside_run_repo_is_allowed(fleet):
    assert guard.check(_payload(fleet, fleet.run / "pkg" / "a.py")) is None


def test_em_inline_write_without_agent_id_is_allowed(fleet):
    assert guard.check(_payload(fleet, fleet.sib / "a.py", agent_id="")) is None


def test_unregistered_checkout_is_allowed(fleet):
    assert guard.check(_payload(fleet, fleet.stranger / "a.py")) is None


def test_non_repo_path_is_allowed(fleet, tmp_path):
    assert guard.check(_payload(fleet, tmp_path / "plain" / "a.py")) is None


def test_granted_sibling_path_is_allowed_and_a_neighbour_is_not(fleet):
    row = SimpleNamespace(
        id="R1", writes=["../sib-repo/src/ok.py"], writes_under=()
    )
    check_cross_repo_writes([row], fleet.run, approved=True, session_id=SESSION)
    assert grant_file(fleet.run, SESSION).is_file()
    assert guard.check(_payload(fleet, fleet.sib / "src" / "ok.py")) is None
    out = guard.check(_payload(fleet, fleet.sib / "src" / "other.py"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_failed_approval_records_no_grant(fleet, monkeypatch):
    from coordinator_core.ops.dispatch_emit import cross_repo_write_refusal as m

    monkeypatch.setattr(m, "is_remote_venue", lambda env=None: False)
    row = SimpleNamespace(id="R1", writes=["../sib-repo/src/ok.py"], writes_under=())
    with pytest.raises(m.CrossRepoApprovalNeeded):
        check_cross_repo_writes([row], fleet.run, approved=False, session_id=SESSION)
    assert not grant_file(fleet.run, SESSION).exists()


def _mint(repo, session=SESSION):
    from coordinator_core.ops.dispatch_emit import run_token

    return run_token.mint(repo, "run-c2", "scripts/x.js", session_id=session)


def _denied(out):
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_session_holding_a_run_in_the_target_repo_is_admitted(fleet):
    _mint(fleet.sib)
    assert guard.check(_payload(fleet, fleet.sib / "docs" / "plans" / "p.md")) is None


def test_third_registered_repo_without_token_is_still_denied(fleet, tmp_path, monkeypatch):
    third = tmp_path / "third"
    (third / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        machine_resolver,
        "merged_flat_registry",
        lambda: {"repos.run": str(fleet.run), "repos.sib": str(fleet.sib), "repos.third": str(third)},
    )
    _mint(fleet.sib)
    out = guard.check(_payload(fleet, third / "a.py"))
    assert _denied(out)
    assert "run-repo" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_token_naming_another_session_does_not_admit(fleet):
    _mint(fleet.sib, session="99999999-0000-0000-0000-000000000000")
    assert _denied(guard.check(_payload(fleet, fleet.sib / "a.py")))


def test_expired_token_does_not_admit(fleet, monkeypatch):
    import time

    from coordinator_core.ops.dispatch_emit import run_token

    _mint(fleet.sib)
    real = time.time()
    monkeypatch.setattr(run_token.time, "time", lambda: real + run_token.TTL_SECONDS + 10)
    assert _denied(guard.check(_payload(fleet, fleet.sib / "a.py")))


def test_empty_session_id_is_denied_even_when_a_token_carries_an_empty_session(fleet):
    _mint(fleet.sib, session="")
    assert _denied(guard.check(_payload(fleet, fleet.sib / "a.py", session_id="")))


def test_em_inline_write_unchanged_with_token_held(fleet):
    _mint(fleet.sib)
    assert guard.check(_payload(fleet, fleet.sib / "a.py", agent_id="")) is None
