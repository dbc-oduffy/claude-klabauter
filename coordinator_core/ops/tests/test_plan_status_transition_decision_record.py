"""
coordinator_core.ops.tests.test_plan_status_transition_decision_record --
a decision-only plan (no `prime_exit_criterion`) closes as `implemented` through
`stamp-implemented --decision-record <committed DR>` in place of a falsifier.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops import plan_status_transition as pst
from coordinator_core.ops.tests.test_plan_status_transition_goal_refusal import (
    _init_repo,
    _run_git,
)
from coordinator_core.session import record_homes

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_DR = "docs/decisions/DR-900-fixture.md"

_PLAN = """---
title: "Fixture plan -- decision only"
created: 2026-09-01
author: test-fixture
status: approved
branch: "work/test-fixture/2026-10-01"
plan_id: "pln-fixture-decision-000001"
deliverable_id: "dlv-fixture-decision-000001"
---

# Fixture plan -- decision only

The deliverable is a decision record.
"""

_PRIME = """prime_exit_criterion:
  statement: "x"
"""


def _seed(root: Path, *, land_dr: bool = True, plan_text: str = _PLAN) -> Path:
    if land_dr:
        dr = root / _DR
        dr.parent.mkdir(parents=True, exist_ok=True)
        dr.write_text("# DR-900\n", encoding="utf-8")
        _run_git(["add", _DR], root)
    sizing = Path(record_homes.record_path(str(root), "sizings", "2026-10-01-fixture-sizing.yaml"))
    sizing.parent.mkdir(parents=True, exist_ok=True)
    sizing.write_text("estimate:\n  tshirt: M\n", encoding="utf-8")
    sizing_rel = sizing.relative_to(root).as_posix()
    plan = root / "plan.md"
    plan.write_text(
        plan_text.replace("status: approved\n", f"status: approved\nsizing_object: {sizing_rel}\n", 1),
        encoding="utf-8",
    )
    _run_git(["add", "plan.md", "state"], root)
    _run_git(["commit", "-q", "-m", "seed", "--allow-empty"], root)
    return plan


def _stamp(plan, *extra):
    return pst.main(["stamp-implemented", "--plan", str(plan), *extra])


def test_decision_record_closes_plan_with_no_criterion(tmp_path):
    _init_repo(tmp_path)
    plan = _seed(tmp_path)

    assert _stamp(plan, "--decision-record", _DR) in (0, 2)

    fm = parse_frontmatter(plan.read_text(encoding="utf-8"))["frontmatter"]
    assert fm["status"] == "implemented"
    assert fm["decision_record"] == _DR


def test_uncommitted_record_is_refused(tmp_path, capsys):
    _init_repo(tmp_path)
    plan = _seed(tmp_path, land_dr=False)
    before = plan.read_text(encoding="utf-8")

    assert _stamp(plan, "--decision-record", _DR) == 1
    assert plan.read_text(encoding="utf-8") == before
    assert "not a committed file" in capsys.readouterr().err


def test_path_escaping_the_repo_is_refused(tmp_path, capsys):
    _init_repo(tmp_path)
    plan = _seed(tmp_path)
    before = plan.read_text(encoding="utf-8")

    assert _stamp(plan, "--decision-record", "../outside.md") == 1
    assert plan.read_text(encoding="utf-8") == before
    assert "not a committed file" in capsys.readouterr().err


def test_plan_with_a_criterion_is_refused(tmp_path, capsys):
    _init_repo(tmp_path)
    plan = _seed(tmp_path, plan_text=_PLAN.replace("---\n\n#", _PRIME + "---\n\n#", 1))
    before = plan.read_text(encoding="utf-8")

    assert _stamp(plan, "--decision-record", _DR) == 1
    assert plan.read_text(encoding="utf-8") == before
    assert "declares a prime_exit_criterion" in capsys.readouterr().err


def test_flag_is_rejected_with_falsifier_flags_and_on_other_verbs(tmp_path, capsys):
    _init_repo(tmp_path)
    plan = _seed(tmp_path)

    assert _stamp(
        plan, "--decision-record", _DR,
        "--falsifier-verdict", "pass", "--falsifier-output", "o", "--prose", "p",
    ) == 1
    assert pst.main(["stamp-reopened", "--plan", str(plan), "--reason", "r", "--decision-record", _DR]) == 1
    assert capsys.readouterr().err.count("--decision-record is for stamp-implemented only") == 2


def test_without_the_flag_the_same_plan_still_refuses(tmp_path):
    _init_repo(tmp_path)
    plan = _seed(tmp_path)

    assert _stamp(plan) == 1
    assert "status: implemented" not in plan.read_text(encoding="utf-8")
