"""Regression guard for C2 (docs/plans/2026-09-02-one-owner-for-where-a-work-
record-lives.md): a hand-built `state/<declared-kind>` path join re-entering
`coordinator_core/` fails this test, where `<declared-kind>` is one of the
work-record kinds `session/record_homes.HOMES` declares.

SCOPE -- corpus-wide, on purpose, unlike its sibling
`test_no_hand_built_machinery_joins.py`. That guard's own SCOPE docstring
names the thing to fix rather than copy: it polices only the 21+7 sites its
own chunk repointed, so a hand-built join in any OTHER module raises nothing.
This guard walks the whole `coordinator_core/` tree, because the plan's
Problem statement is precisely that a record's home has no owner and no
single guard watching for a new literal landing anywhere in it.

THE RATCHET, both directions. `_KNOWN_LITERAL_SITES` is a seeded frozenset of
the files that carry a hand-built `state/<kind>` literal TODAY (measured by
walking the tree at the time this test was written, not copied from the
plan's own "119"/"271" prose -- those numbers describe a moving tree and are
expectations to check, not values to trust). A literal in a file NOT in that
set fails -- a new hand-built join landing anywhere is what this guard exists
to catch. A file IN the set that no longer carries a literal also fails --
the set can only shrink, so a file repointed onto `record_homes.py`'s
accessors cannot quietly regain its exemption by staying listed after the
literal is gone.

`record_homes.py` itself is exempt -- the one module allowed to spell the
`"state"` + `"<kind>"` segments together, the same way
`test_no_hand_built_machinery_joins.py` exempts `machinery_paths.py`.

Negative-spec:
    - Static scan only -- this test never imports or executes any scanned
      module. It reads each file's source text and regex-matches it; a
      module that raises on import, has side effects, or is otherwise
      unsafe to execute is scanned exactly as safely as one that is not.
    - Does NOT sweep the seeded sites onto `record_homes.py`'s accessors --
      that sweep is the plan's own Anti-scope ("Do not sweep the 119 literal
      sites"). This guard ratchets the count; it does not reduce it.
    - Does NOT police a `"state/<segment>"` literal for a segment that is
      NOT a declared `record_homes.HOMES` kind (e.g. a machinery bucket like
      `"state/subagent-share"`, already covered by
      `test_no_hand_built_machinery_joins.py`, or a loose top-level file
      that is not a record kind at all -- see `record_homes.py`'s own
      docstring on why those are excluded from `HOMES`).
    - Does NOT run at cadence -- no `@pytest.mark.cadence` marker, no
      `spawns_process` marker. The write-side literal count regrew
      unguarded between relocations specifically because the allowlist's
      own converse check IS cadence-marked; this guard is the fast-tier
      counterpart that fires on every run instead of at the next cadence
      gate.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from coordinator_core.session import record_homes

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCAN_ROOT = os.path.join(_REPO_ROOT, "coordinator_core")

_EXCLUDED_FILES = {
    os.path.join(_SCAN_ROOT, "session", "record_homes.py"),
    #: This guard's own source: its docstring and pattern-comment prose cite
    #: example literals like `"state/handoffs"` for illustration, which the
    #: scan would otherwise mistake for a hand-built join in itself.
    os.path.abspath(__file__),
}

#: A hand-built `state/<kind>` join for one of the declared work-record
#: kinds: either a single-string literal spelling the joined path
#: (`"state/handoffs"`, `'state/handoffs/foo.md'`), or two adjacent
#: string-literal arguments to a join call
#: (`os.path.join(..., "state", "handoffs", ...)`, `Path(...) / "state" /
#: "handoffs"`). A caller routed correctly through `record_homes.home_dir`/
#: `record_path`/`home_pattern` never spells `"state"` immediately followed
#: by a declared kind segment as a literal -- only a hand-built join does.
_KIND_ALTERNATION = "|".join(
    re.escape(kind) for kind in sorted(record_homes.HOMES, key=len, reverse=True)
)
_JOIN_PATTERN = re.compile(
    r"(?:"
    r'["\']state/(?:' + _KIND_ALTERNATION + r')(?:["\'/]|$)'
    r'|["\']state["\']\s*[,/]\s*["\'](?:' + _KIND_ALTERNATION + r')["\']'
    r")"
)

_KNOWN_LITERAL_SITES = frozenset({
    "backlog_grind_assemble/readers_blitz.py",
    "backlog_grind_assemble/readers_mise.py",
    "backlog_grind_assemble/readers_sweep.py",
    "backlog_grind_assemble/tests/test_apply_unify_batons_verb.py",
    "backlog_grind_assemble/tests/test_readers_mise_unify.py",
    "bash_guards/_alternative_liveness.py",
    "bash_guards/_write_bump_message.py",
    "bash_guards/dispatch_checks.py",
    "bash_guards/tests/guard_message_corpus.py",
    "bash_guards/tests/test_bx16_multiprobe_and_headtail_rewrite.py",
    "bash_guards/tests/test_check_seven_claude_md_budget.py",
    "bash_guards/tests/test_commit_scope_directory_operand_still_advises.py",
    "bash_guards/tests/test_destructive_rm_quote_split_verb.py",
    "bash_guards/tests/test_guard_doctrine_surface_point3_quoted_heredoc_indirection.py",
    "bash_guards/tests/test_reap_stale_git_lock.py",
    "bash_guards/tests/test_scope_orphan_census.py",
    "bash_guards/tests/test_write_bump_message.py",
    "baton_assemble/__init__.py",
    "baton_assemble/tests/test_adopt_prior_attempt_unreadable_candidate.py",
    "baton_assemble/tests/test_apply_degrade_no_compensation.py",
    "baton_assemble/tests/test_apply_orphan_on_commit_pipeline_error.py",
    "baton_assemble/tests/test_brief_segments.py",
    "baton_assemble/tests/test_d2_session_ledger_body_check.py",
    "baton_assemble/tests/test_d6_fan_in_n3_budget.py",
    "baton_assemble/tests/test_d6_routes_through_housekeeping.py",
    "baton_assemble/tests/test_deliverable_ancestor_archived.py",
    "baton_assemble/tests/test_deliverable_collision_budget.py",
    "baton_assemble/tests/test_deliverable_collision_warn.py",
    "baton_assemble/tests/test_deliverable_ids_union_carry.py",
    "baton_assemble/tests/test_discovery_tier_expectation.py",
    "baton_assemble/tests/test_dropped_join_guard_plan_input_axis.py",
    "baton_assemble/tests/test_falsifier_one_hop_over_fan_in_fixture.py",
    "baton_assemble/tests/test_fan_in_cardinality_judgment_point.py",
    "baton_assemble/tests/test_fan_in_mints_fresh_deliverable_id.py",
    "baton_assemble/tests/test_fresh_output_path_hhmmss_strip.py",
    "baton_assemble/tests/test_j_continuation_vs_fork_excise.py",
    "baton_assemble/tests/test_j_divergent_deliverable_id.py",
    "baton_assemble/tests/test_kind_axis.py",
    "baton_assemble/tests/test_ledger_claim_record_liveness.py",
    "baton_assemble/tests/test_plan_input_predecessor.py",
    "baton_assemble/tests/test_plan_owner_stamp.py",
    "baton_assemble/tests/test_plan_stamp_carry.py",
    "baton_assemble/tests/test_predecessor_stage_rank_stamped.py",
    "baton_assemble/tests/test_replay_carries_union.py",
    "baton_assemble/tests/test_repo_identity_gate.py",
    "baton_assemble/tests/test_roadmap_identity_carry.py",
    "baton_assemble/tests/test_spinoff_association_jp.py",
    "baton_assemble/tests/test_spinoff_no_unify.py",
    "baton_assemble/tests/test_supersede_refuses_placeholder_continued_into.py",
    "baton_assemble/tests/test_untitled_mint_derivation.py",
    "benchmarks/boot_backstop_cold.py",
    "benchmarks/handoff_supersede_baseline.py",
    "benchmarks/listener_availability.py",
    "benchmarks/op_fixtures.py",
    "cartography/tests/test_atlas_record.py",
    "cartography/tests/test_file_index.py",
    "ceremony_common/_phantom_sweep_providers.py",
    "claude_md_budget.py",
    "clustering/tests/test_candidates_parent_dir.py",
    "commit_ledger/resolve_owner.py",
    "commit_ledger/store.py",
    "commit_ledger/tests/test_store.py",
    "contract/cockpit_schema/entities/health_status_summary.py",
    "contract/cockpit_schema/entities/roadmap_summary.py",
    "contract/cockpit_schema/tests/test_emit_conformance_fixture.py",
    "contract/cockpit_schema/tests/test_goal_authoring_wire_projection.py",
    "contract/cockpit_schema/tests/test_new_entity_schemas.py",
    "contract/cockpit_schema/tests/test_producer_axis_entity.py",
    "contract/cockpit_schema/tests/test_round_trip.py",
    "contract/cockpit_schema/tests/test_session_hierarchy.py",
    "contract/cockpit_schema/tests/test_verify_superseded_retirement.py",
    "contract/cockpit_schema/tests/test_verify_vocabulary_retirement.py",
    "contract/decision_object/tests/test_reportable_partition.py",
    "contract/emit_memo_schema.py",
    "coverage.py",
    "dag.py",
    "distill/_common.py",
    "distill/curation_status.py",
    "distill/delete_guard.py",
    "distill/tests/test_delete_guard.py",
    "execute_plan_assemble/close_out_and_stamp.py",
    "execute_plan_assemble/tests/test_close_out_goal_refusal.py",
    "fact_contract_gate/dangling_consumer.py",
    "fact_contract_gate/engine_gap_ratchet.py",
    "fact_contract_gate/prose_rederivation.py",
    "fact_contract_gate/tests/test_fact_contract_gate.py",
    "frontmatter/schema_validate.py",
    "frontmatter/tests/test_bug_backlog_corpus_parses.py",
    "frontmatter/tests/test_handoff_kind_enum_alias_parity.py",
    "frontmatter/tests/test_handoff_lineage_corpus_dangling_refs.py",
    "frontmatter/tests/test_parity_handoff_ops.py",
    "frontmatter/tests/test_plan_scaffold_brightline_parity.py",
    "frontmatter/tests/test_plan_scaffold_falsifier_parity.py",
    "frontmatter/tests/test_plan_schema_falsifier_optional.py",
    "frontmatter/tests/test_plan_sizing_object_field.py",
    "frontmatter/tests/test_roadmap_approval_identity.py",
    "frontmatter/tests/test_roadmap_schema_glob.py",
    "frontmatter/tests/test_routed_sizing_carries_a_deliverable_id.py",
    "frontmatter/tests/test_schema_cli_parity.py",
    "frontmatter/tests/test_schema_validate.py",
    "git/test_commit_trailers.py",
    "git/tests/test_eol_declared.py",
    "goals/reassess_krs.py",
    "goals/tests/test_kr_suggestion_reader.py",
    "hooks/nudge_unauthorized_handoff.py",
    "hooks/test_nudge_unrouted_sizing.py",
    "hooks/test_postuse_advisory_dispatch.py",
    "hooks/tests/test_nudge_unrouted_sizing_touch_seam.py",
    "hooks/tests/test_watchdog_undischarged_next_move.py",
    "housekeeping/cycle.py",
    "housekeeping/tests/corpus_fixture.py",
    "housekeeping/tests/test_corpus_fixture.py",
    "housekeeping/tests/test_cycle.py",
    "housekeeping/tests/test_gate_clear.py",
    "housekeeping/tests/test_resolve.py",
    "housekeeping/tests/test_terminal.py",
    "install/tests/test_fleet_env_pin_advance.py",
    "install/tests/test_scaffold_structure.py",
    "install/uninstall_legs.py",
    "invoke/tests/test_repo_scope_refusal.py",
    "op_budget_suspension.py",
    "ops/append_integrator_dispositions.py",
    "ops/backfill_deliverable_spine.py",
    "ops/backfill_reference_edges.py",
    "ops/baton_drift_sweep.py",
    "ops/cascade_backstop_sweep.py",
    "ops/cascade_retract.py",
    "ops/ceremony/consumed_handoff_stamp.py",
    "ops/ceremony/renderers.py",
    "ops/ceremony/resolver.py",
    "ops/ceremony/tail_ops.py",
    "ops/ceremony/tests/test_branch_resolution.py",
    "ops/ceremony/tests/test_ceremony_claim_readers.py",
    "ops/ceremony/tests/test_commit_gates.py",
    "ops/ceremony/tests/test_commit_scoped_trailer_replay.py",
    "ops/ceremony/tests/test_consumed_handoff_stamp.py",
    "ops/ceremony/tests/test_consumed_handoff_stamp_claim_release.py",
    "ops/ceremony/tests/test_consumed_handoff_stamp_multi_deliverable.py",
    "ops/ceremony/tests/test_consumed_handoff_stamp_multi_deliverable_commit.py",
    "ops/ceremony/tests/test_pipeline_context.py",
    "ops/ceremony/tests/test_post_commit_tail.py",
    "ops/ceremony/tests/test_post_commit_tail_claim_release.py",
    "ops/ceremony/tests/test_post_commit_tail_completion_fold.py",
    "ops/ceremony/tests/test_records_query.py",
    "ops/ceremony/tests/test_renderers_plans_join.py",
    "ops/ceremony/tests/test_renderers_roadmap_callout.py",
    "ops/ceremony/tests/test_renderers_unreadable_handoff.py",
    "ops/ceremony/tests/test_tail_ops.py",
    "ops/ceremony/tests/test_wsc_disposition.py",
    "ops/changelog_ops.py",
    "ops/check_competitor_positioning_nudge.py",
    "ops/commit_anchors.py",
    "ops/completion_nature.py",
    "ops/crossrepo_closure_status.py",
    "ops/cutover_gate.py",
    "ops/deferral_detect_orphan_memo.py",
    "ops/deliverable_cascade.py",
    "ops/deliverable_rollup.py",
    "ops/detect_onboarding_offer.py",
    "ops/dispatch_emit/tests/test_emit.py",
    "ops/dispatch_emit/tests/test_op.py",
    "ops/dispatch_emit/tests/test_pathspec.py",
    "ops/distill_apply_disposal.py",
    "ops/distill_scope.py",
    "ops/docgen/tests/test_c6_conformance.py",
    "ops/docgen/tests/test_doc_render.py",
    "ops/docgen/tests/test_dr_corpus_ids_unique.py",
    "ops/draft_plan_aging.py",
    "ops/emit/sections/initiatives.py",
    "ops/emit/sections/routine_signals.py",
    "ops/emit/tests/test_ac_priority_resolution.py",
    "ops/emit/tests/test_ac_priority_single_impl.py",
    "ops/emit/tests/test_ac_priority_spinoff_wall.py",
    "ops/emit/tests/test_backlogs_section.py",
    "ops/emit/tests/test_dead_join_fixes_reviewer_plan_id_superseded_by.py",
    "ops/emit/tests/test_goal_wire_2130.py",
    "ops/emit/tests/test_handoff_id_derivation.py",
    "ops/emit/tests/test_handoffs_section_vocabulary.py",
    "ops/emit/tests/test_human_axis_emission.py",
    "ops/emit/tests/test_human_axis_stays_off_the_wire.py",
    "ops/emit/tests/test_priority_emission.py",
    "ops/emit/tests/test_priority_resolve.py",
    "ops/emit/tests/test_priority_resolve_cache.py",
    "ops/emit/tests/test_producer_passthrough.py",
    "ops/emit/tests/test_provenance_path_relativize.py",
    "ops/emit/tests/test_roadmap_dag_parity.py",
    "ops/emit/tests/test_roadmap_dag_section.py",
    "ops/emit/tests/test_roadmap_initiative_field.py",
    "ops/emit/tests/test_roadmaps_scalars.py",
    "ops/emit/tests/test_sedge03_deliverable_status_liveness.py",
    "ops/extract_cited_sidecars.py",
    "ops/fleet/_common.py",
    "ops/fleet/archive_queue_entry.py",
    "ops/fleet/archive_release_accumulator.py",
    "ops/fleet/archive_sizings.py",
    "ops/fleet/archive_terminal_handoffs.py",
    "ops/fleet/capability_index.py",
    "ops/fleet/consumer_corpus_preflight.py",
    "ops/fleet/migrate_handoff_vocabulary.py",
    "ops/fleet/prune_bugs.py",
    "ops/fleet/tests/test_archival_move_claims_the_sink.py",
    "ops/fleet/tests/test_archive_and_commit_batched_drift_and_restage.py",
    "ops/fleet/tests/test_archive_and_commit_disk_head_drift.py",
    "ops/fleet/tests/test_archive_and_commit_envelope_contract.py",
    "ops/fleet/tests/test_archive_dest_conflict_wedge_detector.py",
    "ops/fleet/tests/test_archive_git_free_seam_smoke.py",
    "ops/fleet/tests/test_archive_release_accumulator_refusal.py",
    "ops/fleet/tests/test_archive_sizings.py",
    "ops/fleet/tests/test_archive_terminal_handoffs.py",
    "ops/fleet/tests/test_capability_index.py",
    "ops/fleet/tests/test_common_claim_release.py",
    "ops/fleet/tests/test_common_tree_build.py",
    "ops/fleet/tests/test_consumer_corpus_preflight.py",
    "ops/fleet/tests/test_fleet_work_state.py",
    "ops/fleet/tests/test_head_race_between_read_tree_and_commit.py",
    "ops/fleet/tests/test_main_index_resync.py",
    "ops/fleet/tests/test_migrate_handoff_vocabulary.py",
    "ops/fleet/tests/test_migrate_vocabulary_discharges_archival.py",
    "ops/fleet/tests/test_record_history_real_git.py",
    "ops/fleet_machinery_sweep.py",
    "ops/goals_match.py",
    "ops/handoff_append_session_ledger.py",
    "ops/handoff_archive_transition.py",
    "ops/handoff_author_fork.py",
    "ops/handoff_children.py",
    "ops/handoff_close_origin_stub.py",
    "ops/handoff_correct_body.py",
    "ops/handoff_discharge_criteria.py",
    "ops/handoff_lineage_ancestry.py",
    "ops/handoff_match.py",
    "ops/handoff_normalize.py",
    "ops/handoff_phase_stamp.py",
    "ops/handoff_repoint_origin.py",
    "ops/handoff_ship_archive.py",
    "ops/handoff_stamp.py",
    "ops/handoff_transition.py",
    "ops/initiatives_serve.py",
    "ops/introspect/tests/test_verify_shipped.py",
    "ops/memo/tests/test_memo_transition_distill_fate.py",
    "ops/memo/tests/test_memo_transition_unit.py",
    "ops/normalize_claimed_frontmatter.py",
    "ops/plan_status_transition.py",
    "ops/promote_shipped_in_flight_stubs.py",
    "ops/propagate_body.py",
    "ops/queue_append.py",
    "ops/queue_scaffold_baton.py",
    "ops/read_sizing_object_fields.py",
    "ops/reap_in_flight_claims.py",
    "ops/reap_orphaned_agent_dirs.py",
    "ops/records_query.py",
    "ops/refresh_roadmap_callout.py",
    "ops/review_brightline_gate.py",
    "ops/review_mint/tests/test_op.py",
    "ops/roadmap_dag.py",
    "ops/roadmap_link_stubs.py",
    "ops/session/fix_concrete_path_citations.py",
    "ops/session/guard_concrete_path_citations.py",
    "ops/session/record_pickup.py",
    "ops/session/resolve_chain_terminal_disposition.py",
    "ops/session/tests/test_guard_concrete_path_citations.py",
    "ops/session/tests/test_record_pickup.py",
    "ops/session/tests/test_resolve_chain_terminal_disposition.py",
    "ops/session/tests/test_safe_commit_offer.py",
    "ops/sizing_decline.py",
    "ops/sizing_ship.py",
    "ops/sizing_spike_verdict.py",
    "ops/strategic/draft_writer.py",
    "ops/strategic/emit_writer.py",
    "ops/strategic/version_highlights.py",
    "ops/test__relative_link.py",
    "ops/test_backfill_deliverable_spine.py",
    "ops/test_baton_drift_sweep.py",
    "ops/test_blocked.py",
    "ops/test_bootstrap_repo.py",
    "ops/test_changelog_compute_day_fields.py",
    "ops/test_changelog_ops.py",
    "ops/test_check_competitor_positioning_nudge.py",
    "ops/test_completion_ops.py",
    "ops/test_detect_onboarding_offer.py",
    "ops/test_dirty_tree_gate.py",
    "ops/test_draft_plan_aging.py",
    "ops/test_emit_artifact_shape_contract.py",
    "ops/test_list_week_changelog.py",
    "ops/test_normalize_claimed_frontmatter.py",
    "ops/test_onboarding_signal_contract.py",
    "ops/test_promote_shipped_in_flight_stubs.py",
    "ops/test_propagate_body.py",
    "ops/test_red_record.py",
    "ops/test_refresh_roadmap_callout.py",
    "ops/test_resolve_baton_path.py",
    "ops/test_verify_schema_registry_sync.py",
    "ops/test_workday_complete_backfill_scan.py",
    "ops/test_workday_stitch_sidecar_summary.py",
    "ops/test_workweek_trail_scope.py",
    "ops/tests/conftest.py",
    "ops/tests/test_archive_transition_refusal_reason.py",
    "ops/tests/test_assert_plan_sizing_citation.py",
    "ops/tests/test_backfill_deliverable_spine.py",
    "ops/tests/test_backfill_reference_edges.py",
    "ops/tests/test_blocker_id_resolves_to_one_chain_terminus.py",
    "ops/tests/test_candidate_quarantine_is_one_line.py",
    "ops/tests/test_cascade_backstop_sweep.py",
    "ops/tests/test_cascade_baton_rows.py",
    "ops/tests/test_cascade_retract.py",
    "ops/tests/test_changelog_parity.py",
    "ops/tests/test_check3_succession_exemption.py",
    "ops/tests/test_commit_anchors_claim_state.py",
    "ops/tests/test_completion_nature.py",
    "ops/tests/test_completion_ops_claim_state.py",
    "ops/tests/test_crossrepo_closure_status.py",
    "ops/tests/test_cutover_gate_handler.py",
    "ops/tests/test_deferral_detect_orphan_memo.py",
    "ops/tests/test_deliverable_cascade.py",
    "ops/tests/test_deliverable_cascade_claim_state.py",
    "ops/tests/test_deliverable_cascade_kinds.py",
    "ops/tests/test_deliverable_cascade_reads_corpus_once.py",
    "ops/tests/test_deliverable_equivalence.py",
    "ops/tests/test_deliverable_fork_detect.py",
    "ops/tests/test_deliverable_ledger_write.py",
    "ops/tests/test_deliverable_rollup.py",
    "ops/tests/test_distill_apply_disposal.py",
    "ops/tests/test_distill_disposal_manifest.py",
    "ops/tests/test_distill_scope.py",
    "ops/tests/test_distill_stamp_disposal.py",
    "ops/tests/test_extract_cited_sidecars.py",
    "ops/tests/test_gate_cascade_clear_terminal_states.py",
    "ops/tests/test_gate_recheck_retires_blocked_by.py",
    "ops/tests/test_generator_provenance_ratchet.py",
    "ops/tests/test_goal_kr_status.py",
    "ops/tests/test_goals_match.py",
    "ops/tests/test_handoff_append_session_ledger.py",
    "ops/tests/test_handoff_archive_transition_holder_live.py",
    "ops/tests/test_handoff_author_fork.py",
    "ops/tests/test_handoff_author_fork_claim_state.py",
    "ops/tests/test_handoff_author_lint.py",
    "ops/tests/test_handoff_carry_gate.py",
    "ops/tests/test_handoff_children.py",
    "ops/tests/test_handoff_close_origin_stub.py",
    "ops/tests/test_handoff_columns_query.py",
    "ops/tests/test_handoff_correct_body.py",
    "ops/tests/test_handoff_correct_body_claim_state.py",
    "ops/tests/test_handoff_lineage_ancestry.py",
    "ops/tests/test_handoff_match.py",
    "ops/tests/test_handoff_normalize.py",
    "ops/tests/test_handoff_normalize_carry_scope.py",
    "ops/tests/test_handoff_repair_archived_shipped_in.py",
    "ops/tests/test_handoff_ship_archive.py",
    "ops/tests/test_handoff_stamp.py",
    "ops/tests/test_handoff_summary_cap_normalize.py",
    "ops/tests/test_handoff_transition_claim_terminal.py",
    "ops/tests/test_handoff_transition_derives_readiness.py",
    "ops/tests/test_handoff_transition_unclaim.py",
    "ops/tests/test_initiatives_serve.py",
    "ops/tests/test_initiatives_store.py",
    "ops/tests/test_lifecycle_pair_consistency.py",
    "ops/tests/test_normalize_claimed_frontmatter.py",
    "ops/tests/test_ownership_index.py",
    "ops/tests/test_path_guard.py",
    "ops/tests/test_plan_capture_persist.py",
    "ops/tests/test_plan_status_transition.py",
    "ops/tests/test_plan_status_transition_live_foreign_holder.py",
    "ops/tests/test_plan_tasks_mutate.py",
    "ops/tests/test_plan_tasks_render.py",
    "ops/tests/test_producer_axis_creation_seam.py",
    "ops/tests/test_queue_append_concurrency.py",
    "ops/tests/test_queue_cluster.py",
    "ops/tests/test_queue_family.py",
    "ops/tests/test_queue_parity.py",
    "ops/tests/test_queue_scaffold_baton.py",
    "ops/tests/test_read_sizing_object_fields.py",
    "ops/tests/test_reap_in_flight_claims.py",
    "ops/tests/test_record_history.py",
    "ops/tests/test_records_query.py",
    "ops/tests/test_repair_archived_verbs.py",
    "ops/tests/test_repair_deployment_state_live_tree.py",
    "ops/tests/test_roadmap_dag.py",
    "ops/tests/test_roadmap_dag_sprint_gates.py",
    "ops/tests/test_roadmap_link_stubs.py",
    "ops/tests/test_session_baton_promote.py",
    "ops/tests/test_session_baton_promote_learned_section.py",
    "ops/tests/test_sizing_citation_archive_fallback.py",
    "ops/tests/test_sizing_decline.py",
    "ops/tests/test_sizing_ship.py",
    "ops/tests/test_sizing_spike_verdict.py",
    "ops/tests/test_strang10_invoke_smoke.py",
    "ops/tests/test_supersede_archives_atomically.py",
    "ops/tests/test_unclaim_parks_a_blocked_baton.py",
    "ops/tests/test_unresolvable_blocker_id_is_not_a_none_state.py",
    "ops/tests/test_updatedocs_gates.py",
    "ops/updatedocs_gates.py",
    "ops/workday_complete_step2_5_dirty_tree.py",
    "ops/workday_stitch_sidecar_summary.py",
    "ops/workflow_fire/tests/test_end_to_end_fire.py",
    "ops/workweek_trail_scope.py",
    "orient_assemble/readers_health_reaper.py",
    "orient_assemble/tests/test_readers_handoff_triage_claim.py",
    "orient_assemble/tests/test_scan_scope_regression.py",
    "orientation/abandoned_claim_signal.py",
    "orientation/test_abandoned_claim_signal.py",
    "orientation/test_expired_grant_signal.py",
    "pickup_assemble/__init__.py",
    "pickup_assemble/apply.py",
    "pickup_assemble/tests/test_adjudicate_claimed_batons.py",
    "pickup_assemble/tests/test_artifact_chain.py",
    "pickup_assemble/tests/test_baton_unification.py",
    "pickup_assemble/tests/test_baton_unification_decision.py",
    "pickup_assemble/tests/test_brief_awaiting_gate_typed_fields.py",
    "pickup_assemble/tests/test_brief_claim_lease.py",
    "pickup_assemble/tests/test_brief_open_budget.py",
    "pickup_assemble/tests/test_claim_rule.py",
    "pickup_assemble/tests/test_claim_state_reads.py",
    "pickup_assemble/tests/test_consumer_field_path_contract.py",
    "pickup_assemble/tests/test_cross_repo_pickup_denied.py",
    "pickup_assemble/tests/test_drop_abandoned_holder.py",
    "pickup_assemble/tests/test_drop_holder_gate.py",
    "pickup_assemble/tests/test_drop_holder_path_ordering.py",
    "pickup_assemble/tests/test_drop_on_a_shipped_baton.py",
    "pickup_assemble/tests/test_emitted_directive_verbs_are_cli_verbs.py",
    "pickup_assemble/tests/test_gate_check_blocked_by_resolution.py",
    "pickup_assemble/tests/test_gate_check_shipped_blocker_evidence.py",
    "pickup_assemble/tests/test_lineage_related_sessions_archived.py",
    "pickup_assemble/tests/test_pickup_claim_stage_stamp_evidence.py",
    "pickup_assemble/tests/test_read_only_invariant.py",
    "pickup_assemble/tests/test_reportable_partition.py",
    "pickup_assemble/tests/test_route_baton_adoption_never_unifies.py",
    "pickup_assemble/tests/test_session_baton_adoption.py",
    "pickup_assemble/tests/test_unification_raise_is_clean.py",
    "pickup_assemble/tools/corpus_diff_deliverable_evidence.py",
    "plan_assemble/predicates/test_composition_lints.py",
    "plan_assemble/predicates/test_exit_gates.py",
    "plan_assemble/predicates/test_triage.py",
    "plan_assemble/test_residue_admission.py",
    "reconcile/ac27_differential_oracle.py",
    "reconcile/commitments_recheck.py",
    "reconcile/handoff_corpus.py",
    "reconcile/tests/test_ac27_differential_oracle.py",
    "reconcile/tests/test_commitments_recheck.py",
    "reconcile/tests/test_continuation_chain_collision.py",
    "reconcile/tests/test_gate_eval.py",
    "review_trail/tests/test_reviewed_set_equivalence.py",
    "roadmap/number_stubs.py",
    "roadmap/spine.py",
    "roadmap/tests/test_audit.py",
    "roadmap/tests/test_audit_sprint_scope.py",
    "roadmap/tests/test_number_stubs.py",
    "roadmap_planning_assemble/tests/test_roadmap_planning_assemble.py",
    "session/claim_neighbours.py",
    "session/claims.py",
    "session/session_facts.py",
    "session/tests/test_artifact_owner.py",
    "session/tests/test_claim_neighbours.py",
    "session/tests/test_claims.py",
    "session/tests/test_no_untracked_relocation.py",
    "session/tests/test_receiver_state.py",
    "session/tests/test_record_homes.py",
    "session/tests/test_session_facts.py",
    "session/tests/test_stale_claims.py",
    "session/tests/test_stale_claims_claim_state.py",
    "session/tests/test_work_state.py",
    "session_baton/tests/test_store.py",
    "session_hierarchy/test_derive.py",
    "session_hierarchy/tests/test_derive_claim_state.py",
    "session_ledger/aggregate_chain_loe.py",
    "session_ledger/test_aggregate_chain_loe.py",
    "session_ledger/test_closing_session.py",
    "session_ledger/test_dispatch_fallback.py",
    "test_archive_stamp.py",
    "test_artifact_subject.py",
    "test_backlog_grind_assemble.py",
    "test_baton_assemble.py",
    "test_claude_md_budget.py",
    "test_handoff_creation_guard.py",
    "test_pickup_apply.py",
    "test_pickup_assemble.py",
    "test_pickup_assemble_reply_closure.py",
    "test_quick_wrap_assemble.py",
    "test_sizing_assemble.py",
    "test_wire_paths_separator.py",
    "tests/_baton_dag_oracle.py",
    "tests/oracles/test_archive_and_commit_spawn_floor.py",
    "tests/test_archival.py",
    "tests/test_archival_claimed_or_shipped.py",
    "tests/test_archive_stamp_claimant_identity.py",
    "tests/test_archive_stamp_human_claimant.py",
    "tests/test_c6_pointer_normalization.py",
    "tests/test_ceremony_brief_budget.py",
    "tests/test_claim_ceremony_stamps_stable_pid.py",
    "tests/test_claim_state_accessor.py",
    "tests/test_commit_anchors.py",
    "tests/test_coverage_bookkeeping_partition.py",
    "tests/test_coverage_claim_resolution.py",
    "tests/test_coverage_dag_archived_repo_root.py",
    "tests/test_dag_git_history_tier_path_shapes.py",
    "tests/test_dag_git_path_ever_tracked_cache.py",
    "tests/test_dag_handoff_id_index.py",
    "tests/test_dag_resolve_target_tier3_dedup.py",
    "tests/test_deep_per_item_spawn_worklist.py",
    "tests/test_docstring_shell_paste_hazard.py",
    "tests/test_draft_plan_aging.py",
    "tests/test_every_register_resolves_or_declares.py",
    "tests/test_foreign_identity_subject_exemptions.py",
    "tests/test_handoff_backfill_claim_stamp.py",
    "tests/test_handoff_children.py",
    "tests/test_handoff_gate_aging.py",
    "tests/test_hooks_roundtrip.py",
    "tests/test_hot_path_surface_test_target_guard.py",
    "tests/test_install_chain_driven_leaf_seed_sweep.py",
    "tests/test_invoke_main.py",
    "tests/test_ipc_scope_touch_self_report.py",
    "tests/test_known_red_ratchet.py",
    "tests/test_no_dangling_machinery_citations.py",
    "tests/test_no_global_os_name_monkeypatch.py",
    "tests/test_op_classification_manifest.py",
    "tests/test_op_suspension_ratchet.py",
    "tests/test_pickup_assemble_scoped_commit.py",
    "tests/test_review_brightline_gate.py",
    "tests/test_sibling_fact.py",
    "tests/test_sizing_disposition.py",
    "tests/test_stamp_verbs_stay_off_the_sweep.py",
    "tests/test_strategic_emit.py",
    "tests/test_strategic_generate.py",
    "tests/test_test_red_record.py",
    "tests/test_tracker_store.py",
    "tests/test_warm_identity_env_reads.py",
    "text/test_query_record_display.py",
    "text/test_refresh_queries.py",
    "updatedocs/plan_prune.py",
    "updatedocs/tests/test_plan_prune.py",
    "warm/tests/test_breadcrumb_and_spawn.py",
    "warm/tests/test_warm_identity_fails_closed_at_mutating_sites.py",
    "workstream_complete/__init__.py",
    "workstream_complete/directives_commit_tail.py",
    "workstream_complete/directives_completion.py",
    "workstream_complete/directives_memo_lifecycle.py",
    "workstream_complete/test_close_coverage_advisory.py",
    "workstream_complete/test_directives_review_scale.py",
    "workstream_complete/test_lesson_capture_reachable.py",
    "workstream_complete/test_workstream_complete.py",
    "workstream_complete/test_workstream_complete_contract.py",
    "workstream_complete/tests/test_close_disposes_non_shipped.py",
    "workstream_complete/tests/test_close_ships_its_batons.py",
    "workstream_complete/tests/test_review_receipt_gates_delivered_close.py",
    "write_guards/nudge_baton_body_bar.py",
    "write_guards/nudge_session_display_name_as_identifier.py",
    "write_guards/nudge_terminal_artifact_edit.py",
    "write_guards/tests/test_ac5_flip_runtime_probes.py",
    "write_guards/tests/test_block_consumed_handoff_edit.py",
    "write_guards/tests/test_block_consumed_handoff_edit_claim_state.py",
    "write_guards/tests/test_block_cutover_phase_hand_edit.py",
    "write_guards/tests/test_check_claude_md_size.py",
    "write_guards/tests/test_deny_text_reachable_override.py",
    "write_guards/tests/test_guard_concrete_path_citations.py",
    "write_guards/tests/test_guard_memory_store_cap.py",
    "write_guards/tests/test_nudge_baton_body_bar.py",
    "write_guards/tests/test_nudge_handoff_ac_shape.py",
    "write_guards/tests/test_nudge_improvement_queue_write.py",
    "write_guards/tests/test_nudge_prose_queue_append.py",
    "write_guards/tests/test_nudge_session_display_name_as_identifier.py",
    "write_guards/tests/test_nudge_tasks_state_folder_split.py",
    "write_guards/tests/test_nudge_terminal_artifact_edit.py",
    "write_guards/tests/test_validate_frontmatter_schema_advisory.py",
    "write_guards/tests/test_validate_frontmatter_schema_deny.py",
    "write_guards/tests/test_validate_frontmatter_schema_handoff_kind_advisory_and_coercion.py",
    "write_guards/tests/test_windows_platform_simulation.py",
    "write_guards/validate_frontmatter_schema_advisory.py",
    "write_guards/validate_frontmatter_schema_deny.py",
})


def _iter_py_files():
    for dirpath, _dirnames, filenames in os.walk(_SCAN_ROOT):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fn)
            if fpath in _EXCLUDED_FILES:
                continue
            yield fpath


@lru_cache(maxsize=1)
def _sites_with_literal():
    """Both tests ask the same question of the same tree, so the walk is
    paid once. This guard runs in the fast tier on a box carrying ~50
    concurrent sessions -- a second full walk of `coordinator_core/` buys
    nothing a cached frozenset does not already hold."""
    hits = set()
    for fpath in _iter_py_files():
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if _JOIN_PATTERN.search(text):
            rel = os.path.relpath(fpath, _SCAN_ROOT).replace(os.sep, "/")
            hits.add(rel)
    return frozenset(hits)


def test_no_new_hand_built_record_home_joins():
    """A literal at a file NOT in `_KNOWN_LITERAL_SITES` fails -- a new
    hand-built `state/<kind>` join landing anywhere in `coordinator_core/`
    (outside `record_homes.py`) is exactly what this guard exists to catch.
    """
    hits = _sites_with_literal()
    unseeded = hits - _KNOWN_LITERAL_SITES
    assert not unseeded, (
        "hand-built 'state/<kind>' path join(s) found outside the seeded "
        f"ratchet set (route through session.record_homes instead): {sorted(unseeded)}"
    )


def test_seeded_sites_still_carry_a_literal():
    """A seeded site that no longer carries a literal fails -- the ratchet
    is one-directional (the set can only shrink), so a file repointed onto
    `record_homes.py`'s accessors must have its exemption removed rather
    than silently kept.
    """
    hits = _sites_with_literal()
    stale = _KNOWN_LITERAL_SITES - hits
    assert not stale, (
        "seeded site(s) no longer carry a hand-built 'state/<kind>' "
        f"literal -- remove from _KNOWN_LITERAL_SITES: {sorted(stale)}"
    )
