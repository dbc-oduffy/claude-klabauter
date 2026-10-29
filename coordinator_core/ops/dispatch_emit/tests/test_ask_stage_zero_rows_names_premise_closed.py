"""The zero-dispatchable-rows stage refusal names premise-closed and transitively held open rows."""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.plan_tasks_mutate import _handler as mutate_handler
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_REL = "docs/plans/p.md"

_PLAN = """---
title: p
created: 2026-10-27
---

# p

## Tasks

```yaml plan-tasks
- id: C1
  title: probe
  change_kind: doc-edit
  surface: docs/x/c1.md
  writes:
    - docs/x/c1.md
  disposition: coded
  disposition_ref: abc1234
- id: C2
  title: fold
  change_kind: doc-edit
  surface: docs/x/c2.md
  writes:
    - docs/x/c2.md
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
      note: "runs only if C1 records overlap yes; else the row closes wont_do citing C1"
- id: C4
  title: consumer
  change_kind: doc-edit
  surface: docs/x/c4.md
  writes:
    - docs/x/c4.md
  depends_on:
    - chunk: C2
      gate_kind: output-consumption-runtime
```
"""


def test_refusal_names_premise_false_and_transitively_held_rows(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = repo / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(_PLAN, encoding="utf-8", newline="\n")
    result = asyncio.run(
        mutate_handler(
            {"verb": "premise-record", "plan_path": str(plan), "id": "C1", "verdict": "no", "ref": "docs/x/c1.md"},
            repo_root=repo / ".git",
        )
    )
    assert result["exit_code"] == 0, result
    err = read_stage_reply(repo, _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=repo))["error"]
    assert "zero dispatchable rows" in err
    assert "C2 (premise-false" in err and "premise C1 recorded no" in err
    assert "C4 (withheld_by_held_dependency" in err
    assert "C1 (" not in err, "a shipped (coded) row is not an open excluded row"
