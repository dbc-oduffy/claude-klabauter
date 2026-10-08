"""
coordinator_core.op_scopes — op-to-scope keying table (dependency-free).

Purpose: Home for _OP_KEY_SCOPE / OP_KEY_SCOPE / WORKTREE_SCOPED_OPS, split out of
coordinator_core.ipc so that `import coordinator_core` (which re-exports OP_KEY_SCOPE /
WORKTREE_SCOPED_OPS as a cross-repo parity surface) does not transitively pull in ipc.py's
`import asyncio` — a stdlib-only op such as coordinator_core.ops.mint_deliverable_id has no
business loading the asyncio stack just to read a plain dict. ipc.py re-exports these names
for backward compatibility (`from coordinator_core.ipc import OP_KEY_SCOPE` keeps working).

Negative-spec: this module MUST remain stdlib-only (types, nothing else) — do not import
ipc.py, asyncio, or anything else that would reintroduce the cold-import cost this split
exists to avoid.

Spec backlink: cross-repo/inbox/2026-07-21-claude-central-em-python-bin-cold-invocation-minutes-per-call.md
"""

from __future__ import annotations

import types as _types
from typing import Dict


_OP_KEY_SCOPE: Dict[str, str] = {
    "goal.append":                           "common_dir",
    "orientation.regenerate_cache":          "common_dir",
    "workflow.fire":                         "common_dir",
    "workflow.fire_status":                  "common_dir",
    "handoff.blocked_by_dependents":         "common_dir",
    "hooks.nudge_foreground_agent_dispatch": "common_dir",
    "hooks.nudge_em_code_dispatch":          "common_dir",
    "hooks.track_touched_files":             "common_dir",
    "hooks.receiver_state_sensor":           "common_dir",
    "hooks.agent_completion_log":            "common_dir",
    "hooks.track_dispatched_agents":         "common_dir",
    "hooks.agent_postuse_dispatch":          "common_dir",
    "hooks.postuse_agent_dispatch":          "common_dir",
    "hooks.subagent_zero_tool_use":          "common_dir",
    "hooks.subagent_zero_tool_use_surface":  "common_dir",
    "hooks.subagent_zero_tool_use_resolve":  "common_dir",
    "hooks.subagent_review_mark":            "common_dir",
    "hooks.subagent_fabrication_check":      "common_dir",
    "hooks.subagent_sidecar_fill_check":     "common_dir",
    "coverage.gate":                         "show_top",
    "cutover.gate":                          "common_dir",
    "cutover.advance":                       "common_dir",
    "memo.transition":                       "show_top",
    "memo.correct_note":                     "show_top",
    "ping":                                  "none",
    "invoke.from_argv":                      "none",
    "hooks.suggest_sonnet_research":         "none",
    "hooks.subagent_arrival_check":          "none",
    "hooks.cater_subagent_start":            "none",
    "hooks.flag_em_poll_in_flight":          "none",
    "hooks.nudge_unauthorized_handoff":      "none",
    "hooks.nudge_named_agent_report_delivery": "none",
    "hooks.flag_em_poll_in_flight": "none",
    "hooks.postuse_advisory_dispatch":       "none",
    "hooks.nudge_autonomous_askuserquestion": "none",
    "hooks.sessionend_archive_session": "none",
    # hooks.watchdog_undischarged_next_move — repo_root handler arg is unused
    # (always None): the handler resolves its OWN repo root from
    # params["payload"]["cwd"] (coordinator_core.git.repo_root.show_toplevel,
    # zero-spawn) for the ledger under state/subagent-share/<session_id>/,
    # and its own git dir (coordinator_core.git.git_dir.resolve_git_dir) for
    # the sizing-object touch-record lookup — never the framework-supplied
    # _origin_worktree. Same "none" class as the other repo_root-less
    # hooks.* ops above, despite being MUTATING (see its own classification
    # entry — scope and mutating-ness are independent axes).
    "hooks.watchdog_undischarged_next_move": "none",
    # hooks.plan_persistence_check — repo_root handler arg is unused; the
    # handler resolves its own target repo(s) from payload["cwd"]/
    # payload["env"]["CLAUDE_PROJECT_DIR"] via a non-spawning git-toplevel
    # walk, exactly like hooks.sessionend_archive_session above.
    "hooks.plan_persistence_check": "none",
    "hooks.runtime_tripwire_em_check": "none",
    "hooks.stop_dispatch": "none",
    "hooks.context_pressure_precompact":     "none",
    "hooks.postusefailure_cross_repo_memo_remediate": "none",
    "hooks.postuse_subagent_compaction_warning": "none",
    "hooks.nudge_cross_repo_cwd_boundary":   "none",
    "hooks.guard_config_change_hookstack_selfdefence": "none",
    "hooks.check_claude_md_size":             "none",
    "hooks.derive_global_doctrine_live_copy": "none",
    "hooks.derive_setup_copies":              "none",
    "hooks.guard_doctrine_surface_bash_write": "none",
    "hooks.guard_doctrine_surface_ratio":     "none",
    "hooks.guard_doctrine_changelog_prose":   "none",
    "hooks.preuse_write_dispatch":            "none",
    "hooks.guard_python_syntax_on_write":     "none",
    "hooks.guard_posix_invocation_doctrine_write": "none",
    "hooks.guard_test_tree_git_fixture_spawn": "none",
    "hooks.guard_handoff_summary_cap_on_write": "none",
    "hooks.guard_repo_setup_claude_home_refusal": "none",
    "hooks.nudge_plan_test_surface_tier":     "none",
    "hooks.preuse_agent_dispatch":            "none",
    "hooks.preuse_skill_dispatch":            "none",
    "hooks.preuse_search_dispatch":           "none",
    "hooks.preuse_sendmessage_dispatch":      "none",
    "hooks.enforce_agent_dispatch_mode":      "none",
    "hooks.block_unenumerated_agent_type":    "none",
    "hooks.guard_named_dispatch_tool_restriction": "none",
    "hooks.guard_host_subagent_bash_ban":     "none",
    "hooks.guard_host_subagent_bash_spawn_shapes": "none",
    "hooks.preuse_bash_dispatch":             "common_dir",
    "hooks.block_workflow_foreign_emission":  "none",
    "hooks.block_workflow_unmodeled_agent":   "none",
    "hooks.allow_emitted_workflow_fire":      "none",
    "hooks.nudge_workflow_authoring_trampoline": "none",
    "hooks.nudge_multiwave_workflow":         "none",
    "hooks.block_dispatch_suite_invocation":  "none",
    "hooks.strip_worktree_isolation":         "none",
    "hooks.block_worktree_tool":              "none",
    "hooks.sessionstart_dispatch":            "none",
    "hooks.sessionstart_async_dispatch":      "none",
    "hooks.assert_em_role":                   "none",
    "hooks.sweep_boot":                       "none",
    "hooks.session_start_announce_job_mode":  "none",
    "hooks.session_start_register_published_engine": "none",
    "hooks.session_start_repin_cloud_engine_root": "none",
    "hooks.session_start_watch_presence":     "none",
    "hooks.session_start_cloud_focus":        "none",
    "hooks.session_start_repair_prepare_commit_msg_hook": "none",
    "hooks.session_start_write_plugin_root_breadcrumb": "none",
    "hooks.sessionstart_bin_drift_refresh":   "none",
    "hooks.sessionstart_ensure_http_forwarder": "none",
    "hooks.guard_hook_generation_self_probe": "none",
    "hooks.session_start_guard_plane_check":  "none",
    "hooks.project_orientation":              "none",
    "hooks.pickup_autofire":                  "none",
    "hooks.mise_autofire":                    "none",
    "hooks.handoff_segment_inject":           "none",
    "hooks.group_em_autofire":                "none",
    "hooks.nudge_initiative_goals_ladder":    "none",
    "hooks.offer_exploration_tier_dispatch":  "none",
    "hooks.observe_config_change":            "none",
    "hooks.observe_post_compact":             "none",
    "hooks.postuse_stop_family_dispatch":     "common_dir",
    "hooks.sessionend_auto_commit":           "none",
    "hooks.subagent_zero_tool_use_detect":    "common_dir",
    "hooks.group_em_park_spool":              "common_dir",
    "hooks.guard_kira_verdict_routed":        "common_dir",
    "hooks.guard_manufactured_blocker":       "common_dir",
    "hooks.guard_terminal_review":            "common_dir",
    "warm_guard.evaluate":                   "none",
    "percolate.run":                         "none",
    "percolate.validate_store":              "none",
    "cruft_sweep.run":                       "none",
    "engine.drift":                          "none",
    "engine.registration_completeness":      "none",
    # plugin_health.drift — no repo state accessed: read-only drift probe that inspects
    # the OPERATOR's OWN machine-local plugin registry (settings-home resolved via
    # coordinator_core._settings_home), not the caller's repo; _origin_worktree not
    # required (same "none" class as engine.drift / percolate.validate_store).
    "plugin_health.drift":                   "none",
    # app_session.launch / .census / .teardown — the launch target is a CONSUMING
    # repo supplied by the caller, and the spawned-process handles persist under
    # that repo's git common dir (never <root>/.git, which is a FILE in a linked
    # worktree). "common_dir" so every linked worktree of one repo censuses and
    # tears down the same set of processes: a launch from one worktree and a
    # teardown from a sibling must not see disjoint state, or teardown silently
    # leaves the process running.
    "app_session.launch":                    "common_dir",
    "app_session.census":                    "common_dir",
    "app_session.teardown":                  "common_dir",
    # plugin_health.scan — no repo state accessed: read-only reader of the
    # OPERATOR's OWN machine-local plugin/consumer sentinel roots
    # (COORDINATOR_PLUGINS_ROOT / COORDINATOR_CONSUMER_HEALTH_ROOT env-resolved,
    # default ~/.claude/plugins and ~/.claude), not the caller's repo;
    # _origin_worktree not required (same "none" class as plugin_health.drift).
    "plugin_health.scan":                    "none",
    # plugin_health.sentinel — no repo state accessed: fires the coordinator-doctor
    # probe suite against the OPERATOR's OWN machine (CLAUDE_HOME, settings-home,
    # machine-local registry, resolved Python interpreter), not the caller's repo;
    # _origin_worktree not required (same "none" class as plugin_health.drift /
    # plugin_health.scan).
    "plugin_health.sentinel":                "none",
    # cartography.* — fleet-generic, COMPUTE_ONLY ops (coordinator-core-op-target-
    # resolution-model: explicit `target_root` wire param, any repo, NOT the caller's
    # own dispatching tree). No repo-specific state is accessed via repo_root/
    # _origin_worktree — every handler ignores the repo_root argument entirely and
    # resolves solely from the caller-supplied target_root param (path-guarded).
    # Spec: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md § C2/C3/C4.
    "cartography.tree":                      "none",
    "cartography.file_index":                "none",
    "cartography.symbols":                   "none",
    "cartography.edges":                     "none",
    "cartography.op_edges":                  "none",
    "docindex.emit":                         "none",
    # goals.reassess_krs — no repo_root-derived state access, all paths (goals_dir,
    # bin_dir, signal_repo_root) are explicit caller-supplied params from the DoE-side
    # trampoline, which resolves them itself exactly as the original bash script
    # derived SCRIPT_DIR/REPO_ROOT from its own BASH_SOURCE location.
    "goals.reassess_krs":                    "none",
    "goal.set_kr_status":                    "none",
    # workflow.validate — fleet-generic, COMPUTE_ONLY op (mirrors the
    # cartography target-resolution model): explicit `script_path` wire
    # param, any repo, NOT the caller's own dispatching tree. No repo-
    # specific state is accessed via repo_root/_origin_worktree — the
    # handler ignores the repo_root argument entirely and resolves solely
    # from the caller-supplied script_path (+ optional target_root),
    # path-guarded. Spec: docs/plans/2026-07-12-workflow-skeleton-stamper-
    # claude-klabauter-engine.md § C2.
    "workflow.validate":                     "none",
    "workflow.bind_args":                    "none",
    "distill.curate_clusters":               "none",
    "distill.workflow_input":                "none",
    # workflow.scaffold — fleet-generic, COMPUTE_ONLY op: pure generation
    # from caller-supplied name/description/phases/pattern params, no repo
    # state accessed at all (not even a path read) — the handler ignores
    # repo_root/_origin_worktree entirely. Spec: docs/plans/2026-07-12-
    # workflow-skeleton-stamper-claude-klabauter-engine.md § C3.
    "workflow.scaffold":                     "none",
    # compute_layer.scaffold — fleet-generic, COMPUTE_ONLY op with two modes,
    # neither of which touches repo state through repo_root/_origin_worktree:
    # `emit` composes producer module TEXT from caller-supplied
    # skill_name/verbs and returns it (the caller writes), and `check` scores
    # the Sub-shape B producers read-only. Listed explicitly rather than
    # relying on this table's absent-entry default, for the same reason
    # dispatch.emit below is: that default covers unclassified and test-only
    # ops, and this is neither. Spec: docs/plans/2026-08-13-compute-layer-
    # scaffolder.md § C4.
    "compute_layer.scaffold":                "none",
    # dispatch.emit — fleet-generic, MUTATING op: reads a caller-supplied plan
    # path and writes an emitted Workflow script, with containment resolved from
    # the `output_path`/`target_root` wire params rather than from
    # repo_root/_origin_worktree — the same target-resolution model as
    # workflow.validate. Listed explicitly rather than relying on this table's
    # absent-entry default: that default is documented for unclassified and
    # test-only ops, and dispatch.emit is neither. Both its siblings above are
    # explicit for the same reason.
    "dispatch.emit":                         "none",
    # strategic.generate — fleet-generic, MUTATING op (mirrors the cartography/
    # workflow target-resolution model): explicit REQUIRED `target_root` wire
    # param, any repo, NOT the caller's own dispatching tree. No repo-specific
    # state is accessed via repo_root/_origin_worktree — the handler ignores
    # the repo_root argument entirely and resolves solely from the caller-
    # supplied target_root, path-guarded. Spec: docs/plans/2026-07-11-claude-klabauter-
    # strategic-self-description-generation-leg.md § C1.
    "strategic.generate":                    "none",
    # strategic.emit — fleet-generic, MUTATING op (mirrors strategic.generate's target-
    # resolution model): explicit REQUIRED `target_root` wire param, any repo, NOT the
    # caller's own dispatching tree. The handler ignores the repo_root argument entirely and
    # resolves solely from the caller-supplied target_root, path-guarded. Spec:
    # tasks/strategic-feed-emission/stub.md
    "strategic.emit":                        "none",
    "fleet.archive_completed_handoffs":      "common_dir",
    "housekeeping.cycle":                    "common_dir",
    "fleet.aggregate_capability_index":      "common_dir",
    "fleet.reap_unintegrated_findings":      "common_dir",
    "fleet.reap_integrated_findings":        "common_dir",
    "fleet.reap_review_trail_rest":          "common_dir",
    # fleet.handoffs_for_plan — COMPUTE_ONLY read op, but still "common_dir": unlike
    # memo.list (registry-only, scope "none"), this op enumerates the CALLING repo's
    # own state/handoffs/ + archive/handoffs/ trees for a given origin_plan_id, so it
    # needs the caller's own worktree resolved via main_worktree_root(common_dir),
    # same keying class as queue.cluster's own-worktree reads. Without
    # this entry dispatch resolves repo_root=None and the handler returns a setup-error
    # envelope rather than silently reading the wrong (or no) repo.
    # Spec: cross-repo memo from claude-central-em, 2026-07-26.
    "fleet.handoffs_for_plan":               "common_dir",
    "commit.anchors":                        "common_dir",
    "handoff.transition":                    "common_dir",
    "handoff.stamp":                         "common_dir",
    "handoff.repair_deployment_state":       "common_dir",
    "handoff.correct_body":                  "common_dir",
    "handoff.discharge_criteria":            "common_dir",
    "handoff.author_lint":                   "common_dir",
    "handoff.append_session_ledger":         "common_dir",
    "handoff.propagate":                     "common_dir",
    "plan.propagate":                        "common_dir",
    "handoff.stamp_phase":                   "common_dir",
    "handoff.discharge_landed":               "common_dir",
    "handoff.backfill_claim_stamp":          "common_dir",
    "handoff.repoint_origin":                "common_dir",
    "baton.supersede":                       "common_dir",
    "baton.awaiting_gate_recheck":           "common_dir",
    "baton.seed_split":                      "common_dir",
    "goal.record_go":                        "common_dir",
    "handoff.close_origin_stub":             "common_dir",
    "handoff.normalize":                     "common_dir",
    "initiative.serve_set":                  "common_dir",
    "roadmap.link_stubs":                    "common_dir",
    "roadmap.plan_gate":                     "common_dir",
    "roadmap.blitz_land":                    "common_dir",
    "roadmap.blitz_stage":                   "common_dir",
    # plan.prep_gate — keyed on git_common_dir: reads one main-worktree-rooted
    # docs/plans/*.md plus that worktree's top-level entry names (the
    # ROOT-EXISTENCE leg), under main_worktree_root(common_dir). Without this
    # entry dispatch resolves repo_root=None and the handler refuses outright.
    "plan.prep_gate":                        "common_dir",
    "plan.stamp_prepped":                    "common_dir",
    "artifact.adopt":                        "common_dir",
    "plan.gated_criteria_met":               "common_dir",
    "plan.cross_plan_gate":                  "common_dir",
    "goal.match_candidates":                 "common_dir",
    "goal.close_day":                        "common_dir",
    "goal.close_day_apply":                  "common_dir",
    "plan.match_candidates":                 "common_dir",
    "handoff.match_candidates":              "common_dir",
    "handoff.lineage_ancestry":               "common_dir",
    "deliverable.rollup":                    "common_dir",
    "spec_backlink.resolve":                 "common_dir",
    "spec_backlink.rewrite":                 "common_dir",
    "queue.append":                          "common_dir",
    "decision_record.mint_id":               "common_dir",
    "decision_record.release_id":            "common_dir",
    "peer_notice.send":                      "common_dir",
    "peer_notice.check":                     "common_dir",
    "queue.promote":                         "common_dir",
    "queue.cluster":                         "common_dir",
    "handoff.scaffold_from_queue":           "common_dir",
    "memo.list":                              "none",
    "memo.check_addressee":                   "common_dir",
    "memo.send":                              "common_dir",
    "memo.reconcile_outbox":                  "common_dir",
    # memo.check_deliveries — "common_dir", the scope its own handler docstring
    # already declares (`ops/fleet/memo_send.py :: _memo_check_deliveries`). It is a
    # sender-side COMPUTE_ONLY sweep over the CALLING repo's own sent-ledger, whose
    # path is derived by main_worktree_root(common_dir) exactly as memo.send does, so
    # it keys the same way. "none" would be wrong on the one axis that matters: the
    # handler fails loud on a missing repo_root rather than degrading, so an omitted
    # entry does not read as a harmless default — it makes the op unreachable.
    "memo.check_deliveries":                  "common_dir",
    "memo.heal_inbox":                        "common_dir",
    "memo.draft":                             "common_dir",
    "memo.compose":                           "common_dir",
    "memo.list_outbox":                       "common_dir",
    "memo.blitz_buckets":                     "common_dir",
    "push.outstanding":                      "common_dir",
    "p4.register_workspace":                 "common_dir",
    "p4.session_state":                      "common_dir",
    # strang-10 A+B residual writer strangle — changelog / completion / review-trail write ops.
    # Keyed on git_common_dir: changelog.* + review_trail.write write main-worktree-rooted state/
    # (handler derives worktree via main_worktree_root(common_dir), never repo_root/'state' directly);
    # completion.* mutate a caller-supplied docs/plans/*.md path. All MUTATING, sanctioned by DR-216.
    # Spec: docs/plans/2026-07-06-strang-10-residual-writer-strangle-command-type.md
    "changelog.append_day":                  "common_dir",
    "changelog.backfill_gaps":               "common_dir",
    "changelog.inject_anchor":               "common_dir",
    # changelog.compute_day_fields — COMPUTE_ONLY sibling of changelog.append_day
    # (Zone-A build #4): keyed identically since it reads the same worktree-rooted
    # git history / state/handoffs / state/review-trail / state/week-changelog
    # substrate append_day writes to (handler derives worktree via
    # main_worktree_root(common_dir), same as append_day).
    "changelog.compute_day_fields":          "common_dir",
    "changelog.upsert_reviewed":             "common_dir",
    "plan.append_session":                   "common_dir",
    # Backfill: records.query (strang-11 C1a COMPUTE_ONLY read op) was registered without an
    # _OP_KEY_SCOPE entry by a concurrent session, leaving the key-scope coverage gate RED on HEAD.
    # Reads main-worktree-rooted project records → common_dir (matches deliverable.rollup precedent).
    "records.query":                         "common_dir",
    "records.history":                        "none",
    "records.by_origin_plan":                 "common_dir",
    "handoff.columns":                       "common_dir",
    "memo.triage":                            "common_dir",
    "updatedocs.gates":                       "common_dir",
    "distill.scope":                          "common_dir",
    # memo.fate_partition (C16, DR-228 § D6 scratch-tier) — keyed on git_common_dir:
    # handler resolves the caller's worktree via main_worktree_root(repo_root) and
    # reads main-worktree-rooted cross-repo/archive/*.md, writing its shard under
    # main-worktree-rooted state/scratch/artifact-distillation/<run_id>/ — same
    # scope class as memo.triage. Without this entry dispatch resolves repo_root=None
    # and the op raises rather than silently no-op'ing (this op is MUTATING, not
    # COMPUTE_ONLY — a resolved write target is mandatory).
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C16
    "memo.fate_partition":                    "common_dir",
    # memo.fate_backfill (2026-08-06, cross-repo ask item 1(a)) — keyed on
    # git_common_dir: handler resolves the caller's worktree via
    # main_worktree_root(repo_root) and reads main-worktree-rooted
    # cross-repo/archive/*.md, same read surface as memo.fate_partition.
    # COMPUTE_ONLY (no shard write, no memo.transition call) — without this
    # entry dispatch resolves repo_root=None and the handler returns the
    # all-zero empty outcome rather than reading the caller's actual corpus.
    # Spec: cross-repo/inbox/2026-08-06-example-retrieval-repo-em-distill-fate-coverage-and-legacy-log-reader.md § 1(a)
    "memo.fate_backfill":                      "common_dir",
    "distill.curation_status":                "common_dir",
    "distill.assemble_disposal_manifest":     "common_dir",
    "distill.stamp_disposal":                  "common_dir",
    "distill.apply_disposal":                  "common_dir",
    "crossrepo.closure_status":               "common_dir",
    # ceremony.update_docs_scan (C17) — keyed on git_common_dir: handler resolves the
    # caller's worktree via main_worktree_root(repo_root) and reads main-worktree-rooted
    # coordinator_core/DIRECTORY.md, docs/plans/*.md, cross-repo/archive/*.md, tasks/*.md,
    # plus a bounded `git log` window scoped to that worktree — same read-only scope
    # class as handoff.has_live_children. Without this entry dispatch resolves
    # repo_root=None and the handler raises rather than silently deriving against the
    # wrong worktree. Never writes.
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C17
    "ceremony.update_docs_scan":               "common_dir",
    "deliverable.cascade_terminal":             "common_dir",
    "deliverable.cascade_retract":              "common_dir",
    "deliverable.cascade_backstop_sweep":       "common_dir",
    "deliverable.cascade_divergence_report":    "common_dir",
    "commit_ledger.join_divergence_report":     "common_dir",
    "goal.kr2_two_repo_rate":                   "common_dir",
    "deliverable.fork_detect":                  "common_dir",
    "sizing.decline":                           "common_dir",
    "sizing.ship":                               "common_dir",
    "sizing.mark_routed":                        "common_dir",
    "sizing.discharge_surfaced":                 "common_dir",
    "sizing.accept_exit_criterion":               "common_dir",
    "sizing.record_xl_exit":                      "common_dir",
    "sizing.record_pm_resolution":                "common_dir",
    "sizing.resize":                             "common_dir",
    "sizing.record_spike_verdict":               "common_dir",
    "sizing.read_object_fields":                 "common_dir",
    "plan.tasks.spine_drift_check":              "common_dir",
    # plan.tasks.spine_drift_check — `common_dir`, decided on converging
    # evidence rather than left silent. The handler's
    # `_plan_claim_holder_session_id` calls `git_common_dir(root)` and derives
    # `plan_claim_dir(common_dir, ...)`; plan claims live in the git COMMON
    # dir, shared across worktrees, so the op's state is keyed there. It also
    # imports `main_worktree_root`, the same normalisation the sizing entries
    # directly above cite for their own `common_dir` verdict.
    #
    # This verdict was previously left OPEN on purpose: an earlier session
    # read the handler, saw two defensible readings (`show_top` vs
    # `common_dir`), and filed both rather than guessing — on the principle
    # that a verdict written by someone who does not know which answer holds
    # converts a live question into permanent silence. That caution was
    # right. It is discharged here by evidence, not by preference: two
    # independent reads of the handler and the sibling precedent above all
    # land on `common_dir`. If a later reader finds the claim-dir derivation
    # has moved, this entry is the thing to re-check first.
    # Spec: state/handoffs/2026-08-21_161715_the-census-that-cannot-miss-an-op.md
    # sizing.read_object_fields — same scope class as sizing.ship/sizing.decline/
    # sizing.record_spike_verdict above: the handler reads a main-worktree-rooted
    # state/sizings/ (or archive/sizings/) file, derived via main_worktree_root
    # (common_dir). Without this entry dispatch resolves repo_root=None and the
    # handler raises rather than silently deriving against the wrong worktree.
    # Spec: state/dispatch-briefs/2026-08-21-engine-half-of-the-roadmap-
    # sprint-spine-split/C6.md
    # strang-11 B8 new ops — all keyed on git_common_dir: handlers derive worktree via
    # main_worktree_root(common_dir), matching the fleet.*/handoff.*/ceremony.* precedent.
    # Class-A ops (fleet.*) use archive_and_commit and read/write main-worktree-rooted paths.
    # Class-B/composite ops (session.*) read/write .git/coordinator-sessions/ and state/ paths,
    # both derived from the main worktree root. Spec: docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C5
    # "fleet.archive_shipped_handoffs" REMOVED -- op key SUBSUMED (not
    # renamed), module deleted 2026-08-25 (C1b, docs/plans/2026-08-25-the-
    # handoff-auto-archive-comes-back-capped.md).
    # fleet.backfill_dispositionless_memos — same "common_dir" keying as the other
    # fleet.* ops: the handler derives the worktree via main_worktree_root(common_dir)
    # to resolve cross-repo/archive/<filename> for each of the 34 backfill-table
    # entries. Spec: docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md § C5
    "fleet.backfill_dispositionless_memos":  "common_dir",
    "fleet.backfill_reference_edges":        "common_dir",
    "session.commits":                       "common_dir",
    # session_baton.mint / session_baton.promote -- "none", mirroring
    # session.record_pickup: both take an explicit session_id and resolve
    # their own store path under the git common dir themselves rather than
    # reading a caller-supplied repo. Spec:
    # docs/plans/2026-08-18-a-session-always-has-a-baton.md C2/C3.
    # session.warm_start — none. DECISION, not a default: this op is
    # machine-scoped, not repo-scoped, and says so in its own handler
    # signature ("this op is machine-scoped, not repo-scoped — it warms
    # THIS machine's engine"). Its whole effect is a debounced detached
    # spawn gated on `warm.settings.is_warm_enabled` plus the
    # `warm.breadcrumb` pre-check; it reads machine-local settings and a
    # breadcrumb, never a caller worktree, and writes no repo substrate.
    # Threading a common_dir in would imply a per-repo warm engine, which
    # is the opposite of the one-engine-per-machine rule it implements.
    # Spec: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C25.
    "session_baton.mint":                    "none",
    "session_baton.promote":                 "none",
    "baton.carry_forward":                   "common_dir",
    "baton.carry_forward_read":              "common_dir",
    "session.reap":                          "common_dir",
    "session.audit_unreapable":               "common_dir",
    # session.boot_sweep — GRAVESTONED 2026-08-27, K-059. No scope row, because
    # there is no module to scope: the rebuild missed too (AC3 max 218.8ms against
    # its own 200ms bar) and the requirement was retired by measurement, not by
    # argument — 198 terminal handoffs archived in 7 days through
    # fleet.archive_terminal_handoffs and the per-artifact lifecycle ops, with the
    # backstop dead throughout. The name survives only in
    # op_budget_suspension.SUSPENDED_OPS, which is the refusal door and needs no
    # module behind it.
    # session.reap_claims_for_repos — fleet-generic per-repo claim-reap primitive: takes an
    # explicit target_roots[] param and reaps each, so it derives NO per-request repo key from
    # the caller's own tree → scope "none" (claude-klabauter's target-resolution convention: fleet-generic
    # ops pass target_root explicitly rather than resolving common_dir). Spec:
    # docs/plans/2026-07-14-claim-lock-liveness-archival-gate-unification.md § C5.
    "session.reap_claims_for_repos":         "none",
    "session.record_pickup":                 "none",
    "session.scope_report":                  "none",
    # session.safe_commit_offer — MUTATING commit+push of THIS session's own
    # claimed dirty paths, but scope "none" for the SAME reason as
    # session.scope_report directly above, whose report it is the mutating
    # counterpart to: it takes optional session_id/cwd wire params and resolves
    # the calling session via
    # coordinator_core.session.core.resolve_session_id(cwd) when session_id is
    # absent, never deriving a target from the engine-supplied repo_root.
    # LOAD-BEARING on the warm path: this process is the server, whose
    # os.environ is its spawner's, so identity MUST come from the caller's cwd
    # and never from this process's environment -- an env-only read here commits
    # under the wrong session's claim. Registered 2026-08-27, replacing the
    # module's `main()` CLI door (two interpreter starts to reach a warm engine
    # that answers in 13ms through coordinator-invoke.exe -- DR-344).
    "session.safe_commit_offer":             "none",
    # session.guard_settings_integrity — MUTATING (classification.py) but scope "none":
    # handler resolves config_dir from an explicit params["config_dir"] override or
    # falls back to CLAUDE_CONFIG_DIR/$HOME/.claude env resolution — never from
    # repo_root. Missing from this table was an oversight (per the table's own
    # docstring), not an intentional default; behaviorally inert today since "none"
    # is the correct scope, but undetected by any test. Review: code-reviewer.
    "session.guard_settings_integrity":      "none",
    # session.guard_hooks_kill_switch_detail — same module and same resolution story as
    # session.guard_settings_integrity above: a read-only reporter over the settings-home
    # kill-switch marker, resolved from CLAUDE_CONFIG_DIR/$HOME/.claude, never from
    # repo_root. Absent from this table until 2026-07-31, when the registration-quad
    # check surfaced it alongside handoff.correct_body; behaviorally inert, same as its
    # sibling, but an incomplete quad is exactly what that check exists to catch.
    "session.guard_hooks_kill_switch_detail": "none",
    # session.resolve_address — read-only resolver mapping a session UUID to the live
    # SendMessage address, reading <claude-config>/sessions/<pid>.json via
    # coordinator_core.session.harness_registry.registry_dir(), which resolves through
    # claude_config_dir() (CLAUDE_CONFIG_DIR/$HOME/.claude) and never from repo_root →
    # scope "none", same resolution story as session.guard_settings_integrity above.
    # The harness peer registry is machine-global, not per-worktree: two linked
    # worktrees of one repo see the identical peer set, so neither "common_dir" nor
    # "show_top" would key anything meaningful.
    # Spec: state/handoffs/2026-08-13-session-owner-reachability-registry.md § 1.
    "session.resolve_address":                "none",
    "session.whoami_live":                    "none",
    "session.peer_roster":                    "none",
    "groupem.enter":                          "none",
    # groupem.stamp / groupem.resolve_addressee / groupem.idle_report / groupem.standing --
    # same resolution story as groupem.enter immediately above: each
    # composes over the machine-global harness peer registry and/or a
    # repo-scoped path passed VERBATIM as the wire-level repo_root param,
    # never resolved against this engine-injected kwarg.
    # Spec: state/dispatch-briefs/2026-09-01-the-crowns-standing-surfaces-report-themselves/C7.md.
    "groupem.stamp":                          "none",
    "groupem.resolve_addressee":              "none",
    "groupem.idle_report":                    "none",
    "groupem.standing":                       "none",
    # session.work_state — read-only held/unclaimed corpus read over
    # state/handoffs/, which is main-worktree-rooted repo state -- exactly
    # the case this table's own header comment names for "common_dir"
    # (git_common_dir(resolved_worktree), shared across linked worktrees).
    # Deliberately DIFFERENT from session.peer_roster's "none" just above:
    # the harness peer registry is machine-global, but this op's own
    # state/handoffs/ scan is not. Takes the engine-injected repo_root
    # kwarg verbatim; no wire param of the same name.
    # Spec: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md, chunk C3.
    "session.work_state":                     "common_dir",
    # fleet.work_state — fleet-generic aggregation of session.work_state's
    # per-repo held/unclaimed core across every registered ACTIVE sibling
    # (C5). Deliberately DIFFERENT from session.work_state's "common_dir"
    # just above: this op is not scoped to the calling repo's own tree at
    # all — it reaches across every registered sibling via
    # _memo_resolver.read_registry_repos(), the same fleet-generic
    # target-resolution convention session.reap_claims_for_repos /
    # session.record_pickup / session.scope_report already use → "none".
    # repo_root arrives as None (unused) for this op.
    # Spec: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md, chunk C5.
    "fleet.work_state":                       "none",
    "fleet.record_history":                   "none",
    "session.artifact_owner":                 "none",
    # repo_root is a wire param, same story as session.peer_roster.
    "session.incident_claim":                 "none",
    "session.incident_peers":                 "none",
    "handoff.author_fork":                   "common_dir",
    "plan.persist_capture":                  "common_dir",
    "plan.tasks.mutate":                     "common_dir",
    "plan.tasks.grouping_digest":             "common_dir",
    "plan.narrow_criterion":                  "common_dir",
    "session_ledger.aggregate_chain_loe":    "common_dir",
    "session_hierarchy.derive":              "none",
    "deferral.detect_orphan_memo":           "common_dir",
    "deferral.detect_partial_strangle":      "common_dir",
    "schema.describe":                       "none",
    "schema.validate":                       "none",
    "fleet.archive_release_accumulator":     "common_dir",
    "fleet.archive_paper_trail":              "common_dir",
    "fleet.archive_queue_entry":              "common_dir",
    "fleet.archive_actioned_memos":          "common_dir",
    "fleet.archive_completed_plans":          "common_dir",
    "fleet.delete_superseded_decisions":     "common_dir",
    "fleet.prune_closed_bugs":               "common_dir",
    "fleet.prune_emitted_output":            "common_dir",
    "fleet.scratch_hygiene":                 "none",
    "fleet.archive_sweep_status":            "common_dir",
    "fleet.migrate_handoff_vocabulary":       "common_dir",
    "fleet.archive_terminal_sizings":         "common_dir",
    "git_branch.compute_descendant_tip":      "common_dir",
    "git_branch.detect_unpushed_commits":     "show_top",
    "git_branch.list_unmerged_work":          "common_dir",
    "git_branch.verify_commit_in_review_window": "common_dir",
    # scratchpad.sweep — "none", MUTATING (dry-run by default; `reclaim: true`
    # is the sole destructive opt-in — see module docstring's two-gate
    # deletion contract). The handler (_handler in scratchpad_sweep.py)
    # accepts `repo_root` for dispatch-signature parity only and NEVER passes
    # it to `sweep_scratchpads` — confirmed by its own docstring ("Scope:
    # none ... repo_root is accepted ... but unused") and by reading the
    # `_handler` body, which forwards only temp_root/project_slugs/ttl_days/
    # reclaim. The op is fleet-generic by construction: it walks
    # `<temp_root>/claude/` across EVERY discovered project-slug directory
    # (via `_build_slug_to_root_map`'s Tier A + Tier A.5 discovery), not the
    # caller's own dispatching tree — same "any repo, never the caller's own
    # tree" target-resolution model as cartography.*/workflow.validate,
    # confirmed by test_scratchpad_sweep.py's cross-repo-isolation tests
    # (test_two_project_slugs_resolve_independent_liveness), which sweep
    # multiple project slugs in one call, none derived from repo_root.
    # Spec: this module's own docstring (Two-gate deletion contract /
    # Liveness-scope fix, 2026-08-10).
    "scratchpad.sweep":                       "none",
    "cartography.count_references":           "none",
    "doctrine.assert_cross_reference_counts": "common_dir",
    "percolate.check_inverse_drift":          "none",
    "percolate.build_token_index":            "none",
    # repo.clone_and_register — "none": operates on a sibling repo path and the
    # OPERATOR's own machine-local registry, neither of which is the caller's own
    # dispatching tree; matches the plugin_health.* operator-machine-scoped class.
    "repo.clone_and_register":                "none",
    "release.cut_tag":                        "common_dir",
    "release.cut_tag_and_publish":             "common_dir",
    "dependency.detect_changed_manifests":    "show_top",
    "detect.plugin_layout":                   "none",
    "detect.primary_languages":                "none",
    "cartography.stack":                       "none",
    # cartography.chunk_table — "none": same cartography.* target-resolution
    # model as its siblings above (explicit target_root wire param, any repo,
    # NOT the caller's own dispatching tree); repo_root is ignored entirely.
    # MUTATING (unlike its COMPUTE_ONLY siblings) because emit=true writes
    # one scratch-tier JSON artifact under target_root/state/scratch/
    # cartography-chunk-table/<run_id>/ — DR-228 § D6 (amended) sanctions the
    # write; scope classification is unaffected by that (still "none", since
    # no repo-specific _origin_worktree state is read).
    # Spec: cross-repo/inbox/2026-08-06-coordinator-content-repo-em-cartography-chunk-table-producer-seam.md
    "cartography.chunk_table":                 "none",
    # ceremony.chunk_commits — "none": same cartography.* / workflow.validate
    # target-resolution model (explicit caller-supplied plan_path, any repo,
    # NEVER the caller's own dispatching tree); the op derives its git repo
    # from wherever the resolved path lands and ignores repo_root entirely.
    # COMPUTE_ONLY — two read-only `git log` invocations, no writes.
    #
    # Registered EXPLICITLY rather than defaulting to "none" by omission,
    # which is what this entry's absence was: the op shipped relying on the
    # table's default, so its "scope is deliberately none" claim lived only in
    # a docstring. That claim is load-bearing — it is the reason the
    # caller-cwd defect (d4d429d21) was fixed in the bin/ forwarder rather
    # than by resolving against a forwarded --repo, which would have made the
    # op's answer depend on where it was dispatched from.
    # Spec: cross-repo/inbox/2026-08-10-coordinator-content-repo-em-chunk-commits-forwarder-relative-path.md
    "ceremony.chunk_commits":                  "none",
    "install.detect_python3_appx_stub":        "none",
    "lessons.filter_undated_universal":        "none",
    "lessons.reject_orphan_strip_entries":     "common_dir",
    "completion.flip_to_released":             "common_dir",
    "install.clone_idempotent":                "none",
    "coverage.halt_on_uncovered":              "show_top",
    "ceremony.init_anchor_injection_state":    "none",
    "install.write_shell_rc_guard_block":      "none",
    "install.wrapper_onto_path":               "none",
    "install.detect_cmd_autorun_coverage":     "none",
    "install.write_cmd_autorun_guard":         "none",
    "install.strip_cmd_autorun_guard":         "none",
    "percolate.list_files_newer_than_marker":  "none",
    "plan.list_stale_executing":               "common_dir",
    "plan.list_orphaned":                      "common_dir",
    "plan.suggest_completion_steps":           "common_dir",
    "cli.parse_flag":                          "none",
    "cli.parse_date_flags":                    "none",
    "merge.quiet_activity_gate":               "show_top",
    # update_docs.probe_fresh_repo_noop — common_dir: DIRECTORY.md / archive/ /
    # tasks/ are main-worktree-rooted paths, shared across linked worktrees of the
    # same repo per this table's own common_dir definition.
    "update_docs.probe_fresh_repo_noop":       "common_dir",
    # install.probe_skill_frontmatter_valid / install.probe_windows_terminal_presence
    # — "none": both inspect the OPERATOR's own machine (coordinator-claude
    # installation tree / PATH+winget packages), same "none" class as engine.drift /
    # plugin_health.scan.
    "install.probe_skill_frontmatter_valid":   "none",
    "install.probe_windows_terminal_presence": "none",
    "mcp.resolve_server_cli_path":              "none",
    "baton.resolve_swept_in_archive":          "common_dir",
    "percolate.run_ci_smoke_check":            "none",
    "delegation.check":                        "none",
    "percolate.run_identity_check":            "none",
    "ci.run_pip_audit":                        "show_top",
    "ci.run_semgrep_scan":                     "show_top",
    "ci.run_shellcheck_sweep":                 "show_top",
    "ci.run_commenting_sweep":                 "show_top",
    "findings.self_persist_fallback":          "none",
    "review.snapshot_diff_and_head":           "show_top",
    "review.freeze_diff":                      "show_top",
    "review.partition_slices":                 "show_top",
    "workday.stitch_sidecar_into_summary":     "common_dir",
    "repo_setup.validate_target_root":         "none",
    "research.verify_scout_inventory_completeness": "common_dir",
    # install.write_identity_file — "none": writes to CLAUDE_HOME/.claude/
    # coordinator-identity.yaml, a fixed per-machine operator-config path, not the
    # caller's repo.
    "install.write_identity_file":              "none",
    "baton.resolve_path_and_repo":              "none",
    "branch.merge_into_workstream":             "show_top",
    "bug_sweep.verify_fix_files_changed":       "show_top",
    "commit.exec_bit_change":                   "common_dir",
    "ceremony.commit_v2":                       "common_dir",
    # dispatch.terminal_commit — MUTATING: lands the run's terminal commit via
    # ONE in-process ceremony.commit_v2 call. Keyed identically to commit_v2
    # itself (D3) -- the caller's own worktree, never a params override.
    "dispatch.terminal_commit":                 "common_dir",
    # dispatch.ask_gate / dispatch.ask_stage — keyed like dispatch.terminal_commit: the caller's own worktree.
    "dispatch.ask_gate":                        "common_dir",
    "dispatch.ask_stage":                       "common_dir",
    # review_stamp.mint / review_stamp.check — MUTATING (mint only): both read
    # and mint writes only the caller's own worktree, keyed identically to
    # dispatch.terminal_commit (never a `params.repo_root` override).
    "review_stamp.mint":                        "common_dir",
    "review_stamp.rejudge":                     "common_dir",
    "review_stamp.check":                       "common_dir",
    # review_mint.bookkeep_wave — MUTATING: writes only the caller's own
    # worktree, keyed identically to review_stamp.mint/dispatch.terminal_commit
    # (never a `params.repo_root` override). 2026-09-28 PM order step b'.
    "review_mint.bookkeep_wave":                "common_dir",
    # review_mint.record_superseding_review / receipt.approve — MUTATING: write
    # only the caller's own worktree, never a `params.repo_root` override.
    "review_mint.record_superseding_review":    "common_dir",
    # test_verdict.record — MUTATING: writes only the caller's own worktree sidecar.
    "test_verdict.record":                      "common_dir",
    "receipt.approve":                          "common_dir",
    "fanout.poll_scratch_dir":                  "none",
    "fanout.compose":                           "none",
    "fanout.census":                            "none",
    "fanout.reconcile":                         "none",
    "machine.hibernate":                        "none",
    "percolate.run_pre_ci_hooks":               "none",
    "percolate.scan_content_leakage_tiers":     "none",
    "repo.create_and_push_remote":              "common_dir",
    "repo_setup.copy_console_subprocess_tripwire": "show_top",
    "research.archive_workdir":                 "common_dir",
    "research.restructure_for_repeat_topic":    "common_dir",
    "session.resolve_chain_terminal_disposition": "common_dir",
    # session.rotate_orphan_sweep_log — common_dir: must match
    # session.boot_sweep's existing common_dir entry for the SAME
    # tasks/orphan-sweep-notes.md write target (GIT_ROOT-worktree-rooted per
    # boot_sweep.py's own documented convention); a mismatched scope would
    # truncate a different repo's copy of the file than the one boot_sweep wrote
    # to.
    "session.rotate_orphan_sweep_log":          "common_dir",
    "workday.surface_auto_push_failure_stats":  "show_top",
    # git.push_failure_verdict — show_top: every signal it classifies on
    # (staged/unstaged diff, MERGE_HEAD, upstream ahead/behind) is
    # per-worktree state, same class as coverage.gate and merge.
    # quiet_activity_gate above; only the push-failures.log read inside the
    # handler itself resolves through the git COMMON dir (writer's own
    # keying), independent of this scope key. See the op module's own
    # docstring for the full rationale.
    "git.push_failure_verdict":                 "show_top",
    # git.maintenance — common_dir: the handler resolves its target as
    # `params["repo"] or repo_root or cwd` and runs maintenance tiers (prune,
    # repack, orphan-pack reaping) against that repository's object store,
    # which lives in the git COMMON dir and is shared by every worktree —
    # never per-worktree state. Same verdict as every sibling repo-operating
    # git op: commit.anchors, push.outstanding, commit.exec_bit_change,
    # git_branch.compute_descendant_tip.
    # BACKFILL, not this op's author: registered in _registry_map.py by
    # cf467d0abb without a scope entry. Verdict derived from the handler
    # signature and the sibling precedents above; if cf467d0abb's author
    # intended a different one, correct it here.
    "git.maintenance":                          "common_dir",
    "tracker.advance_status":                   "common_dir",
    # tracker.fold_observed_set — common_dir, the SAME scope
    # session.boot_sweep itself uses (this op is actuated FROM boot_sweep,
    # per docs/plans/2026-07-28-sat-01b-observed-set-fold-actuator.md § Tasks
    # C5). DECISION, not a default: common_dir scoping means one
    # sovereign-tracker store shared across all LINKED WORKTREES of the same
    # repo — matching every other session.*/fleet.* op's scoping — not a
    # per-worktree store. An op missing from this table silently degrades to
    # repo_root=None, which would break the DEC-11 confinement DR-241's
    # Amendment affirms; do not omit this entry.
    "tracker.fold_observed_set":                "common_dir",
    # tracker.mint_person — common_dir, the SAME scope tracker.fold_observed_set
    # and session.boot_sweep use. DECISION, not a default: this op's WRITE
    # BOUND (PM ruling 2026-08-12, see coordinator_core/ops/tracker/
    # mint_person.py module docstring) confines every write to the LOCAL
    # repo's own worktree root, derived via main_worktree_root(common_dir) —
    # never a holder/peer repo's root. An op missing from this table silently
    # degrades to repo_root=None, which would break that per-repo
    # confinement bound silently; do not omit this entry.
    "tracker.mint_person":                      "common_dir",
    # tracker.assign — common_dir, the SAME scope tracker.mint_person and
    # tracker.fold_observed_set use. DECISION, not a default: this op's
    # writes are confined to the LOCAL repo's own worktree root, derived via
    # main_worktree_root(common_dir) — never a holder/peer repo's root. An
    # op missing from this table silently degrades to repo_root=None, which
    # would break that per-repo confinement bound silently; do not omit
    # this entry.
    "tracker.assign":                           "common_dir",
    # tracker.render_status — common_dir, the SAME scope every other tracker.*
    # op uses. DECISION, not a default: render_status reads the LOCAL repo's
    # own event shard via tracker_projection, resolved from
    # main_worktree_root(common_dir) — never a holder/peer repo's root. An op
    # missing from this table silently degrades to repo_root=None, which for a
    # read op means projecting the CENTRAL scope's events instead of the
    # caller's and returning a confidently wrong status
    # (state/lessons/2026-07-06-compute-only-op-registration-needs-an-op.yaml);
    # do not omit this entry.
    "tracker.render_status":                    "common_dir",
    # tracker.assert_code_complete — common_dir, the SAME scope every other
    # tracker.* op uses (C11). DECISION, not a default: this op writes into
    # the LOCAL repo's own event shard via tracker_completion_policy.
    # emit_code_complete_assert, resolved from main_worktree_root(common_dir)
    # — never a holder/peer repo's root. An op missing from this table
    # silently degrades to repo_root=None, which would mis-scope this write
    # to the CENTRAL scope instead of the caller's own repo
    # (state/lessons/2026-07-06-compute-only-op-registration-needs-an-op.yaml);
    # do not omit this entry.
    "tracker.assert_code_complete":              "common_dir",
    # tracker.push_suggestion — common_dir, the SAME scope every other
    # tracker.* op uses (C4). DECISION, not a default: repo_root is always
    # the CALLING repo's own worktree, derived from main_worktree_root(
    # common_dir) — ownership resolution (local-vs-peer routing through
    # tracker_holder.write_root_for) happens INSIDE the handler, not via a
    # different repo_root scope. An op missing from this table silently
    # degrades to repo_root=None, which would break the D3 consistency check
    # and the local-write worktree derivation silently
    # (state/lessons/2026-07-06-compute-only-op-registration-needs-an-op.yaml);
    # do not omit this entry.
    "tracker.push_suggestion":                   "common_dir",
    # tracker.fold_ownership — common_dir, the SAME scope every other
    # tracker.* op uses. DECISION, not a default: this op reads the LOCAL
    # repo's own worktree store, derived via main_worktree_root(common_dir) —
    # never a holder/peer repo's root. An op missing from this table
    # silently degrades to repo_root=None, which would break that per-repo
    # confinement bound silently; do not omit this entry.
    "tracker.fold_ownership":                   "common_dir",
    "priority.set":                             "none",
    "priority.drain":                           "none",
    "plugin_health.forwarder_drift":            "none",
    # diagnostics.* — "none", and here that value is exact rather than merely
    # closest-fitting: these three probes read no state at all (not a path, not an
    # env var, not a param), so there is genuinely no per-request repo key to
    # derive and _origin_worktree is not required. Same class as ping, one step
    # purer — ping is the nearest precedent and the honest analogy.
    # Scope-table caveat, stated so a reader does not over-read this row: "none"
    # keys REPO STATE, not write-freedom (install.write_identity_file is "none"
    # and writes a file). The write-free-by-construction property these ops exist
    # for is carried by OP_CLASSIFICATION=COMPUTE_ONLY plus the module's own
    # empty import surface, not by this table.
    # Spec: docs/plans/2026-08-07-safe-target-for-transport-failure-probes.md § C1
    "diagnostics.always_succeeds":              "none",
    "diagnostics.always_refuses":               "none",
    "diagnostics.always_structural_pin":        "none",
    "gate.validate_invocable":                  "none",
    "gate_liveness.resolve":                    "show_top",
    "gate_liveness.reconcile":                  "show_top",
    "op_census.breaches":                       "show_top",
    "freshness.commit_delta":                   "show_top",
    "merge_assemble.apply":                      "show_top",
    "baton_assemble.brief":                      "show_top",
    "baton_assemble.apply":                      "show_top",
    "learn_lessons_pipeline.brief":               "show_top",
    "learn_lessons_pipeline.apply":                "show_top",
    "lessons.extract":                            "show_top",
    "lessons.verify_extraction":                  "show_top",
    "doctrine.surface_split_regenerate":          "show_top",
    # fleet.mode_set / fleet.mode_show — "none", and exact rather than defaulted: the
    # record these ops read and write lives under `_settings_home.settings_home()`, which
    # session/fleet_mode.py resolves with no repo input at all. Neither handler accepts
    # or forwards repo_root (both take it for signature parity and ignore it), so there
    # is no repo-specific state to key. Per this table's own header, a production op
    # absent here is an oversight, not a silent promotion — hence the explicit rows.
    # Scope-table caveat, same one stated for diagnostics.* / install.write_identity_file
    # above: "none" keys REPO STATE, not write-freedom. fleet.mode_set is MUTATING in
    # OP_CLASSIFICATION and scope "none" here, and the two are not in tension.
    "fleet.mode_set":                            "none",
    "fleet.mode_show":                           "none",
    "warm.request_status":                       "none",
}

