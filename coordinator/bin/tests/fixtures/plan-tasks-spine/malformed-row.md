---
title: "Fixture plan — malformed row (missing required field)"
created: 2026-07-09
author: test-fixture
status: draft
branch: "work/test-fixture/2026-07-09"
plan_id: "pln-fixture-malformed-row-000001"
deliverable_id: "dlv-fixture-malformed-row-000001"
---

# Fixture plan — malformed row

## Tasks

```yaml plan-tasks
- id: C1
  title: "Well-formed row, ships normally"
  change_kind: script-edit
  surface: coordinator/bin/widget.py
  deferred: false
  body: |
    Fine.
- id: D1
  title: "Malformed deferred row — missing change_kind and surface"
  deferred: true
  pm_approved: true
  body: |
    This row is missing the required change_kind and surface fields — the
    harvest must SKIP-WITH-WARNING, never crash, and the coverage-checker
    must flag it as malformed.
```
