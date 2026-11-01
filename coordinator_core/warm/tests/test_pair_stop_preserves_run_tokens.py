"""A warm-pair stop leaves run tokens redeemable: the records live in the git common dir, not in warm memory."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.session.core import _win_create_time_epoch
from coordinator_core.warm import breadcrumb, pair_stop

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


def test_stop_that_terminates_a_real_server_keeps_token_redeemable(tmp_path):
    """The stop reaches a live recorded server (the ask lands on it), and the tokens survive it."""
    repo = _tmp_repo(tmp_path)
    run_id = run_token.new_ask_run_id()
    token = run_token.mint(repo, run_id, _SCRIPT)

    engine_root = tmp_path / "engine"
    engine_root.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        breadcrumb.write_breadcrumb(
            pipe="unused-endpoint",
            pid=child.pid,
            stable_pid_start_epoch=_win_create_time_epoch(child.pid) or 0,
            engine_sha="tokens-falsifier",
            engine_root=engine_root,
        )
        assert child.poll() is None

        asked: list[str] = []

        def _server_exits_on_ask(pipe: str) -> bool:
            asked.append(pipe)
            child.kill()
            child.wait(timeout=30)
            return True

        result = pair_stop.stop_warm_pair(
            engine_root,
            ask_server=_server_exits_on_ask,
            ask_front_door=lambda crumb: True,
        )

        assert asked == ["unused-endpoint"], "the stop never reached the recorded server"
        assert result.ok()
        assert run_token.is_live(repo, run_id)
        assert run_token.redeem(repo, run_id, token, _SCRIPT)
    finally:
        if child.poll() is None:
            child.kill()
        breadcrumb.unlink_breadcrumb(engine_root)
