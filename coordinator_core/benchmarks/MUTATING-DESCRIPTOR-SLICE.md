# MUTATING op neutralization descriptor — Phase-0-sized follow-slice

> Spec backlink: `docs/plans/2026-07-10-qsub-01-latency-benchmark-harness.md` chunk C10.
> This document **defines** a descriptor schema for safely including MUTATING ops in the
> latency-benchmark harness. It enumerates all 30 live MUTATING ops and classifies each by
> neutralization envelope. **It does not implement anything** — no code, no smoke test, no
> harness change. Building the neutralization runner (a `MutatingOpRunner` or equivalent that
> actually drives these envelopes end-to-end) is out of scope for wave-1 and is named here as a
> dedicated Phase-0-sized follow-slice (AC7) for a future plan.

## Why this exists

Wave-1 of the latency-benchmark harness (this plan) measures only the 20 `COMPUTE_ONLY` ops —
those are safe to invoke repeatedly against a fixture repo with no state mutation. The other 30
ops classified `OpClass.MUTATING` in `coordinator_core/authz/classification.py` write to disk
(frontmatter mutation, JSONL append, archival moves, git operations, shell-script delegation).
Benchmarking them naively would corrupt fixture state on every sample and conflate "op is slow"
with "op is slow AND now the fixture is poisoned for the next sample." A harness that wants
end-to-end latency numbers for MUTATING ops needs each sample's side effects **neutralized** —
either by using the op's own preview/no-write mode, or by running the mutation for real against
a disposable copy of the fixture that is discarded after the sample.

## Descriptor schema

Each MUTATING op gets one descriptor:

```
{
  op: <str>,                          # the OP_CLASSIFICATION key, e.g. "fleet.archive_completed_plans"
  side_effect_neutralization: {
    kind: <one of the 4 enum values below>,
    params: <dict>                    # kind-specific — see § Kind semantics
  }
}
```

### Kind semantics

