"""ask_stage admits the staged plan file to the session's review-targets ledger without widening the review-diff set."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

PLAN_REL = "docs/plans/2026-10-01-fixture.md"

PLAN = """---
deliverable_id: dlv-fixture-abc123
---
# Fixture plan

## Problem

One row.

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
```
"""


def _repo(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    write_run_scripts(tmp_path, "r1")
    return tmp_path


def _targets(repo, session):
    path = repo / ".git" / "coordinator-sessions" / session / "review-targets.txt"
    return path.read_text(encoding="utf-8").split()


def test_staged_plan_path_is_a_review_target_but_not_a_review_diff_path(tmp_path):
    repo = _repo(tmp_path)
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL, session_id="sess-1")
    targets = _targets(repo, "sess-1")
    assert PLAN_REL in targets
    assert "pkg/a.py" in targets
    assert PLAN_REL not in m.review_declared_paths
