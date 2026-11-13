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
