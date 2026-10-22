"""stamp-implemented closes the halted run of its plan's sizing and reports the siblings it frees."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run, run_close
from coordinator_core.ops.plan_status_transition import main
from coordinator_core.warp_fire import fire_command
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}
PARENT = "ask-parent-20261015T000000-abcdef"
PRED = "ask-20261016T152609-443905"
SUCC = "ask-20261016T152609-db6c90"
PRED_SIZING = "state/sizings/2026-10-16-pred.yaml"
SUCC_SIZING = "state/sizings/2026-10-16-succ.yaml"

_PLAN = """---
title: T
status: executing
owner: x
{sizing}---

# Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: only row
  change_kind: code-edit
  surface: a.py
  writes:
    - a.py
  disposition: coded
  body: |
    do it
```
"""


def _git(root: Path, *args: str) -> None:
    r = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, env=_ENV, timeout=30,
        **no_console_creationflags(),
    )
    assert r.returncode == 0, r.stderr


def _repo(root: Path, sizing_object: str | None = PRED_SIZING) -> Path:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "commit.gpgsign", "false")
    plan = root / "p.md"
    line = f"sizing_object: {sizing_object}\n" if sizing_object else ""
    plan.write_text(_PLAN.format(sizing=line), encoding="utf-8")
    _git(root, "add", "--", "p.md")
    _git(root, "commit", "-q", "-m", "initial")
    return plan


def _seed_siblings(root: Path, *, halted: bool = True) -> None:
    runs = [
        c.SiblingRun(PRED, PRED, (), PRED_SIZING, c.STATUS_READY),
        c.SiblingRun(SUCC, SUCC, (PRED,), SUCC_SIZING, c.STATUS_AFTER),
    ]
    rec = root / c.parent_record_rel(PARENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(c.ParentRecord(PARENT, tuple(runs)).to_json()), encoding="utf-8")
    copy = root / c.run_parent_copy_rel(PRED)
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(rec.read_text(encoding="utf-8"), encoding="utf-8")
    st = root / c.status_rel(PRED)
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps({"status": c.STATUS_RUNNING}), encoding="utf-8")
    if halted:
        run_close.sibling_release(root, PRED, None)
        halted_run.write_halt_marker(
            root / "state" / "warp" / PRED, halted="gate", sizing_rel=PRED_SIZING
        )


def _status(root: Path) -> dict:
    return json.loads((root / c.status_rel(PRED)).read_text(encoding="utf-8"))


def test_no_commit_flip_closes_halted_run_and_reports_successor(tmp_path, capsys):
    plan = _repo(tmp_path / "r")
    _seed_siblings(plan.parent)
    assert _status(plan.parent)["status"] == "halted"
    assert "closed_by" not in _status(plan.parent)

    assert main(["stamp-implemented", "--plan", str(plan), "--no-commit"]) == 0

    assert _status(plan.parent)["closed_by"] == "p.md"
    out = capsys.readouterr().out
    assert PRED in out and fire_command(SUCC_SIZING) in out


def test_restamp_of_implemented_plan_closes_the_halted_run(tmp_path, capsys):
    plan = _repo(tmp_path / "r")
    assert main(["stamp-implemented", "--plan", str(plan), "--no-commit"]) == 0
    _seed_siblings(plan.parent)
    capsys.readouterr()

    assert main(["stamp-implemented", "--plan", str(plan), "--no-commit"]) == 0

    assert _status(plan.parent)["closed_by"] == "p.md"
    assert fire_command(SUCC_SIZING) in capsys.readouterr().out


def test_superseded_successor_is_reported_not_released(tmp_path, capsys):
    plan = _repo(tmp_path / "r")
    _seed_siblings(plan.parent)
    marker = plan.parent / c.superseded_rel(SUCC)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({c.SUPERSEDED_BY_KEY: "ask-20261017T033957-569176"}), encoding="utf-8")

    assert main(["stamp-implemented", "--plan", str(plan), "--no-commit"]) == 0

    out = capsys.readouterr().out
    assert fire_command(SUCC_SIZING) not in out
    assert "ask-20261017T033957-569176" in out


def test_plan_without_sizing_object_is_a_silent_noop(tmp_path, capsys):
    plan = _repo(tmp_path / "r", sizing_object=None)
    _seed_siblings(plan.parent)

    assert main(["stamp-implemented", "--plan", str(plan), "--no-commit"]) == 0

    assert "closed_by" not in _status(plan.parent)
    assert "closed halted run" not in capsys.readouterr().out


def test_plan_with_no_halted_run_is_a_silent_noop(tmp_path, capsys):
    plan = _repo(tmp_path / "r")

    assert main(["stamp-implemented", "--plan", str(plan), "--no-commit"]) == 0

    assert "closed halted run" not in capsys.readouterr().out


def test_raising_closer_leaves_exit_code_unchanged(tmp_path, monkeypatch, capsys):
    plan = _repo(tmp_path / "r")

    def boom(*_a, **_k):
        raise RuntimeError("closer exploded")

    monkeypatch.setattr(run_close, "close_halted_runs_for_plan", boom)

    assert main(["stamp-implemented", "--plan", str(plan), "--no-commit"]) == 0

    err = capsys.readouterr().err
    assert "halted-run close failed" in err and "closer exploded" in err
    assert "status: implemented" in plan.read_text(encoding="utf-8")
