"""A queue-grind run's in-run terminal commit redeems the token its own emit minted.

Mints the way the queue-grind emit CLI does (``run_token.mint(repo, run_stamp, script_path,
kind="grind")``), builds the terminal params exactly as the composed script's ``_terminalCommit`` does
(``script_path``, ``run_id``, ``run_token``, ``grind: {profile, rows, halted}``) and fires them through
``dispatch.land_run`` and ``dispatch.terminal_commit``, the two entries a terminal commit reaches.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token, terminal_commit
from coordinator_core.testing.git_write import write_seen_by_git
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "20261113T030303Z"
SCRIPT = "scratch/warp/queue-grind-lessons.workflow.mjs"
PROFILE = "lessons"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "lesson.md").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    write_seen_by_git(root / "lesson.md", "x = 2\n")
    return root


def _terminal_params(token: str) -> dict:
    row = {"row_id": "r1", "outcome": "settled", "paths": ["lesson.md"], "deleted_paths": []}
    return {
        "script_path": SCRIPT, "run_id": RUN, "run_token": token,
        "grind": {"profile": PROFILE, "rows": [row], "halted": None},
    }


def test_land_run_redeems_the_token_the_queue_grind_emit_minted(repo):
    token = run_token.mint(repo, RUN, SCRIPT, kind="grind")
    reply = land_run._dispatch(_terminal_params(token), repo / ".git")
    assert reply.get("refused") != "run-token", (
        f"run-token refused: minted run_id={RUN!r} script_path={SCRIPT!r}; fired the same "
        f"run_id/script_path, so the divergence is the token or the record home: {reply}"
    )
    assert reply["committed"] is True, reply
    assert reply.get("refused") in (None, "")


def test_terminal_commit_redeems_the_token_the_queue_grind_emit_minted(repo):
    token = run_token.mint(repo, RUN, SCRIPT, kind="grind")
    reply = terminal_commit._terminal_commit_inner(
        _terminal_params(token), repo / ".git", {}, {}, None, None, None, None, {}
    )
    assert reply.get("refused") != "run-token", reply
    assert reply["committed"] is True, reply


def test_a_second_landing_on_the_spent_token_lands_nothing_new(repo):
    token = run_token.mint(repo, RUN, SCRIPT, kind="grind")
    first = land_run._dispatch(_terminal_params(token), repo / ".git")
    assert first["committed"] is True, first
    again = land_run._dispatch(_terminal_params(token), repo / ".git")
    assert again["committed"] is True
    assert again.get("sha") == first["sha"]


def test_a_forged_token_is_still_refused(repo):
    run_token.mint(repo, RUN, SCRIPT, kind="grind")
    reply = land_run._dispatch(_terminal_params("0" * 32), repo / ".git")
    assert reply["committed"] is False
    assert reply["refused"] == "run-token"


@pytest.fixture
def consumer(tmp_path):
    """A consumer repo distinct from the engine root, with its params under scratch/."""
    root = tmp_path / "consumer"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "lesson.md").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    write_seen_by_git(root / "lesson.md", "x = 2\n")
    (root / "scratch").mkdir()
    return root


@pytest.fixture
def engine(tmp_path):
    root = tmp_path / "engine"
    root.mkdir()
    _git(["init", "-q"], root)
    return root


def _absolute_params(consumer: Path, token: str, script: str) -> dict:
    params = _terminal_params(token)
    params["script_path"] = script
    return params


def test_a_call_routed_to_another_checkout_redeems_in_the_minting_consumer_repo(consumer, engine):
    script = str(consumer / "scratch" / "lessons.workflow.mjs")
    token = run_token.mint(consumer, RUN, script, kind="grind")
    reply = land_run._dispatch(_absolute_params(consumer, token, script), engine / ".git")
    assert reply.get("refused") != "run-token", reply
    assert reply["committed"] is True, reply
    assert _git(["log", "-1", "--format=%s"], consumer).startswith("grind(lessons)")


def test_the_routed_call_lands_nothing_in_the_engine_checkout(consumer, engine):
    script = str(consumer / "scratch" / "lessons.workflow.mjs")
    token = run_token.mint(consumer, RUN, script, kind="grind")
    land_run._dispatch(_absolute_params(consumer, token, script), engine / ".git")
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=str(engine), capture_output=True, **no_console_creationflags()
    )
    assert head.returncode != 0


def test_a_routed_call_with_a_wrong_token_is_refused(consumer, engine):
    script = str(consumer / "scratch" / "lessons.workflow.mjs")
    run_token.mint(consumer, RUN, script, kind="grind")
    reply = land_run._dispatch(_absolute_params(consumer, "0" * 32, script), engine / ".git")
    assert reply["committed"] is False
    assert reply["refused"] == "run-token"
    assert run_token.is_live(consumer, RUN)


def test_a_routed_call_naming_a_different_script_is_refused(consumer, engine):
    script = str(consumer / "scratch" / "lessons.workflow.mjs")
    token = run_token.mint(consumer, RUN, script, kind="grind")
    other = str(consumer / "scratch" / "other.workflow.mjs")
    reply = land_run._dispatch(_absolute_params(consumer, token, other), engine / ".git")
    assert reply["committed"] is False
    assert reply["refused"] == "run-token"
    assert run_token.is_live(consumer, RUN)


def test_a_routed_repeat_call_is_answered_from_the_spent_receipt(consumer, engine):
    script = str(consumer / "scratch" / "lessons.workflow.mjs")
    token = run_token.mint(consumer, RUN, script, kind="grind")
    first = land_run._dispatch(_absolute_params(consumer, token, script), engine / ".git")
    again = land_run._dispatch(_absolute_params(consumer, token, script), engine / ".git")
    assert first["committed"] is True, first
    assert again["committed"] is True
    assert again["sha"] == first["sha"]


def test_a_relative_and_an_absolute_spelling_of_the_script_both_redeem(consumer):
    script = str(consumer / "scratch" / "lessons.workflow.mjs")
    token = run_token.mint(consumer, RUN, script, kind="grind")
    reply = land_run._dispatch(_absolute_params(consumer, token, "scratch/lessons.workflow.mjs"), consumer / ".git")
    assert reply["committed"] is True, reply
