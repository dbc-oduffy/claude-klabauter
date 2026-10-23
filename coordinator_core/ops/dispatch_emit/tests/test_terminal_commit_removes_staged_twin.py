"""A redeemed run token deletes the run's staged twins; a refused redeem leaves them."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.win_portability import no_console_creationflags

SHA = "a" * 64
SCRIPT = "scratch/warp/run1.workflow.mjs"


@pytest.fixture
def repo(tmp_path):
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, **no_console_creationflags()
    )
    d = tmp_path / "scratch" / "warp"
    d.mkdir(parents=True)
    (d / "run1.workflow.mjs").write_text("first")
    (d / f"run1.staged-{SHA}.workflow.mjs").write_text("twin")
    (d / f"run2.staged-{SHA}.workflow.mjs").write_text("other")
    return tmp_path


def _twin(repo, run="run1"):
    return repo / "scratch" / "warp" / f"{run}.staged-{SHA}.workflow.mjs"


def test_successful_redeem_leaves_no_twin(repo):
    token = run_token.mint(repo, "run1", SCRIPT)
    assert terminal_commit._redeem_or_answer(repo, "run1", token, SCRIPT) is None
    assert not _twin(repo).exists()
    assert _twin(repo, "run2").exists()
    assert (repo / SCRIPT).exists()


def test_refused_redeem_leaves_the_twin(repo):
    run_token.mint(repo, "run1", SCRIPT)
    reply = terminal_commit._redeem_or_answer(repo, "run1", "0" * 32, SCRIPT)
    assert reply is not None
    assert _twin(repo).exists()


def test_removal_failure_never_fails_the_redeem(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import launcher_staged_twin

    def boom(*a, **k):
        raise OSError("denied")

    monkeypatch.setattr(launcher_staged_twin, "remove_twins", boom)
    token = run_token.mint(repo, "run1", SCRIPT)
    assert terminal_commit._redeem_or_answer(repo, "run1", token, SCRIPT) is None
