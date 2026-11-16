"""Composed land_run commit commands pin the run's own repo with a literal `--repo <root>`."""

from __future__ import annotations

import json

import pytest

from coordinator_core.op_scopes import WORKTREE_SCOPED_OPS
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.land_run_contract import OP_NAME
from coordinator_core.ops.dispatch_emit.ask_compose import _commit_stage_call, _halt_record_commit_fn

_ROOT = "/mnt/other-repo"
_COMMAND = (
    f"coordinator-invoke {OP_NAME} --params-file {_ROOT}/scratch/warp/run-1/terminal-commit-params.json "
    f"--repo {_ROOT}"
)


@pytest.fixture(autouse=True)
def _writer_bound(monkeypatch):
    monkeypatch.setattr(ask_compose, "params_writer_bound", lambda *_a, **_k: True)


def test_commit_stage_command_carries_repo_after_params_file():
    call = _commit_stage_call(head="h", anchor="a", repo_root=_ROOT, run_id="run-1")
    assert call.count(_COMMAND) == 1


def test_halt_record_command_carries_repo_after_params_file():
    fn = _halt_record_commit_fn(head="h", anchor="a", script_path=None, repo_root=_ROOT, run_id="run-1")
    assert fn.count(_COMMAND) == 1


def test_params_file_substring_still_matches():
    call = _commit_stage_call(head="h", anchor="a", repo_root=_ROOT, run_id="run-1")
    assert f"--params-file {_ROOT}/scratch/warp/run-1/terminal-commit-params.json" in call


def test_commit_prompts_hold_one_invoke_and_no_quoted_json_argument():
    call = _commit_stage_call(head="h", anchor="a", repo_root=_ROOT, run_id="run-1")
    fn = _halt_record_commit_fn(head="h", anchor="a", script_path=None, repo_root=_ROOT, run_id="run-1")
    for text in (call, fn):
        assert text.count("coordinator-invoke dispatch.") == 1
        assert "'{" not in text
        assert "already written to" in text


def test_unbound_params_writer_refuses_at_compose_time(monkeypatch):
    monkeypatch.setattr(ask_compose, "params_writer_bound", lambda *_a, **_k: False)
    with pytest.raises(ask_compose.AskComposeRefused):
        _commit_stage_call(head="h", anchor="a", repo_root=_ROOT, run_id="run-1")
    with pytest.raises(ask_compose.AskComposeRefused):
        _halt_record_commit_fn(head="h", anchor="a", script_path=None, repo_root=_ROOT, run_id="run-1")


def test_hostile_params_round_trip_through_the_params_file(tmp_path):
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import materialize

    params = {"run_id": "run-1", "note": "it's \"quoted\"\nline two 'x' {y}"}
    target = tmp_path / "terminal-commit-params.json"
    assert materialize(target, params)
    assert json.loads(target.read_text(encoding="utf-8"))["note"] == params["note"]


def test_land_run_is_worktree_scoped_so_repo_resolves_the_origin():
    assert OP_NAME in WORKTREE_SCOPED_OPS
    assert ask_compose.LAND_RUN_OP == OP_NAME
