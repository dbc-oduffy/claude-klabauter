---
title: "Script-fired reconcile merge: start, file-only resolver, conclude"
status: approved
plan_id: "pln-script-fired-reconcile-merge-s-7b016e"
scope_mode: feature
branch: "work/machine-a/2026-10-04to14"
---
<!-- FIXTURE, not a plan: frozen copy of docs/plans/2026-10-14-script-fired-reconcile-merge.md
     (git blob last touched at 8e50ac5cb7). Frontmatter reduced to what the emitter reads;
     dispositions reset to open so every row is dispatchable. Consumed by the emit full-path
     process-budget gate. Do not edit to track the source plan. -->

# Script-fired reconcile merge: start, file-only resolver, conclude

## PM brief

> no, your instinct to go to the agent means that we should either give the commit agent that power, change doctrine in some way, or make this script-fired for the consolidate-git operations so that it doesn't need an agent per se
>
> remember, any friction you encounter with this skill is yours to fix!
>
> /workday-start

## Tasks

```yaml plan-tasks
- id: S0
  title: Spike — open-merge window, per-op process time and the driver's halt-and-emit shape
  change_kind: verification
  surface: docs/research/2026-10-14-script-fired-reconcile-merge-spike.md
  traces_to_brief: "make this script-fired for the consolidate-git operations"
  writes:
    - docs/research/2026-10-14-script-fired-reconcile-merge-spike.md
  reads_at_head:
    - coordinator_core/ops/workday_start_step0_reconcile.py
    - coordinator_core/ceremony_common/run/driver.py
    - coordinator_core/ceremony_common/run/runner.py
    - coordinator_core/workday_start/run_spec.py
  queue_scope: project
  disposition: open
  body: |
    Discharges the sizing's `premise_unproven` detent before C4 is built. On a fixture repo under
    the session scratchpad (never the repo root): (a) create a genuine conflict, hold the merge
    open, and record what a second process's `git commit -- <path>` and `apply_base.scoped_commit`
    do during the window, and whether either leaves entries staged in the shared index that
    conclude's foreign-staged check would then refuse on; (b) measure process time and spawn count of the start arm (excluding
    the merge child, and with it) and of the four-spawn conclude path; (c) read the ceremony
    driver and runner and state whether a gates_next step's rule can halt later stages, emit one
    agent group and run a step at finish without a driver change, naming the smallest change if
    not. Verdict `viable` / `not-viable` per `coordinator:spike`; `not-viable` on (a) is a
    direction-class finding for the APM, not a plan edit.
- id: C0
  title: Interface — conflict-manifest module and the pinned outcome strings, CLI name and marker predicate
  change_kind: code-edit
  surface: coordinator_core/git/reconcile_manifest.py
  traces_to_brief: "so that it doesn't need an agent per se"
  writes:
    - coordinator_core/git/reconcile_manifest.py
    - coordinator_core/git/tests/test_reconcile_manifest.py
  reads_at_head:
    - coordinator_core/git/git_dir.py
    - coordinator_core/ops/workday_start_step0_reconcile.py
  queue_scope: project
  disposition: open
  body: |
    The wave-0 contract every other row builds against (see ## Design, Manifest). Functions:
    `manifest_path(repo_root)`, `write(repo_root, *, merge_head, branch, paths)`,
    `read(repo_root) -> Manifest | None` (fail-closed against the live MERGE_HEAD),
    `remove(repo_root)`, `has_marker(text) -> bool`. Constants for every outcome and refusal
    string and `CONCLUDE_CLI = "reconcile-merge-conclude"`. No subprocess. Tests under tmp_path:
    round trip; absent, malformed, wrong-schema and stale-MERGE_HEAD reads return None; marker
    predicate matches only line-anchored markers.
- id: C1
  title: Start — the Step 0 reconcile's conflict arm holds the merge open and writes the manifest
  change_kind: code-edit
  surface: coordinator_core/ops/workday_start_step0_reconcile.py
  traces_to_brief: "make this script-fired for the consolidate-git operations"
  writes:
    - coordinator_core/ops/workday_start_step0_reconcile.py
    - coordinator_core/ops/test_workday_start_step0_reconcile.py
    - coordinator_core/ops/test_workday_start_step0_reconcile_classify.py
    - coordinator/bin/workday-start-step0-reconcile.py
    - coordinator/bin/workday-start-step0.py
  consumes:
    - coordinator_core/git/reconcile_manifest.py
    - docs/research/2026-10-14-script-fired-reconcile-merge-spike.md
  depends_on:
    - chunk: S0
      gate_kind: epistemic-premise
      note: Holding the merge open in the shared tree is exactly what S0 (a) measures; a not-viable window makes this row rework, so it waits on S0's verdict like C4.
  queue_scope: project
  disposition: open
  body: |
    Only the `RECONCILE-CONFLICT` outcome changes (see ## Design, Start): no abort, manifest
    written, last stdout line `RECONCILE-CONFLICT-OPEN branch=<b>`, CONFLICT-PATHS block kept,
    exit 3. An empty `_conflict_paths` result still aborts with today's `RECONCILE-CONFLICT`
    line; test it. The shared `merge --abort` call moves under the arms that keep it. The other
    two failed-merge outcomes keep aborting. The two bin files change only in
    their docstrings' outcome list. Extend the cadence test with a real-conflict fixture
    asserting MERGE_HEAD present and the manifest's paths; the classify test is unchanged unless
    a constant moves to C0.
- id: C2a
  title: Conclude op — verify markers, unmerged and foreign-staged, then commit the merge
  change_kind: code-edit
  surface: coordinator_core/ops/reconcile_merge_conclude.py
  traces_to_brief: "make this script-fired for the consolidate-git operations"
  writes:
    - coordinator_core/ops/reconcile_merge_conclude.py
    - coordinator_core/ops/test_reconcile_merge_conclude.py
    - coordinator_core/tests/test_no_pathspec_less_commit.py
  consumes:
    - coordinator_core/git/reconcile_manifest.py
  reads_at_head:
    - coordinator_core/ops/workday_start_step0_reconcile.py
    - coordinator_core/git_lock_retry.py
    - coordinator_core/contract/apply_base.py
  queue_scope: project
  disposition: open
  body: |
    `main(argv)`, one verb, no subcommands. Order and spawn plan per ## Design,
    Conclude. Refusals print the pinned refusal string plus the offending paths on stderr, exit 3,
    and write nothing. Success prints `RECONCILED-MERGE-CONCLUDED branch=<b>`, removes the
    manifest, exit 0. Git calls use the no-console helper and `run_with_lock_retry` as the start
    op does. Add one `_KNOWN_BARE_COMMIT_SITES` entry for the merge commit, classified with the
    foreign-staged refusal as its safety reason. Tests under tmp_path cover every refusal, the
    success commit's parents.
- id: C2b
  title: CLI trampoline reconcile-merge-conclude and every registry enumerating bin entrypoints
  change_kind: script-edit
  surface: coordinator/bin/reconcile-merge-conclude.py
  traces_to_brief: "make this script-fired for the consolidate-git operations"
  writes:
    - coordinator/bin/reconcile-merge-conclude.py
    - coordinator/bin/reconcile-merge-conclude.cmd
    - docs/install/bin-inventory.json
    - coordinator_core/ops/warm_entrypoint_allowlist.json
    - coordinator/bin/tests/test_hand_rolled_cli_help_sweep.py
  consumes:
    - coordinator_core/git/reconcile_manifest.py
  reads_at_head:
    - coordinator/bin/workday-start-step0-reconcile.py
    - coordinator/bin/workday-start-step0-reconcile.cmd
  queue_scope: project
  disposition: open
  body: |
    Copy the shape of `workday-start-step0-reconcile.py` exactly (engine-root resolution,
    `run_op_main("coordinator_core.ops.reconcile_merge_conclude", ...)`), generated `.cmd`
    launcher. Add the name to each registry that enumerates `workday-start-step0-reconcile`
    (bin-inventory, both warm-allowlist lists, help sweep). Before returning, grep the repo for
    every other file enumerating `workday-start-step0-reconcile` by name; any not in this row's
    writes is reported BLOCKED with the path, never edited.
- id: C3
  title: Pre-Edit sizing gate allows edits to live-manifest-listed paths, fail-closed
  change_kind: hook-edit
  surface: coordinator_core/write_guards/block_em_strict_dispatch_code_write.py
  traces_to_brief: "any friction you encounter with this skill is yours to fix"
  writes:
    - coordinator_core/write_guards/block_em_strict_dispatch_code_write.py
    - coordinator_core/write_guards/tests/test_strict_dispatch_guard.py
  consumes:
    - coordinator_core/git/reconcile_manifest.py
  queue_scope: project
  disposition: open
  body: |
    After the doc/data pass and before the sizing read: if `reconcile_manifest.read(repo_root)`
    returns a manifest and the target path, made repo-relative and `/`-normalised, is in its
    `paths`, return None. One stat of MERGE_HEAD plus one small JSON read, only on the
    flagged-session path the guard already runs; no subprocess. Any exception in the leg falls
    through to the existing logic (never allows on error beyond what the module already does).
    Docstring gains one deny-condition note. Tests: listed path allowed; unlisted code path
    denied; listed path denied after MERGE_HEAD is removed; stale manifest denied.
- id: C4
  title: Workday-start composes the open-merge outcome — halt committing stages, one resolver group, conclude at finish
  change_kind: code-edit
  surface: coordinator_core/workday_start/rules.py
  traces_to_brief: "/workday-start"
  writes:
    - coordinator_core/workday_start/rules.py
    - coordinator_core/workday_start/run_spec.py
    - coordinator_core/workday_start/tests/test_decide.py
    - coordinator_core/workday_start/tests/test_cli_end_to_end.py
    - coordinator_core/ceremony_common/run/driver.py
    - coordinator_core/ceremony_common/run/runner.py
  consumes:
    - coordinator_core/git/reconcile_manifest.py
    - docs/research/2026-10-14-script-fired-reconcile-merge-spike.md
  depends_on:
    - chunk: S0
      gate_kind: epistemic-premise
      note: S0 decides whether the in-tree open-merge window is acceptable and whether the driver needs a change; the two driver files are the worst-case writes and are dropped if S0 shows none is needed.
  queue_scope: project
  disposition: open
  body: |
    `rules.py`: `RECONCILE-CONFLICT-OPEN` is recognised by `_RECONCILE_LINE`; it is not deferred
    and not a failure — it records one Decision `resolve-in-run` with the manifest paths as
    evidence. `run_spec.py`: `agent_groups` adds a `reconcile` group (phase 0, one entry, the
    resolver brief inline, manifest paths as inputs, `argv: None`); finish runs
    `reconcile-merge-conclude` before the briefing and a refusal is a `failed` row with its
    refusal string. The resolver brief names the files, forbids every git verb, and says the
    guards will deny one. Later committing stages carry `halted-by-gate` with reason
    `reconcile-open` (shape per S0). Executes against HEAD; blocks only AC6's closure until the
    DoE template lands.
- id: C6
  title: End-to-end flow test and per-op process-time evidence
  change_kind: test-edit
  surface: coordinator_core/ops/test_reconcile_merge_flow.py
  traces_to_brief: "so that it doesn't need an agent per se"
  writes:
    - coordinator_core/ops/test_reconcile_merge_flow.py
    - coordinator_core/workday_start/step_costs.json
  consumes:
    - coordinator_core/ops/workday_start_step0_reconcile.py
    - coordinator_core/ops/reconcile_merge_conclude.py
  queue_scope: project
  disposition: open
  body: |
    Cadence-marked test over a tmp_path fixture: conflicted Step 0 reconcile, plain file writes
    resolving the listed paths (the resolver's act, no git), conclude refused once with a marker
    left in, then concluded. Asserts per the prime exit criterion's expected_when_true. Measure
    each op's process time and spawns (merge child separate) and record them with
    `plan.tasks.mutate evidence-append`, never in the plan body; add a `step0-reconcile-conclude`
    row to `step_costs.json` with the measured class.
```
