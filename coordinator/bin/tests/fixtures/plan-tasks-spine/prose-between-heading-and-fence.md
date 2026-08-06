---
title: "Fixture plan — load-bearing prose between the ## Tasks heading and the fence"
created: 2026-07-20
author: test-fixture
status: draft
branch: "work/test-fixture/2026-07-20"
plan_id: "pln-fixture-prose-between-000001"
deliverable_id: "dlv-fixture-prose-between-000001"
---

# Fixture plan — load-bearing prose between the ## Tasks heading and the fence

Fixture for the containment-vs-adjacency regression. Real reviewed plans routinely
carry load-bearing prose — a pinned-interface paragraph both waves must code against,
and a wave map — between the `## Tasks` heading and the real fenced block. The old
`_locate_tasks_block` guard permitted only blank lines there, so it returned None on
every such plan and warn-and-skipped the harvest, silently losing the `deferred: true`
rows in the plan's real spine.

The adjacency guard was never load-bearing for disambiguation: the function already
enforces exactly-one `yaml plan-tasks` fence across the WHOLE document before it looks
at position, so with a single candidate fence there is nothing to disambiguate. What
the guard was really providing was section containment (don't match a fence that lives
in some later section), which is now enforced directly by bounding the search at the
next `## ` heading.

Originating incident: `docs/plans/2026-07-20-machine-blind-repo-identity.md` — an
authored, twice-reviewed, coverage-checked plan whose spine `plan-coverage-checker`
parsed fine (8 rows) while the harvest CLI reported "no locatable block". Two consumers
of one pinned contract disagreeing on what parses, when the contract says they should
disagree only on severity.

## Tasks

<!-- Machine-parseable task spine — the EM edits row values in place; the structure
     below is a lay-up, not a from-scratch authoring task. Exactly ONE fenced
     ```yaml plan-tasks``` block belongs directly under this heading (parser-locate
     rule — zero or >1 blocks is a defined error). -->

**Pinned interface (both waves code against this — do not re-derive).** Load-bearing
prose that a reviewer put here deliberately, because an executor reading only the spine
would otherwise re-derive the interface and drift. This is exactly the content that must
NOT cause the harvest to skip.

**Wave map.** Wave 0 = C1. Wave 1 = D1 (deferred, PM-ratified).

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
    load-bearing prose sits between the heading and the real fence.
```

## Some Later Section

This section exists to prove the containment bound is real: the fence above is inside
`## Tasks`, and this heading terminates that section.
