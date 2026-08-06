---
title: "Fixture plan — multiple fenced plan-tasks blocks (parser-locate error)"
created: 2026-07-09
author: test-fixture
status: draft
branch: "work/test-fixture/2026-07-09"
plan_id: "pln-fixture-multi-blocks-000001"
deliverable_id: "dlv-fixture-multi-blocks-000001"
---

# Fixture plan — multiple fenced plan-tasks blocks

## Tasks

```yaml plan-tasks
- id: C1
  title: "First block"
  change_kind: doc-edit
  surface: docs/foo.md
  deferred: false
```

Some narrative text between the two blocks (still under ## Tasks by document
order, whether or not this is itself well-formed authoring — the point of
this fixture is the parser-locate rule: >1 fenced ```yaml plan-tasks``` block
anywhere in the document is a defined error).

```yaml plan-tasks
- id: C2
  title: "Second block — ambiguous spine"
  change_kind: doc-edit
  surface: docs/bar.md
  deferred: false
```
