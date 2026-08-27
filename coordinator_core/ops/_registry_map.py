"""
coordinator_core.ops._registry_map — op-name -> owning-module lazy-import seam.

Purpose: seed a name->module map so dispatch_message (ipc.py) can import ONLY the
module that owns a given op on a registry MISS, instead of eagerly importing all
~55 op modules at process startup (F6 / cold-start tax — Windows has no
__pycache__ warm-up, so compiling 55 modules to run one wastes ~150-250ms per
invoke). This module contains ONLY dotted-path strings — it must NOT import any
op module itself, or it would defeat its own purpose (and risk a circular import
with coordinator_core.ipc, which every op module imports register_op from).

Maintenance: this map is a PERFORMANCE OPTIMIZATION, not a correctness gate. If an
entry is missing or stale (an op renamed/moved without updating this table), the
lazy-import miss path in ipc.dispatch_message falls back to importing the whole
coordinator_core.ops package (today's eager behavior) and retries — so a stale/
incomplete map degrades to today's correctness, never to a broken dispatch. Keep
this in sync with coordinator_core/ops/__init__.py's import list on a best-effort
basis; the fallback is the enforcement mechanism, not a hand-audit.

Spec backlink: pln-claude-klabauter-windows-portability-a48fac § C4
"""

from __future__ import annotations

from typing import Dict

