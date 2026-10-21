"""plan.cross_plan_gate treats a predecessor row landed by a checkpoint at HEAD as satisfied."""

from __future__ import annotations

import pytest

from coordinator_core.ops import plan_cross_plan_gate as op
from coordinator_core.ops.dispatch_emit.tests.test_checkpoint_rows_landed import _commit, _git

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PRED = "docs/plans/pred.md"
_TASKS = "## Tasks\n\n```yaml plan-tasks\n{rows}\n```\n"
_PRED_ROWS = "- id: P1\n  title: p\n  surface: a.py\n  writes: [a.py]\n  change_kind: code-edit"
_DEP_ROWS = (
    "- id: D1\n  title: d\n  surface: b.py\n  writes: [b.py]\n  change_kind: code-edit\n"
    f"  depends_on_plan:\n    - {{plan: {PRED}, chunk: P1, gate_kind: output-consumption-runtime}}"
)


def _write(root, name, rows):
    p = root / "docs" / "plans" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntitle: t\nstatus: executing\n---\n\n" + _TASKS.format(rows=rows), encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "seed")
    _write(tmp_path, "pred.md", _PRED_ROWS)
    _write(tmp_path, "dep.md", _DEP_ROWS)
    return tmp_path


def _gate(root):
    return op._handler({"plan": "docs/plans/dep.md"}, repo_root=root)


def test_unlanded_predecessor_holds(repo):
    out = _gate(repo)
    assert out["satisfied"] is False and out["holds"][0]["scope"] == "D1"


def test_checkpoint_landed_predecessor_satisfies(repo):
    _commit(repo, "checkpoint(wave 0): 1 rows — P1", f"Checkpoint-Plan: {PRED}")
    out = _gate(repo)
    assert out["satisfied"] is True and out["holds"] == [] and out["dangling"] == []


def test_gate_owed_checkpoint_still_holds(repo):
    _commit(
        repo, "checkpoint(wave 0): 1 rows — P1", f"Checkpoint-Plan: {PRED}\nCheckpoint-Gate-Owed: P1"
    )
    assert _gate(repo)["satisfied"] is False
