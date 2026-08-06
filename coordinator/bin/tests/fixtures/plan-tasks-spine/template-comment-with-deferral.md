---
title: "Fixture plan — unedited template HTML comment above the real fence"
created: 2026-07-11
author: test-fixture
status: draft
branch: "work/test-fixture/2026-07-11"
plan_id: "pln-fixture-template-comment-000001"
deliverable_id: "dlv-fixture-template-comment-000001"
---

# Fixture plan — unedited template HTML comment above the real fence

Fixture for the silent-data-loss regression: a plan that still carries the
writing-plans.md template's unedited authoring comment directly under
'## Tasks', ABOVE the real fenced block. The comment embeds a literal
```yaml plan-tasks``` string as documentation, and is non-blank content
between the heading and the real fence — both of which used to trip
`_locate_tasks_block`'s guards and silently return None, losing this plan's
`deferred: true` row (D1) without ever surfacing a warning that named the
real cause.

## Tasks

<!-- Machine-parseable task spine — the EM edits row values in place; the structure
     below is a lay-up, not a from-scratch authoring task. Exactly ONE fenced
     ```yaml plan-tasks``` block belongs directly under this heading (parser-locate
     rule — zero or >1 blocks is a defined error). Each list item validates against
     schemas/plan-tasks.schema.json. Delete the two sample rows below and replace with
     real chunks; keep at least the shape (id/title/change_kind/surface required).
     change_kind enum SSOT: docs/wiki/lessons-outbox-schema.md § Change-kind enum.
     Full authoring contract: docs/wiki/writing-plans.md § Machine-Parseable Task Spine. -->

```yaml plan-tasks
- id: C1
  title: "Ship the widget"
  change_kind: script-edit
  surface: coordinator/bin/widget.py
  deferred: false
  body: |
    Ship the widget end to end.
- id: D1
  title: "Retro-migrate old widgets"
  change_kind: doc-edit
  surface: docs/plans/
  deferred: true
  pm_approved: true
  queue_scope: project
  body: |
    A PM-ratified deferral that must be located and harvested even though
    the template's unedited HTML comment sits directly above the real fence.
```