# op-name -> dotted module path whose import triggers that op's register_op(...)
# side-effect. Some modules register multiple related ops (e.g. coordinator_core.hooks
# registers all 10 hooks.* ops in one import) — those ops share the same module value.
#
# Every key here must ALSO be reachable from the eager-import path
# (coordinator_core/ops/__init__.py::_EAGER_OP_MODULES), or the op registers only under
# whichever import order happens to pull its module in — see coordinator_core/hooks/
# __init__.py for the order-dependent drift-guard failure that shape produces.
OP_MODULE_MAP: Dict[str, str] = {
    "ping":                                   "coordinator_core.ops.ping",
    "invoke.from_argv":                       "coordinator_core.ops.invoke_from_argv",
    "cutover.gate":                           "coordinator_core.ops.cutover_gate",
    "cutover.advance":                        "coordinator_core.ops.cutover_advance",
    # decision_record.mint_id / decision_record.release_id — one module registers
    # both ops, same shared-value shape as the hooks.* / spec_backlink.* blocks.
    # Spec: state/improvement-queue/2026-08-23-nothing-allocates-dr-numbers-so-a-
    # plan-s-7aa417a58bce.yaml
    "decision_record.mint_id":                "coordinator_core.ops.decision_record_mint",
    "decision_record.release_id":             "coordinator_core.ops.decision_record_mint",
    "handoff.blocked_by_dependents":          "coordinator_core.ops.handoff_children",
    # peer_notice.send / peer_notice.check — same-repo peer-contention notice channel
    # (see op_scopes.py::_OP_KEY_SCOPE's peer_notice.* entries, both "common_dir"),
    # each registered by its own
    # owning module. Registered in _REGISTRY and _OP_KEY_SCOPE/OP_CLASSIFICATION but
    # absent from this map until C3's three-way reconciliation (docs/plans/
    # 2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md § C3) — a real
    # registry_map.py::OP_MODULE_MAP gap, not a deliberate omission; per this
    # module's docstring the absence degraded silently to the eager-import
    # fallback rather than breaking dispatch, which is why it went unnoticed.
    "peer_notice.send":                       "coordinator_core.ops.peer_notice_send",
    "peer_notice.check":                      "coordinator_core.ops.peer_notice_check",
    "op_census.breaches":                     "coordinator_core.ops.op_budget_breaches",
    # coordinator_core.hooks registers all 16 hooks.* ops (6 advisory + 8 bookkeeping
    # + 1 pull/poll arrival-check + 1 subagent-fabrication check) in a single module
    # import. This package-level
    # granularity (one shared module value for all 15 keys, rather than a per-op
    # owning submodule) IS the correct mapping here, not a stand-in for a finer
    # split — confirmed C2 (docs/plans/2026-08-06-windows-hot-path-less-work-per-
    # interpreter.md): under the lazy hooks channel (C1), importing the shared
    # "coordinator_core.hooks" value alone is a lazy-gated no-op that registers
    # nothing, so ipc._lazy_import_and_lookup adds a hooks-scoped fallback stage
    # (coordinator_core.hooks._eager_import_all()) ahead of the ops-wide SAFE
    # FALLBACK, rather than repointing these entries to nonexistent per-op
    # submodules.
    "hooks.nudge_foreground_agent_dispatch":  "coordinator_core.hooks",
    "hooks.nudge_named_agent_report_delivery": "coordinator_core.hooks",
    "hooks.nudge_em_code_dispatch":           "coordinator_core.hooks",
    "hooks.track_touched_files":              "coordinator_core.hooks",
    "hooks.session_heartbeat":                "coordinator_core.hooks",
    "hooks.agent_completion_log":             "coordinator_core.hooks",
    "hooks.track_dispatched_agents":          "coordinator_core.hooks",
    "hooks.agent_postuse_dispatch":           "coordinator_core.hooks",
    "hooks.suggest_sonnet_research":          "coordinator_core.hooks",
    "hooks.nudge_unauthorized_handoff":       "coordinator_core.hooks",
    "hooks.postuse_advisory_dispatch":        "coordinator_core.hooks",
    "hooks.context_pressure_precompact":      "coordinator_core.hooks",
    "hooks.subagent_review_mark":             "coordinator_core.hooks",
    "hooks.subagent_zero_tool_use":           "coordinator_core.hooks",
    "hooks.subagent_zero_tool_use_surface":   "coordinator_core.hooks",
    "hooks.subagent_zero_tool_use_resolve":   "coordinator_core.hooks",
    "hooks.subagent_arrival_check":           "coordinator_core.hooks",
    "hooks.subagent_fabrication_check":       "coordinator_core.hooks",
    "hooks.receiver_state_sensor":            "coordinator_core.hooks",
    "hooks.subagent_sidecar_fill_check":      "coordinator_core.hooks",
    "hooks.cater_subagent_start":             "coordinator_core.hooks",
    "backlog.record":                         "coordinator_core.ops.emit.recorder",
    "goal.append":                            "coordinator_core.ops.goal_append",
    "goal.close_day":                         "coordinator_core.ops.goal_close_day",
    "goal.close_day_apply":                   "coordinator_core.ops.goal_close_day",
    "goals.reassess_krs":                     "coordinator_core.goals.reassess_krs",
    "orientation.regenerate_cache":            "coordinator_core.orientation.regenerate_cache",
    "fleet.archive_completed_handoffs":       "coordinator_core.ops.fleet.archive_terminal_handoffs",
    "fleet.archive_actioned_memos":           "coordinator_core.ops.fleet.archive_actioned_memos",
    "fleet.archive_sweep_status":             "coordinator_core.ops.fleet.sweep_status",
    "fleet.handoffs_for_plan":                "coordinator_core.ops.fleet.plan_handoffs",
    "fleet.work_state":                       "coordinator_core.ops.fleet.work_state",
    "fleet.record_history":                   "coordinator_core.ops.fleet.record_history",
    "fleet.aggregate_capability_index":       "coordinator_core.ops.fleet.capability_index",
    "distill.curate_clusters":                "coordinator_core.ops.distill_curate_clusters",
    "gate_liveness.resolve":                  "coordinator_core.ops.gate_liveness.resolve",
    "gate_liveness.reconcile":                "coordinator_core.ops.gate_liveness.reconcile",
    "memo.fate_backfill":                     "coordinator_core.ops.memo_fate_backfill",
    "updatedocs.gates":                       "coordinator_core.ops.updatedocs_gates",
    "commit.anchors":                         "coordinator_core.ops.commit_anchors",
    "memo.transition":                        "coordinator_core.ops.memo_transition",
    "handoff.transition":                     "coordinator_core.ops.handoff_transition",
    "handoff.stamp":                          "coordinator_core.ops.handoff_stamp",
    "handoff.stamp_phase":                    "coordinator_core.ops.handoff_phase_stamp",
    "handoff.backfill_claim_stamp":           "coordinator_core.ops.handoff_backfill_claim_stamp",
    "handoff.ship_and_archive":               "coordinator_core.ops.handoff_ship_archive",
    "handoff.repoint_origin":                 "coordinator_core.ops.handoff_repoint_origin",
    "handoff.normalize":                      "coordinator_core.ops.handoff_normalize",
    "handoff.correct_body":                   "coordinator_core.ops.handoff_correct_body",
    "handoff.discharge_criteria":             "coordinator_core.ops.handoff_discharge_criteria",
    "handoff.author_lint":                     "coordinator_core.ops.handoff_author_lint",
    "handoff.append_session_ledger":           "coordinator_core.ops.handoff_append_session_ledger",
    "handoff.propagate":                      "coordinator_core.ops.propagate_body",
    "plan.propagate":                         "coordinator_core.ops.propagate_body",
    "goal.match_candidates":                  "coordinator_core.ops.goals_match",
    "goal.set_kr_status":                     "coordinator_core.ops.goal_kr_status",
    "plan.match_candidates":                  "coordinator_core.ops.plan_match",
    "plan.persist_capture":                   "coordinator_core.ops.plan_capture_persist",
    "handoff.match_candidates":               "coordinator_core.ops.handoff_match",
    "initiative.serve_set":                   "coordinator_core.ops.initiatives_serve",
    # roadmap.link_stubs — DR-264, chunk C4 (docs/plans/2026-08-05-roadmap-graph-
    # enforcement-gap.md): the first op that AUTHORS a blocked_by/blocks
    # roadmap-dependency edge (reciprocal, two-file compound transaction).
    "roadmap.link_stubs":                     "coordinator_core.ops.roadmap_link_stubs",
    "queue.append":                           "coordinator_core.ops.queue_append",
    "queue.cluster":                          "coordinator_core.ops.queue_cluster",
    "queue.promote":                          "coordinator_core.ops.queue_promote",
    "memo.list":                              "coordinator_core.ops.fleet.memo_list",
    "memo.draft":                             "coordinator_core.ops.fleet.memo_draft",
    # memo.send — rebuilt 2026-08-25 (docs/plans/2026-08-25-memo-send-three-
    # writes-and-one-commit-th.md § C2) after the 2026-08-23 kill (K-050).
    # NOT a resurrection of the killed module — three-write shape only.
    "memo.send":                              "coordinator_core.ops.fleet.memo_send",
    "memo.reconcile_outbox":                  "coordinator_core.ops.fleet.memo_reconcile_outbox",
    "memo.compose":                           "coordinator_core.ops.fleet.memo_compose",
    "memo.list_outbox":                       "coordinator_core.ops.fleet.memo_list_outbox",
    "memo.blitz_buckets":                     "coordinator_core.ops.fleet.memo_blitz_buckets",
    "memo.check_addressee":                   "coordinator_core.ops.fleet.memo_check_addressee",
    "deliverable.rollup":                     "coordinator_core.ops.deliverable_rollup",
    # spec_backlink.resolve / spec_backlink.rewrite — one module registers both ops,
    # same shared-value shape as the hooks.* block above.
    # Spec: pln-spec-backlinks-cite-a-stable-d-451b3e § C1
    "spec_backlink.resolve":                  "coordinator_core.ops.spec_backlink_resolve",
    "spec_backlink.rewrite":                  "coordinator_core.ops.spec_backlink_resolve",
    "sizing.decline":                          "coordinator_core.ops.sizing_decline",
    "sizing.ship":                              "coordinator_core.ops.sizing_ship",
    "sizing.record_spike_verdict":              "coordinator_core.ops.sizing_spike_verdict",
    "sizing.read_object_fields":                "coordinator_core.ops.read_sizing_object_fields",
    "deliverable.cascade_retract":             "coordinator_core.ops.cascade_retract",
    "deliverable.cascade_backstop_sweep":      "coordinator_core.ops.cascade_backstop_sweep",
    "deliverable.fork_detect":                 "coordinator_core.ops.deliverable_fork_detect",
    "push.outstanding":                       "coordinator_core.ops.push_outstanding",
    "records.query":                          "coordinator_core.ops.records_query",
    "records.history":                        "coordinator_core.ops.record_history",
    "handoff.columns":                        "coordinator_core.ops.handoff_columns_query",
    "changelog.append_day":                   "coordinator_core.ops.changelog_ops",
    "changelog.backfill_gaps":                "coordinator_core.ops.changelog_ops",
    "changelog.compute_day_fields":           "coordinator_core.ops.changelog_ops",
    "changelog.inject_anchor":                "coordinator_core.ops.changelog_ops",
    "changelog.upsert_reviewed":              "coordinator_core.ops.changelog_ops",
    "cruft_sweep.run":                        "coordinator_core.ops.cruft_sweep",
    "plan.append_session":                    "coordinator_core.ops.completion_ops",
    "review_trail.readjudication_report":     "coordinator_core.ops.review_trail_readjudication_report",
    "fleet.backfill_dispositionless_memos":   "coordinator_core.ops.fleet.backfill_memo_disposition",
    "fleet.reap_unintegrated_findings":       "coordinator_core.ops.fleet.reap_unintegrated_findings",
    "fleet.reap_integrated_findings":         "coordinator_core.ops.fleet.reap_integrated_findings",
    "session.reap":                           "coordinator_core.ops.session.reap",
    "session.reap_claims_for_repos":          "coordinator_core.ops.session.reap",
    "session.audit_unreapable":               "coordinator_core.ops.session.reap",
    "session.guard_settings_integrity":       "coordinator_core.ops.session.guard_settings_integrity",
    "session.guard_hooks_kill_switch_detail": "coordinator_core.ops.session.guard_settings_integrity",
    "session.record_pickup":                  "coordinator_core.ops.session.record_pickup",
    "session.scope_report":                   "coordinator_core.ops.session.scope_report",
    "session.safe_commit_offer":              "coordinator_core.ops.session.safe_commit_offer",
    "session.resolve_address":                "coordinator_core.ops.session_resolve_address",
    "session.peer_roster":                    "coordinator_core.ops.session_peer_roster",
    "session.work_state":                     "coordinator_core.ops.session_work_state",
    "session.artifact_owner":                 "coordinator_core.ops.session_artifact_owner",
    "handoff.author_fork":                    "coordinator_core.ops.handoff_author_fork",
    "handoff.scaffold_from_queue":            "coordinator_core.ops.queue_scaffold_baton",
    "handoff.lineage_ancestry":               "coordinator_core.ops.handoff_lineage_ancestry",
    "plan.tasks.mutate":                      "coordinator_core.ops.plan_tasks_mutate",
    "plan.tasks.grouping_digest":             "coordinator_core.ops.plan_tasks_grouping_digest",
    "plan.tasks.spine_drift_check":           "coordinator_core.ops.plan_tasks_spine_drift_check",
    "engine.drift":                           "coordinator_core.ops.engine_drift",
    "plugin_health.drift":                    "coordinator_core.plugin_health.drift",
    "plugin_health.scan":                     "coordinator_core.plugin_health.scan",
    "plugin_health.sentinel":                 "coordinator_core.plugin_health.sentinel",
    "plugin_health.forwarder_drift":          "coordinator_core.plugin_health.forwarder_drift",
    "cartography.tree":                       "coordinator_core.ops.cartography_tree",
    "cartography.file_index":                 "coordinator_core.ops.cartography_file_index",
    "cartography.symbols":                    "coordinator_core.ops.cartography_symbols",
    "cartography.edges":                      "coordinator_core.ops.cartography_edges",
    "cartography.op_edges":                   "coordinator_core.ops.cartography_op_edges",
    "memo.triage":                            "coordinator_core.ops.memo_triage",
    "distill.scope":                          "coordinator_core.ops.distill_scope",
    "distill.workflow_input":                 "coordinator_core.ops.distill_workflow_input",
    "memo.fate_partition":                    "coordinator_core.ops.memo_fate_partition",
    "workflow.validate":                      "coordinator_core.ops.workflow_validate",
    "workflow.scaffold":                      "coordinator_core.ops.workflow_scaffold",
    "compute_layer.scaffold":                 "coordinator_core.ops.compute_layer_scaffold.op",
    "dispatch.emit":                          "coordinator_core.ops.dispatch_emit.op",
    "workflow.fire":                          "coordinator_core.ops.workflow_fire.op",
    "workflow.fire_status":                   "coordinator_core.ops.workflow_fire.op",
    "review.mint_workflow":                   "coordinator_core.ops.review_mint.op",
    "strategic.generate":                     "coordinator_core.ops.strategic_generate",
    "strategic.emit":                         "coordinator_core.ops.strategic_emit",
    "handoff.close_origin_stub":              "coordinator_core.ops.handoff_close_origin_stub",
    "session_hierarchy.derive":               "coordinator_core.ops.session_hierarchy_derive",
    "session_ledger.aggregate_chain_loe":     "coordinator_core.session_ledger.aggregate_chain_loe",
    "deferral.detect_orphan_memo":             "coordinator_core.ops.deferral_detect_orphan_memo",
    "deferral.detect_partial_strangle":        "coordinator_core.ops.deferral_detect_partial_strangle",
    "schema.describe":                        "coordinator_core.frontmatter.schema_cli",
    "schema.validate":                        "coordinator_core.frontmatter.schema_cli",
    "fleet.archive_release_accumulator":      "coordinator_core.ops.fleet.archive_release_accumulator",
    "fleet.archive_paper_trail":              "coordinator_core.ops.fleet.archive_paper_trail",
    "fleet.archive_queue_entry":              "coordinator_core.ops.fleet.archive_queue_entry",
    "fleet.migrate_handoff_vocabulary":       "coordinator_core.ops.fleet.migrate_handoff_vocabulary",
    "fleet.archive_terminal_sizings":         "coordinator_core.ops.fleet.archive_sizings",
    "git_branch.compute_descendant_tip":      "coordinator_core.ops.orphan_branch_sweep",
    "git_branch.detect_unpushed_commits":     "coordinator_core.ops.orphan_branch_sweep",
    "git_branch.list_unmerged_work":          "coordinator_core.ops.orphan_branch_sweep",
    "git_branch.verify_commit_in_review_window": "coordinator_core.ops.orphan_branch_sweep",
    "cartography.count_references":           "coordinator_core.ops.cartography_edges",
    "doctrine.assert_cross_reference_counts": "coordinator_core.ops.assert_doctrine_cross_reference_counts",
    "repo.clone_and_register":                "coordinator_core.ops.repo_bootstrap",
    "release.cut_tag":                        "coordinator_core.ops.release_tagging",
    "release.cut_tag_and_publish":            "coordinator_core.ops.release_tagging",
    "dependency.detect_changed_manifests":    "coordinator_core.ops.detect_changed_dependency_manifests",
    "detect.plugin_layout":                   "coordinator_core.ops.detect_plugin_layout",
    "detect.primary_languages":               "coordinator_core.ops.detect_primary_languages",
    "cartography.stack":                      "coordinator_core.ops.cartography_stack",
    "cartography.chunk_table":                "coordinator_core.ops.cartography_chunk_table",
    "install.detect_python3_appx_stub":       "coordinator_core.ops.ensure_python3_exe_shim",
    "lessons.filter_undated_universal":       "coordinator_core.ops.lessons_filter",
    "lessons.reject_orphan_strip_entries":    "coordinator_core.ops.lessons_filter",
    "completion.flip_to_released":            "coordinator_core.ops.completion_ops",
    "install.clone_idempotent":               "coordinator_core.install.clone_sibling_repo",
    "ceremony.init_anchor_injection_state":   "coordinator_core.ops.init_anchor_injection_state",
    "install.write_shell_rc_guard_block":     "coordinator_core.install.shell_rc_guard",
    "install.wrapper_onto_path":              "coordinator_core.install.wrapper_onto_path",
    "percolate.list_files_newer_than_marker": "coordinator_core.ops.list_files_newer_than_marker",
    "plan.list_stale_executing":              "coordinator_core.ops.draft_plan_aging",
    "plan.list_orphaned":                     "coordinator_core.ops.draft_plan_aging",
    "plan.suggest_completion_steps":          "coordinator_core.ops.plan_suggest_completion_steps",
    "cli.parse_flag":                         "coordinator_core.ops.parse_cli_args",
    "cli.parse_date_flags":                   "coordinator_core.ops.parse_cli_args",
    "merge.quiet_activity_gate":              "coordinator_core.ops.merge_quiet_activity_gate",
    "schema.drift_gate":                      "coordinator_core.ops.schema_drift_gate",
    "update_docs.probe_fresh_repo_noop":      "coordinator_core.ops.probe_fresh_repo_noop",
    "install.probe_skill_frontmatter_valid":  "coordinator_core.install.prereq_probe",
    "install.probe_windows_terminal_presence": "coordinator_core.install.prereq_probe",
    "mcp.resolve_server_cli_path":            "coordinator_core.ops.resolve_mcp_server_cli_path",
    "baton.resolve_swept_in_archive":         "coordinator_core.ops.resolve_swept_baton",
    "ci.run_pip_audit":                       "coordinator_core.ops.run_pip_audit",
    "ci.run_semgrep_scan":                    "coordinator_core.ops.run_semgrep_scan",
    "ci.run_shellcheck_sweep":                "coordinator_core.ops.run_shellcheck_sweep",
    "review_trail.scan_unresolved_ubt":       "coordinator_core.ops.scan_unresolved_ubt_records",
    "findings.self_persist_fallback":         "coordinator_core.ops.self_persist_findings",
    "review.snapshot_diff_and_head":          "coordinator_core.ops.ceremony.snapshot_diff_and_head",
    "review.freeze_diff":                     "coordinator_core.ops.review_freeze_diff",
    "workday.stitch_sidecar_into_summary":    "coordinator_core.ops.workday_stitch_sidecar_summary",
    "workday.drain_pending_push":             "coordinator_core.ops.workday_drain_pending_push",
    "repo_setup.validate_target_root":        "coordinator_core.ops.bootstrap_repo",
    "research.verify_scout_inventory_completeness": "coordinator_core.ops.verify_scout_inventory_completeness",
    "install.write_identity_file":            "coordinator_core.ops.write_identity_file",
    "research.archive_workdir":               "coordinator_core.ops.research_archive_workdir",
    "research.restructure_for_repeat_topic":  "coordinator_core.ops.research_dir_restructure",
    "session.rotate_orphan_sweep_log":        "coordinator_core.ops.session.rotate_orphan_sweep_log",
    "repo.create_and_push_remote":            "coordinator_core.ops.create_github_remote",
    "branch.merge_into_workstream":           "coordinator_core.ops.merge_branch_into_workstream",
    "repo_setup.copy_console_subprocess_tripwire": "coordinator_core.ops.copy_plugin_template",
    "workday.surface_auto_push_failure_stats": "coordinator_core.ops.workday_surface_auto_push_failure_stats",
    "git.push_failure_verdict":               "coordinator_core.ops.push_failure_verdict",
    "bug_sweep.verify_fix_files_changed":     "coordinator_core.ops.verify_fix_files_changed",
    "machine.hibernate":                      "coordinator_core.ops.hibernate_machine",
    "commit.exec_bit_change":                 "coordinator_core.ops.ceremony.commit_exec_bit",
    "ceremony.commit_v2":                     "coordinator_core.ops.ceremony.commit_v2",
    "percolate.run_pre_ci_hooks":             "coordinator_core.ops.run_pre_ci_hooks",
    "percolate.scan_content_leakage_tiers":   "coordinator_core.ops.scan_content_leakage",
    "session.resolve_chain_terminal_disposition": "coordinator_core.ops.session.resolve_chain_terminal_disposition",
    "baton.resolve_path_and_repo":            "coordinator_core.ops.resolve_baton_path",
    "fanout.poll_scratch_dir":                "coordinator_core.ops.poll_scratch_dir",
    "scratchpad.sweep":                       "coordinator_core.ops.scratchpad_sweep",
    "distill.curation_status":                "coordinator_core.ops.distill_curation_status",
    "distill.assemble_disposal_manifest":     "coordinator_core.ops.distill_disposal_manifest",
    "distill.stamp_disposal":                 "coordinator_core.ops.distill_stamp_disposal",
    "distill.apply_disposal":                 "coordinator_core.ops.distill_apply_disposal",
    "crossrepo.closure_status":               "coordinator_core.ops.crossrepo_closure_status",
    "ceremony.update_docs_scan":              "coordinator_core.ops.ceremony.update_docs_scan",
    "tracker.advance_status":                 "coordinator_core.ops.tracker.advance_status",
    "tracker.assign":                         "coordinator_core.ops.tracker.assign",
    "tracker.fold_observed_set":              "coordinator_core.ops.tracker.fold_observed_set",
    "tracker.mint_person":                    "coordinator_core.ops.tracker.mint_person",
    "tracker.fold_ownership":                 "coordinator_core.ops.tracker.fold_ownership",
    "tracker.render_status":                  "coordinator_core.ops.tracker.render_status",
    "tracker.assert_code_complete":           "coordinator_core.ops.tracker.completion_policy",
    "tracker.push_suggestion":                "coordinator_core.ops.tracker.push_suggestion",
    "priority.set":                           "coordinator_core.ops.priority_set",
    "priority.drain":                         "coordinator_core.ops.priority_drain",
    # diagnostics.* — the three write-free transport-failure probes; one shared
    # owning module, same many-keys-one-value shape as the hooks.* entries above.
    # Spec: docs/plans/2026-08-07-safe-target-for-transport-failure-probes.md § C1
    "diagnostics.always_succeeds":            "coordinator_core.ops.diagnostics_probes",
    "diagnostics.always_refuses":             "coordinator_core.ops.diagnostics_probes",
    "diagnostics.always_structural_pin":      "coordinator_core.ops.diagnostics_probes",
    "ceremony.chunk_commits":                 "coordinator_core.ops.ceremony.chunk_commits",
    "session.commits":                        "coordinator_core.ops.session_commits",
    "gate.validate_invocable":                 "coordinator_core.ops.gate_validate_invocable",
    "install.detect_cmd_autorun_coverage":    "coordinator_core.ops.cmd_autorun_guard",
    "install.write_cmd_autorun_guard":        "coordinator_core.ops.cmd_autorun_guard",
    "install.strip_cmd_autorun_guard":        "coordinator_core.ops.cmd_autorun_guard",
    # app_session.* — launch/census/teardown against a consuming repo's local
    # dev app; one shared owning module, same many-keys-one-value shape as
    # the hooks.* block above.
    # Spec: docs/plans/2026-08-15-app-session-launch-census-teardown-ops.md § C3
    "app_session.launch":                     "coordinator_core.ops.app_session",
    "app_session.census":                     "coordinator_core.ops.app_session",
    "app_session.teardown":                   "coordinator_core.ops.app_session",
    "fleet.backfill_reference_edges":         "coordinator_core.ops.backfill_reference_edges",
    "session_baton.mint":                     "coordinator_core.ops.session_baton_mint",
    "session_baton.promote":                  "coordinator_core.ops.session_baton_promote",
    "warm_guard.evaluate":                     "coordinator_core.ops.warm_guard_evaluate",
    "merge_assemble.apply":                    "coordinator_core.merge_assemble.ops",
}


