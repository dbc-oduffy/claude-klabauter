---
title: "Fixture plan — zero fenced plan-tasks blocks, but a deferred:true line present"
created: 2026-07-11
author: test-fixture
status: draft
branch: "work/test-fixture/2026-07-11"
plan_id: "pln-fixture-zero-blocks-deferred-000001"
deliverable_id: "dlv-fixture-zero-blocks-deferred-000001"
---

# Fixture plan — zero fenced plan-tasks blocks, but a deferred:true line present

Belt-and-suspenders silent-data-loss regression fixture: `_locate_tasks_block`
genuinely fails to locate a fenced block (there is none — a real authoring
mistake, not a template-comment false-negative), but the '## Tasks' region
still visibly contains a `deferred: true` line. This is the exact
silent-loss shape the loud-skip guard targets: the harvest must escalate to
a LOUD, non-zero-exit failure instead of the default soft `exit 0` skip.

## Tasks

Prose only, no fenced ```yaml plan-tasks``` block, but this row was
hand-pasted as a reminder and never wrapped in the real fence:

- id: D1
  title: "Forgot to fence this row"
  change_kind: doc-edit
  surface: docs/plans/
  deferred: true
  pm_approved: true
