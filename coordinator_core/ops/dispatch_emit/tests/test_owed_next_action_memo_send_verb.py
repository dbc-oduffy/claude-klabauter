"""An all-EM stage refusal names verb resolve for memo-send rows and em-discharge for em-performed rows."""

from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from pathlib import Path

PLAN_REL = "docs/plans/2026-11-09-owed-kinds.md"
_SENT = ".coordinator-local/memo-outbox/sent"
_PLAN = f"""---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: C3
  title: tell the other repo
  change_kind: doc-edit
  surface: docs/x.md
  performer: em
  kind: memo-send
  memo_to: other-repo-em
  receipt: {_SENT}/topic-x.md
  writes: []
- id: C4
  title: merge by hand
  change_kind: code-edit
  surface: pkg/m.md
  performer: em
  writes:
    - pkg/m.md
```
"""


def _refuse(tmp_path) -> dict:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_PLAN, encoding="utf-8", newline="\n")
    return _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)


def test_refusal_carries_the_route_for_memo_rows_only(tmp_path: Path) -> None:
    reply = _refuse(tmp_path)
    assert reply["owed_em_ids"] == ["C3", "C4"]
    assert [m["id"] for m in reply["owed_memo_rows"]] == ["C3"]
    route = reply["owed_memo_rows"][0]["route"]
    assert "verb resolve, id C3, disposition delivered_elsewhere" in route
    assert f"{_SENT}/topic-x.md" in route
