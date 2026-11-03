"""An over-budget land_run call's run token claim is adopted by the next call, and the abandoned
call's late settle is a no-op: a second land is not refused `run-token-in-flight`."""
from __future__ import annotations

import time

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token

RUN_ID = "ask-1"
SCRIPT = "scratch/warp/ask-1.workflow.mjs"
BUDGET = 30.0


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.setattr(land_run, "_claim_budget_secs", lambda: BUDGET)
    return tmp_path


def _claim(repo, token, state):
    assert land_run._redeem_or_answer(repo, RUN_ID, token, SCRIPT, state) is None
    state["claim"] = (repo, RUN_ID, token, SCRIPT)


def test_a_second_land_inside_the_budget_is_refused_in_flight(repo):
    token = run_token.mint(repo, RUN_ID, SCRIPT)
    _claim(repo, token, {})
    reply = land_run._redeem_or_answer(repo, RUN_ID, token, SCRIPT, {})
    assert reply["refused"] == "run-token-in-flight"


def test_a_second_land_past_the_budget_adopts_and_the_late_settle_is_a_noop(repo, monkeypatch):
    token = run_token.mint(repo, RUN_ID, SCRIPT)
    first: dict = {}
    _claim(repo, token, first)
    real = time.time()
    monkeypatch.setattr(run_token.time, "time", lambda: real + BUDGET + 1)
    second: dict = {}
    _claim(repo, token, second)  # adopts: no run-token-in-flight
    assert land_run._claim_adopted(first["identity"], first["claim"])
    assert not land_run._claim_adopted(second["identity"], second["claim"])

    land_run._settle_token(first, landed=False)  # the abandoned call ends late
    assert run_token.claim_pending(repo, RUN_ID, token, SCRIPT) is True  # the adopter still holds it
    assert not run_token.admits(repo, RUN_ID, token, SCRIPT)  # not released back to live

    land_run._settle_token(second, landed=False)
    assert run_token.admits(repo, RUN_ID, token, SCRIPT)  # the holder's own settle still releases
