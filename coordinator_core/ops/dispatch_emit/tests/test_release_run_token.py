"""run_token.discard and the dispatch.release_run_token op."""

from __future__ import annotations

import pytest

from coordinator_core import warp_live
from coordinator_core.ipc import get_op_handler
from coordinator_core.ops.dispatch_emit import release_run_token, run_token

SIZING = "state/sizings/s.yaml"


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _mint(repo, run_id="ask-1"):
    return run_token.mint(repo, run_id, "scratch/w.mjs", session_id="s1", sizing_rel=SIZING)


def test_discard_returns_record_and_removes_unclaimed_file(tmp_path):
    repo = _repo(tmp_path)
    _mint(repo)
    path = run_token._record_path(repo, "ask-1")
    record = run_token.discard(repo, "ask-1")
    assert record["session_id"] == "s1"
    assert not path.exists()
    assert run_token.discard(repo, "ask-1") is None


def test_discard_leaves_a_claimed_file(tmp_path):
    repo = _repo(tmp_path)
    token = _mint(repo)
    assert run_token.redeem(repo, "ask-1", token, "scratch/w.mjs")
    claimed = run_token._record_path(repo, "ask-1", claimed=True)
    assert run_token.discard(repo, "ask-1") is None
    assert claimed.exists()


def test_discard_malformed_id_returns_none(tmp_path):
    assert run_token.discard(_repo(tmp_path), "../x") is None


def test_handler_round_trip(tmp_path):
    repo = _repo(tmp_path)
    _mint(repo)
    handler = release_run_token._handler
    reply = handler({"run_id": "ask-1"}, repo_root=repo)
    assert reply["ok"] is True and reply["released"] is True
    assert set(reply["record"]) == {"session_id", "sizing_rel", "minted_at"}
    assert reply["record"]["sizing_rel"] == SIZING
    assert "sha256" not in reply["record"]
    assert not run_token.is_live(repo, "ask-1")
    assert warp_live.live_run_for_sizing(repo, SIZING) is None
    again = handler({"run_id": "ask-1"}, repo_root=repo)
    assert again["ok"] is True and again["released"] is False and again["record"] is None


@pytest.mark.parametrize("params", [{}, {"run_id": ""}, {"run_id": 7}, {"run_id": None}])
def test_handler_refuses_bad_run_id_and_removes_nothing(tmp_path, params):
    repo = _repo(tmp_path)
    _mint(repo)
    reply = release_run_token._handler(params, repo_root=repo)
    assert reply["ok"] is False and "run_id" in reply["error"]
    assert run_token.is_live(repo, "ask-1")


def test_handler_without_repo_root_errors():
    reply = release_run_token._handler({"run_id": "ask-1"}, repo_root=None)
    assert reply["ok"] is False and "repo_root" in reply["error"]


def test_handler_reachable_through_get_op_handler():
    assert get_op_handler(release_run_token.OP_NAME) is release_run_token._handler
