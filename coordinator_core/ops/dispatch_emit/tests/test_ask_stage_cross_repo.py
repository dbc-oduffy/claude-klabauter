"""ask_stage refuses a spine writing outside repoRoot before any brief or manifest lands."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_stage import _handler

PLAN_REL = "docs/plans/2026-10-02-xrepo.md"

PLAN = """# Fixture

## Tasks

```yaml plan-tasks
- id: C1
  title: ok
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: C2
  title: escapes
  change_kind: code-edit
  surface: ../sibling/x.md
  writes:
    - ../sibling/x.md
```
"""


def test_escaping_row_refused_before_anything_is_staged(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)
    assert "C2" in reply["error"] and "../sibling/x.md" in reply["error"]
    run_dir = tmp_path / RUN_DIR_ROOT / "r1"
    assert not (run_dir / "briefs").exists()
    assert not (run_dir / "manifest.json").exists()
