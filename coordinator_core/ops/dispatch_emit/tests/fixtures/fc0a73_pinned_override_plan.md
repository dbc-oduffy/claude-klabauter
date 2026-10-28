---
title: "Test runs-dir isolation and run_in_group mock"
created: 2026-10-27
status: executing
scope_mode: spec-dispatch
plan_id: "pln-test-runs-dir-isolation-and-ru-d20605"
---

# Test runs-dir isolation and run_in_group mock

Committed copy of the plan-tasks block of experiments run fc0a73's plan. C2 overrides
`agent_model: haiku` on `coordinator:executor`, which is pinned to sonnet.

## Tasks

```yaml plan-tasks
- id: C1
  title: Autouse tmp_path RUNS_DIR fixture, guard test, remove stray runs/rid
  change_kind: test-edit
  surface: naked-vs-coordinator/tests/conftest.py
  writes: [naked-vs-coordinator/tests/conftest.py, naked-vs-coordinator/tests/test_no_real_runs_dir_writes.py]
  reads_at_head: [naked-vs-coordinator/config.py, naked-vs-coordinator/arms.py, naked-vs-coordinator/runner.py, naked-vs-coordinator/tests/conftest.py, naked-vs-coordinator/tests/test_ladder_test_cmd_resolution.py]
  consumes: []
  model: sonnet
  effort: medium
  agent_type: coordinator:executor
  queue_scope: project
  disposition: open
  body: |
    Outcome: every test sees config.RUNS_DIR under tmp_path via one autouse fixture.
- id: C2
  title: Mock run_in_group in test_ladder_judging summary-parse test
  change_kind: test-edit
  surface: naked-vs-coordinator/tests/test_ladder_judging.py
  writes: [naked-vs-coordinator/tests/test_ladder_judging.py]
  reads_at_head: [naked-vs-coordinator/objective.py, naked-vs-coordinator/scrubbed_env.py, naked-vs-coordinator/tests/test_ladder_judging.py]
  consumes: []
  model: haiku
  effort: low
  agent_type: coordinator:executor
  queue_scope: project
  disposition: open
  body: |
    Outcome: the summary-parse test stubs objective.run_in_group so no real process spawns.
```
