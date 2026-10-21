"""A `halted_by` terminal_commit of kind `parked` lands the record dir, the run's plan and sizing, and stays resumable."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PARKED, HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261021T000000-aaaaaa"
SIZING = "state/sizings/2026-10-21-parked-run.yaml"
PLAN = "docs/plans/2026-10-21-parked-run.md"
REASON = "parked after plan: workflow.revise stop_after=plan"


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
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    for name in ("ask.md", "pm-intent.md"):
        (rec / name).write_text(f"{name}\n", encoding="utf-8")
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING}) + "\n", encoding="utf-8")
    (root / "state" / "sizings").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / SIZING).write_text("a: 1\n", encoding="utf-8")
    (root / PLAN).write_text("plan\n", encoding="utf-8")
    return root


def _parked(root: Path) -> dict:
    halted = {"halted": HALT_PARKED, "reason": REASON, "plan": PLAN, "sizing": SIZING, "run_id": RUN}
    return terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: halted}, repo_root=root / ".git"
    )


def test_parked_halt_lands_record_dir_plan_and_sizing(repo):
    out = _parked(repo)

    assert out["committed"] is True, out
    names = set(_git(["show", "--name-only", "--format=", out["sha"]], repo).split())
    assert {f"{RECORD_DIR_ROOT}/{RUN}/{n}" for n in ("ask.md", "pm-intent.md", "run.json", "halted.json")} <= names
    assert {PLAN, SIZING} <= names
    assert "Halted-By: parked" in _git(["log", "-1", "--format=%B"], repo)
    assert _git(["status", "--porcelain", "-uall"], repo).strip() == "?? run.mjs"


def test_parked_marker_reads_halted_and_a_refire_resumes_it(repo):
    assert _parked(repo)["committed"] is True

    marker = json.loads((repo / RECORD_DIR_ROOT / RUN / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
    assert marker["status"] == halted_run.STATUS_HALTED
    assert marker["halted"] == HALT_PARKED
    assert halted_run.halted_ask_run_for(repo, SIZING) == RUN
