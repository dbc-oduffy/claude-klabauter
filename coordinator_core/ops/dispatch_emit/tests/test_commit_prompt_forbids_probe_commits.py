"""Pins F1: the commit-phase brief forbids probe commits and requires
enumerating every refused path before choosing a reconciliation case.

Incident: `2c0684479`, `63a3bf5fd` -- a wave commit agent landed two real
commits on a shared branch while testing which path the guard disliked, and
a truncated denial (which names only its first refused path) was read as
the whole refused set."""

from coordinator_core.ops.dispatch_emit.emit import _commit_agent_call


def test_commit_prompt_forbids_probing_a_path_alone_to_test_the_guard():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], results_var="wave1Results"
    )
    assert "NEVER commit a" in call
    assert "on its own to test whether the guard" in call
    assert "cannot be rewritten" in call


def test_commit_prompt_requires_who_claims_path_on_every_refused_path():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], results_var="wave1Results"
    )
    assert "naming only its FIRST refused path" in call
    assert "on EVERY refused path" in call
    assert "not only the one printed" in call


def test_commit_prompt_orphan_set_names_every_orphan_and_stops():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], results_var="wave1Results"
    )
    assert "naming EVERY" in call
    assert "Stop there -- never retry or probe further." in call
