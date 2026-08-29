# coordinator_core — Directory Guide

In-process command-type coordinator engine: a JSON-RPC 2.0 op-registry + dispatch surface
for the coordinator plugin. Relocated from `~/.claude` (stop-the-rot C7, 2026-07-03).
Maintained by `/update-docs` — regenerate rather than hand-edit structural sections.

**Coverage note (2026-07-19):** the `ops/` table below (and, to a lesser extent,
`ops/emit/`, `ops/fleet/`) predates the BIG_PORT waves and the strang-* strangler
sequence — a `find coordinator_core/ops -name '*.py'` vs. the table below shows the
table covering roughly a third of the op modules actually on disk (workday/workweek
checks, setup/install helpers, cartography, roadmap/goal ops, verify-*/check-* scripts,
and their paired tests are the largest undocumented clusters). Full reconciliation is a
fan-out-shaped job (one directory-scout per subpackage, per this doc's own Phase-2
instructions) rather than a single-pass inline edit — flagged to the EM/PM rather than
half-done here. Last spot-checked: 2026-07-19.

Last refreshed: 2026-08-06 (spot-check only — see coverage note above).

## Top-level modules

| Module | Purpose |
|---|---|
| `__init__.py` | Package entry — op-registry + dispatch surface exports |
| `__main__.py` | CLI/module entrypoint |
| `ipc.py` | JSON-RPC 2.0 in-process dispatch + op-registry seam (`register_op`, `dispatch_message`) |
| `dag.py` | Python port of `bin/lib/walk-handoff-dag.js` — DAG-walker |
| `coverage.py` | Review-coverage core (SAFE_RANGE/trail-parse/verdict-filter) + gate (DAG-vs-flat, fixpoint, scope-filter, verdict-line) engine |
| `lifecycle.py` | Repo-root utilities and version helpers (resident-daemon machinery stripped by C3) |
| `liveness.py` | Canonical liveness seam — bridges to authoritative Bash liveness predicates |
| `cache.py` | Content-hash-keyed revalidating read cache (sha256 stamp primitive) |
| `archival.py` | Reverse-membership guard for the handoff archival pipeline |
| `locked_write.py` | Cross-process file-level RMW lock helper (`locked_rmw`; fcntl on POSIX, msvcrt on Windows) |
| `doctor_envelope.py` | Shared probe-verdict envelope schema + reducer (BROKEN/DEGRADED/INFO/PASS) |
| `_settings_home.py` | Bootstrap-safe `coordinator-settings-home` precedence resolver (zero external calls) |
| `engine_version.py` | Engine self-version surface — resolves the running commit for receipt-stamping/drift-probe consumers |
| `git_scope.py` | Foreign-repo git probing — strips the repo-scoping env `git -C` does not override, confines the resolved git dir, and returns a yes/no/**unknown** tri-state |

## Sub-packages

### `ops/` — IPC operation handlers
Each sub-module self-registers its op via `register_op()` at import time.

| File | Op | Purpose |
|---|---|---|
| `_fm_util.py` | — | Shared frontmatter scalar extraction primitive |
| `_path_guard.py` | — | Shared caller-supplied-path containment helpers (generalized from `handoff_lineage_ancestry.py`) |
| `assert_doctrine_cross_reference_counts.py` | `doctrine.assert_cross_reference_counts` | Read-only doctrine cross-reference count assertion over the caller's skills/wiki doctrine tree |
| `cartography_stack.py` | `cartography.stack` | Read-only project-stack fingerprint (languages, test frameworks, config files) via pathlib scan |
| `changelog_ops.py` | — | Family-A changelog write ops (strang-10 C1) |
| `commit_anchors.py` | `commit.anchors` | COMPUTE_ONLY — derives git-trailer block from read-model + staged diff |
| `completion_nature.py` | — | Heuristic completion-nature classifier (replaces per-session Sonnet dispatch) |
| `completion_ops.py` | — | In-place mutators for completion-entry commits reconciliation + plan session appending |
| `copy_plugin_template.py` | `repo_setup.copy_console_subprocess_tripwire` | Content-idempotent template copy (write only if absent/byte-identical) + pytest smoke check |
| `create_github_remote.py` | `repo.create_and_push_remote` | Idempotent GitHub remote creation + first push (existing-remote pre-check) |
| `deferral_detect_orphan_memo.py` | `deferral.detect_orphan_memo` | Detector: actionable+open+aging cross-repo inbox memo with no owning plan/baton/DR (offers-not-nags). |
| `deferral_detect_partial_strangle.py` | `deferral.detect_partial_strangle` | Detector: strangler migration with declared verbs neither shipped nor planned — hidden scope-deferral (offers-not-nags). |
| `deliverable_rollup.py` | `deliverable.rollup` | COMPUTE_ONLY — scans deliverable-spine read-model by `deliverable_id` |
| `detect_changed_dependency_manifests.py` | `dependency.detect_changed_manifests` | Detects whether a repo's dependency manifests changed within a since-days window |
| `detect_plugin_layout.py` | `detect.plugin_layout` | Detects flat vs. nested plugin directory layout via a docs/install/AGENT.md existence check |
| `detect_primary_languages.py` | `detect.primary_languages` | Read-only file-extension tally (pathlib.rglob + Counter) to detect a repo's primary language(s) |
| `engine_drift.py` | `engine.drift` | Read-only three-state drift probe — running engine SHA vs. `MIN_KNOWN_GOOD_SHA` floor |
| `goal_append.py` | `goal.append` | Appends a goal-event record |
| `goals_match.py` | `goal.match_candidates` | Read-only ranked resolver over `state/goals/*.md` |
| `handoff_author_fork.py` | `handoff.author_fork` | MUTATING — generates fork/spinoff handoff artifact |
| `handoff_children.py` | `handoff.has_live_children` | In-memory walk detecting whether a handoff has live (non-terminal) children |
| `handoff_close_origin_stub.py` | `handoff.close_origin_stub` | Closes an origin stub on ship + roadmap-baton join-fix (walks `{predecessor, origin_handoff}` to the nearest `spinoff`/`spinoff-roadmap` ancestor) |
| `handoff_discharge_criteria.py` | `handoff.discharge_criteria` | Bounded wrapper over `handoff.correct_body` for ticking/splitting `## Acceptance criteria` checkboxes by criterion identity (`AC-N`) or structural position, never raw line text — DR-274 §D3-sanctioned second body-mutating verb |
| `handoff_lineage_ancestry.py` | `handoff.lineage_ancestry` | Read-only — walks a fork handoff's `origin_handoff` chain to compute ancestry |
| `handoff_match.py` | `handoff.match_candidates` | Read-only ranked resolver over `state/handoffs/*.md` |
| `handoff_normalize.py` | `handoff.normalize` | Port of `normalize-handoff-frontmatter.js` |
| `handoff_reconcile.py` | `handoff.reconcile_open` | Auto-reconcile orchestrator — enumerates the widened consumed+non-terminal dead zone (`consumed`+`in_flight`, not only `active`) and reconciles pinned predecessor chains on ship, routing C3 (`gate_eval`) verdicts to `ship_and_archive`/`gate-cascade-clear` or `surfaced[]`; the C2 (`commit_reality`) route is DELETED (C10, `state/kill-ledger.md`, 2026-08-26) — no longer "turnable-on"; op name RATIFIED (DoE 2026-07-13); forward-compatible subset of DoE's lvv-04/C3 fleet-archive fix (cc'd, not colliding) |
| `handoff_stamp.py` | `handoff.stamp` | Port of `stamp-shipped-in.js` |
| `handoff_transition.py` | `handoff.transition` | Port of `handoff-transition.js` — atomic lifecycle frontmatter mutation |
| `hibernate_machine.py` | `machine.hibernate` | Cross-platform (macOS pmset/Windows/Linux systemctl) machine hibernate dispatch, no shell-out |
| `init_anchor_injection_state.py` | `ceremony.init_anchor_injection_state` | Resolves the coordinator-claude/DoE root + today's date and initializes the empty anchor-injection accumulator |
| `initiatives_serve.py` | `initiative.serve_set` | Read-only — serves attachable-initiative set |
| `lessons_filter.py` | `lessons.filter_undated_universal`, `lessons.reject_orphan_strip_entries` | learn-lessons routing-set filters — undated-universal-lesson filter + orphan strip-list-entry rejection |
| `list_files_newer_than_marker.py` | `percolate.list_files_newer_than_marker` | Lists files newer than a marker file (`Path.stat().st_mtime` comparison), capped at 20 results |
| `match_core.py` | — | Shared difflib-based candidate ranking kernel (used by `goals_match`, siblings) |
| `memo_transition.py` | `memo.transition` | Native port of `memo-transition.js` |
| `merge_branch_into_workstream.py` | `branch.merge_into_workstream` | Idempotent branch consolidation into the active workstream branch (merge-base ancestor pre-check + `merge --abort` on conflict) |
| `merge_quiet_activity_gate.py` | `merge.quiet_activity_gate` | Read-only quiet-activity gate comparing now vs. the caller's most recent commit timestamp |
| `parse_cli_args.py` | `cli.parse_flag`, `cli.parse_date_flags` | Native argparse/regex replacements for the sed/case-based CLI flag + date-flag parsers |
| `percolate_check_inverse_drift.py` | `percolate.check_inverse_drift` | Read-only git-log query for inverse drift on a publish dest since last sync |
| `percolate_ci_smoke_check.py` | `percolate.run_ci_smoke_check` | Invokes a publish target's optional CI smoke-check script (skips, doesn't error, when absent) |
| `percolate_run.py` | `percolate.run` | Thin RPC wrapper over `percolate/engine.py`'s phase-model orchestrator (pre_rsync/post_rsync/pre_ci) |
| `percolate_validate.py` | `percolate.validate_store` | Validates a consumer-owned percolation store against the current vendored schema |
| `ping.py` | `ping` | Cheapest registered COMPUTE_ONLY op |
| `plan_match.py` | `plan.match_candidates` | Read-only ranked resolver over `docs/plans/*.md` |
| `plan_tasks_mutate.py` | `plan.tasks.mutate` | Authoritative mutation of a plan's `## Tasks` task-spine (`add-task`/`stamp` verbs) |
| `poll_scratch_dir.py` | `fanout.poll_scratch_dir` | Blocks until a scratch dir holds ≥N entries or a timeout elapses (`time.monotonic()` loop, no shell) |
| `probe_fresh_repo_noop.py` | `update_docs.probe_fresh_repo_noop` | Read-only fresh-repo probe (DIRECTORY.md/archive/tasks existence) for `/update-docs` no-op detection |
| `queue_append.py` | `queue.append` | Port of `coordinator-queue-append` — one YAML entry writer |
| `queue_promote.py` | `queue.promote` | Port of `coordinator-lesson-promote` — lessons-outbox appender |
| `records_query.py` | `records.query` | COMPUTE_ONLY — queries claude-klabauter's own state records |
| `release_tagging.py` | `release.cut_tag`, `release.cut_tag_and_publish` | Idempotent annotated-tag cutting, with an optional GitHub-release publish step |
| `repo_bootstrap.py` | `repo.clone_and_register` | Clones + registers a sibling repo in the operator's machine-local registry (guarded no-op on already-present) |
| `research_archive_workdir.py` | `research.archive_workdir` | Idempotently archives a completed research run's workdir into `docs/research/archive/` (`os.rename` with EXDEV fallback) |
| `research_dir_restructure.py` | `research.restructure_for_repeat_topic` | Resumable two-step restructure of `docs/research/` for a repeat-topic run (per-step already-done skip) |
| `resolve_baton_path.py` | `baton.resolve_path_and_repo` | Resolves a caller-supplied baton path to its absolute native form + owning git repo |
| `resolve_mcp_server_cli_path.py` | `mcp.resolve_server_cli_path` | Resolves an MCP server's CLI path + project root from `~/.claude.json` |
| `resolve_swept_baton.py` | `baton.resolve_swept_in_archive` | Finds a swept (already-archived) baton by basename across the three known archive dirs |
| `roadmap_dag.py` | — | Pure derivation helper — builds per-roadmap `{nodes, edges, roll_up, critical_path}` |
| `roadmap_serve.py` | `roadmap.serve` | Read-only single-initiative DAG view |
| `run_pip_audit.py` | `ci.run_pip_audit` | Runs pip-audit against a lock file, with optional `--extra-index-url` detection for non-PyPI wheel sources |
| `bootstrap_repo.py` | `repo_setup.validate_target_root` | Git-as-revert bootstrap primitive for one target repo (full port; DoE `.sh` becomes a thin polyglot trampoline over this) |
| `self_persist_findings.py` | `findings.self_persist_fallback` | Native port of the ad hoc `python3 -c` write / bash-heredoc fallback for findings reports with escaping-hostile content |
| `write_identity_file.py` | `install.write_identity_file` | Native port of the `mktemp`+heredoc+`mv` shell fence that persists `~/.claude/coordinator-identity.yaml`, unifying two call sites into one fields-dict write |
| `workday_stitch_sidecar_summary.py` | `workday.stitch_sidecar_into_summary` | Port of the `commands/workday-complete.md` fence — atomically stitches a daily-observer sidecar into the day's summary file, then deletes the sidecar |
| `run_pre_ci_hooks.py` | `percolate.run_pre_ci_hooks` | Discovers and invokes registered pre-CI publish hooks for a percolate target (native Python hook contract) |
| `run_semgrep_scan.py` | `ci.run_semgrep_scan` | Runs a tiered semgrep scan over a diff scope, native `shutil.which` fallback-tier dispatch |
| `run_shellcheck_sweep.py` | `ci.run_shellcheck_sweep` | Runs shellcheck over the caller's own worktree's tracked `.sh` files |
| `scan_content_leakage.py` | `percolate.scan_content_leakage_tiers` | Three-tier (HIGH/MEDIUM/LOW) content-leakage regex sweep over an about-to-publish tree |
| `schema_drift_gate.py` | `schema.drift_gate` | GATING reduction of `schema_drift_watch.scan_vendored_schema_drift()` to a pass/fail verdict — blocks only on a positively observed DRIFT, never on INDETERMINATE/UNRESOLVED |
| `session_context.py` | — | Shared `resolve_current_session_id(worktree_root)` resolver |
| `verify_fix_files_changed.py` | `bug_sweep.verify_fix_files_changed` | Read-only comparison of a fix-manifest's claimed-fixed files against `git diff --name-only` |
| `verify_scout_inventory_completeness.py` | `research.verify_scout_inventory_completeness` | Read-only disk-first existence/line-count check of expected scout inventory files |
| `workday_surface_auto_push_failure_stats.py` | `workday.surface_auto_push_failure_stats` | Read-only 24h aggregation over the caller's `.git/push-failures.log` |

### `ops/emit/` — Cockpit-emission stack
Spine in `context.py`/`validate.py`; per-entity porters under `sections/`.

| File | Purpose |
|---|---|
| `_slug.py` | Shared hostname slug helper |
| `backlog_history.py` | Backlog-history block assembly (C5) |
| `context.py` | `EmitContext` + provenance envelope builder |
| `deliverable_status.py` | §8.16 `deliverable_status` cross-entity join |
| `doe_drift.py` | DoE-HEAD conformance fixture resolver + drift-check |
| `enrich.py` | Parallel, order-preserving last-modified-at enrichment |
| `resolvers.py` | Run-context resolution, root resolvers, git-ancestor / shipped-on-main helpers (not an emitter — the writer half was cut 2026-08-22/23) |
| `normalizers.py` | Shared AC5-PROVENANCE normalization utilities |
| `recorder.py` | Backlog-history recorder (`backlog.record` op) |
| `validate.py` | Zod validation against the vendored contract pin |

`ops/emit/sections/` — one porter module per cockpit entity family (envelope key noted):
`backlogs` (backlogs), `branch` (branches), `coordinator_roots` (coordinator_roots),
`cross_repo_memos` (cross_repo_memos), `decision_guides` (decision_guides),
`exec_summary` (exec_summaries),
`goals` (goals_current), `handoffs` (handoffs), `health` (health),
`initiatives` (initiatives), `lessons` (lessons), `plans` (plans),
`review_trail` (review_trail), `roadmap_dag` (roadmap_dag_nodes/edges),
`roadmaps` (roadmaps), `rollups` (completion_rollups), `routine_signals` (routine_signals,
6 fixed records), `session_hierarchy` (session_hierarchies), `trackers` (trackers).
`_shared.py` holds constants/helpers used across ≥2 porters.

`competitor_summaries` / `intelligence_signals` (cockpit-contract v2.16.0) have NO porter —
present-but-empty-by-design in `resolvers.py`'s skeleton. Claude-klabauter is not the data path for
Example-market-data-repo (routes to cockpit `ingestEmission` directly); do not add a section here.

### `ops/fleet/` — fleet.* MUTATING archival ops
Confirm→act (`dry_run:true`/`dry_run:false`) wire contract; git-mv terminal artifacts into `archive/`.

| File | Op | Purpose |
|---|---|---|
| `_common.py` | — | Shared helpers for fleet.* ops |
| `_findings_reap.py` | — | Shared polarity-agnostic scan/act core (`scan_findings`/`reap_findings`) consumed by both review-findings reap legs (DR-218) |
| `archive_paper_trail.py` | `fleet.archive_paper_trail` | Archives a research-session paper-trail workdir into the caller's own `docs/research/archive/` tree, dry_run/act contract |
| `archive_queue_entry.py` | `fleet.archive_queue_entry` | Single-entry git-mv of one closed `state/improvement-queue/*.yaml` entry into `archive/improvement-queue/YYYY-MM/`, dry_run/act contract |
| `archive_release_accumulator.py` | `fleet.archive_release_accumulator` | git-mv's the most recent `state/week-changelog/*-pending-release.md` accumulator into `archive/release-notes/` under a tag-suffixed name, if one exists |
| `archive_terminal_handoffs.py` | `fleet.archive_completed_handoffs` | Cap-bounded sweep of terminal, childless, unclaimed handoffs (subsumes the former deployment-axis-only `archive_shipped_handoffs.py`, deleted C1b) |
| `memo_send.py` | `memo.send` | MUTATING UDS op handler for cross-repo memo send |
| `prune_bugs.py` | `fleet.prune_closed_bugs` | Prunes closed bug-backlog entries |
| `reap_integrated_findings.py` | `fleet.reap_integrated_findings` | Reaps marker-present (integrated) review-findings sidecars from `state/review-trail/findings/`, no age gate — leg (a) of the DR-218 review-trail cleanup split; custom (non-two-phase) result shape |
| `reap_unintegrated_findings.py` | `fleet.reap_unintegrated_findings` | Reaps aged (>14d), marker-absent (unintegrated) review-findings sidecars from `state/review-trail/findings/` — leg (b) of the DR-218 review-trail cleanup split; custom (non-two-phase) result shape |

### `ops/ceremony/` — Ceremony-as-pipeline ops
Home of `ceremony.wsc_tail` and the shared ceremony schema/context data models.
`wsc_commit.py` and `wsc_resolve.py` were deleted 2026-07-29 (kill-list op
removal, both ops' JSON-RPC registrations superseded by `ceremony.wsc_tail`
with no live caller) — neither is on disk. `wsc_resolve.py`'s engine (still
live — imported directly by `ceremony.session_instructions`) survives as
`branch_resolution.py`.

| File | Op | Purpose |
|---|---|---|
| `commit_exec_bit.py` | `commit.exec_bit_change` | Windows-safe exec-bit change + commit (DR-151 footgun), skips if already executable |
| `node_handlers.py` | — | Handler library for D/J/F/B/X node types |
| `pipeline_context.py` | — | Ceremony resolved-state data model |
| `receipt_emit.py` | — | Ceremony evidence receipt emitter |
| `receipt_schema.py` | — | Ceremony evidence receipt schema |
| `snapshot_diff_and_head.py` | `review.snapshot_diff_and_head` | Idempotency-token-keyed git range-diff + HEAD SHA snapshot for ceremony use |
| `branch_resolution.py` | — | Branch-resolution engine (session-shape read, 17-branch resolution) — the surviving engine of the retired `ceremony.wsc_resolve` op; imported live by `ceremony.session_instructions` |
| `git_native.py` | — | Windows-safe shared `git` subprocess helper (`_git`) — every native git call in the `wsc_tail` rebuild routes through this single choke point (creationflags/stdin/capture_output/text) |
| `commit_message.py` | — | Commit-message composer + dual path-set computation — pure functions, no I/O |
| `commit_gates.py` | — | Deletion-block and dirty-tree classification gates |
| `resolver.py` | — | Public `resolve_in_repo`/`find_all_consumed_handoffs`/`get_handoff_consumed_by` helpers shared by `branch_resolution.py` and the `wsc_tail` rebuild |
| `consumed_handoff_stamp.py` | — | Consumed-handoff ship-stamp + R1-R4 ship-drift correctness — `post_commit_stamp_and_ship()`, called by the commit pipeline with no ceremony-wide lock held |
| `tail_ops.py` | — | Reused tail-op wiring (`coverage.gate`/`review_trail.write`) plus native `cs_archive`/`cs_release_artifact` ports; archive sweeps fire DETACHED via `fire_archive_sweeps_detached` (C2, 2026-07-23), not in-process |
| `completion_entry.py` | — | Native completion-entry scaffold (op 0) + residue fill (op D2), no bash/node spawn |
| `records_query.py` | — | In-process frontmatter enumerate + equality-AND `where`-filter over handoff/handoff-archived/cross-repo-memo records — read-side foundation for `renderers.py` |
| `renderers.py` | — | Pure-Python ports of `render-handoff-tracker.js` and `refresh-queries.js`'s roadmap-callout BEGIN/END query-block regen |
| `wsc_tail.py` | `ceremony.wsc_tail` | Single-pass orchestrator — sequences resolve, pre-commit tail ops, locked stage/commit/push, post-commit consumed-handoff ship-stamp, and receipt, entirely in-process |

### `ops/introspect/` — read-only, cross-signal "is this shipped?" primitives
Answers questions about live repo state; never mutates. Distinct from `ops/emit/`
(assembles/writes the cockpit snapshot) and `ops/fleet/` (archives terminal artifacts).

| File | Purpose |
|---|---|
| `verify_shipped.py` | `verify_shipped(ref_or_sha, plan_path=None)` — combines git ancestry (`ops/emit/resolvers.py`'s promoted `check_origin_main_reachable`/`sha_on_origin_main`/`resolve_ref`), live plan/handoff frontmatter, and a leg-3 `state/cockpit-emission.json` cross-check that can no longer fire (the artifact was deleted 2026-08-23, DR-351 — the leg degrades to None by design and is now permanently silent) into one `ShipVerdict`, keeping disagreement between signals visible (`verdict: shipped/not_shipped/disagreement/indeterminate`) rather than collapsing to a boolean |

### `ops/session/` — Class-B session-substrate ops
Ops mutating untracked `.git/coordinator-sessions/` substrate — do NOT use `git commit` /
`archive_and_commit`; carry their own safety spec.

| File | Op | Purpose |
|---|---|---|
| `boot_sweep.py` | `session.boot_sweep` | Boot-time orphan sweep |
| `reap.py` | `session.reap` | Class-B cadence-gated reaper |
| `record_pickup.py` | `session.record_pickup` | Append-only/versioned pickup write (DR-059 port of bash `cs_session_shape_set`'s pickup field) |
| `resolve_chain_terminal_disposition.py` | `session.resolve_chain_terminal_disposition` | Native classification of session/chain terminal disposition (5-way session-id resolution + dual-detector reads, no grep/awk) |
| `rotate_orphan_sweep_log.py` | `session.rotate_orphan_sweep_log` | Atomic (`locked_rmw`) tail/head rotation of `tasks/orphan-sweep-notes.md` |

### `install/` — install-chain op handlers (BIG_PORT wave-2 additions)
Coverage note: this section lists only the wave-2 BIG_PORT-ported op modules — the pre-existing
`install/` subpackage (`first_run.py`, `substrate.py`, `maximalist.py`, etc.) predates this table
and is not yet reconciled (see the top-of-file Coverage note).

| File | Op | Purpose |
|---|---|---|
| `clone_sibling_repo.py` | `install.clone_idempotent` | Idempotent sibling-repo clone (guards on `.git` presence before cloning) |
| `prereq_probe.py` | `install.probe_skill_frontmatter_valid`, `install.probe_windows_terminal_presence` (+ sibling probes) | SSOT functional-prerequisite probe suite for the coordinator install Step Zero gate |
| `shell_rc_guard.py` | `install.write_shell_rc_guard_block` | Idempotent sentinel-guarded CLAUDE_KLABAUTER_CLONE shell-rc block installer |
| `wrapper_onto_path.py` | `install.wrapper_onto_path` | Content-idempotent wrapper-executable copy onto the operator's per-user bin dir + PATH-membership check |

### `reconcile/` — auto-reconcile compute engines (COMPUTE_ONLY, DR-208)
Pure read+compute modules consumed by `ops/handoff_reconcile.py`'s orchestrator; none of these
write files, git objects, or frontmatter — `handoff.reconcile_open` (via `ship_and_archive`/C8's
`gate-cascade-clear`) is the only authorized actor on their verdicts. See
`coordinator_core/contract/handoff-reconcile-producer-contract.md` for the wire-level pin.

| File | Purpose |
|---|---|
| `commit_reality.py` | helper residue for `archive_stamp` and `completion_ops`; no public entry point; `_git` scheduled for rehoming (DEC-1 three-signal shipped-ness matcher deleted C10, `state/kill-ledger.md`, 2026-08-26) |
| `gate_eval.py` | Unified gate evaluator (`evaluate_gate`) — structured `blocked_by` DAG-edge path + prose `gate_dependency` fallback path, `clear`/`narrow`/`surface`/`not-cleared` verdicts |
| `policy_loader.py` | DoE-owned `auto-reconcile-policy.yaml` reader (`load_policy`) — fail-closed (absent=silent, malformed=warned) against the C9 grammar pin |

### `benchmarks/` — qsub-01 per-op latency benchmark harness (COMPUTE_ONLY, offline)
CLI-driven N-iteration benchmark loop measuring per-op end-to-end invocation latency against
the DR-215 per-op budget; produces code_sha-keyed conformance records for the qsub-03 gate.

| File | Purpose |
|---|---|
| `__main__.py` | CLI runner for the per-op latency benchmark harness |
| `harness.py` | Per-op N-iteration benchmark loop + record assembly |
| `timer.py` | Spawn-to-exit timing primitive |
| `budget.py` | Two-level per-op latency budget resolver |
| `floor.py` | Cold-start floor probe |
| `gate.py` | Gate verdict — pure function mapping (observed_statistic, target, tolerance, N) → verdict |
| `record.py` | Conformance-record data model — the qsub-01↔qsub-03 contract surface |
| `baseline_store.py` | Append-only, code_sha-keyed conformance-record baseline store |
| `op_fixtures.py` | Per-op fixture/input generators for the benchmark loop |

### `percolate/` — generic percolation engine (data-driven, per-consumer store)
Four-phase (pre_rsync/post_rsync/pre_ci) orchestrator over transform-kinds; driven by
`percolate_run.py`/`percolate_validate.py` in `ops/`. Byte-parity with the retired DoE-claude
v3 shell scripts, verified via golden fixtures.

| File | Purpose |
|---|---|
| `engine.py` | Phase-model orchestrator — dispatches per-transform-kind hooks against a target tree |
| `store.py` | Vendored+drift-gated store schema, loader/validator with target inheritance |
| `guards.py` | Declarative guard/assert transform-kind checks |
| `substitute.py` | Exact-substitution transform-kind (longest-first, match-mode auto-detect) |
| `depersonalize.py` | Depersonalize transform-kind — codename map + stem + provenance |
| `rewrite_stem.py` / `rewrite_path.py` / `rewrite_basename.py` | Structural-rewrite transform-kinds (stem, path+discovery, basename+manifest) |
| `surface.py` | File-surface discovery (include globs + shebang-sniff, exclude, binary-guard, symlink decision) |
| `inject.py` | Inject/preserve transform-kind — class-c drain-stdin + paired-sentinel preserve-destination-native |

### `authz/` — invoke-op write-semantics classification
| File | Purpose |
|---|---|
| `classification.py` | Op write-semantics classification registry (`OpClass`, `OP_CLASSIFICATION`, `classify`) |
| `contract.py` | Vacated two-tier token authorization surface |
| `token.py` | Token provisioning primitives |

### `frontmatter/` — YAML frontmatter primitives
Python port of the DoE-claude coordinator JS text-manipulation primitives.

| File | Purpose |
|---|---|
| `primitives.py` | Core read/write/mutate primitives |
| `schema_validate.py` | Frontmatter schema validation |
| `body_blocks.py` | Shared fenced-block locator for plan-body YAML blocks (e.g. `## Tasks` `yaml plan-tasks` spine) |

### `hooks/` — advisory + bookkeeping hook op handlers
Each sub-module self-registers under the `hooks.<name>` method namespace.

| File | Purpose |
|---|---|
| `_envelope.py` | Return-envelope builders for advisory hook ops |
| `_payload.py` | Flat-scalar input field helpers |
| `agent_completion_log.py` | PostToolUse write hook |
| `nudge_em_code_dispatch.py` | PreToolUse advisory op |
| `nudge_foreground_agent_dispatch.py` | REROUTE gate — rewrites foreground Agent dispatches to background (updatedInput) |
| `nudge_unauthorized_handoff.py` | PostToolUse advisory hook |
| `postuse_advisory_dispatch.py` | PostToolUse advisory dispatcher |
| `session_heartbeat.py` | Pre+PostToolUse bookkeeping hook |
| `suggest_sonnet_research.py` | PreToolUse advisory hook |
| `track_dispatched_agents.py` | PostToolUse Agent bookkeeping op |
| `track_touched_files.py` | PostToolUse bookkeeping hook |

### `invoke/` — command-type dispatch package (DR-215)
JSON-RPC dispatch utilities for the command-type execution model. The HTTP invoke surface
(`serve_http_async`/`http_server`) was retired — see
`docs/decisions/DR-215-coordinator-core-command-type-execution-model.md`.

| File | Purpose |
|---|---|
| `__main__.py` | Generic in-process op dispatcher entrypoint |
| `dispatch.py` | JSON-RPC error helper + dispatch utilities |

### `contract/`
Vendored cockpit-contract pin (see `docs/wiki/cockpit-contract-revendor.md`). Also home of
producer-contract docs for claude-klabauter-owned ops, e.g. `handoff-reconcile-producer-contract.md`
(`handoff.reconcile_open`, turnable-on) and `auto-reconcile-policy.grammar.md` (the DoE-owned
policy YAML's grammar pin).

### `subagent_sandbox/` — resolver + policy-load + provision/report seam
Python engine-ification of DoE's `block-reviewer-write-outside-sidecar.sh` PreToolUse hook
(Port of: block-reviewer-write-outside-sidecar.sh, DoE 8b29fa14, 2026-07-12; DR-047
contract-vs-engine split), since narrowed by DR-058: the ALLOW/DENY enforcement half
(the two-tier confinement decision matrix) was retired as friction-over-EM-intent, leaving the
shared resolver + policy-load layer and the `report_sidecar` provision/report path. Reads DoE's
policy YAML at an injected path; never vendors it. See `subagent_sandbox/CONTRACT.md` for the
pinned policy-grammar + provision/report_sidecar contract.

| File | Purpose |
|---|---|
| `engine.py` | Policy loader + agent-id/git-root resolvers (consumed by `bash_guards`) — deny matrix removed per DR-058 |
| `provision_report.py` | `python3 -m coordinator_core.subagent_sandbox.provision_report` — spawn-time `report_sidecar` doc provisioner + path emitter |
| `CONTRACT.md` | Pinned policy-YAML grammar + provision/`report_sidecar`-only contract note |

### `tests/`, `ops/tests/`, `ops/emit/tests/`, `ops/fleet/tests/`, `ops/session/tests/`, `subagent_sandbox/tests/`
Pytest suites (`pytest.ini`: `testpaths = ["coordinator_core"]`, `test_*.py` convention,
established by pcore-05 C1). 19 `conftest.py`/test-support files across the tree.

---
*Last regenerated: 2026-07-22. Maintained by `/update-docs` Phase 2 — re-run rather than hand-patch after structural changes (new op modules, package moves).*
