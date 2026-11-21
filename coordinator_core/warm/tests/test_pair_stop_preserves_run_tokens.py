"""A warm-pair stop leaves run tokens redeemable: the records live in the git common dir, not in warm memory."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.warm import pair_stop

_SCRIPT = "scratch/warp/example/run.js"


def _tmp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_live_token_still_admits_after_stop_warm_pair(tmp_path):
    repo = _tmp_repo(tmp_path)
    run_id = run_token.new_ask_run_id()
    token = run_token.mint(repo, run_id, _SCRIPT)
    assert run_token.admits(repo, run_id, token, _SCRIPT)
    assert run_token.is_live(repo, run_id)

    engine_root = tmp_path / "engine"
    engine_root.mkdir()
    result = pair_stop.stop_warm_pair(
        engine_root,
        ask_server=lambda pipe: True,
        ask_front_door=lambda crumb: True,
    )

    assert result.ok()
    assert run_token.is_live(repo, run_id)
    assert run_token.admits(repo, run_id, token, _SCRIPT)
    assert not run_token.admits(repo, run_id, "wrong-token", _SCRIPT)


def test_stop_in_the_repo_itself_keeps_token_redeemable(tmp_path):
    repo = _tmp_repo(tmp_path)
    run_id = run_token.new_ask_run_id()
    token = run_token.mint(repo, run_id, _SCRIPT)

    result = pair_stop.stop_warm_pair(repo, ask_server=lambda pipe: True, ask_front_door=lambda crumb: True)

    assert result.ok()
    assert run_token.redeem(repo, run_id, token, _SCRIPT)