def resolves(op_key: str) -> bool:
    """True when `op_key` is DISPATCHABLE right now: it either has a lazy-import
    entry in OP_MODULE_MAP (dispatch_message can import its owning module on a
    registry miss) or it is already live in coordinator_core.ipc._REGISTRY
    (already registered, e.g. under eager-import mode or after a prior
    dispatch). This is a weaker claim than `"x" in _REGISTRY`: that proves "x is
    registered right now"; `resolves("x")` proves "x is dispatchable" -- true
    even with an empty _REGISTRY, since a mapped miss still resolves via lazy
    import. It is NOT a substitute for a registry read where the assertion's
    subject is registry state itself (an empty-registry proof, or a
    binding-identity check of which callable is bound) -- see
    docs/plans/2026-08-22-the-import-path-costs-nothing.md § C3 for the two
    excluded assertion classes.

    `ipc` is imported locally, not at module scope, to preserve this module's
    dotted-path-strings-only load-time contract (see module docstring) --
    resolves() is a test/inspection helper, not part of the hot dispatch path.

    Spec backlink: docs/plans/2026-08-22-the-import-path-costs-nothing.md § C3
    """
    if op_key in OP_MODULE_MAP:
        return True
    from coordinator_core import ipc

    return op_key in ipc._REGISTRY
