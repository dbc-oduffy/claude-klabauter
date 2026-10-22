"""Composed land_run commit commands pin the run's own repo with a literal `--repo <root>`."""

from __future__ import annotations

from coordinator_core.op_scopes import WORKTREE_SCOPED_OPS
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.land_run_contract import OP_NAME
from coordinator_core.ops.dispatch_emit.ask_compose import _commit_stage_call, _halt_record_commit_fn

_ROOT = "/mnt/other-repo"
_COMMAND = (
    f"coordinator-invoke {OP_NAME} --params-file {_ROOT}/scratch/warp/run-1/terminal-commit-params.json "
    f"--repo {_ROOT}"
)


def test_commit_stage_command_carries_repo_after_params_file():
    call = _commit_stage_call(head="h", anchor="a", repo_root=_ROOT, run_id="run-1")
    assert call.count(_COMMAND) == 1


def test_halt_record_command_carries_repo_after_params_file():
    fn = _halt_record_commit_fn(head="h", anchor="a", script_path=None, repo_root=_ROOT, run_id="run-1")
    assert fn.count(_COMMAND) == 1


def test_params_file_substring_still_matches():
    call = _commit_stage_call(head="h", anchor="a", repo_root=_ROOT, run_id="run-1")
    assert f"--params-file {_ROOT}/scratch/warp/run-1/terminal-commit-params.json" in call


def test_land_run_is_worktree_scoped_so_repo_resolves_the_origin():
    assert OP_NAME in WORKTREE_SCOPED_OPS
    assert ask_compose.LAND_RUN_OP == OP_NAME
