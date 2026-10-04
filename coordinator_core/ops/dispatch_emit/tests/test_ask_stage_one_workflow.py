"""ask_stage refuses external_gate rows and appends self-DR discharge clauses to dependent briefs."""

from __future__ import annotations

import time
from pathlib import Path, PurePath

import yaml

from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.session.record_homes import home_dir, record_path

PLAN_REL = "docs/plans/2026-10-02-one-wf.md"
SIZING_REL = PurePath(record_path("", "sizings", "s.yaml")).as_posix()

GATED = """---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: G1
  title: gated
  change_kind: code-edit
  surface: C:/other/repo/a.py  # abs-path-ok: fixture needs a drive-letter write
  writes:
    - C:/other/repo/a.py  # abs-path-ok: fixture needs a drive-letter write
  external_gate:
    - owner_repo: other
      requires: their change
- id: G2
  title: dependent
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
  depends_on:
    - chunk: G1
```
"""


def _plan(cite: bool) -> str:
    fm = f'---\ntitle: fixture\nsizing_object: "{SIZING_REL}"\n---\n' if cite else "---\ntitle: fixture\n---\n"
    return fm + """# Fixture

## Tasks

```yaml plan-tasks
- id: D1
  title: decision
  change_kind: doc-new
  surface: docs/decisions/DR-1.md
  writes:
    - docs/decisions/DR-1.md
- id: R1
  title: dependent
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
  depends_on:
    - chunk: D1
```
"""


def _sizing(mode: str) -> str:
    return yaml.safe_dump({"exit_criterion": {
        "statement": "ship it",
        "accepted": {"mode": mode, "pm_quote": "go ahead", "on": "2026-10-02"},
    }})


def _repo(tmp_path, plan: str, sizing: str | None = None):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    if sizing is not None:
        Path(home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
        (tmp_path / SIZING_REL).write_text(sizing, encoding="utf-8", newline="\n")


def _stage(tmp_path, run_id="r1"):
    return _handler({"run_id": run_id, "plan_path": PLAN_REL}, repo_root=tmp_path)


def _brief(tmp_path, row, run_id="r1"):
    return (tmp_path / RUN_DIR_ROOT / run_id / "briefs" / f"{row}.md").read_text(encoding="utf-8")


def test_all_gated_plan_refuses_naming_every_gated_row(tmp_path):
    _repo(tmp_path, GATED)
    reply = _stage(tmp_path)
    assert "zero dispatchable rows" in reply["error"]
    assert "G1" in reply["error"] and "G2" in reply["error"]
    run_dir = tmp_path / RUN_DIR_ROOT / "r1"
    assert not (run_dir / "briefs").exists()
    assert not (run_dir / "manifest.json").exists()


def test_pm_accepted_sizing_appends_clause_to_dependent_only(tmp_path):
    _repo(tmp_path, _plan(True), _sizing("pm"))
    start = time.process_time()
    reply = _stage(tmp_path)
    elapsed_ms = (time.process_time() - start) * 1000
    assert "error" not in reply
    assert "Decision-record gate discharged" in _brief(tmp_path, "R1")
    assert "Decision-record gate discharged" not in _brief(tmp_path, "D1")
    assert elapsed_ms < 500


def test_hands_on_sizing_stages_briefs_identical_to_no_sizing(tmp_path):
    _repo(tmp_path, _plan(True), _sizing("hands-on"))
    assert "error" not in _stage(tmp_path)
    with_sizing = {r: _brief(tmp_path, r) for r in ("D1", "R1")}

    (tmp_path / PLAN_REL).write_text(_plan(False), encoding="utf-8", newline="\n")
    assert "error" not in _stage(tmp_path, "r2")
    assert with_sizing == {r: _brief(tmp_path, r, "r2") for r in ("D1", "R1")}
