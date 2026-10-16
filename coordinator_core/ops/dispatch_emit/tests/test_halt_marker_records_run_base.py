"""The halt commit writes the run's base into the committed halted.json."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import HALT_RUN_BASE_FIELD, HALTED_FILE
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-10-16-x-slice-1-20261016T000000-aaaaaa"
SIZING = "state/sizings/2026-10-16-x-slice-1.yaml"
PARAM_BASE = "a" * 40
SUBMISSION_BASE = "b" * 40


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


def _halt(repo: Path, **extra) -> dict:
    out = terminal_commit._handler(
        {
            "script_path": "run.mjs",
            "run_id": RUN,
            HALTED_RECORD_PARAM: {"halted": "criterion_not_met", "reason": "r"},
            **extra,
        },
        repo_root=repo / ".git",
    )
    assert out["committed"] is True, out
    return json.loads(_git(["show", f"HEAD:{RECORD_DIR_ROOT}/{RUN}/{HALTED_FILE}"], repo))


def test_param_base_is_carried(repo):
    assert _halt(repo, run_base_sha=PARAM_BASE)[HALT_RUN_BASE_FIELD] == PARAM_BASE


def test_submission_base_is_carried_without_a_param(repo):
    scratch = repo / "scratch" / "warp" / RUN
    scratch.mkdir(parents=True)
    (scratch / "submission.json").write_text(
        json.dumps({"run_base_sha": SUBMISSION_BASE}), encoding="utf-8"
    )

    assert _halt(repo)[HALT_RUN_BASE_FIELD] == SUBMISSION_BASE


def test_no_known_base_leaves_the_key_absent(repo):
    assert HALT_RUN_BASE_FIELD not in _halt(repo)
