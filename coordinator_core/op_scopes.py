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
    "ping":                                  "none",
    "invoke.from_argv":                      "none",
    "hooks.suggest_sonnet_research":         "none",
    "hooks.subagent_arrival_check":          "none",
    "hooks.cater_subagent_start":            "none",
    "hooks.nudge_unauthorized_handoff":      "none",
    "hooks.nudge_named_agent_report_delivery": "none",
    "hooks.postuse_advisory_dispatch":       "none",
    "hooks.nudge_autonomous_askuserquestion": "none",
    "hooks.sessionend_archive_session": "none",
    # hooks.* ops above, despite being MUTATING (see its own classification
    "hooks.watchdog_undischarged_next_move": "none",
    # payload["env"]["CLAUDE_PROJECT_DIR"] via a non-spawning git-toplevel
    "hooks.plan_persistence_check": "none",
    "hooks.runtime_tripwire_em_check": "none",
    "hooks.stop_dispatch": "none",
    "hooks.context_pressure_precompact":     "none",
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
    "hooks.guard_review_integrator_sidecar_intake": "common_dir",
    "hooks.nudge_plan_test_surface_tier":     "none",
    "hooks.preuse_agent_dispatch":            "none",
    "hooks.preuse_skill_dispatch":            "none",
    "hooks.preuse_search_dispatch":           "none",
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
    "hooks.session_start_register_doe_claude_root": "none",
    "hooks.session_start_register_published_engine": "none",
    "hooks.repin_cloud_engine_root":          "none",
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
    "warm_guard.evaluate":                   "none",
    "percolate.run":                         "none",
    "percolate.validate_store":              "none",
    "cruft_sweep.run":                       "none",
    "engine.drift":                          "none",
    # the OPERATOR's OWN machine-local plugin registry (settings-home resolved via
    "plugin_health.drift":                   "none",
    # app_session.launch / .census / .teardown — the launch target is a CONSUMING
    "app_session.launch":                    "common_dir",
    "app_session.census":                    "common_dir",
    "app_session.teardown":                  "common_dir",
    # OPERATOR's OWN machine-local plugin/consumer sentinel roots
    # (COORDINATOR_PLUGINS_ROOT / COORDINATOR_CONSUMER_HEALTH_ROOT env-resolved,
    "plugin_health.scan":                    "none",
    # probe suite against the OPERATOR's OWN machine (CLAUDE_HOME, settings-home,
    "plugin_health.sentinel":                "none",
    # cartography.* — fleet-generic, COMPUTE_ONLY ops (coordinator-core-op-target-
    "cartography.tree":                      "none",
    "cartography.file_index":                "none",
    "cartography.symbols":                   "none",
    "cartography.edges":                     "none",
    "cartography.op_edges":                  "none",
    "docindex.emit":                         "none",
    # derived SCRIPT_DIR/REPO_ROOT from its own BASH_SOURCE location.
    "goals.reassess_krs":                    "none",
    "goal.set_kr_status":                    "none",
    # workflow.validate — fleet-generic, COMPUTE_ONLY op (mirrors the
    "workflow.validate":                     "none",
    "workflow.bind_args":                    "none",
    "distill.curate_clusters":               "none",
    "distill.workflow_input":                "none",
    # workflow.scaffold — fleet-generic, COMPUTE_ONLY op: pure generation
    "workflow.scaffold":                     "none",
    # compute_layer.scaffold — fleet-generic, COMPUTE_ONLY op with two modes,
    "compute_layer.scaffold":                "none",
    # dispatch.emit — fleet-generic, MUTATING op: reads a caller-supplied plan
    "dispatch.emit":                         "none",
    # review.mint_workflow — fleet-generic, MUTATING op: reads a caller-
    "review.mint_workflow":                  "none",
    # strategic.generate — fleet-generic, MUTATING op (mirrors the cartography/
    # workflow target-resolution model): explicit REQUIRED `target_root` wire
    "strategic.generate":                    "none",
    # strategic.emit — fleet-generic, MUTATING op (mirrors strategic.generate's target-
    # resolution model): explicit REQUIRED `target_root` wire param, any repo, NOT the
    "strategic.emit":                        "none",
    "fleet.archive_completed_handoffs":      "common_dir",
    "housekeeping.cycle":                    "common_dir",
    "fleet.aggregate_capability_index":      "common_dir",
    "fleet.reap_unintegrated_findings":      "common_dir",
    "fleet.reap_integrated_findings":        "common_dir",
    "fleet.reap_review_trail_rest":          "common_dir",
    # fleet.handoffs_for_plan — COMPUTE_ONLY read op, but still "common_dir": unlike
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
    "handoff.ship_and_archive":              "common_dir",
    "handoff.backfill_claim_stamp":          "common_dir",
    "handoff.repoint_origin":                "common_dir",
    "handoff.close_origin_stub":             "common_dir",
    "handoff.normalize":                     "common_dir",
    "initiative.serve_set":                  "common_dir",
    "roadmap.link_stubs":                    "common_dir",
    "roadmap.plan_gate":                     "common_dir",
    "roadmap.blitz_land":                    "common_dir",
    # ROOT-EXISTENCE leg), under main_worktree_root(common_dir). Without this
    "plan.prep_gate":                        "common_dir",
    "plan.stamp_prepped":                    "common_dir",
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
    # sender-side COMPUTE_ONLY sweep over the CALLING repo's own sent-ledger, whose
    "memo.check_deliveries":                  "common_dir",
    "memo.heal_inbox":                        "common_dir",
    "memo.draft":                             "common_dir",
    "memo.compose":                           "common_dir",
    "memo.list_outbox":                       "common_dir",
    "memo.blitz_buckets":                     "common_dir",
    "push.outstanding":                      "common_dir",
    "p4.register_workspace":                 "common_dir",
    "p4.session_state":                      "common_dir",
    # completion.* mutate a caller-supplied docs/plans/*.md path. All MUTATING, sanctioned by DR-216.
    "changelog.append_day":                  "common_dir",
    "changelog.backfill_gaps":               "common_dir",
    "changelog.inject_anchor":               "common_dir",
    # changelog.compute_day_fields — COMPUTE_ONLY sibling of changelog.append_day
    "changelog.compute_day_fields":          "common_dir",
    "changelog.upsert_reviewed":             "common_dir",
    "plan.append_session":                   "common_dir",
    # Backfill: records.query (strang-11 C1a COMPUTE_ONLY read op) was registered without an
    # _OP_KEY_SCOPE entry by a concurrent session, leaving the key-scope coverage gate RED on HEAD.
    "records.query":                         "common_dir",
    "records.history":                        "none",
    "handoff.columns":                       "common_dir",
    "memo.triage":                            "common_dir",
    "updatedocs.gates":                       "common_dir",
    "distill.scope":                          "common_dir",
    # and the op raises rather than silently no-op'ing (this op is MUTATING, not
    # COMPUTE_ONLY — a resolved write target is mandatory).
    "memo.fate_partition":                    "common_dir",
    # COMPUTE_ONLY (no shard write, no memo.transition call) — without this
    "memo.fate_backfill":                      "common_dir",
    "distill.curation_status":                "common_dir",
    "distill.assemble_disposal_manifest":     "common_dir",
    "distill.stamp_disposal":                  "common_dir",
    "distill.apply_disposal":                  "common_dir",
    "crossrepo.closure_status":               "common_dir",
    # coordinator_core/DIRECTORY.md, docs/plans/*.md, cross-repo/archive/*.md, tasks/*.md,
    "ceremony.update_docs_scan":               "common_dir",
    "deliverable.cascade_retract":              "common_dir",
    "deliverable.cascade_backstop_sweep":       "common_dir",
    "deliverable.cascade_divergence_report":    "common_dir",
    "deliverable.fork_detect":                  "common_dir",
    "sizing.decline":                           "common_dir",
    "sizing.ship":                               "common_dir",
    "sizing.discharge_surfaced":                 "common_dir",
    "sizing.record_spike_verdict":               "common_dir",
    "sizing.read_object_fields":                 "common_dir",
    "plan.tasks.spine_drift_check":              "common_dir",
    # "fleet.archive_shipped_handoffs" REMOVED -- op key SUBSUMED (not
    "fleet.backfill_dispositionless_memos":  "common_dir",
    "fleet.backfill_reference_edges":        "common_dir",
    "session.commits":                       "common_dir",
    # session.warm_start — none. DECISION, not a default: this op is
    "session_baton.mint":                    "none",
    "session_baton.promote":                 "none",
    "baton.carry_forward":                   "common_dir",
    "baton.carry_forward_read":              "common_dir",
    "session.reap":                          "common_dir",
    "session.audit_unreapable":               "common_dir",
    # session.boot_sweep — GRAVESTONED 2026-08-27, K-059. No scope row, because
    # op_budget_suspension.SUSPENDED_OPS, which is the refusal door and needs no
    "session.reap_claims_for_repos":         "none",
    "session.record_pickup":                 "none",
    "session.scope_report":                  "none",
    # session.safe_commit_offer — MUTATING commit+push of THIS session's own
    # LOAD-BEARING on the warm path: this process is the server, whose
    "session.safe_commit_offer":             "none",
    # session.guard_settings_integrity — MUTATING (classification.py) but scope "none":
    # falls back to CLAUDE_CONFIG_DIR/$HOME/.claude env resolution — never from
    "session.guard_settings_integrity":      "none",
    # kill-switch marker, resolved from CLAUDE_CONFIG_DIR/$HOME/.claude, never from
    "session.guard_hooks_kill_switch_detail": "none",
    # claude_config_dir() (CLAUDE_CONFIG_DIR/$HOME/.claude) and never from repo_root →
    "session.resolve_address":                "none",
    "session.whoami_live":                    "none",
    "session.peer_roster":                    "none",
    "groupem.enter":                          "none",
    # repo-scoped path passed VERBATIM as the wire-level repo_root param,
    "groupem.stamp":                          "none",
    "groupem.resolve_addressee":              "none",
    "groupem.idle_report":                    "none",
    # Deliberately DIFFERENT from session.peer_roster's "none" just above:
    "session.work_state":                     "common_dir",
    # (C5). Deliberately DIFFERENT from session.work_state's "common_dir"
    "fleet.work_state":                       "none",
    "fleet.record_history":                   "none",
    "session.artifact_owner":                 "none",
    "handoff.author_fork":                   "common_dir",
    "plan.persist_capture":                  "common_dir",
    "plan.tasks.mutate":                     "common_dir",
    "plan.tasks.grouping_digest":             "common_dir",
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
    "fleet.archive_sweep_status":            "common_dir",
    "fleet.migrate_handoff_vocabulary":       "common_dir",
    "fleet.archive_terminal_sizings":         "common_dir",
    "git_branch.compute_descendant_tip":      "common_dir",
    "git_branch.detect_unpushed_commits":     "show_top",
    "git_branch.list_unmerged_work":          "common_dir",
    "git_branch.verify_commit_in_review_window": "common_dir",
    # scratchpad.sweep — "none", MUTATING (dry-run by default; `reclaim: true`
    "scratchpad.sweep":                       "none",
    "cartography.count_references":           "none",
    "doctrine.assert_cross_reference_counts": "common_dir",
    "percolate.check_inverse_drift":          "none",
    "percolate.build_token_index":            "none",
    # OPERATOR's own machine-local registry, neither of which is the caller's own
    "repo.clone_and_register":                "none",
    "release.cut_tag":                        "common_dir",
    "release.cut_tag_and_publish":             "common_dir",
    "dependency.detect_changed_manifests":    "show_top",
    "detect.plugin_layout":                   "none",
    "detect.primary_languages":                "none",
    "cartography.stack":                       "none",
    # MUTATING (unlike its COMPUTE_ONLY siblings) because emit=true writes
    "cartography.chunk_table":                 "none",
    # COMPUTE_ONLY — two read-only `git log` invocations, no writes.
    # Registered EXPLICITLY rather than defaulting to "none" by omission,
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
    "update_docs.probe_fresh_repo_noop":       "common_dir",
    # — "none": both inspect the OPERATOR's own machine (coordinator-claude
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
    "findings.self_persist_fallback":          "none",
    "review.snapshot_diff_and_head":           "show_top",
    "review.freeze_diff":                      "show_top",
    "workday.stitch_sidecar_into_summary":     "common_dir",
    "repo_setup.validate_target_root":         "none",
    "research.verify_scout_inventory_completeness": "common_dir",
    # install.write_identity_file — "none": writes to CLAUDE_HOME/.claude/
    "install.write_identity_file":              "none",
    "baton.resolve_path_and_repo":              "none",
    "branch.merge_into_workstream":             "show_top",
    "bug_sweep.verify_fix_files_changed":       "show_top",
    "commit.exec_bit_change":                   "common_dir",
    "ceremony.commit_v2":                       "common_dir",
    "fanout.poll_scratch_dir":                  "none",
    "machine.hibernate":                        "none",
    "percolate.run_pre_ci_hooks":               "none",
    "percolate.scan_content_leakage_tiers":     "none",
    "repo.create_and_push_remote":              "common_dir",
    "repo_setup.copy_console_subprocess_tripwire": "show_top",
    "research.archive_workdir":                 "common_dir",
    "research.restructure_for_repeat_topic":    "common_dir",
    "session.resolve_chain_terminal_disposition": "common_dir",
    # tasks/orphan-sweep-notes.md write target (GIT_ROOT-worktree-rooted per
    "session.rotate_orphan_sweep_log":          "common_dir",
    "workday.surface_auto_push_failure_stats":  "show_top",
    # (staged/unstaged diff, MERGE_HEAD, upstream ahead/behind) is
    "git.push_failure_verdict":                 "show_top",
    # BACKFILL, not this op's author: registered in _registry_map.py by
    "git.maintenance":                          "common_dir",
    "tracker.advance_status":                   "common_dir",
    # C5). DECISION, not a default: common_dir scoping means one
    # sovereign-tracker store shared across all LINKED WORKTREES of the same
    "tracker.fold_observed_set":                "common_dir",
    # and session.boot_sweep use. DECISION, not a default: this op's WRITE
    "tracker.mint_person":                      "common_dir",
    # tracker.fold_observed_set use. DECISION, not a default: this op's
    "tracker.assign":                           "common_dir",
    # op uses. DECISION, not a default: render_status reads the LOCAL repo's
    "tracker.render_status":                    "common_dir",
    # tracker.* op uses (C11). DECISION, not a default: this op writes into
    "tracker.assert_code_complete":              "common_dir",
    # tracker.* op uses (C4). DECISION, not a default: repo_root is always
    "tracker.push_suggestion":                   "common_dir",
    # tracker.* op uses. DECISION, not a default: this op reads the LOCAL
    "tracker.fold_ownership":                   "common_dir",
    "priority.set":                             "none",
    "priority.drain":                           "none",
    "plugin_health.forwarder_drift":            "none",
    # for is carried by OP_CLASSIFICATION=COMPUTE_ONLY plus the module's own
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
    # above: "none" keys REPO STATE, not write-freedom. fleet.mode_set is MUTATING in
    # OP_CLASSIFICATION and scope "none" here, and the two are not in tension.
    "fleet.mode_set":                            "none",
    "fleet.mode_show":                           "none",
    "warm.request_status":                       "none",
}

# OP_KEY_SCOPE       — read-only view of the op→scope keying table. A consumer
# WORKTREE_SCOPED_OPS — the frozenset of ops that REQUIRE a top-level
#   A consumer allowlist A is CONSISTENT iff:
#     (1) A ⊆ WORKTREE_SCOPED_OPS                 — never inject on a non-scoped op
#     (2) (ops the consumer invokes) ∩ WORKTREE_SCOPED_OPS ⊆ A
#   Raw set-equality (A == WORKTREE_SCOPED_OPS) is WRONG when the consumer invokes
OP_KEY_SCOPE = _types.MappingProxyType(dict(_OP_KEY_SCOPE))
WORKTREE_SCOPED_OPS = frozenset(
    op for op, scope in _OP_KEY_SCOPE.items() if scope in ("common_dir", "show_top")
)
