"""Each halt composer stamps ``Checkpoint-Run-Halted`` for the halted run's base, never ``Checkpoint-Run-Closed``."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import abandoned_runs as ar
from coordinator_core.ops.dispatch_emit import land_run_halt, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261015T000000-aaaaaa"
REAP_A = "ask-20261001T000000-aaaaaa"
REAP_B = "ask-20261001T000001-bbbbbb"
BASE = "a" * 40
BASE_A = "b" * 40
BASE_B = "c" * 40
HALT = {"halted": "ledger-check", "reason": "row R1 open"}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _record(root: Path, run_id: str, head: str, age: float = 0.0) -> None:
    rec = root / RECORD_DIR_ROOT / run_id
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    (rec / "run.json").write_text("{}", encoding="utf-8")
    (rec / "manifest.json").write_text(json.dumps({"head_sha": head}), encoding="utf-8")
    if age:
        stamp = time.time() - age
        for f in rec.iterdir():
            os.utime(f, (stamp, stamp))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _message(root: Path) -> str:
    return _git(root, "log", "-1", "--format=%B")


def _assert_halted_only(message: str, *bases: str) -> None:
    for b in bases:
        assert f"Checkpoint-Run-Halted: {b}" in message
    assert message.count("Checkpoint-Run-Halted:") == len(bases)
    assert "Checkpoint-Run-Closed" not in message


def test_halted_record_commit_stamps_run_halted(repo):
    _record(repo, RUN, BASE)
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: HALT}, repo_root=repo / ".git"
    )
    assert out["committed"] is True, out
    _assert_halted_only(_message(repo), BASE)


def test_land_run_halt_stamps_run_halted(repo):
    _record(repo, RUN, BASE)
    out = land_run_halt.land(repo, repo, {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: HALT})
    assert out["committed"] is True, out
    _assert_halted_only(_message(repo), BASE)


def test_run_without_a_resolvable_base_stamps_nothing(repo):
    rec = repo / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    out = land_run_halt.land(repo, repo, {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: HALT})
    assert out["committed"] is True, out
    assert "Checkpoint-Run-" not in _message(repo)


def test_reaper_commit_carries_both_runs_bases(repo):
    old = run_token.TTL_SECONDS + 3600
    _record(repo, REAP_A, BASE_A, age=old)
    _record(repo, REAP_B, BASE_B, age=old)
    out = ar._handler({}, repo_root=repo / ".git")
    assert out["ok"] is True and out["reaped"] == [REAP_A, REAP_B], out
    message = _message(repo)
    assert "Halted-By: abandoned" in message
    _assert_halted_only(message, BASE_A, BASE_B)
