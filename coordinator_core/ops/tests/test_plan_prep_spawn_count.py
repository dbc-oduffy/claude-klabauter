"""Exact-equality spawn count for the census python-target check in `plan.prep_gate` and
`plan.stamp_prepped`: every python census target is resolved by ONE batched `git ls-files`
however many entries name one. The figure is read from the budget manifest, never chosen here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import plan_prep_gate, plan_stamp_prepped

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REL = "docs/plans/2026-09-07-fixture.md"

_CENSUS = "".join(
    f"  - question: q{i}\n    command: python tool.py\n    result: '1'\n" for i in range(5)
)

_FM = (
    "census:\n"
    + _CENSUS
    + "prime_exit_criterion:\n"
    "  statement: the op reports per class\n"
    "  derived_from: state/sizings/2026-09-07-fixture.yaml\n"
)

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""


def _budget(op: str) -> int:
    return load_manifest()["overrides"][op]["spawn_count_budget"]["census_python_target_tracking"]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _fixture(root: Path) -> Path:
    _git(root, "init", "-q")
    (root / "tool.py").write_text("print(1)\n", encoding="utf-8")
    _git(root, "add", "tool.py")
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (root / REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-09-07\n"
        + _FM
        + "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n"
        + _SPINE
        + "```\n",
        encoding="utf-8",
    )
    return root / ".git"


def _assert_one_ls_files(spawns, op: str) -> None:
    assert len(spawns) == _budget(op), [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns)
    assert all("ls-files" in s.argv for s in spawns), [s.argv for s in spawns]


def test_prep_gate_spawns_exactly_the_budgeted_ls_files(tmp_path, monkeypatch):
    common = _fixture(tmp_path)
    with _count_spawns_attributed(monkeypatch) as spawns:
        result = plan_prep_gate._handler({"plan": REL}, common)
    assert "error" not in result, result
    _assert_one_ls_files(spawns, "plan.prep_gate")


def test_stamp_prepped_spawns_exactly_the_budgeted_ls_files(tmp_path, monkeypatch):
    common = _fixture(tmp_path)
    with _count_spawns_attributed(monkeypatch) as spawns:
        plan_stamp_prepped._handler({"plan": REL, "by": "test-session-01"}, common)
    _assert_one_ls_files(spawns, "plan.stamp_prepped")
