"""An all-EM stage refusal names verb resolve for memo-send rows and em-discharge for em-performed rows."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_stage import _handler

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


def _next_action(manifest: dict) -> str:
    expr = ask_compose._all_em_next_action_js("state/sizings/x.yaml")
    js = (
        f"const _manifest = {json.dumps(manifest)}; const _planRel = 'p.md'; const _sizingRel = 'state/sizings/x.yaml';"
        f"const _r = {expr}; console.log(_r.next_action);"
    )
    return subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout


def test_refusal_carries_the_route_for_memo_rows_only(tmp_path):
    reply = _refuse(tmp_path)
    assert reply["owed_em_ids"] == ["C3", "C4"]
    assert [m["id"] for m in reply["owed_memo_rows"]] == ["C3"]
    route = reply["owed_memo_rows"][0]["route"]
    assert "verb resolve, id C3, disposition delivered_elsewhere" in route
    assert f"{_SENT}/topic-x.md" in route


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_next_action_branches_per_row_kind(tmp_path):
    reply = _refuse(tmp_path)
    text = _next_action({k: reply[k] for k in ("owed_em_ids", "owed_plan", "owed_memo_rows")})
    memo_part, em_part = text.split("; coordinator-invoke", 1)
    assert "verb resolve, id C3, disposition delivered_elsewhere" in memo_part
    assert "em-discharge" not in memo_part
    assert '"verb":"em-discharge"' in em_part and '"id":"C4"' in em_part
    assert "C3" not in em_part


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_unknown_kind_keeps_the_em_discharge_default():
    text = _next_action({"owed_em_ids": ["C9"], "owed_plan": "p.md"})
    assert '"verb":"em-discharge"' in text and '"id":"C9"' in text
    assert "delivered_elsewhere" not in text
