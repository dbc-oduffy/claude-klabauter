"""ask_stage.stage classifies engine-plane rows into the manifest; StageManifest round-trips them."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest

PLAN_REL = "docs/plans/2026-10-14-engine-rows.md"
PLAN = """---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: T1
  title: cwd-owned
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: T8
  title: engine-delivered
  change_kind: code-edit
  surface: coordinator_core/ops/sizing_intake.py
  writes:
    - coordinator_core/ops/sizing_intake.py
```
"""


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    return tmp_path


def test_stage_stores_the_classifier_rows_and_hands_it_the_ungated_plan_rows(tmp_path, monkeypatch):
    seen = {}
    row = {"id": "T8", "owner_repo": "claude-klabauter", "owner_root": "R", "root_class": "c", "engine": True}

    def fake(plan_rows, register, evidence, cwd):
        seen.update(ids=[r["id"] for r in plan_rows], cwd=cwd)
        return [row]

    monkeypatch.setattr(ask_stage, "engine_owner_rows", fake)
    root = _repo(tmp_path)
    manifest = ask_stage.stage(root, run_id="r1", plan_rel=PLAN_REL)
    assert manifest.engine_rows == (row,)
    assert manifest.to_json()["engine_rows"] == [row]
    assert seen["ids"] == ["T1", "T8"]
    assert seen["cwd"] == root


def test_stage_without_engine_rows_stores_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ask_stage, "engine_owner_rows", lambda *_: [])
    manifest = ask_stage.stage(_repo(tmp_path), run_id="r2", plan_rel=PLAN_REL)
    assert manifest.engine_rows == ()


def test_manifest_round_trips_and_an_absent_key_reads_empty():
    row = {"id": "T8", "owner_repo": "claude-klabauter", "owner_root": "R", "root_class": "c", "engine": True}
    base = ask_stage.StageManifest(run_dir="d", rows=(), review_declared_paths=(), marker_path="m", engine_rows=(row,))
    assert StageManifest.from_json(base.to_json()).engine_rows == (row,)
    data = base.to_json()
    del data["engine_rows"]
    assert StageManifest.from_json(data).engine_rows == ()