# ---------------------------------------------------------------------------
# Public parity surface (cross-repo contract) — DR § AC-1b
#
# OP_KEY_SCOPE       — read-only view of the op→scope keying table. A consumer
#                      (e.g. DoE's coordinator-core-shim) imports this to keep its
#                      _origin_worktree-injection allowlist in lock-step with the
#                      engine's registered scopes, instead of hand-mirroring it
#                      (which drifts silently the next time an op's scope changes).
# WORKTREE_SCOPED_OPS — the frozenset of ops that REQUIRE a top-level
#                      _origin_worktree envelope field (scope ∈ {common_dir, show_top}).
#                      Ops NOT in this set (central / none) ignore _origin_worktree.
#
# Parity-check contract (RAG-bait — the consumer test SHAPE, not raw set-equality):
#   A consumer allowlist A is CONSISTENT iff:
#     (1) A ⊆ WORKTREE_SCOPED_OPS                 — never inject on a non-scoped op
#     (2) (ops the consumer invokes) ∩ WORKTREE_SCOPED_OPS ⊆ A
#                                                 — never OMIT on a scoped op it calls
#   Raw set-equality (A == WORKTREE_SCOPED_OPS) is WRONG when the consumer invokes
#   only a subset of engine ops. See DR-208 / global-multiplex DR § AC-1b.
# ---------------------------------------------------------------------------
OP_KEY_SCOPE = _types.MappingProxyType(dict(_OP_KEY_SCOPE))
WORKTREE_SCOPED_OPS = frozenset(
    op for op, scope in _OP_KEY_SCOPE.items() if scope in ("common_dir", "show_top")
)
