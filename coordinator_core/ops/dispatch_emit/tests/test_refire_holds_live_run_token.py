"""A run reaching its commit stage after a resume or refire holds a live run token for that commit.

Diagnosis (reproduced through `op._dispatch_emit`): a fresh refire id, a rebound halted id and a
halted call's claim-and-release all leave a redeemable token. The state that kills a token is a
SECOND emit of a run id that is already minted and running (a deferred or resume launcher script
re-running its emit leg, or a resume submitted onto the same id): the ask arm's `mint` retired the
record the running script's token digest names, so its commit stage was refused as "does not match a
live, unspent token". The ask mint site now passes `carry_prior=True`: the replaced digests stay
admitted on the new record (one record, so still one landed commit), and a spent receipt answers a
repeat call from either token. A plain `mint` still retires every earlier token.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import op, run_token
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    ASK,
    REL,
    WARP,
    _halt,
    composed,
    repo,
)

LANDED = {"sha": "d" * 40, "rows": ["C1"], "paths": []}


def _emit(repo, *, launcher_child=False, **extra):
    params = {"ask": True, "sizing_path": REL, **extra}
    if launcher_child:
        params["launcher_child"] = True
    reply = op._dispatch_emit(params, repo_root=repo)
    script = f"scratch/warp/{reply['run_id']}.workflow.mjs"
    token = reply["fire_args"].get("runToken") or run_token.escrowed(repo, reply["run_id"])
    return reply, script, token


def _halted_call(repo, run_id, token, script):
    """A halted run's record-only landing: claim the token, then release it back to live."""
    assert run_token.redeem(repo, run_id, token, script)
    assert run_token.release(repo, run_id, token, script)


@pytest.mark.parametrize("launcher_child", [False, True])
def test_rebound_resume_after_a_halted_call_holds_a_live_token(repo, composed, launcher_child):
    _halt(repo, WARP, "a" * 40)
    first, script, token = _emit(repo, launcher_child=launcher_child)
    assert first["run_id"] == WARP
    _halted_call(repo, WARP, token, script)
    _halt(repo, WARP, "a" * 40)
    again, script2, token2 = _emit(repo, launcher_child=launcher_child)
    assert again["run_id"] == WARP and token2 and token2 != token
    assert run_token.redeem(repo, WARP, token2, script2)


@pytest.mark.parametrize("launcher_child", [False, True])
def test_refire_onto_a_fresh_run_holds_a_live_token(repo, composed, launcher_child):
    _halt(repo, ASK, "a" * 40)
    reply, script, token = _emit(repo, launcher_child=launcher_child)
    assert reply["run_id"] != ASK
    assert token and run_token.admits(repo, reply["run_id"], token, script)
    assert run_token.redeem(repo, reply["run_id"], token, script)


@pytest.mark.parametrize("launcher_child", [False, True])
def test_a_second_emit_of_a_running_id_keeps_the_first_token_live(repo, composed, launcher_child):
    _halt(repo, WARP, "a" * 40)
    first, script, token1 = _emit(repo, launcher_child=launcher_child)
    assert first["run_id"] == WARP
    _halt(repo, WARP, "a" * 40)
    second, _script, token2 = _emit(repo, launcher_child=launcher_child)
    assert second["run_id"] == WARP and token1 != token2
    assert run_token.redeem(repo, WARP, token1, script)


def test_the_newer_token_also_redeems_after_a_second_emit(repo, composed):
    _halt(repo, WARP, "a" * 40)
    _first, script, token1 = _emit(repo)
    _halt(repo, WARP, "a" * 40)
    _second, _script, token2 = _emit(repo)
    assert run_token.redeem(repo, WARP, token2, script)
    assert not run_token.redeem(repo, WARP, token1, script)


def test_one_commit_lands_for_two_live_tokens_and_both_answer_the_repeat_call(repo, composed):
    _halt(repo, WARP, "a" * 40)
    _first, script, token1 = _emit(repo)
    _halt(repo, WARP, "a" * 40)
    _second, _script, token2 = _emit(repo)
    assert run_token.redeem(repo, WARP, token1, script)
    run_token.consume(repo, WARP, landed=LANDED)
    assert not run_token.redeem(repo, WARP, token2, script)
    assert run_token.spent_receipt(repo, WARP, token1, script) == LANDED
    assert run_token.spent_receipt(repo, WARP, token2, script) == LANDED


def test_a_carried_token_does_not_admit_another_script(repo, composed):
    _halt(repo, WARP, "a" * 40)
    _first, script, token1 = _emit(repo)
    _halt(repo, WARP, "a" * 40)
    _emit(repo)
    assert not run_token.admits(repo, WARP, token1, "scratch/warp/other.workflow.mjs")


def test_a_plain_remint_still_retires_the_earlier_token(tmp_path):
    (tmp_path / ".git").mkdir()
    first = run_token.mint(tmp_path, "ask-1", "a.mjs")
    second = run_token.mint(tmp_path, "ask-1", "a.mjs")
    assert not run_token.admits(tmp_path, "ask-1", first, "a.mjs")
    assert run_token.admits(tmp_path, "ask-1", second, "a.mjs")


def test_carry_prior_across_a_different_script_retires_the_earlier_token(tmp_path):
    (tmp_path / ".git").mkdir()
    first = run_token.mint(tmp_path, "ask-1", "a.mjs")
    second = run_token.mint(tmp_path, "ask-1", "b.mjs", carry_prior=True)
    assert not run_token.admits(tmp_path, "ask-1", first, "a.mjs")
    assert not run_token.admits(tmp_path, "ask-1", first, "b.mjs")
    assert run_token.admits(tmp_path, "ask-1", second, "b.mjs")


def test_carried_digests_are_bounded(tmp_path):
    (tmp_path / ".git").mkdir()
    tokens = [run_token.mint(tmp_path, "ask-1", "a.mjs", carry_prior=True) for _ in range(run_token.MAX_PRIOR + 3)]
    assert run_token.admits(tmp_path, "ask-1", tokens[-1], "a.mjs")
    assert run_token.admits(tmp_path, "ask-1", tokens[-1 - run_token.MAX_PRIOR], "a.mjs")
    assert not run_token.admits(tmp_path, "ask-1", tokens[0], "a.mjs")
