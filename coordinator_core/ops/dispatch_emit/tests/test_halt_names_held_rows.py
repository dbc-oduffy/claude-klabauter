"""halted.json carries the resume snapshot's held rows, from both halt writers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, land_run_halt, resume_snapshot, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261024T000001-bbbbbb"
PLAN = "docs/plans/p.md"
HALT = {"halted": "ledger-check", "reason": "row X1 open", "plan": PLAN}
HELD = [("X1", "performer em"), ("Y2", "claimed by a live peer")]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    plan = tmp_path / PLAN
    plan.parent.mkdir(parents=True)
    plan.write_text("plan\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    record = tmp_path / RECORD_DIR_ROOT / RUN
    record.mkdir(parents=True)
    (record / "run.json").write_text("{}\n", encoding="utf-8")
    return tmp_path


def _terminal(root: Path, params: dict) -> dict:
    return terminal_commit._handler(params, repo_root=root / ".git")


def _land_run(root: Path, params: dict) -> dict:
    return land_run_halt.land(root, root, params)


WRITERS = pytest.mark.parametrize("call", [_terminal, _land_run], ids=["halted_record_commit", "land_run_halt"])


def _marker(root: Path) -> dict:
    return json.loads((root / RECORD_DIR_ROOT / RUN / halted_run.HALTED_FILE).read_text(encoding="utf-8"))


@WRITERS
def test_held_rows_equal_the_snapshots_held(repo, call):
    resume_snapshot.write(repo, RUN, PLAN, [], [], held=HELD)

    out = call(repo, {"run_id": RUN, HALTED_RECORD_PARAM: HALT})

    assert out["committed"] is True, out
    assert _marker(repo)["held_rows"] == [{"id": i, "reason": r} for i, r in HELD]


def test_snapshot_is_read_at_the_worktree_root_not_the_git_handle(repo):
    """terminal_commit hands the `.git` handle as repo_root; the snapshot lives at the worktree root."""
    resume_snapshot.write(repo / ".git", RUN, PLAN, [], [], held=HELD)

    out = _terminal(repo, {"run_id": RUN, HALTED_RECORD_PARAM: HALT})

    assert out["committed"] is True, out
    assert "held_rows" not in _marker(repo)


@WRITERS
def test_no_snapshot_writes_no_key(repo, call):
    out = call(repo, {"run_id": RUN, HALTED_RECORD_PARAM: HALT})

    assert out["committed"] is True, out
    assert "held_rows" not in _marker(repo)


@WRITERS
def test_a_snapshot_for_another_plan_writes_no_key(repo, call):
    resume_snapshot.write(repo, RUN, "docs/plans/other.md", [], [], held=HELD)

    call(repo, {"run_id": RUN, HALTED_RECORD_PARAM: HALT})

    assert "held_rows" not in _marker(repo)
