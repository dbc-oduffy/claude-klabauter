"""dispatch.land_run at default arguments: a halting run's own stale claim on its run token never makes
the halt commit refuse ``run-token-in-flight``; a claim a live peer thread holds still does."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, land_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT, record_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_MINT = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
RUN = f"ask-{_MINT}-c1c1c1"
SCRIPT = "run.mjs"
HALT = {"halted": "ledger-check", "reason": "row R1 open"}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "seed.md", "seed\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _halt_marker(root: Path) -> None:
    halted_run.write_halt_marker(record_dir(root, RUN), halted=HALT["halted"], sizing_rel=None)


def _land(root: Path, token: str, **extra) -> dict:
    params = {"script_path": SCRIPT, "run_id": RUN, "run_token": token, **extra}
    return land_run._handler(params, repo_root=root / ".git")


def _product(root: Path, token: str) -> dict:
    return _land(root, token, incomplete_chunks=[])


def _claimed(root: Path) -> bool:
    return run_token._record_path(root, RUN, claimed=True).exists()


def test_halt_commit_adopts_the_halting_runs_own_stale_claim_and_leaves_no_claim(repo):
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN}/ask.md", "ask\n")
    token = run_token.mint(repo, RUN, SCRIPT)
    assert run_token.redeem(repo, RUN, token, SCRIPT)  # an earlier call of this thread ended unsettled

    out = _land(repo, token, **{HALTED_RECORD_PARAM: HALT})

    assert out.get("refused") != "run-token-in-flight", out
    assert out["committed"] is True, out
    assert not _claimed(repo)
    assert run_token.is_live(repo, RUN)


def test_product_halt_commit_is_not_refused_in_flight_by_its_own_stale_claim(repo):
    _halt_marker(repo)
    token = run_token.mint(repo, RUN, SCRIPT)
    assert run_token.redeem(repo, RUN, token, SCRIPT)

    out = _product(repo, token)

    assert out.get("refused") not in ("run-token-in-flight", "run-token"), out
    assert not _claimed(repo)


def test_a_halted_runs_claim_held_by_a_live_peer_thread_is_still_refused_in_flight(repo):
    _halt_marker(repo)
    token = run_token.mint(repo, RUN, SCRIPT)
    held, done = threading.Event(), threading.Event()

    def peer():
        assert run_token.redeem(repo, RUN, token, SCRIPT)
        held.set()
        done.wait(10)

    worker = threading.Thread(target=peer)
    worker.start()
    try:
        assert held.wait(10)
        out = _product(repo, token)
    finally:
        done.set()
        worker.join()

    assert out["refused"] == "run-token-in-flight", out
    assert _claimed(repo)


def test_a_run_that_is_not_halted_keeps_the_in_flight_refusal_for_a_same_thread_claim(repo):
    token = run_token.mint(repo, RUN, SCRIPT)
    assert run_token.redeem(repo, RUN, token, SCRIPT)

    reply = land_run._redeem_or_answer(repo, RUN, token, SCRIPT, {})

    assert reply["refused"] == "run-token-in-flight"
    assert _claimed(repo)


def test_release_own_ended_claim_refuses_another_threads_claim_and_a_wrong_token(repo):
    token = run_token.mint(repo, RUN, SCRIPT)
    assert run_token.redeem(repo, RUN, token, SCRIPT)

    assert run_token.release_own_ended_claim(repo, RUN, "wrong-token", SCRIPT) is False
    assert _claimed(repo)
    assert run_token.release_own_ended_claim(repo, RUN, token, SCRIPT) is True
    assert not _claimed(repo) and run_token.is_live(repo, RUN)
