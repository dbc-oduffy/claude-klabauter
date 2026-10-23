"""Reachability grading of AlternativeRecord members per receiving caller class."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards._alternative_reachability import Verdict, reachable
from coordinator_core.guard_messages.alternative_record import (
    AdaptableOffer,
    Audience,
    CapabilityRef,
    EscalationRoute,
    Override,
    ReadyCommand,
)
from coordinator_core.subagent_sandbox.engine import load_policy

REVIEWER = "coordinator:code-reviewer"
EXECUTOR = "coordinator:executor"


@pytest.fixture(scope="module")
def policy():
    return load_policy(None)


def _grade(member, caller, policy, audience=Audience.SUBAGENT):
    return reachable(member, caller, policy, audience)


def test_reviewer_env_export_alternative_is_unreachable(policy):
    cmd = ReadyCommand(text="export COORDINATOR_ALLOW_X=1 && git status")
    assert _grade(cmd, REVIEWER, policy) is Verdict.UNREACHABLE
    assert _grade(ReadyCommand(text="export FOO=1"), REVIEWER, policy) is Verdict.UNREACHABLE


def test_reviewer_allowlisted_command_is_reachable(policy):
    assert _grade(ReadyCommand(text="git status"), REVIEWER, policy) is Verdict.REACHABLE
    assert _grade(AdaptableOffer("ls -la src", "dir"), REVIEWER, policy) is Verdict.REACHABLE


def test_reviewer_non_allowlisted_command_is_unreachable(policy):
    assert _grade(ReadyCommand(text="rm -rf build"), REVIEWER, policy) is Verdict.UNREACHABLE


def test_unresolved_subagent_fails_closed_for_argv_members(policy):
    assert _grade(ReadyCommand(text="git status"), None, policy) is Verdict.UNREACHABLE
    assert _grade(AdaptableOffer("git status", "x"), None, policy) is Verdict.UNREACHABLE


def test_unresolved_subagent_keeps_escalation_route(policy):
    route = EscalationRoute("what", "authorize")
    assert _grade(route, None, policy) is Verdict.REACHABLE


def test_em_with_none_caller_class_reaches_every_non_override(policy):
    em = Audience.EM
    assert _grade(ReadyCommand(text="rm -rf build"), None, policy, em) is Verdict.REACHABLE
    assert _grade(EscalationRoute("a", "b"), None, policy, em) is Verdict.REACHABLE


def test_override_is_pre_launch_for_em_and_unreachable_for_subagent(policy):
    ov = Override("COORDINATOR_ALLOW_X")
    assert _grade(ov, None, policy, Audience.EM) is Verdict.PRE_LAUNCH_ONLY
    assert _grade(ov, EXECUTOR, policy) is Verdict.UNREACHABLE
    assert _grade(ov, REVIEWER, policy) is Verdict.UNREACHABLE


def test_unconfined_subagent_class_reaches_argv_members(policy, monkeypatch):
    from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as guard

    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda _t: False)
    assert _grade(ReadyCommand(text="rm -rf build"), EXECUTOR, policy) is Verdict.REACHABLE


def test_capability_ref_graded_against_manifest(policy):
    ref = CapabilityRef("guard_inprocess_search::in-process")
    assert _grade(ref, REVIEWER, policy) is Verdict.REACHABLE
    assert _grade(ref, None, policy) is Verdict.REACHABLE
