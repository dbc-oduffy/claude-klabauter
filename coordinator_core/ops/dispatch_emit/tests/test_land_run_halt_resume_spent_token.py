"""A resumed pass's halt_commit lands after the first pass spent the run token."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run_halt, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_MINT = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
RUN = f"ask-{_MINT}-aaaaaa"
LEDGER = {"halted": "ledger-check", "reason": "row R1 open"}


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
    _write(tmp_path, "docs/plans/peer.md", "peer\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/run.json", "run\n")
    _write(tmp_path, "run.mjs", "// script\n")
    return tmp_path


def _halt(root: Path, token: str) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: LEDGER, "run_token": token}
    return land_run_halt.land(root, root, params)


def _first_pass_spends(root: Path, token: str, landed: dict | None) -> None:
    assert run_token.redeem(root, RUN, token, "run.mjs")
    run_token.consume(root, RUN, landed=landed)
    assert not run_token.admits(root, RUN, token, "run.mjs")


@pytest.mark.parametrize("landed", [None, {"sha": "0" * 40, "rows": ["C1"], "paths": ["prod.py"]}])
def test_resume_halt_commit_lands_after_first_pass_spent_the_token(repo, landed):
    token = run_token.mint(repo, RUN, "run.mjs")
    _first_pass_spends(repo, token, landed)

    out = _halt(repo, token)

    assert out.get("refused") != "run-token", out
    assert out["committed"] is True, out


def test_a_token_never_minted_for_the_run_is_still_refused(repo):
    token = run_token.mint(repo, RUN, "run.mjs")
    _first_pass_spends(repo, token, None)

    out = _halt(repo, "not-the-token")

    assert out.get("refused") == "run-token", out
