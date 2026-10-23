"""A ledger-check halt committed into a repo other than the engine worktree carries ``Checkpoint-Run-Halted``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run_halt, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261023T041100-040d15"
MANIFEST_BASE = "0" * 6 + "a6d4259c" + "e" * 26
PARAM_BASE = "f" * 40
HALT = {"halted": "ledger-check", "reason": "row X1 open"}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def foreign(tmp_path):
    root = tmp_path / "foreign"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    (rec / "run.json").write_text("{}", encoding="utf-8")
    return root


def _manifest(root: Path) -> None:
    (root / RECORD_DIR_ROOT / RUN / "manifest.json").write_text(
        json.dumps({"head_sha": MANIFEST_BASE}), encoding="utf-8"
    )


def _message(root: Path) -> str:
    return _git(root, "log", "-1", "--format=%B")


def test_terminal_commit_ledger_halt_in_foreign_repo_stamps_manifest_base(foreign):
    _manifest(foreign)
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: HALT}, repo_root=foreign / ".git"
    )
    assert out["committed"] is True, out
    message = _message(foreign)
    assert "Halted-By: ledger-check" in message
    assert f"Checkpoint-Run-Halted: {MANIFEST_BASE}" in message
    assert "Checkpoint-Run-Closed" not in message


def test_land_run_halt_ledger_halt_in_foreign_repo_stamps_manifest_and_param_bases(foreign):
    _manifest(foreign)
    out = land_run_halt.land(
        foreign, foreign,
        {"script_path": "run.mjs", "run_id": RUN, "run_base_sha": PARAM_BASE, HALTED_RECORD_PARAM: HALT},
    )
    assert out["committed"] is True, out
    message = _message(foreign)
    assert f"Checkpoint-Run-Halted: {MANIFEST_BASE}" in message
    assert f"Checkpoint-Run-Halted: {PARAM_BASE}" in message


def test_ledger_halt_with_no_resolvable_base_stamps_nothing(foreign):
    out = land_run_halt.land(foreign, foreign, {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: HALT})
    assert out["committed"] is True, out
    assert "Checkpoint-Run-" not in _message(foreign)
