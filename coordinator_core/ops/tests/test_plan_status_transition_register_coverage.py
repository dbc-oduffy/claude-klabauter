"""stamp-implemented refuses a register row no spine row claims, or whose claimants' writes are all absent
(the d9a207 shape: revise minted R2 after a plan whose rows claim only R1)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops import plan_status_transition as pst
from coordinator_core.ops.tests.test_plan_status_transition_goal_refusal import _init_repo, _run_git
from coordinator_core.ops.tests.test_plan_status_transition_records_exit_criterion import _FLAGS, _seed
from coordinator_core.ops.tests.test_plan_status_transition_register_tally import (
    _REGISTER_SIZING,
    _row,
    _write_sizing,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _plan(root: Path, spine: list, rows: list, claimed=("R1", "R2")) -> Path:
    plan = _seed(root)
    _write_sizing(root, rows)
    text = plan.read_text(encoding="utf-8")
    claims = f"register_claims:\n  sizing: {_REGISTER_SIZING}\n  rows: {list(claimed)}\n".replace("'", "")
    block = "\n## Tasks\n\n```yaml plan-tasks\n" + yaml.safe_dump(spine) + "```\n"
    plan.write_text(text.replace("status: approved\n", "status: approved\n" + claims, 1) + block, encoding="utf-8")
    _run_git(["add", "."], root)
    _run_git(["commit", "-q", "-m", "claims"], root)
    return plan


def _stamp(plan):
    return pst.main(["stamp-implemented", "--plan", str(plan), *_FLAGS])


def _rows():
    return [_row("R1", "met", wired=True), _row("R2", "met", wired=True)]


def test_unclaimed_row_refuses_not_built(tmp_path, capsys):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    plan = _plan(tmp_path, [{"id": "C1", "writes": ["a.py"], "register_rows": ["R1"]}], _rows())
    before = plan.read_text(encoding="utf-8")

    assert _stamp(plan) != 0
    assert "register: R2 not_built (unclaimed)" in capsys.readouterr().err
    assert plan.read_text(encoding="utf-8") == before


def test_writes_absent_claimant_refuses(tmp_path, capsys):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    spine = [
        {"id": "C1", "writes": ["a.py"], "register_rows": ["R1"]},
        {"id": "C2", "writes": ["never.py"], "register_rows": ["R2"]},
    ]
    plan = _plan(tmp_path, spine, _rows())

    assert _stamp(plan) != 0
    assert "register: R2 not_built (writes-absent)" in capsys.readouterr().err


def test_claimed_rows_with_present_writes_stamp(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    spine = [{"id": "C1", "writes": ["a.py"], "register_rows": ["R1", "R2"]}]
    plan = _plan(tmp_path, spine, _rows())

    assert _stamp(plan) in (0, 2)
    assert parse_frontmatter(plan.read_text(encoding="utf-8"))["frontmatter"]["status"] == "implemented"
