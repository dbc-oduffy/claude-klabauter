"""A halt commit lands the run's own record and never a stopped sibling run's untracked plan."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import stopped_runs, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261015T000000-aaaaaa"
PEER_RUN = "ask-20261015T000001-bbbbbb"
PEER_PLAN = "docs/plans/2026-10-21-peer-run-plan.md"
SELF = "5e1f0000-0000-4000-8000-0000000000b3"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
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
    for name in ("ask.md", "run.json"):
        (rec / name).write_text(f"{name}\n", encoding="utf-8")
    (root / PEER_PLAN).parent.mkdir(parents=True)
    (root / PEER_PLAN).write_text("peer plan\n", encoding="utf-8")
    monkeypatch.setattr(
        stopped_runs, "residue",
        lambda *a, **k: stopped_runs.StoppedResidue({PEER_RUN: (PEER_PLAN,)}, {}),
    )
    return root


def _committed_files(root: Path, sha: str) -> set:
    return set(_git(["show", "--name-only", "--format=", sha], root).split())


def test_control_commit_v2_default_sweeps_the_stopped_runs_plan(repo):
    out = commit_v2._handler(
        {"paths": [f"{RECORD_DIR_ROOT}/{RUN}/ask.md"], "message": "m", "session_id": SELF},
        repo_root=repo / ".git",
    )
    assert PEER_PLAN in _committed_files(repo, out["sha"])


def test_halt_commit_lands_own_record_and_leaves_the_peer_plan_untracked(repo):
    out = terminal_commit._handler(
        {
            "script_path": "run.mjs", "run_id": RUN, "session_id": SELF,
            HALTED_RECORD_PARAM: {"halted": "refusal", "reason": "r"},
        },
        repo_root=repo / ".git",
    )
    assert out["committed"] is True, out
    landed = _committed_files(repo, out["sha"])
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in landed
    assert PEER_PLAN not in landed
    assert _git(["status", "--porcelain", "--", PEER_PLAN], repo).startswith("??")
