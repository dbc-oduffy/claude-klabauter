---
title: "Fixture plan — valid task-spine with deferrals"
created: 2026-07-09
author: test-fixture
status: draft
branch: "work/test-fixture/2026-07-09"
plan_id: "pln-fixture-valid-spine-000001"
deliverable_id: "dlv-fixture-valid-spine-000001"
---

# Fixture plan — valid task-spine with deferrals

Fixture for coordinator-side tests of the ## Tasks task-spine contract
(C7, `docs/plans/2026-07-09-plan-full-coverage-and-deferred-harvest.md`).

## Tasks

```yaml plan-tasks
- id: C1
  title: "Ship the widget"
  change_kind: script-edit
  surface: coordinator/bin/widget.py
  deferred: false
  body: |
    Ship the widget end to end.
- id: C2a
  title: "Widget write-target A"
  change_kind: doc-edit
  surface: docs/wiki/widget-a.md
  deferred: false
  body: |
    Disjoint write-target A of a fanned-out C2 spine row.
- id: C2b
  title: "Widget write-target B"
  change_kind: doc-edit
  surface: docs/wiki/widget-b.md
  deferred: false
  body: |
    Disjoint write-target B of a fanned-out C2 spine row (expansion fixture).
- id: D1
  title: "Retro-migrate old widgets"
  change_kind: doc-edit
  surface: docs/plans/
  deferred: true
  pm_approved: true
  queue_scope: project
  body: |
    A PM-ratified deferral, project-scope, queue-eligible change_kind.
- id: D2
  title: "Central-scope deferred doctrine note"
  change_kind: doctrine-edit
  surface: CLAUDE.md
  deferred: true
  pm_approved: true
  queue_scope: central
  body: |
    A PM-ratified deferral, central-scope, doctrine-class change_kind — routes
    to coordinator-lesson-promote, not coordinator-queue-append.
- id: D3
  title: "Unratified deferral — pending PM"
  change_kind: skill-edit
  surface: coordinator/skills/some-skill/SKILL.md
  deferred: true
  pm_approved: false
  body: |
    Deferred but NOT PM-ratified — plan-coverage-checker flag fixture.
```
