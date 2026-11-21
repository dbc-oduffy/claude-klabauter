"""
coordinator_core.ops.tests.test_plan_tasks_mutate_register_rows_empty

A row declaring `register_rows: []` (resting on no register row) is schema-valid,
so plan.tasks.mutate resolve marks it coded; a malformed register_rows still refuses.
"""

from __future__ import annotations

import json
from pathlib import Path


_SCHEMA = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "plan-tasks.schema.json"

_PLAN = """\
---
title: "Test Plan"
status: draft
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  register_rows: %s
  body: |
    Do the first thing.
```
"""


def test_schema_register_rows_has_no_min_items():
    prop = json.loads(_SCHEMA.read_text(encoding="utf-8"))["properties"]["register_rows"]
    assert "minItems" not in prop
