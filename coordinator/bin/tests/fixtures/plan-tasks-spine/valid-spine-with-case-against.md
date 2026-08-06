---
title: "Fixture plan — valid task-spine with case_against carry-through"
created: 2026-08-06
author: test-fixture
status: draft
branch: "work/test-fixture/2026-08-06"
plan_id: "pln-fixture-case-against-000001"
deliverable_id: "dlv-fixture-case-against-000001"
---

# Fixture plan — valid task-spine with case_against carry-through

Fixture for coordinator-side tests of the `case_against` harvest carry-through
(example-doctrine-repo cross-repo memo, `cross-repo/inbox/2026-08-06-example-doctrine-repo-em-deferral-both-sides-adopted-three-legs-for-you.md`,
leg 3).

## Tasks

```yaml plan-tasks
- id: D1
  title: "Deferral with a recorded case against"
  change_kind: doc-edit
  surface: docs/plans/
  deferred: true
  pm_approved: true
  queue_scope: project
  case_against: "The counter-argument that lost — do it later instead."
  body: |
    A PM-ratified deferral carrying case_against — must harvest through to
    the queue entry's own case_against field.
- id: D2
  title: "Deferral with no recorded case against"
  change_kind: doc-edit
  surface: docs/plans/
  deferred: true
  pm_approved: true
  queue_scope: project
  body: |
    A PM-ratified deferral carrying NO case_against (legitimately possible —
    the pre-existing corpus is not retro-fitted) — must harvest cleanly with
    the field simply omitted, never an empty string or placeholder.
```