| `kind` | Meaning | `params` shape |
|---|---|---|
| `dry_run_param` | Op accepts a `dry_run: bool` (or op-specific preview toggle) that short-circuits all writes and returns a preview/candidate-set result. Benchmark by invoking with the toggle set to the no-write value. | `{toggle_key: str, no_write_value: <bool>}` |
| `write_param` | Op accepts a param that gates whether writes occur (not literally named `dry_run`, e.g. `write: bool`). Same neutralization mechanic as `dry_run_param`, different param name — kept as a separate kind because the wire contract differs from the fleet `{mode, dry_run, candidate_ids}` two-phase envelope (per each op's own docstring, e.g. `handoff_normalize.py`: "Does NOT use the fleet {mode, dry_run, candidate_ids} envelope"). | `{toggle_key: str, no_write_value: <bool>}` |
| `temp_worktree` | Op has no native no-write mode — it always writes when invoked. Neutralization requires running the op for real against an ephemeral throwaway worktree (git worktree or tmpdir clone of the fixture repo) that is torn down after the sample, so the mutation never touches the fixture used by other samples/ops. | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` |
| `compute_only_noop` | Reserved for ops whose classification is MUTATING by contract/authorization posture but whose actual body performs no disk write for a specific parameter shape (none identified in the live 30 — kept in the enum for schema completeness / future ops). | `{}` |

## Live MUTATING op count (verified against disk)

```
$ python -c "from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass; \
  print(len([o for o,c in OP_CLASSIFICATION.items() if c==OpClass.MUTATING]))"
30
```

Table below enumerates all 30 — count matches the live query above.

## Full enumeration (30/30)

| # | op | kind | params | evidence |
|---|---|---|---|---|
| 1 | `fleet.archive_actioned_memos` | `dry_run_param` | `{toggle_key: "dry_run", no_write_value: true}` | `coordinator_core/ops/fleet/archive_actioned_memos.py` — fleet `{mode, dry_run, candidate_ids}` two-phase envelope; `dry_run:true` → T1 preview (enumerate only), `dry_run:false` → T3 act |
| 2 | `fleet.archive_completed_handoffs` | `dry_run_param` | `{toggle_key: "dry_run", no_write_value: true}` | `coordinator_core/ops/fleet/archive_handoffs.py` — same fleet two-phase envelope; `_handle_preview_handoffs` (dry_run:true) vs `_handle_act_handoffs` (dry_run:false) |
| 3 | `fleet.archive_completed_plans` | `dry_run_param` | `{toggle_key: "dry_run", no_write_value: true}` | `coordinator_core/ops/fleet/archive_plans.py` — module docstring: "(dry_run:true / dry_run:false) wire contract" |
| 4 | `fleet.archive_shipped_handoffs` | `dry_run_param` | `{toggle_key: "dry_run", no_write_value: true}` | `coordinator_core/ops/fleet/archive_shipped_handoffs.py` — `_scan_shipped` (preview) vs `_handle_act` (dry_run:false act path) |
| 5 | `fleet.prune_closed_bugs` | `dry_run_param` | `{toggle_key: "dry_run", no_write_value: true}` | `coordinator_core/ops/fleet/prune_bugs.py` — module docstring: "confirm→act (dry_run:true / dry_run:false)"; `dry_run:true → candidates[] of closed bugs; mutates nothing" |
| 6 | `memo.send` | `dry_run_param` | `{toggle_key: "dry_run", no_write_value: true}` | `coordinator_core/ops/fleet/memo_send.py:297,303` — "Required params: dry_run (bool), topic (slug), to (str), title (str), body (str)"; validated as required bool |
| 7 | `handoff.normalize` | `write_param` | `{toggle_key: "write", no_write_value: false}` | `coordinator_core/ops/handoff_normalize.py:336` — `write: bool = bool(params.get("write", False))`; docstring: "dry_run (bool) — True if this was a dry-run (write=False or write absent)"; explicitly NOT the fleet envelope |
| 8 | `artifact.emit` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/artifact_emit.py` — no dry_run/write param; writes canonical work-state snapshot to disk unconditionally |
| 9 | `backlog.record` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/emit/recorder.py` — no toggle; single direct write to per-machine JSONL shard every invocation |
| 10 | `ceremony.wsc_commit` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/ceremony/wsc_commit.py` — orchestration op that runs a shell op (`_run_shell_op`), fleet sub-ops in two-phase (`_run_fleet_op_two_phase`), and a coverage gate; no single top-level toggle neutralizes the whole op |
| 11 | `ceremony.wsc_resolve` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/ceremony/wsc_resolve.py` — module docstring: "Emits state/ceremony/wsc/<sid-short>-<emitted_at>.json (phase-2, overwrites...)"; runs git log/diff scans and emits unconditionally, no toggle |
| 12 | `changelog.append_day` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/changelog_ops.py` — `_append_day_handler`; no dry_run/write param, `_atomic_write` fires unconditionally |
| 13 | `changelog.backfill_gaps` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/changelog_ops.py` — `_backfill_gaps_handler`; no toggle param, writes daily files for each gap found |
| 14 | `completion.reconcile_commits` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/completion_ops.py` — `_reconcile_commits_handler`; no toggle param, folds commits into content unconditionally |
| 15 | `goal.append` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/goal_append.py` — `_goal_append`; no toggle param, appends unconditionally |
| 16 | `handoff.author_fork` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/handoff_author_fork.py` — `_handler`; params are content fields (title/branch/kind/workstream/body/origin_*), no write-gating toggle; always authors a new handoff file |
| 17 | `handoff.stamp` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/handoff_stamp.py` — docstring: "Does NOT use the fleet mode/dry_run/candidate_ids envelope (this is a [single-target write])"; always mutates on success |
| 18 | `handoff.transition` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/handoff_transition.py` — docstring: "Does NOT use the fleet _common.py {mode, dry_run, candidate_ids} envelope"; verb (consume/supersede/ship) always mutates the target handoff |
| 19 | `hooks.agent_completion_log` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/hooks/agent_completion_log.py` — `_append_audit_entry`; no toggle param, appends unconditionally |
| 20 | `hooks.session_heartbeat` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/hooks/session_heartbeat.py` — `_handler`; no toggle param, touches heartbeat file unconditionally |
| 21 | `hooks.track_dispatched_agents` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/hooks/track_dispatched_agents.py` — `_process_dispatched_locked`/`_write_backpointer_sync`; no toggle param, writes backpointer + mutates dispatch tracker unconditionally |
| 22 | `hooks.track_touched_files` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/hooks/track_touched_files.py` — `_dedup_append_locked`; no toggle param, appends touched-file entry unconditionally |
| 23 | `memo.transition` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/memo_transition.py` — verb (claim/action/release) selects mutation shape, no write-gating toggle across all three; each verb's `_mutate` always fires |
| 24 | `plan.append_session` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/completion_ops.py` — `_append_session_handler`; no toggle param, appends session block unconditionally |
| 25 | `plan.tasks.mutate` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/plan_tasks_mutate.py` — verb-driven (params: verb/plan_path/task/updates), no write-gating toggle; mutation always applied on success |
| 26 | `queue.append` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/queue_append.py` — schema-validated field write; no toggle param, `_build_yaml`+write fires unconditionally |
| 27 | `queue.promote` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/queue_promote.py` — no toggle param; promotion writes unconditionally on success |
| 28 | `review_trail.write` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/review_trail_write.py` — `_review_trail_write_handler`; no toggle param, `_build_json_record` written unconditionally to trail dir |
| 29 | `session.boot_sweep` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/session/boot_sweep.py:46,50` — module docstring explicit: "Does NOT expose a dry_run/candidate_ids two-phase round-trip... Does NOT use the fleet mode/dry_run/candidates wire envelope" |
| 30 | `session.reap` | `temp_worktree` | `{setup: "git-worktree-add-fixture-clone", teardown: "rm-worktree-after-sample"}` | `coordinator_core/ops/session/reap.py:44` — module docstring explicit: "Does NOT expose a dry_run/candidate_ids two-phase contract — this is not a fleet op" |

## Summary by kind

| kind | count | ops |
|---|---|---|
| `dry_run_param` | 6 | `fleet.archive_actioned_memos`, `fleet.archive_completed_handoffs`, `fleet.archive_completed_plans`, `fleet.archive_shipped_handoffs`, `fleet.prune_closed_bugs`, `memo.send` |
| `write_param` | 1 | `handoff.normalize` |
| `temp_worktree` | 23 | all remaining ops (see table rows 8-30) |
| `compute_only_noop` | 0 | none identified in the live 30; kind retained for schema completeness |
| **Total** | **30** | matches live `OP_CLASSIFICATION` MUTATING count |

## Explicitly out of scope for wave-1 (this plan)

This document is a **definition-only** artifact. Wave-1 of the latency-benchmark harness
(`coordinator_core/benchmarks/`) invokes only the 20 `COMPUTE_ONLY` ops and does **not**:

- implement a `temp_worktree` setup/teardown runner,
- implement dry-run/write-param invocation wiring for the 7 ops with native toggles,
- add any of the 30 MUTATING ops to the benchmark's op-list, budget manifest, or gate logic,
- run any smoke test against a MUTATING op.

Building that neutralization runner and wiring the 30 ops into the harness is a **dedicated
Phase-0-sized follow-slice** for a future plan — comparable in size to this plan's own Phase 0
(harness scaffolding), not a same-plan extension. The `temp_worktree` majority (23/30 ops) in
particular implies real git-worktree lifecycle management per sample, which is materially more
engineering than the `COMPUTE_ONLY` fixture-repo-reuse model this plan's wave-1 harness uses.
