"""commit_halted_record lands the halt marker in the record dir it commits."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-10-16-x-slice-1-20261016T000000-aaaaaa"
SIZING = "state/sizings/2026-10-16-x-slice-1.yaml"


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
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING}) + "\n", encoding="utf-8")
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    return root


def test_halt_marker_lands_in_head_tree(repo):
    out = terminal_commit._handler(
        {
            "script_path": "run.mjs",
            "run_id": RUN,
            HALTED_RECORD_PARAM: {"halted": "criterion_not_met", "reason": "r"},
        },
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    rel = f"{RECORD_DIR_ROOT}/{RUN}/{HALTED_FILE}"
    assert rel in _git(["show", "--name-only", "--format=", "HEAD"], repo).split()
    marker = json.loads(_git(["show", f"HEAD:{rel}"], repo))
    assert marker["status"] == "halted"
    assert marker["halted"] == "criterion_not_met"
    assert marker["sizing_rel"] == SIZING


def test_no_record_dir_writes_no_marker(repo):
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": "ask-20261015T000000-bbbbbb", HALTED_RECORD_PARAM: {"halted": "refusal"}},
        repo_root=repo / ".git",
    )

    assert out["nothing_to_commit"] is True
    assert not (repo / RECORD_DIR_ROOT / "ask-20261015T000000-bbbbbb").exists()
