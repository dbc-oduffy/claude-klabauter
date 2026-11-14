# Test tiers — markers, worker count, selection

> Detail behind `CLAUDE.md` § Build & Test. Read this and `coordinator.local.md`'s Test-commands
> note before changing either tier.

## Roots and config

**Read the root list off `pyproject.toml` `[tool.pytest.ini_options]` `testpaths`, not off any
prose — including this doc.** `test_*.py` convention. No CI.

Any test-count or `testpaths` figure written in prose — here or in `CLAUDE.md` — is a dated
observation, not a fact: it drifts whenever anyone edits `pyproject.toml`, which is often, and
nothing couples prose to that file. Derive the current root list and selection count with
`pytest --collect-only` rather than trusting a number written down here.

## The fast/full split is marker-scoped, not path-scoped — and the tiers are NOT equal

- `fast_test_cmd`: `-m 'not cadence and not complete and not pending_fix and not designed_red' -n auto`, capped by
  `--maxprocesses` — read the current cap off `coordinator.local.md` frontmatter, not from here.
- `full_test_cmd`: drops the `cadence` exclusion and runs serial.

Four markers govern it:

| Marker | Meaning |
|---|---|
| `cadence` | Heavy suites, cadence-gate tier only |
| `complete` | Complete-tier only: leaves the fast tier (15-minute budget), runs in `full_test_cmd`. Differs from `cadence`: `complete` runs in `full_test_cmd`; `cadence` runs only at the cadence gate |
| `pending_fix` | Known-broken subject, excluded from BOTH tiers. Itemized worklist lives under `state/audits/` |
| `designed_red` | Red by design — its failure output IS the worklist, never a gate |

A `designed_red` test's failure count is itself a live worklist, not a fact to cite in prose —
run it and read its own output for the current itemized set rather than trusting a number written
here.

## Fast-tier budget

The fast tier is budgeted at **15 minutes (900s) wall clock on the fleet-floor box** (12c/24GB,
derived worker cap 6), target 720s for headroom. The figure is projected, not run on that box:
`state/test-tiers/fast-tier-ledger.json` records the measuring box (physical cores, RAM, cap), the
measured wall clock, per-file seconds, and `projected_floor_wall_s` scaled to the floor cap. Current
ledger (2026-11-07, a9c3eb after-cut): 458.0s measured on an 18c/64GB box (cap 9), 687.0s projected for
the floor (cap 6), within budget with headroom under the 720s target. Read the live figures off the ledger, not this sentence.

- **Move list:** the ledger's `moves` array is the authoritative list of tests moved from `fast` to
  `complete` (file, measured seconds, new marker); the table below renders it. Tests leave the tier
  by gaining `pytest.mark.complete`, never by deletion and never to `cadence`.
- **Regenerate:** `python coordinator/bin/fast-tier-budget.py record <junit.xml>` rewrites the ledger
  from a fast-tier junit run; `... rank` lists the next files to move.
- **Re-measure:** EM only, on a quiet slot under a live `tier-u-grant-cli` grant (see § Who runs a
  full or broad suite): run `fast_test_cmd` with `--junitxml`, then `record`.
- **Ratchet:** `coordinator_core/tests/test_fast_tier_budget_ratchet.py` reads the ledger (no clock,
  no subprocess) and fails when `projected_floor_wall_s` exceeds 900, a `complete`-marked file is
  absent from `moves`, or a listed move does not carry the mark (it would run in no tier).

### Move table (rendered from the ledger)

| File | Seconds | New marker |
|---|---|---|
| `coordinator_core/tests/test_home_resolution_lint.py` | 409.9 | complete |
| `coordinator_core/benchmarks/tests/test_clone_teardown_leaves_no_supervisor.py` | 188.0 | complete |
| `coordinator_core/git/tests/test_rollback_check_budget.py` | 92.8 | complete |
| `coordinator_core/bash_guards/tests/test_block_subagent_destructive_action.py` | 89.7 | complete |
| `coordinator/tests/test_publish_payload_import_closure.py` | 87.6 | complete |
| `coordinator_core/bash_guards/tests/test_validate_commit_machine_a_budget.py` | 81.1 | complete |
| `coordinator_core/tests/test_no_uncounted_spawn_on_budgeted_path.py` | 67.5 | complete |
| `coordinator_core/tests/test_docstring_shell_paste_hazard.py` | 62.8 | complete |
| `coordinator_core/testing/test_scoped_pytest.py` | 62.5 | complete |
| `coordinator_core/ops/tests/test_plan_spine_corpus_parses.py` | 61.2 | complete |
| `coordinator/tests/test_percolate_allowlist.py` | 57.9 | complete |
| `coordinator_core/tests/test_no_hardcoded_paths.py` | 53.7 | complete |
| `coordinator_core/tests/test_no_new_spawning_tests.py` | 49.2 | complete |
| `coordinator/tests/test_publish_identity_gate_ephemeral_degrade.py` | 46.0 | complete |
| `coordinator_core/tests/test_no_unsanctioned_shell_spawn.py` | 44.7 | complete |
| `coordinator_core/benchmarks/tests/test_terminal_commit_ab.py` | 42.9 | complete |
| `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py` | 34.7 | complete |
| `coordinator_core/bash_guards/tests/test_guard_message_register_lint.py` | 34.6 | complete |
| `coordinator_core/tests/test_no_bare_hot_path_spawn.py` | 31.8 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_leg_register_ratchet.py` | 30.6 | complete |
| `coordinator_core/percolate/tests/test_function_gate.py` | 29.3 | complete |
| `coordinator/bin/tests/test_bin_emitted_messages_no_unreachable_repo.py` | 27.8 | complete |
| `coordinator_core/bash_guards/tests/test_corpus_audience_axis.py` | 26.9 | complete |
| `coordinator_core/bash_guards/tests/test_alternative_liveness_gate.py` | 26.3 | complete |
| `coordinator_core/session/tests/test_no_untracked_relocation.py` | 25.4 | complete |
| `coordinator_core/tests/test_leaf_spawn_creationflags.py` | 24.9 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_emit.py` | 24.4 | complete |
| `coordinator_core/tests/test_all_modules_compile.py` | 21.1 | complete |
| `coordinator_core/tests/test_no_console_kwargs_are_splatted.py` | 20.7 | complete |
| `coordinator_core/plan_assemble/predicates/test_context.py` | 20.4 | complete |
| `coordinator_core/bash_guards/tests/test_terminal_commit_never_starts_without_params.py` | 20.2 | complete |
| `coordinator/tests/test_arrival_plan_spine_check.py` | 19.7 | complete |
| `coordinator_core/ops/tests/test_check_native_door_interpreter_handoff.py` | 19.7 | complete |
| `coordinator_core/tests/test_citation_graph_claude_klabauter_corpus.py` | 19.5 | complete |
| `coordinator_core/tests/test_no_output_swallowing_no_console_spawn.py` | 18.2 | complete |
| `coordinator_core/session/tests/test_sharded_record_writers.py` | 17.1 | complete |
| `coordinator_core/hooks/tests/test_submit_engine_ask_timeout_names_run.py` | 16.5 | complete |
| `coordinator_core/tests/test_no_retired_root_env_writes_in_fixtures.py` | 16.3 | complete |
| `coordinator_core/git/tests/test_checkin_bytes_text_pin_crlf.py` | 16.3 | complete |
| `coordinator_core/ops/review_mint/tests/test_execute_review.py` | 16.2 | complete |
| `coordinator_core/install/tests/test_no_venv_dependency_resolution.py` | 16.2 | complete |
| `coordinator_core/ceremony_common/test_phantom_resolves_id_sweep.py` | 16.0 | complete |
| `coordinator_core/tests/test_no_silent_bail_on_live_root_resolver.py` | 16.0 | complete |
| `coordinator/bin/tests/test_published_cli_cross_tree_imports_resolve.py` | 15.7 | complete |
| `coordinator_core/tests/test_windows_path_in_escaping_context.py` | 15.7 | complete |
| `coordinator/tests/guards/test_no_decorative_test_assertions.py` | 15.7 | complete |
| `coordinator_core/guard_routes/tests/test_deny_route_key_audience.py` | 15.3 | complete |
| `coordinator_core/bash_guards/tests/test_override_route_inventory.py` | 14.5 | complete |
| `coordinator_core/bash_guards/tests/test_no_machine_absolute_path_in_guard_messages.py` | 14.4 | complete |
| `coordinator_core/workday_complete/test_workday_complete_contract.py` | 14.4 | complete |
| `coordinator_core/tests/test_no_dangling_first_party_import_claude_klabauter_corpus.py` | 14.3 | complete |
| `coordinator_core/bash_guards/tests/test_census_pinned_fates.py` | 13.5 | complete |
| `coordinator_core/tests/test_no_cwd_unguarded_coordinator_core_subprocess_spawn.py` | 13.2 | complete |
| `coordinator_core/install/tests/test_klabauter_freshness_apply.py` | 12.7 | complete |
| `coordinator_core/bash_guards/tests/test_block_reviewer_bash_outside_allowlist.py` | 12.6 | complete |
| `coordinator_core/tests/test_no_dangling_first_party_import.py` | 12.5 | complete |
| `coordinator/bin/tests/test_unimatrix_peer_router.py` | 12.5 | complete |
| `coordinator_core/tests/test_no_bare_racy_stat_write.py` | 12.4 | complete |
| `coordinator_core/bug_blitz/tests/test_targeted_run.py` | 12.2 | complete |
| `coordinator/tests/test_falsifier_proves_the_platform_it_names.py` | 12.2 | complete |
| `coordinator_core/ceremony_common/test_argv_prog_slot_contract.py` | 12.2 | complete |
| `coordinator_core/tests/test_no_bare_chain_terminal_literal.py` | 12.0 | complete |
| `coordinator_core/benchmarks/tests/test_gate_v2_sweep_scaling.py` | 11.8 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_compose.py` | 11.7 | complete |
| `coordinator_core/bash_guards/tests/test_guard_heavy_command_admission.py` | 11.7 | complete |
| `coordinator_core/tests/test_ceremony_budget_ratchet.py` | 11.6 | complete |
| `coordinator_core/bash_guards/tests/test_guard_message_size.py` | 11.5 | complete |
| `coordinator_core/fact_contract_gate/tests/test_fact_contract_gate.py` | 11.5 | complete |
| `coordinator_core/sizing_assemble/test_interaction_mode_touchpoints.py` | 11.4 | complete |
| `coordinator_core/tests/test_commit_surface_protected_blocks.py` | 11.2 | complete |
| `coordinator_core/hooks/tests/test_submit_engine_ask.py` | 11.2 | complete |
| `coordinator_core/roadmap/tests/test_prep_gate_claude_klabauter_corpus.py` | 11.2 | complete |
| `coordinator_core/bash_guards/tests/test_guard_message_size_claude_klabauter_corpus.py` | 11.0 | complete |
| `coordinator_core/bash_guards/tests/test_block_approval_sentinel_creation.py` | 10.9 | complete |
| `coordinator_core/tests/test_advisory_census.py` | 10.8 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_land_run_clean_overlay_verify.py` | 10.6 | complete |
| `coordinator_core/benchmarks/tests/test_land_swap_hook_window.py` | 10.6 | complete |
| `coordinator_core/git/tests/test_direct_git_action_routes_through_the_seam.py` | 10.3 | complete |
| `coordinator_core/baton_assemble/tests/test_apply_degrade_no_compensation.py` | 10.2 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_no_mechanical_halt_reaches_em.py` | 10.1 | complete |
| `coordinator_core/bash_guards/tests/test_check_test_suite_invocation_public_api.py` | 10.1 | complete |
| `coordinator_core/session/tests/test_no_flat_only_output_tree_listing.py` | 9.9 | complete |
| `coordinator_core/warm/tests/test_pool_dispatch_claim_and_stall_dump.py` | 9.8 | complete |
| `coordinator_core/ops/tests/test_distill_apply_disposal_spawn_count.py` | 9.7 | complete |
| `coordinator_core/install/tests/test_no_homebrew_prefix_pip_install.py` | 9.3 | complete |
| `coordinator_core/bash_guards/tests/test_git_tree_facts.py` | 9.2 | complete |
| `coordinator_core/workday_complete/test_brief_completion_receipts.py` | 9.0 | complete |
| `coordinator_core/reconcile/tests/test_no_second_gate_verdict_walk.py` | 8.6 | complete |
| `coordinator_core/tests/test_amp_scan_incremental_equivalence.py` | 8.6 | complete |
| `coordinator_core/bash_guards/tests/test_destructive_action_declared_rm_tracked.py` | 8.4 | complete |
| `coordinator_core/bash_guards/tests/test_python_c_inert_payload_exemption.py` | 8.3 | complete |
| `coordinator_core/install/tests/test_settings_home_report.py` | 8.2 | complete |
| `coordinator_core/install/test_maximalist.py` | 8.2 | complete |
| `coordinator_core/bash_guards/tests/test_guard_message_corpus.py` | 8.1 | complete |
| `coordinator_core/warm/tests/test_supervisor_hook_serves_real_guard.py` | 8.1 | complete |
| `coordinator_core/test_sizing_assemble.py` | 7.8 | complete |
| `coordinator_core/roadmap/tests/test_probe_store.py` | 7.8 | complete |
| `coordinator_core/bash_guards/tests/test_heavy_command_class.py` | 7.6 | complete |
| `coordinator_core/bash_guards/tests/test_python_c_constant_folding.py` | 7.6 | complete |
| `coordinator_core/plugin_health/tests/test_sentinel.py` | 7.5 | complete |
| `coordinator_core/ops/tests/test_handoff_append_session_ledger.py` | 7.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_emit_post_exec_tail_overlap.py` | 7.3 | complete |
| `coordinator_core/hooks/tests/test_post_executor_verification.py` | 7.3 | complete |
| `coordinator_core/ops/ceremony/tests/test_pre_commit_gates_landed_path_scale.py` | 7.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_commit_legs_are_haiku.py` | 7.2 | complete |
| `coordinator_core/hooks/tests/test_submit_engine_ask_mint_or_refuse.py` | 7.1 | complete |
| `coordinator_core/tests/test_shared_git_runner.py` | 6.9 | complete |
| `coordinator_core/frontmatter/tests/test_primitives.py` | 6.9 | complete |
| `coordinator_core/tests/test_holds_no_legacy_root.py` | 6.8 | complete |
| `coordinator_core/benchmarks/tests/test_assembler_brightline_conformance.py` | 6.7 | complete |
| `coordinator_core/ops/ceremony/tests/test_commit_route_release_tripwire.py` | 6.7 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_fan_launcher.py` | 6.7 | complete |
| `coordinator_core/tests/test_reverify_scope.py` | 6.7 | complete |
| `coordinator_core/tests/test_re_sub_replacement_template_is_literal_or_callable.py` | 6.7 | complete |
| `coordinator_core/ops/tests/test_registration_annotations_resolve.py` | 6.6 | complete |
| `coordinator_core/group_em/tests/test_gem_home_sync.py` | 6.6 | complete |
| `coordinator_core/ops/tests/test_seam_baton_mint.py` | 6.6 | complete |
| `coordinator_core/benchmarks/tests/test_terminal_sizing_drain_budget.py` | 6.5 | complete |
| `coordinator_core/warm/tests/test_front_door.py` | 6.5 | complete |
| `coordinator_core/roadmap/tests/test_probe_store_dir_stats.py` | 6.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_hunk_stage.py` | 6.4 | complete |
| `coordinator_core/bash_guards/tests/test_destructive_action_declared_git_rm.py` | 6.4 | complete |
| `coordinator_core/bash_guards/tests/test_blanket_add_quoted_dash_c.py` | 6.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_land_credited.py` | 6.3 | complete |
| `coordinator_core/bash_guards/tests/test_quote_split_verb_bypass_sweep.py` | 6.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_agent_route.py` | 6.2 | complete |
| `coordinator_core/git/tests/test_index_lock_abandon_recovery.py` | 6.2 | complete |
| `coordinator_core/tests/test_orient_brief_health.py` | 6.1 | complete |
| `coordinator_core/housekeeping/tests/test_corpus.py` | 6.1 | complete |
| `coordinator_core/bash_guards/tests/test_operator_override_note_no_key_literal.py` | 6.1 | complete |
| `coordinator_core/tests/test_guard_module_name_resolution.py` | 6.0 | complete |
| `coordinator_core/tests/test_hot_path_disk_saturation.py` | 6.0 | complete |
| `coordinator_core/tests/test_no_legacy_touch_record_literal.py` | 6.0 | complete |
| `coordinator_core/ops/tests/test_no_unmanaged_temp_creation.py` | 6.0 | complete |
| `coordinator_core/roadmap/tests/test_engine_key_semantics_pin.py` | 6.0 | complete |
| `coordinator_core/tests/test_no_bare_timestamp_render.py` | 5.8 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_land_deadline_leg_cost.py` | 5.8 | complete |
| `coordinator_core/ops/tests/test_changelog_ops_backfill_week.py` | 5.8 | complete |
| `coordinator/bin/tests/test_publish_process_target_empty_staging.py` | 5.8 | complete |
| `coordinator_core/session/tests/test_touch_kind_read_vs_write.py` | 5.8 | complete |
| `coordinator_core/bash_guards/tests/test_guard_message_corpus_no_process_control.py` | 5.7 | complete |
| `coordinator_core/percolate/tests/test_published_test_module_passes.py` | 5.7 | complete |
| `coordinator/tests/test_doe_script_arrivals.py` | 5.7 | complete |
| `coordinator_core/ops/tests/test_plan_readiness_verdict.py` | 5.7 | complete |
| `coordinator_core/percolate/tests/test_no_py313_only_pathlib_kwargs.py` | 5.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_emit_wake_digest.py` | 5.6 | complete |
| `coordinator_core/workstream_complete/test_close_coverage_advisory.py` | 5.5 | complete |
| `coordinator_core/bash_guards/tests/test_guard_render_baseline.py` | 5.5 | complete |
| `coordinator_core/execute_plan_assemble/tests/test_verify_disposition_ref_qualified.py` | 5.4 | complete |
| `coordinator_core/bash_guards/tests/test_confinement_deny_band_shape.py` | 5.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_grind_compose.py` | 5.4 | complete |
| `coordinator_core/frontmatter/tests/test_no_node_schema_shellout.py` | 5.4 | complete |
| `coordinator_core/warm/door/tests/test_hook_mode_post_delivery_names_timeout.py` | 5.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_objective_write.py` | 5.4 | complete |
| `coordinator/bin/tests/test_coordinator_safe_commit_pathspec_from_file.py` | 5.4 | complete |
| `coordinator_core/testing/test_fs_containment.py` | 5.4 | complete |
| `coordinator_core/warm/tests/test_stuck_pool_request_reads_terminal.py` | 5.4 | complete |
| `coordinator_core/tests/test_fleet_mode_reader_set.py` | 5.3 | complete |
| `coordinator_core/ops/test_verify_coverage.py` | 5.3 | complete |
| `coordinator_core/workday_complete/test_apply_memory_footprint.py` | 5.2 | complete |
| `coordinator_core/bash_guards/tests/test_git_commit_safe_commit_firing_shape.py` | 5.2 | complete |
| `coordinator_core/bash_guards/tests/test_no_guard_builds_bare_envelope.py` | 5.2 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_land_run_measured_rev_peer_dirt.py` | 5.2 | complete |
| `coordinator_core/tests/test_no_bash_dependency.py` | 5.2 | complete |
| `coordinator_core/bash_guards/tests/test_commit_scope_directory_operand_still_advises.py` | 5.2 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_emit_script_size_cap.py` | 5.2 | complete |
| `coordinator_core/test_no_bare_argv0_script_launch.py` | 5.1 | complete |
| `coordinator_core/ops/tests/test_eager_op_modules_covers_every_register_op.py` | 5.1 | complete |
| `coordinator_core/percolate/tests/test_entrypoint_gate_timeout_classification.py` | 5.1 | complete |
| `coordinator_core/tests/test_no_pm_only_unlocks.py` | 5.1 | complete |
| `coordinator_core/git/tests/test_commit_paths_pin_loop_reads_once.py` | 5.1 | complete |
| `coordinator_core/bug_blitz/tests/test_runner_shapes_admitted.py` | 5.1 | complete |
| `coordinator_core/tests/test_no_listener_pid_status_reliance.py` | 5.1 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_run_token_removers_enumerated.py` | 5.0 | complete |
| `coordinator/bin/tests/test_bootstrap_preamble_has_one_home.py` | 5.0 | complete |
| `coordinator_core/tests/test_denial_guard_fleet_mode_independence.py` | 5.0 | complete |
| `coordinator_core/ops/tests/test_no_unguarded_cross_repo_import.py` | 4.9 | complete |
| `coordinator_core/warm/tests/test_host_pin.py` | 4.9 | complete |
| `coordinator_core/warm/door/tests/test_hook_mode_repo_root_precheck.py` | 4.8 | complete |
| `coordinator_core/test_trusted_root_guard.py` | 4.7 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_land_run.py` | 4.7 | complete |
| `coordinator_core/install/tests/test_shaped_writers_report.py` | 4.6 | complete |
| `coordinator_core/tests/test_known_red_ratchet.py` | 4.6 | complete |
| `coordinator_core/reconcile/tests/test_gate_eval.py` | 4.6 | complete |
| `coordinator/tests/test_arrival_emit_wave_fire.py` | 4.6 | complete |
| `coordinator_core/ops/tests/test_app_session_validate_run.py` | 4.6 | complete |
| `coordinator_core/tests/test_no_dead_bin_references_claude_klabauter_corpus.py` | 4.5 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_sibling_claims.py` | 4.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_grind_stages.py` | 4.4 | complete |
| `coordinator_core/test_daily_branch.py` | 4.4 | complete |
| `coordinator_core/tests/test_operator_cli_engine_contact.py` | 4.3 | complete |
| `coordinator_core/warm/tests/test_http_listener.py` | 4.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_replay_pd_invariant_map_commit_time.py` | 4.3 | complete |
| `coordinator_core/ops/tests/test_seam_unreachable_later_consumer.py` | 4.2 | complete |
| `coordinator_core/ops/tests/test_gen_dod_backlog_fragment.py` | 4.2 | complete |
| `coordinator/bin/tests/test_unimatrix_host_guard.py` | 4.2 | complete |
| `coordinator_core/warm/tests/test_cookie_gate.py` | 4.2 | complete |
| `coordinator_core/ops/tests/test_daily_summary_gap.py` | 4.1 | complete |
| `coordinator_core/tests/test_hook_envelope_validity.py` | 4.1 | complete |
| `coordinator_core/testing/test_run.py` | 4.0 | complete |
| `coordinator_core/resolution/test_facade.py` | 4.0 | complete |
| `coordinator_core/frontmatter/tests/test_routed_sizing_carries_a_deliverable_id.py` | 4.0 | complete |
| `coordinator_core/warm/tests/test_live_runs.py` | 4.0 | complete |
| `coordinator_core/tests/test_session_leaves_checkout_head_and_index.py` | 4.0 | complete |
| `coordinator_core/ops/docgen/tests/test_volatility.py` | 3.9 | complete |
| `coordinator_core/warm/tests/test_client_fallback.py` | 3.9 | complete |
| `coordinator_core/install/tests/test_uninstall_agentic_judgment_no_enumeration.py` | 3.9 | complete |
| `coordinator_core/tests/test_no_forked_frontmatter_key_regex.py` | 3.9 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_landing_integrity_reverify_indeterminate.py` | 3.9 | complete |
| `coordinator/bin/tests/test_function_gate_wiring.py` | 3.8 | complete |
| `coordinator_core/ops/ceremony/tests/test_node_handlers.py` | 3.8 | complete |
| `coordinator_core/install/test_forwarder_write_lock_and_venv_swap_sweep.py` | 3.8 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_seam_wave_boundary.py` | 3.8 | complete |
| `coordinator_core/ops/test_verify_skill_anchor_links.py` | 3.7 | complete |
| `coordinator_core/workweek_complete/test_apply_memory_footprint.py` | 3.7 | complete |
| `coordinator_core/bash_guards/tests/test_blanket_disarm.py` | 3.7 | complete |
| `coordinator_core/warm/tests/test_front_door_stop_drains.py` | 3.7 | complete |
| `coordinator_core/install/tests/test_uninstall_agentic_judgment_no_enumeration_claude_klabauter_corpus.py` | 3.7 | complete |
| `coordinator_core/percolate/tests/test_peer_publish_drift.py` | 3.6 | complete |
| `coordinator/lib/percolate/tests/test_unimatrix_bin_in_payload.py` | 3.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_foreign_hunks.py` | 3.6 | complete |
| `coordinator_core/benchmarks/tests/test_land_run_leg_budget_profile.py` | 3.6 | complete |
| `coordinator_core/roadmap/tests/test_sizing_match.py` | 3.6 | complete |
| `coordinator_core/tests/test_install_substrate_uninstall_legs.py` | 3.5 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_run_close.py` | 3.5 | complete |
| `coordinator/lib/test_home_resolution_lint_engine.py` | 3.5 | complete |
| `coordinator_core/roadmap/tests/test_gate_v2.py` | 3.5 | complete |
| `coordinator_core/benchmarks/tests/test_integration_claude_klabauter_corpus.py` | 3.5 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_terminal_commit_ignore_answer_scale.py` | 3.5 | complete |
| `coordinator_core/ops/tests/test_gen_ported_ops_fragment.py` | 3.5 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_every_emitted_leg_names_its_sidecar.py` | 3.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_deferred_em_context_arrival.py` | 3.4 | complete |
| `coordinator_core/symbol_knowledge/tests/test_flag.py` | 3.4 | complete |
| `coordinator_core/group_em/tests/test_watch.py` | 3.4 | complete |
| `coordinator_core/ops/tests/test_updatedocs_gates.py` | 3.4 | complete |
| `coordinator_core/benchmarks/tests/test_land_run_idle_wait_profile.py` | 3.4 | complete |
| `coordinator_core/roadmap/tests/test_prep_gate.py` | 3.4 | complete |
| `coordinator_core/ops/test_percolate_preflight_scratch_publish.py` | 3.4 | complete |
| `coordinator_core/install/tests/test_install_timeout_family.py` | 3.4 | complete |
| `coordinator_core/tests/test_red_set_report.py` | 3.4 | complete |
| `coordinator_core/group_em/tests/test_gem_home.py` | 3.4 | complete |
| `coordinator_core/bash_guards/tests/test_scratch_clone_failures.py` | 3.3 | complete |
| `coordinator/bin/tests/test_group_em_memory_governor.py` | 3.3 | complete |
| `coordinator_core/tests/test_session_dir_has_one_constructor.py` | 3.3 | complete |
| `coordinator/tests/test_published_bin_lib_imports_resolve.py` | 3.3 | complete |
| `coordinator_core/tests/test_landing_refuses_only_irreversible.py` | 3.3 | complete |
| `coordinator_core/tests/test_pretooluse_advisory_ratchet.py` | 3.3 | complete |
| `coordinator_core/benchmarks/tests/test_benchmark_drivers_declare_origin.py` | 3.3 | complete |
| `coordinator_core/agent_pipe/tests/test_peer.py` | 3.3 | complete |
| `coordinator_core/percolate/tests/test_codename_provenance_seed.py` | 3.3 | complete |
| `coordinator/tests/test_coordinator_install.py` | 3.3 | complete |
| `coordinator_core/ops/tests/test_clean_clone_proof.py` | 3.2 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_gate.py` | 3.2 | complete |
| `coordinator_core/warm/tests/test_pool_timeout_names_blocked_leg.py` | 3.2 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_write_set_shared_once.py` | 3.1 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_checkpoint_commit_and_push.py` | 3.1 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_own_hunk_stage.py` | 3.1 | complete |
| `coordinator_core/tests/test_op_path_reads_bounded_ratchet.py` | 3.1 | complete |
| `scripts/tests/test_cloud_setup_orchestration.py` | 3.1 | complete |
| `coordinator_core/bash_guards/tests/test_destructive_core_powershell_oracle.py` | 3.1 | complete |
| `scripts/test_setup_receipt_route.py` | 3.0 | complete |
| `coordinator/bin/test_bin_clis_bootstrap_lib.py` | 3.0 | complete |
| `coordinator_core/session/tests/test_output_tree_sharded_e2e.py` | 3.0 | complete |
| `coordinator_core/hooks/tests/test_submit_engine_ask_resume_record.py` | 3.0 | complete |
| `coordinator_core/ops/test_emit_artifact_shape_contract.py` | 3.0 | complete |
| `coordinator_core/hooks/tests/test_submit_engine_ask_overran_no_phantom.py` | 3.0 | complete |
| `coordinator_core/ops/tests/test_anchor_premise.py` | 3.0 | complete |
| `coordinator_core/session/tests/test_touch_record.py` | 2.9 | complete |
| `coordinator/tests/test_queue_append_legacy_collision.py` | 2.9 | complete |
| `coordinator_core/tests/test_no_pathspec_less_commit.py` | 2.9 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_refire_resumes_halted_run.py` | 2.9 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_emit_rolling_review_final_opus.py` | 2.9 | complete |
| `coordinator_core/contract/decision_object/tests/test_reportable_partition.py` | 2.9 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_landing_refuses_lost_definition.py` | 2.9 | complete |
| `coordinator/bin/tests/test_workstream_complete_assemble_redirect.py` | 2.9 | complete |
| `coordinator_core/benchmarks/tests/test_real_shape_fixture_fidelity.py` | 2.9 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_compose_end_to_end.py` | 2.9 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_land_remainder_commit.py` | 2.9 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_requirements_ledger.py` | 2.8 | complete |
| `coordinator/bin/tests/test_classify_env_var_callers.py` | 2.8 | complete |
| `coordinator_core/ops/review_mint/tests/test_execute_review_slice_id_binding.py` | 2.8 | complete |
| `coordinator_core/tests/test_atomic_append_blocked_sink.py` | 2.8 | complete |
| `coordinator_core/ops/compute_layer_scaffold/tests/test_check.py` | 2.8 | complete |
| `coordinator_core/ops/tests/test_review_reverify_head_clamp.py` | 2.8 | complete |
| `coordinator/bin/tests/test_bootstrap_dominance.py` | 2.8 | complete |
| `coordinator/bin/tests/test_unimatrix_mirror_roster.py` | 2.8 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_halted_run.py` | 2.8 | complete |
| `coordinator_core/hooks/tests/test_submit_engine_ask_deferred_long_brief.py` | 2.8 | complete |
| `coordinator_core/ops/tests/test_handoff_transition_readiness_no_corpus_walk.py` | 2.8 | complete |
| `coordinator_core/tests/test_pickup_brief_chain_no_full_index.py` | 2.8 | complete |
| `coordinator_core/benchmarks/tests/test_terminal_commit_real_session_phases.py` | 2.8 | complete |
| `coordinator_core/git/tests/test_store_hygiene.py` | 2.8 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_m_plus_chain_first_fire_run_id.py` | 2.8 | complete |
| `coordinator_core/bash_guards/tests/test_guard_em_post_fire_shell_write.py` | 2.8 | complete |
| `coordinator_core/warm/tests/test_dispatch_ack.py` | 2.8 | complete |
| `coordinator_core/session/tests/test_claim_index.py` | 2.7 | complete |
| `coordinator_core/roadmap/tests/test_blitz_stage.py` | 2.7 | complete |
| `scripts/tests/test_migrate_root_sizings_to_shard.py` | 2.7 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_op.py` | 2.7 | complete |
| `coordinator_core/warm/tests/test_pool_death_diagnosis.py` | 2.7 | complete |
| `coordinator_core/tests/test_no_hand_built_legacy_share_root.py` | 2.7 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_land_credited_leg_budget.py` | 2.7 | complete |
| `coordinator/bin/tests/test_unimatrix_peer_router_durable.py` | 2.7 | complete |
| `scripts/test_setup.py` | 2.7 | complete |
| `coordinator/bin/lib/tests/test_assemble_help_contract.py` | 2.7 | complete |
| `coordinator_core/landing_audit/tests/test_foreign_hunks.py` | 2.7 | complete |
| `coordinator_core/benchmarks/tests/test_measure_cli.py` | 2.7 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_terminal_commit_refire_inherits_withheld_scope.py` | 2.7 | complete |
| `coordinator_core/test_merge_assemble.py` | 2.7 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_landing_integrity.py` | 2.7 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_land_credited_receipt_off_path.py` | 2.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_spine_read_depends_on_plan.py` | 2.6 | complete |
| `coordinator_core/tests/test_atomic_append_multiprocess_soak.py` | 2.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_measured_rev_nested_checkout.py` | 2.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_minted_records.py` | 2.6 | complete |
| `coordinator_core/ops/review_mint/tests/test_roster.py` | 2.6 | complete |
| `coordinator/bin/tests/test_no_raw_sys_executable_argv_lands.py` | 2.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_review_only_emit.py` | 2.6 | complete |
| `coordinator_core/ops/session/tests/test_guard_concrete_path_citations.py` | 2.6 | complete |
| `coordinator_core/roadmap/tests/test_post_stamp_clause.py` | 2.6 | complete |
| `coordinator_core/agent_pipe/tests/test_relay.py` | 2.6 | complete |
| `coordinator_core/bash_guards/tests/test_tier_t_concurrency_leg.py` | 2.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_emit_artifact_naming_and_out.py` | 2.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_agent_model_table_matches_doe.py` | 2.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_submission_agent_count_ratchet.py` | 2.5 | complete |
| `coordinator_core/write_guards/tests/test_guard_memory_store_cap.py` | 2.5 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_land_remainder_verify.py` | 2.5 | complete |
| `coordinator/bin/tests/test_cc_invoke_stamped_in_process.py` | 2.5 | complete |
| `coordinator_core/benchmarks/tests/test_land_run_leg_budget_profile_real_record.py` | 2.5 | complete |
| `coordinator_core/testing/test_spawn_depth.py` | 2.5 | complete |
| `coordinator_core/hooks/tests/test_guard_em_prefire_survey.py` | 2.5 | complete |
| `coordinator/bin/tests/test_unimatrix_pair.py` | 2.5 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_op_emission_receipt.py` | 2.5 | complete |
| `coordinator/lib/percolate/tests/test_import_closure_commit_names.py` | 2.5 | complete |
| `coordinator/tests/test_bin_cli_patch_seams_module_scoped.py` | 2.5 | complete |
| `coordinator_core/benchmarks/tests/test_land_run_halted_shape_budget.py` | 2.5 | complete |
| `coordinator_core/testing/test_collect.py` | 2.5 | complete |
| `coordinator_core/bash_guards/tests/test_record_audience_destructive_family.py` | 2.5 | complete |
| `coordinator/tests/test_percolate_ignore_composition.py` | 2.5 | complete |
| `coordinator_core/bash_guards/tests/test_suite_invocation_shell_c_chained_payload.py` | 2.5 | complete |
| `coordinator_core/session/tests/test_session_facts.py` | 2.5 | complete |
| `coordinator_core/bash_guards/tests/test_guard_grep_via_bash.py` | 2.5 | complete |
| `coordinator_core/bash_guards/tests/test_commit_operand_scan_redirection.py` | 2.5 | complete |
| `coordinator_core/housekeeping/tests/test_archive_index_cache.py` | 2.5 | complete |
| `coordinator_core/ops/fleet/tests/test_reap_unintegrated_findings.py` | 2.5 | complete |
| `coordinator_core/tests/test_no_direct_retired_root_env_reads.py` | 2.5 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_m_plus_chain_emit_fallback.py` | 2.5 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_objective_calibrate.py` | 2.5 | complete |
| `coordinator_core/roadmap/tests/test_gate_v2_claude_klabauter_corpus.py` | 2.5 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_retest_fresh_by_blob.py` | 2.5 | complete |
| `coordinator_core/warm/tests/test_pair_stop.py` | 2.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_land_run_bounded_waits.py` | 2.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_spine_read.py` | 2.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_inventory_split_cap_and_refusal.py` | 2.4 | complete |
| `coordinator_core/ops/tests/test_handoff_discharge_criteria.py` | 2.4 | complete |
| `coordinator_core/ops/tests/test_withhold_registration_census.py` | 2.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_emit_prompts_name_sidecar_dir.py` | 2.4 | complete |
| `coordinator_core/search/tests/test_sources_powershell.py` | 2.4 | complete |
| `coordinator_core/percolate/tests/test_registry_slug_scrub_coverage.py` | 2.4 | complete |
| `coordinator_core/ops/tests/test_plan_seam_check.py` | 2.4 | complete |
| `coordinator_core/benchmarks/tests/test_no_unsanctioned_clone_destination.py` | 2.4 | complete |
| `coordinator_core/spawn_policy/tests/test_detect.py` | 2.4 | complete |
| `coordinator_core/agent_pipe/tests/test_acp.py` | 2.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_refire_withheld_plus_unclaimed_replans_and_executes.py` | 2.4 | complete |
| `coordinator_core/codex_runner/tests/test_agy.py` | 2.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_run_token.py` | 2.3 | complete |
| `coordinator_core/commit_ledger/tests/test_producer_coverage.py` | 2.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_op_lanes.py` | 2.3 | complete |
| `coordinator_core/contract/test_apply_base.py` | 2.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_dispatch_emit_fixtures_attach.py` | 2.3 | complete |
| `coordinator_core/tests/test_raw_writes_have_a_disposition.py` | 2.3 | complete |
| `coordinator/bin/tests/test_percolate_identity_check_gate.py` | 2.3 | complete |
| `coordinator_core/warm/tests/test_pool_dispatch_hang_fix.py` | 2.3 | complete |
| `coordinator/tests/test_arrival_waste_signal.py` | 2.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_compose_launcher_child_resume.py` | 2.3 | complete |
| `coordinator/bin/tests/test_publish_refuses_dirty_publisher_code.py` | 2.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_minted_parent.py` | 2.3 | complete |
| `coordinator_core/ops/ceremony/tests/test_close_background.py` | 2.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_gate_design_approval.py` | 2.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_gate_owed.py` | 2.3 | complete |
| `coordinator_core/hooks/tests/test_block_ungranted_opus_subagent.py` | 2.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_terminal_commit_cli_escape.py` | 2.3 | complete |
| `coordinator/tests/test_arrival_compose_review_wave.py` | 2.2 | complete |
| `coordinator_core/tests/test_no_stage_without_commit.py` | 2.2 | complete |
| `coordinator_core/ops/test_learn_lessons_roots.py` | 2.2 | complete |
| `coordinator_core/testing/test_nested_session_guard.py` | 2.2 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_unverifiable_enricher_row.py` | 2.2 | complete |
| `coordinator_core/workday_complete/tests/test_register_stall_point.py` | 2.2 | complete |
| `coordinator_core/write_guards/tests/test_validate_frontmatter_schema_advisory.py` | 2.2 | complete |
| `coordinator_core/tests/test_install_health_run.py` | 2.2 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_design_conformance.py` | 2.2 | complete |
| `coordinator_core/install/tests/test_write_reaching_modules_declare.py` | 2.2 | complete |
| `coordinator_core/ops/tests/test_initiatives_store.py` | 2.2 | complete |
| `coordinator_core/bash_guards/tests/test_block_worktree_sentinel_creation.py` | 2.2 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_emit_verify_leg.py` | 2.2 | complete |
| `coordinator/bin/tests/test_unimatrix_membership.py` | 2.2 | complete |
| `coordinator_core/warm/tests/test_every_allowlisted_name_warm_serves.py` | 2.2 | complete |
| `coordinator_core/percolate/tests/test_entrypoint_depth_probe.py` | 2.2 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_from_body.py` | 2.2 | complete |
| `coordinator_core/warm/tests/test_warm_engine_stall_self_recycle.py` | 2.2 | complete |
| `coordinator_core/bash_guards/tests/test_block_subagent_destructive_action_scratch_clone.py` | 2.2 | complete |
| `coordinator_core/bash_guards/tests/test_guard_subagent_heavy_ue_launch.py` | 2.2 | complete |
| `coordinator_core/ceremony_common/test_phantom_resolves_sweep_discovery.py` | 2.2 | complete |
| `coordinator_core/ops/tests/test_review_reverify_foreign_dirt_survives_revert.py` | 2.2 | complete |
| `coordinator_core/ops/test_warp_run_deliverable.py` | 2.2 | complete |
| `coordinator_core/percolate/tests/test_store.py` | 2.1 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_refire_holds_live_run_token.py` | 2.1 | complete |
| `coordinator/bin/tests/test_unimatrix_relay_membership.py` | 2.1 | complete |
| `coordinator_core/hooks/tests/test_arrival_w4_c12.py` | 2.1 | complete |
| `coordinator_core/ops/tests/test_review_findings_ledger.py` | 2.1 | complete |
| `coordinator_core/tests/test_async_handler_discipline.py` | 2.1 | complete |
| `coordinator_core/bash_guards/tests/test_no_false_clean_on_unparsed_dialect.py` | 2.1 | complete |
| `coordinator_core/warm/tests/test_saturated_pool_queued_request_never_runs_late.py` | 2.1 | complete |
| `coordinator_core/bash_guards/tests/test_block_dev_repo_sentinel_removal.py` | 2.1 | complete |
| `coordinator_core/ops/tests/test_deliverable_ledger_write.py` | 2.1 | complete |
| `coordinator_core/ops/ceremony/tests/test_detached_spawn.py` | 2.1 | complete |
| `coordinator_core/group_em/tests/test_idle_report.py` | 2.1 | complete |
| `coordinator_core/ops/tests/test_session_landing_hold_op.py` | 2.1 | complete |
| `coordinator_core/workday_start/tests/test_cli_end_to_end.py` | 2.1 | complete |
| `coordinator/tests/test_arrival_measure_loopback_round_trip.py` | 2.1 | complete |
| `coordinator_core/percolate/tests/test_guards.py` | 2.1 | complete |
| `coordinator_core/telemetry/tests/test_op_latency.py` | 2.1 | complete |
| `coordinator_core/install/tests/test_ensure_venv.py` | 2.1 | complete |
| `coordinator_core/install/test_prereq_probe.py` | 2.0 | complete |
| `coordinator/tests/test_cross_platform_invocability_gate.py` | 2.0 | complete |
| `coordinator_core/install/test_sandbox_check.py` | 2.0 | complete |
| `coordinator_core/tests/test_no_global_os_name_monkeypatch.py` | 2.0 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_unclaimed_dirt_stage.py` | 2.0 | complete |
| `coordinator_core/sizing_assemble/test_research_flags.py` | 2.0 | complete |
| `coordinator_core/ops/ceremony/tests/test_renderers_plans_join.py` | 2.0 | complete |
| `coordinator_core/ops/tests/test_plan_merge.py` | 2.0 | complete |
| `coordinator/bin/tests/test_unimatrix_install.py` | 2.0 | complete |
| `coordinator_core/install/tests/test_door_install_posix_build.py` | 2.0 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_land_run_leg_budget.py` | 2.0 | complete |
| `coordinator_core/tests/test_directive_cli_subcommand_arity.py` | 2.0 | complete |
| `coordinator_core/ops/tests/test_review_prep_pre_review_snapshot.py` | 2.0 | complete |
| `coordinator/tests/test_arrival_atlas_citation_check.py` | 2.0 | complete |
| `coordinator_core/bash_guards/tests/test_block_subagent_commit_terminal_params_file.py` | 2.0 | complete |
| `coordinator_core/ops/fleet/tests/test_cancelled_commit_does_not_outlive_its_index.py` | 2.0 | complete |
| `coordinator_core/guard_routes/tests/test_assemble.py` | 2.0 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_retest_covered_source.py` | 2.0 | complete |
| `coordinator_core/testing/test_pytest_spawners_capped.py` | 2.0 | complete |
| `coordinator_core/symbol_knowledge/tests/test_close_leg_e2e.py` | 2.0 | complete |
| `coordinator_core/ops/ceremony/tests/test_receipt_schema.py` | 2.0 | complete |
| `coordinator_core/source_edit_gate/tests/test_gate.py` | 2.0 | complete |
| `coordinator/bin/tests/test_unimatrix_peer_codex.py` | 2.0 | complete |
| `coordinator_core/ceremony_common/test_apply_halt.py` | 2.0 | complete |
| `coordinator_core/ops/tests/test_research_grounding.py` | 2.0 | complete |
| `coordinator_core/frontmatter/tests/test_lint_tree.py` | 2.0 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_fanout_contract.py` | 2.0 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_gate_resume_kept_work.py` | 2.0 | complete |
| `coordinator_core/invoke/tests/test_warm_fail_hard.py` | 1.9 | complete |
| `coordinator_core/tests/test_cold_mutating_op_reports_real_outcome.py` | 1.9 | complete |
| `coordinator_core/hooks/tests/test_multi_ask_sibling_runs_e2e.py` | 1.9 | complete |
| `coordinator_core/bash_guards/tests/test_block_subagent_destructive_action_ceilinged_init.py` | 1.9 | complete |
| `coordinator_core/ops/tests/test_plan_capture_persist.py` | 1.9 | complete |
| `coordinator_core/roadmap/tests/test_mise_residue.py` | 1.9 | complete |
| `coordinator_core/ops/tests/test_handoff_append_session_ledger_two_ceremony.py` | 1.9 | complete |
| `coordinator_core/bash_guards/tests/test_no_patch_on_dispatch_checks_facade.py` | 1.9 | complete |
| `coordinator_core/ops/tests/test_plan_mise_prep_probe.py` | 1.9 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_baton_sizing_object.py` | 1.9 | complete |
| `coordinator_core/tests/test_machine_profile.py` | 1.9 | complete |
| `coordinator_core/percolate/tests/test_entrypoint_gate_layout_canonicalisation.py` | 1.9 | complete |
| `coordinator/bin/tests/test_group_em_home_engine_root.py` | 1.9 | complete |
| `coordinator_core/ops/generator_census/tests/test_census_semantics.py` | 1.9 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_spent_run_reemit_next_tranche.py` | 1.9 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_inventory_mint.py` | 1.9 | complete |
| `coordinator/tests/test_arrival_plan_completeness.py` | 1.9 | complete |
| `coordinator_core/warm/tests/test_warm_guard_result.py` | 1.9 | complete |
| `coordinator_core/snippet_sync/tests/test_verify.py` | 1.9 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_fanout.py` | 1.9 | complete |
| `coordinator_core/session/tests/test_harness_registry.py` | 1.9 | complete |
| `coordinator_core/tests/test_c6_pointer_normalization.py` | 1.9 | complete |
| `coordinator_core/ops/tests/test_baton_close_report_decide.py` | 1.9 | complete |
| `coordinator_core/ops/tests/test_plan_stamp_prepped.py` | 1.9 | complete |
| `coordinator_core/tests/test_cloud_em_run_in_mounted_repo.py` | 1.8 | complete |
| `coordinator_core/ops/test_detect_guardless_sessions.py` | 1.8 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_cli_admission.py` | 1.8 | complete |
| `coordinator_core/codex_runner/tests/test_grok.py` | 1.8 | complete |
| `coordinator/bin/tests/test_unimatrix_autolink.py` | 1.8 | complete |
| `coordinator/bin/tests/test_unimatrix_doctor.py` | 1.8 | complete |
| `coordinator_core/ops/fleet/tests/test_terminal_sizing_drain.py` | 1.8 | complete |
| `coordinator_core/session/tests/test_fleet_delegation.py` | 1.8 | complete |
| `coordinator_core/ops/tests/test_anchor_check.py` | 1.8 | complete |
| `coordinator_core/tests/test_orient_brief_contract.py` | 1.8 | complete |
| `coordinator_core/percolate/tests/test_scrub_table_shape_publish_surface.py` | 1.8 | complete |
| `coordinator_core/hooks/tests/test_submit_engine_ask_direct_start_chain.py` | 1.8 | complete |
| `coordinator_core/ops/test_gen_claude_author_shim.py` | 1.8 | complete |
| `coordinator_core/text/test_refresh_queries.py` | 1.8 | complete |
| `coordinator_core/ops/tests/test_completion_nature.py` | 1.8 | complete |
| `coordinator_core/ops/tests/test_wiring_contract.py` | 1.8 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_run_terminality_op.py` | 1.8 | complete |
| `coordinator_core/sizing_assemble/test_write_back.py` | 1.8 | complete |
| `coordinator_core/workstream_complete/test_completion_verdict.py` | 1.8 | complete |
| `coordinator_core/ops/tests/test_review_reverify_sibling_owned.py` | 1.7 | complete |
| `coordinator_core/warm/door/tests/test_hook_live_engine_slow_guard_gets_verdict.py` | 1.7 | complete |
| `coordinator_core/hooks/tests/test_arrival_w4_c10.py` | 1.7 | complete |
| `coordinator_core/tests/test_settings_home.py` | 1.7 | complete |
| `coordinator_core/tests/test_dispatch_message.py` | 1.7 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_terminal_commit_ships_run_baton.py` | 1.7 | complete |
| `coordinator_core/test_watchdog.py` | 1.7 | complete |
| `coordinator_core/ops/tests/test_deferral_detect_partial_strangle.py` | 1.7 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_test_stage_baseline.py` | 1.7 | complete |
| `coordinator_core/percolate/tests/test_token_index.py` | 1.7 | complete |
| `coordinator_core/warm/tests/test_hook_degrade_record.py` | 1.7 | complete |
| `coordinator_core/ops/tests/test_session_baton_promote_dr173_predicate.py` | 1.7 | complete |
| `coordinator_core/invoke/tests/test_warm_miss.py` | 1.7 | complete |
| `coordinator_core/warm/tests/test_entrypoint_argv_route_parity.py` | 1.7 | complete |
| `coordinator_core/warm/tests/test_two_homes_one_warm_server.py` | 1.7 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_fanout_sizings_arm.py` | 1.7 | complete |
| `coordinator_core/hooks/tests/test_guard_em_prefire_survey_through_hook_run.py` | 1.7 | complete |
| `coordinator_core/install/tests/test_door_install.py` | 1.7 | complete |
| `coordinator_core/ops/docgen/tests/test_doc_render.py` | 1.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_compose_reacher_legs_per_arm.py` | 1.6 | complete |
| `coordinator/tests/test_cold_path_remediation_is_runnable.py` | 1.6 | complete |
| `coordinator_core/install/test_dep_check.py` | 1.6 | complete |
| `coordinator_core/roadmap/tests/test_plan_gate.py` | 1.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_halt_on_unstarted_dependents.py` | 1.6 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_summary_gated_open.py` | 1.6 | complete |
| `coordinator_core/housekeeping/tests/test_corpus_fixture.py` | 1.6 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_test_runner_verify_row_releases_dependents.py` | 1.5 | complete |
| `coordinator_core/plugin_health/tests/test_forwarder_drift.py` | 1.5 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_sizing_refire_composes_no_size_stage.py` | 1.5 | complete |
| `coordinator/tests/test_lesson_promote_legacy_collision.py` | 1.5 | complete |
| `coordinator_core/ops/emit/tests/test_deep_spawn_worklist_signal.py` | 1.5 | complete |
| `coordinator/bin/tests/test_percolate_exclusion_vocabulary_drift.py` | 1.5 | complete |
| `coordinator/bin/tests/test_agent_helper_shim_doe_corpus_coverage.py` | 1.5 | complete |
| `coordinator_core/cartography/tests/test_edges.py` | 1.5 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_launcher_child_m_plus_chains_incident.py` | 1.5 | complete |
| `coordinator_core/ops/fleet/tests/test_memo_resolver.py` | 1.5 | complete |
| `coordinator_core/agent_pipe/tests/test_codex_app_server.py` | 1.5 | complete |
| `coordinator/tests/test_chunk6_onboarding_setup_doctor.py` | 1.5 | complete |
| `coordinator_core/ops/session/tests/test_guard_foreign_platform_paths.py` | 1.5 | complete |
| `coordinator_core/percolate/tests/test_engine_permit_path.py` | 1.5 | complete |
| `coordinator_core/distill/tests/test_log_normalize.py` | 1.5 | complete |
| `coordinator_core/ops/tests/test_foreign_symbols.py` | 1.5 | complete |
| `coordinator_core/install/test_install_one_overwrite_policy.py` | 1.5 | complete |
| `scripts/test_cloud_setup.py` | 1.5 | complete |
| `coordinator_core/session/tests/test_receiver_state.py` | 1.5 | complete |
| `coordinator_core/ops/test_verify_schema_registry_sync.py` | 1.5 | complete |
| `coordinator_core/group_em/tests/test_suite_grant.py` | 1.5 | complete |
| `coordinator_core/hooks/tests/test_submit_engine_ask_chain_run_id_from_path.py` | 1.4 | complete |
| `coordinator/bin/tests/test_session_spend_report_engine_root.py` | 1.4 | complete |
| `coordinator_core/hooks/tests/test_warm_hook_migration_smoke.py` | 1.4 | complete |
| `coordinator_core/hooks/tests/test_preuse_search_dispatch.py` | 1.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_plain_resume_refire_stages_current_rows.py` | 1.4 | complete |
| `coordinator/tests/test_disk_budget_interface.py` | 1.4 | complete |
| `coordinator_core/ops/tests/test_review_reverify_foreign_path.py` | 1.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_gate_records_ruling.py` | 1.4 | complete |
| `coordinator_core/tests/test_warm_identity_env_reads.py` | 1.4 | complete |
| `coordinator_core/install/tests/test_door_image_needs_an_engine_entrypoint.py` | 1.4 | complete |
| `coordinator_core/ops/test_warp_run_deliverable_halted_claims.py` | 1.4 | complete |
| `coordinator_core/percolate/tests/test_surface.py` | 1.4 | complete |
| `coordinator_core/usage/tests/test_price_table.py` | 1.4 | complete |
| `coordinator_core/write_guards/tests/test_validate_frontmatter_schema_handoff_kind_advisory_and_coercion.py` | 1.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_unmarked_ask_source_derivation.py` | 1.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_run_close_baton_ship.py` | 1.4 | complete |
| `coordinator_core/write_guards/tests/test_block_consumed_handoff_edit.py` | 1.4 | complete |
| `coordinator_core/group_em/tests/test_nomination.py` | 1.4 | complete |
| `coordinator/bin/tests/test_install_doc_payload_gate_wiring.py` | 1.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_emit_dag.py` | 1.4 | complete |
| `coordinator/tests/test_percolate_ensure_required_targets.py` | 1.4 | complete |
| `coordinator_core/hooks/tests/test_postuse_context_pressure.py` | 1.4 | complete |
| `coordinator_core/hooks/tests/test_submit_engine_ask_one_door.py` | 1.4 | complete |
| `coordinator/tests/test_arrival_triage_plan_candidates.py` | 1.4 | complete |
| `coordinator_core/benchmarks/tests/test_engine_stall_repro.py` | 1.4 | complete |
| `coordinator_core/write_guards/tests/test_nudge_private_git_fact_resolver.py` | 1.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_sizing_xs_mint.py` | 1.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_resume_repair_delta_carries_review_verdicts.py` | 1.4 | complete |
| `coordinator_core/ops/test_scratchpad_sweep.py` | 1.4 | complete |
| `coordinator_core/tests/test_no_detached_process_spawn.py` | 1.4 | complete |
| `coordinator_core/roadmap/tests/test_number_stubs.py` | 1.4 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_cli_research_route.py` | 1.4 | complete |
| `coordinator_core/plugin_health/tests/test_fleet_reachability.py` | 1.4 | complete |
| `coordinator/bin/tests/test_session_claim_cli.py` | 1.4 | complete |
| `coordinator_core/ops/tests/test_cutover_gate_derivation.py` | 1.3 | complete |
| `coordinator_core/baton_assemble/tests/test_dirty_tree_attribution_injected_status.py` | 1.3 | complete |
| `coordinator_core/warm/tests/test_late_result_reaches_ack_store.py` | 1.3 | complete |
| `coordinator_core/ops/tests/test_deferral_detect_orphan_memo.py` | 1.3 | complete |
| `coordinator/bin/test_check_doctrine_citations.py` | 1.3 | complete |
| `coordinator_core/workday_start/tests/test_day_envelope.py` | 1.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_roadmap_fanout_e2e.py` | 1.3 | complete |
| `coordinator/tests/test_arrival_goal_assessment_staleness.py` | 1.3 | complete |
| `coordinator_core/hooks/tests/test_submit_engine_ask_minted_stage_pending.py` | 1.3 | complete |
| `coordinator_core/ops/tests/test_doctor_boot_banners.py` | 1.3 | complete |
| `coordinator_core/frontmatter/tests/test_body_blocks.py` | 1.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ledger_verdict_closes_halted_run.py` | 1.3 | complete |
| `coordinator_core/percolate/tests/test_output_functional_identifier_drift.py` | 1.3 | complete |
| `coordinator/tests/test_arrival_mise_prep_run.py` | 1.3 | complete |
| `coordinator_core/ops/fleet/tests/test_scratch_hygiene_plan.py` | 1.3 | complete |
| `coordinator_core/group_em/tests/test_read_pass.py` | 1.3 | complete |
| `coordinator_core/tests/test_register_rows.py` | 1.3 | complete |
| `coordinator_core/group_em/tests/test_send_pass.py` | 1.3 | complete |
| `coordinator_core/ops/tests/test_agent_pipe_op.py` | 1.3 | complete |
| `coordinator_core/hooks/tests/test_subagent_review_mark.py` | 1.3 | complete |
| `coordinator_core/test_env_locality.py` | 1.3 | complete |
| `coordinator_core/test_state_root.py` | 1.3 | complete |
| `coordinator_core/percolate/tests/test_directory_rename.py` | 1.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_terminal_commit_params_channel.py` | 1.3 | complete |
| `coordinator_core/warm/tests/test_warm_telemetry.py` | 1.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_revise_accept_partial_releases_dependents.py` | 1.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_shared_path_landing.py` | 1.3 | complete |
| `coordinator_core/contract/tests/test_decisions_readers_normalize.py` | 1.3 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_research_emit_scout_targets.py` | 1.3 | complete |
| `coordinator_core/testing/test_tier_t_slots.py` | 1.3 | complete |
| `coordinator_core/ops/test_detect_hardware.py` | 1.3 | complete |
| `coordinator_core/ops/test_ensure_python3_exe_shim.py` | 1.2 | complete |
| `coordinator_core/hooks/support/tests/test_support_runners.py` | 1.2 | complete |
| `coordinator/lib/percolate/tests/test_publish_sync_only_paths.py` | 1.2 | complete |
| `coordinator_core/ops/fleet/tests/test_receiver_landing.py` | 1.2 | complete |
| `coordinator/bin/tests/test_checked_repo_resolver.py` | 1.2 | complete |
| `coordinator_core/ops/dispatch_emit/tests/test_ask_gate_resume_snapshot.py` | 1.2 | complete |
| `coordinator/tests/test_path_equality_conformance.py` | 1.2 | complete |
| `coordinator_core/merge_assemble/tests/test_apply_op_dispatch.py` | 1.2 | complete |
| `coordinator/tests/test_publish_main_batch_dispatch.py` | 1.2 | complete |
| `coordinator_core/tests/test_warp_live.py` | 1.2 | complete |
| `coordinator_core/session/tests/test_guard_unlock_sentinel.py` | 1.2 | complete |
| `coordinator_core/ops/tests/test_scratch_delete.py` | 1.2 | complete |
| `coordinator_core/ops/tests/test_app_session.py` | 1.2 | complete |
| `coordinator_core/ops/test_setup_fnm_pin.py` | 1.2 | complete |
| `coordinator/tests/test_coordinator_currency.py` | 1.2 | complete |
| `coordinator/bin/tests/test_publish_repo_key_literal_gate.py` | 1.1 | complete |
| `coordinator/bin/tests/test_identity_file_populated_gate.py` | 1.1 | complete |
| `coordinator/bin/tests/test_unimatrix_pairing_e2e.py` | 1.1 | complete |
| `coordinator/bin/tests/test_unimatrix_peer_agy.py` | 1.1 | complete |
| `coordinator/bin/tests/test_box_memory_snapshot_admit_atomic.py` | 1.1 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_roadmap_baton_sizing_gate.py` | 1.1 | complete |
| `coordinator/bin/tests/test_denied_package_importers_are_locator_axis.py` | 1.1 | complete |
| `coordinator/tests/test_arrival_land_wave.py` | 1.0 | complete |
| `coordinator/bin/tests/test_box_memory_snapshot.py` | 1.0 | complete |
| `coordinator/bin/tests/test_unimatrix_relay_grant_mode.py` | 1.0 | complete |
| `coordinator/bin/tests/test_unimatrix_agy_hook.py` | 0.9 | complete |
| `coordinator/bin/tests/test_no_client_timeout_dial_raises.py` | 0.9 | complete |
| `coordinator/bin/tests/test_emit_wave_fire_chain_flag.py` | 0.9 | complete |
| `coordinator/tests/test_arrival_recycle_check.py` | 0.9 | complete |
| `coordinator/bin/tests/test_plan_spine_check_gate_created_by_other_row.py` | 0.8 | complete |
| `coordinator/tests/test_queue_corpus_parses.py` | 0.8 | complete |
| `coordinator/tests/test_run_report_retirement_grep_clean.py` | 0.8 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_shard_path.py` | 0.8 | complete |
| `coordinator/tests/test_publish_doc_path_refs_gate.py` | 0.8 | complete |
| `coordinator/tests/test_detect_initiative_candidates_unattached.py` | 0.8 | complete |
| `coordinator/bin/tests/test_run_outcome_audit.py` | 0.8 | complete |
| `coordinator/bin/tests/test_cc_invoke_mutating_caller_gate.py` | 0.8 | complete |
| `coordinator/tests/test_percolate_store_basename_rename_coverage.py` | 0.8 | complete |
| `scripts/tests/test_setup_rag_fleet_registration.py` | 0.7 | complete |
| `scripts/tests/test_setup_engine_floor_decide.py` | 0.7 | complete |
| `coordinator/tests/test_percolate_deferred.py` | 0.7 | complete |
| `coordinator/bin/tests/test_scoped_pytest_launcher.py` | 0.7 | complete |
| `coordinator/bin/tests/test_unimatrix_payload_paths_resolve.py` | 0.7 | complete |
| `coordinator/tests/test_verify_publish_targets_portable_sync.py` | 0.6 | complete |
| `coordinator/tests/test_arrival_generate_doctrine_surface_split.py` | 0.6 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_new_chain_suppresses_ambient_carry.py` | 0.6 | complete |
| `coordinator/tests/test_dr084_single_accessor_guard.py` | 0.6 | complete |
| `coordinator/bin/tests/test_doc_new_memo_to_slug_lowercased.py` | 0.6 | complete |
| `coordinator/bin/tests/test_publish_paths_carry_no_codename_token.py` | 0.6 | complete |
| `coordinator/bin/tests/test_function_gate_parse_sweep.py` | 0.6 | complete |
| `coordinator/bin/tests/test_content_root_routing.py` | 0.6 | complete |
| `coordinator/lib/tests/test_lib_disk_budget_interface.py` | 0.6 | complete |
| `coordinator/tests/test_publish_allowlist_check.py` | 0.6 | complete |
| `coordinator/bin/tests/test_publish_preswap_payload_gate.py` | 0.5 | complete |
| `coordinator/bin/test_coordinator_queue_append_yaml_gate.py` | 0.5 | complete |
| `coordinator/bin/tests/test_percolate_round_open_only.py` | 0.5 | complete |
| `coordinator/tests/test_arrival_check_settings_env.py` | 0.5 | complete |
| `coordinator/bin/tests/test_publish_fold_bar_refusal.py` | 0.5 | complete |
| `coordinator/tests/test_publish_dry_refire_gate_refuses_push.py` | 0.5 | complete |
| `coordinator/bin/tests/test_survey_consume_gate_rag_predicate.py` | 0.5 | complete |
| `scripts/dispatch_emit_split/test_measure.py` | 0.5 | complete |
| `scripts/tests/test_cloud_setup_path_resilience.py` | 0.4 | complete |
| `scripts/tests/test_cloud_setup_repo_bundle.py` | 0.4 | complete |
| `bin/tests/test_claude_klabauter_doctor_c14_probes.py` | 0.4 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_decision_id_mint.py` | 0.4 | complete |
| `coordinator/tests/test_percolate_phase4_audit.py` | 0.4 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_session_ledger_block.py` | 0.4 | complete |
| `coordinator/tests/test_arrival_subagent_statusline.py` | 0.4 | complete |
| `coordinator/tests/test_arrival_red_set_report.py` | 0.4 | complete |
| `coordinator/bin/tests/test_unimatrix_agy_plugin_gen.py` | 0.4 | complete |
| `coordinator/bin/tests/test_unimatrix_scripts.py` | 0.4 | complete |
| `coordinator/tests/test_arrival_baton_chain_closure.py` | 0.4 | complete |
| `coordinator/tests/test_mise_certify_and_falsifier_record.py` | 0.4 | complete |
| `coordinator/bin/tests/test_claude_author_control_transport_posix.py` | 0.4 | complete |
| `coordinator/tests/test_percolate_ignore_coordinator_core.py` | 0.4 | complete |
| `bin/tests/test_claude_klabauter_doctor_publish_mirror_clean_probe.py` | 0.4 | complete |
| `coordinator/tests/test_publish_allowlist_is_not_drifted.py` | 0.4 | complete |
| `coordinator/bin/tests/test_validator_negative_corpus.py` | 0.4 | complete |
| `coordinator/tests/test_arrival_check_projectragignore_fleet_drift.py` | 0.3 | complete |
| `coordinator/tests/test_workweek_close_week_only_header.py` | 0.3 | complete |
| `coordinator/bin/tests/test_claude_author_control_relay_posix.py` | 0.3 | complete |
| `coordinator/bin/tests/test_machine_local_ported_verbs.py` | 0.3 | complete |
| `coordinator/tests/test_handoff_corpus_conformance.py` | 0.3 | complete |
| `coordinator/bin/tests/test_percolate_round_lock_denies_fast.py` | 0.3 | complete |
| `coordinator/bin/tests/test_fleet_test_write_scan.py` | 0.3 | complete |
| `coordinator/tests/test_arrival_instrument_can_report_red.py` | 0.3 | complete |
| `coordinator/bin/tests/test_safe_commit_refusal_names_a_reachable_holder.py` | 0.3 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_goal_roadmap_seed_pickup_ready.py` | 0.3 | complete |
| `coordinator/bin/tests/test_refresh_plugin_child_stdio.py` | 0.3 | complete |
| `coordinator/tests/test_doctor_hook_generation_currency.py` | 0.3 | complete |
| `coordinator/bin/tests/test_mirror_native_inject_fidelity.py` | 0.3 | complete |
| `coordinator/bin/tests/test_emit_wave_fire_plan_review.py` | 0.3 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_goals_scope_type_scoping.py` | 0.3 | complete |
| `bin/tests/test_claude_klabauter_doctor_new_probes.py` | 0.3 | complete |
| `coordinator/bin/tests/test_publish_allowlist_sibling_shellouts.py` | 0.3 | complete |
| `coordinator/bin/tests/test_census_recheck.py` | 0.3 | complete |
| `scripts/tests/test_measure_setup_steps.py` | 0.3 | complete |
| `coordinator/tests/test_arrival_survey_consume_gate.py` | 0.3 | complete |
| `coordinator/bin/tests/test_coordinator_publish_runs_clone_driver.py` | 0.3 | complete |
| `coordinator/tests/test_disk_budget_projects.py` | 0.3 | complete |
| `coordinator/bin/tests/test_claude_author_control_supervisor.py` | 0.3 | complete |
| `coordinator/bin/tests/test_percolate_mirror.py` | 0.3 | complete |
| `coordinator/tests/test_arrival_generate_doctrine_surfaces.py` | 0.3 | complete |
| `coordinator/bin/tests/test_bootstrap_pinned_lib_missing_names.py` | 0.3 | complete |
| `coordinator/tests/test_arrival_measure_hook_class_cost.py` | 0.2 | complete |
| `coordinator/tests/test_publish_fleet_only_strip.py` | 0.2 | complete |
| `coordinator/bin/tests/test_coordinator_safe_commit_declared_reverts.py` | 0.2 | complete |
| `coordinator/tests/test_arrival_tier_last_run.py` | 0.2 | complete |
| `coordinator/tests/test_arrival_goal_kr_evidence.py` | 0.2 | complete |
| `coordinator/bin/tests/test_publish_commit_subject_names_source_sha.py` | 0.2 | complete |
| `coordinator/bin/tests/test_plan_spine_check_prep_gate_parity.py` | 0.2 | complete |
| `coordinator/bin/tests/test_unimatrix_liveness.py` | 0.2 | complete |
| `coordinator/lib/tests/test_lib_disk_budget_projects.py` | 0.2 | complete |
| `coordinator/tests/test_arrival_aggregate_rollup.py` | 0.2 | complete |
| `coordinator/bin/tests/test_plan_spine_check_for_execution.py` | 0.2 | complete |
| `coordinator/tests/test_handoff_lifecycle_accessor.py` | 0.2 | complete |
| `coordinator/tests/test_emit_dispatch_door_routes_plan.py` | 0.2 | complete |
| `coordinator/bin/tests/test_identity_checker_source_pinned.py` | 0.2 | complete |
| `coordinator/tests/test_arrival_export_catering_resolution.py` | 0.2 | complete |
| `coordinator/tests/test_arrival_validate_code_comparison_record_checker.py` | 0.2 | complete |
| `coordinator/tests/test_percolate_ignore.py` | 0.2 | complete |
| `scripts/tests/test_setup_install_contract.py` | 0.2 | complete |
| `coordinator/tests/test_arrival_host_gpu_probe.py` | 0.2 | complete |
| `coordinator/tests/test_disk_budget_cache.py` | 0.2 | complete |
| `bin/tests/test_claude_klabauter_doctor_schema_drift_probe.py` | 0.2 | complete |
| `coordinator/bin/tests/test_foreign_dir_names_index.py` | 0.2 | complete |
| `coordinator/bin/tests/test_setup_verify.py` | 0.2 | complete |
| `coordinator/lib/percolate/tests/test_publish_sync_orphan_sweep_per_directory_override.py` | 0.2 | complete |
| `coordinator/bin/lib/test_fleet_heal_persists_across_orient_check.py` | 0.2 | complete |
| `coordinator/tests/test_arrival_mise_prep_entry.py` | 0.2 | complete |
| `coordinator/bin/tests/test_publish_batch_delta_honest_reporting.py` | 0.2 | complete |
| `coordinator/bin/tests/test_percolate_round_publish_slot.py` | 0.2 | complete |
| `coordinator/bin/tests/test_bin_lazy_engine_imports_resolve.py` | 0.2 | complete |
| `coordinator/tests/test_bin_entrypoints_are_executable.py` | 0.2 | complete |
| `coordinator/bin/tests/test_box_memory_host.py` | 0.2 | complete |
| `coordinator/bin/tests/test_pm_brief.py` | 0.2 | complete |
| `coordinator/bin/tests/test_precommit_trampoline_engine_axis.py` | 0.2 | complete |
| `coordinator/tests/test_arrival_mise_prep_run_residue.py` | 0.2 | complete |
| `coordinator/lib/tests/test_lib_disk_budget_cache.py` | 0.2 | complete |
| `coordinator/tests/test_git_hook_install_resolve_coord_bin.py` | 0.2 | complete |
| `coordinator/bin/tests/test_publish_repo_bundle_at_ceremony.py` | 0.2 | complete |
| `coordinator/bin/tests/test_file_cache_trim.py` | 0.2 | complete |
| `coordinator/tests/test_baton_chain_closure_lineage_scan.py` | 0.2 | complete |
| `coordinator/bin/tests/test_deferral_prose_audit.py` | 0.2 | complete |
| `coordinator/bin/tests/test_plan_spine_check_test_callers.py` | 0.2 | complete |
| `coordinator/bin/tests/test_publish_gate_predicate_symmetry.py` | 0.2 | complete |
| `coordinator/lib/claude-home/tests/test_claude_home.py` | 0.2 | complete |
| `coordinator/tests/test_arrival_validate_code_comparison_record.py` | 0.2 | complete |
| `coordinator/tests/test_readme_mirror_parity.py` | 0.2 | complete |
| `coordinator/bin/tests/test_publish_cold_removals_this_source_shipped.py` | 0.2 | complete |
| `coordinator/bin/tests/test_emit_wave_fire_overseer_flag.py` | 0.2 | complete |
| `coordinator/bin/tests/test_unimatrix_doctrine_seed.py` | 0.2 | complete |
| `coordinator/bin/tests/test_lesson_add.py` | 0.1 | complete |
| `coordinator/bin/tests/test_workweek_complete_close.py` | 0.1 | complete |
| `scripts/tests/test_cloud_setup_cloud_preboot.py` | 0.1 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_plural_carry.py` | 0.1 | complete |
| `coordinator/bin/tests/test_wait_for_count.py` | 0.1 | complete |
| `coordinator/tests/test_publish_row_content_strip_declared.py` | 0.1 | complete |
| `coordinator/bin/tests/test_cc_invoke_timeout_remedy.py` | 0.1 | complete |
| `coordinator/bin/tests/test_publish_writes_expected_manifest.py` | 0.1 | complete |
| `coordinator/bin/tests/test_unimatrix_peer_contract.py` | 0.1 | complete |
| `coordinator/tests/test_session_spend.py` | 0.1 | complete |
| `coordinator/bin/tests/test_emit_wave_fire_deliverable_id.py` | 0.1 | complete |
| `coordinator/bin/tests/test_handoff_terminal_vocabulary_consumers.py` | 0.1 | complete |
| `coordinator/tests/test_arrival_check_provisional_expiry.py` | 0.1 | complete |
| `coordinator/bin/tests/test_group_em_memory_governor_wait.py` | 0.1 | complete |
| `coordinator/bin/tests/test_merge_gate_and_pr.py` | 0.1 | complete |
| `coordinator/bin/tests/test_workday_complete_step1_validate.py` | 0.1 | complete |
| `coordinator/lib/percolate/tests/test_removal_live_source_refusal.py` | 0.1 | complete |
| `coordinator/bin/tests/test_workday_complete_step1_validate_test_red.py` | 0.1 | complete |
| `coordinator/tests/test_setup_non_tty_is_non_interactive.py` | 0.1 | complete |
| `coordinator/bin/tests/test_frontmatter_scan.py` | 0.1 | complete |
| `coordinator/bin/tests/test_handoff_archive_transition.py` | 0.1 | complete |
| `coordinator/tests/test_queue_triage_cli.py` | 0.1 | complete |
| `coordinator/bin/tests/test_archive_stamp_cli_prose_file_siblings.py` | 0.1 | complete |
| `coordinator/bin/tests/test_publish_identity_check_staged_deadlock.py` | 0.1 | complete |
| `bin/tests/test_claude_klabauter_doctor_warm_roundtrip_probe.py` | 0.1 | complete |
| `coordinator/bin/tests/test_coordinator_resolve_validation_cmd.py` | 0.1 | complete |
| `coordinator/bin/tests/test_lesson_promote.py` | 0.1 | complete |
| `coordinator/bin/tests/test_percolate_codename_redaction_guard.py` | 0.1 | complete |
| `coordinator/tests/install/test_bin_templates_manifest_sync.py` | 0.1 | complete |
| `coordinator/bin/tests/test_publish_stamp_names_the_copied_tree.py` | 0.1 | complete |
| `coordinator/bin/tests/test_publish_parity_group_ordering.py` | 0.1 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_placeholder_id_mint.py` | 0.1 | complete |
| `coordinator/bin/tests/test_scip_rebuild_at_ceremony.py` | 0.1 | complete |
| `coordinator/bin/tests/test_coordinator_safe_commit_lock_contention_retry.py` | 0.1 | complete |
| `coordinator/bin/tests/test_full_payload_proof_source_pin.py` | 0.1 | complete |
| `coordinator/bin/tests/test_emit_wave_fire_from_sizing_golden.py` | 0.1 | complete |
| `coordinator/bin/test_query_records.py` | 0.1 | complete |
| `coordinator/bin/tests/test_unimatrix_far_from_and_label.py` | 0.1 | complete |
| `coordinator/bin/tests/test_unscanned_published_gate.py` | 0.1 | complete |
| `coordinator/tests/test_step9_pre_staged_ownership_filter.py` | 0.1 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_spinoff_ordering_edge.py` | 0.1 | complete |
| `coordinator/bin/tests/test_structural_index_refresh.py` | 0.1 | complete |
| `coordinator/bin/tests/test_coordinator_safe_commit_pathspec_batch.py` | 0.1 | complete |
| `coordinator/tests/test_arrival_corpus_currency_probe.py` | 0.1 | complete |
| `coordinator/bin/tests/test_break_glass.py` | 0.1 | complete |
| `coordinator/bin/tests/test_claude_author_control_protocol.py` | 0.1 | complete |
| `coordinator/bin/tests/test_coordinator_doc_new_spinoff_resolvable_fields.py` | 0.1 | complete |

## Run the whole tier at most once

A full-tier run carries a standing set of pre-existing failures unrelated to any given change, so
a re-run to re-confirm a baseline buys no new information. Targeted suites plus one consumer-side
suite are sufficient evidence to commit. When failure attribution is genuinely in doubt, stash the
changed files and re-run only the failing set. Derive current runtime and suite size with
`pytest --collect-only` rather than trusting a figure written down here.

## Worker count is a derivation, not a constant

Parallelism is load-bearing, not an optimization: serial cannot complete the fast-tier gate in
reasonable time. **But bare `-n auto` is unbounded and has killed a session.**

`-n auto` takes the host's core count with no ceiling — **physical only when psutil is importable**
(pytest-xdist 3.8.0 `xdist/plugin.py::auto_detect_cpus`; logical otherwise). Admit workers against
what the box has free at the moment of the call, not against a core fraction:

```
max(1, min(available_RAM_GB * 1024 / 150MB, logical_cpus * (1 - cpu_load)))
```

`available_RAM_GB` is *available* memory, never total; `cpu_load` is the current busy fraction in
[0, 1]. Both come from `host_sampler.read_load` (`coordinator_core/telemetry/host_sampler.py`),
which needs no psutil: Windows reads `GlobalMemoryStatusEx` and a `GetSystemTimes` delta, POSIX
reads `MemAvailable` and the load average over logical CPUs. A busier or fuller box admits fewer
workers; a quiet one admits more.

**That ceiling resolves per box, at read time.** `cs_resolve_fast_test_cmd`
(`coordinator_core/resolve_validation_cmd.py`) hands every `-n auto`/`-n logical` command to
`derive_worker_cap.cap_command_for_this_box`, which sets `--maxprocesses` from
`concurrency_probe.compute_measured_cap` against the host that is about to run it. `python -m
coordinator_core.install.derive_worker_cap` prints the figures and the cap; do not hand-edit the
number.

The reason it resolves at read time rather than being written down: **`coordinator.local.md` is
tracked, not per-box.** `git ls-files` finds it and `.gitignore` does not, so its `fast_test_cmd` is
one committed constant every clone shares, and no single constant is safe across a fleet whose
free memory and load differ by box and by minute. The committed `--maxprocesses` survives as an
inert fallback for anything reading the frontmatter raw, and for a host whose memory or load cannot
be read (`read_measured_headroom` raises; the resolver keeps the committed command and names why on
stderr).

A cap is a point-in-time admission, not a reservation: a box already near its memory or CPU limit
derives a small number (never below 1), and the same box derives a larger one once peers finish.

**Recompute it for your box rather than inheriting any cached figure, and if the tier is
unstable, lower the cap before reaching for serial.**

## Known-red set — derive, do not trust prose

The bash-guards known-red set regrows whenever an unmarked test starts failing and nobody
classifies it, so any count written in prose here goes stale immediately. Get the live census
with `python coordinator/bin/red-set-report.py coordinator_core/bash_guards/tests/` (serial or
capped workers, never bare `-n auto`), and read `state/bash-guards/known-red.json` for the
currently classified set (owner, reason, `review_by` per entry).

A regrowth ratchet gates the tree (`coordinator_core/tests/test_known_red_ratchet.py`, hosted
outside `coordinator_core/bash_guards/tests/` because it shells out to a pass that executes the
measured tree, so hosting it inside would recurse without bound): the observed unmarked-red
node-id set must be a subset of the registry (catches regrowth and fix-one-break-one swaps by
naming the offending node id), every marker in the tree must have a registry entry with
non-empty owner/reason (catches mark-to-satisfy), and no registry entry may be past its
`review_by` (catches decay into permanence). Regenerate the registry via
`coordinator/bin/regenerate-known-red-registry.py`, wired to `red-set-report.py` — never
hand-edit the JSON file's node-id set directly.

**Marker-DECORATOR counts are not the same quantity as marked-NODE-ID counts, and conflating them
is a standing failure mode.** One decorator on a parametrised test expands to many node ids — a
single `cadence` use can cover dozens of cells, a single `pending_fix` use can cover one. Derive
both counts separately with `pytest --collect-only -m "cadence or pending_fix or designed_red"`
rather than trusting either figure from prose. The split between marked and unmarked-red node ids
is derived from two `--collect-only` passes (`marked = collected_all - collected_fast`), which
execute nothing and so cannot be perturbed by a concurrently-committing peer — never inferred by
diffing the failure sets of two separately-executed runs, which is vulnerable to attributing a
transient failure to marker exclusion and under-firing the ratchet's subset gate.

**Derive these numbers, or read them from source at the moment you need them — including when you
are certain an existing number is wrong.** A confident correction of a number is exactly how this
document's figures go stale.

## A tier-U guard can reject the repo's own configured fast-tier command

`check_test_suite_invocation.py` has rejected the repo's own configured `fast_test_cmd`, run
verbatim, as unauthorized Tier-U. Root cause: a marker-filter deselect expression was read as
unscoped by `_tier_for_cfg_match`, discharged via `coordinator.local.md`'s R6
`fast_tier_unscoped_reason`. The wrapped-invocation repro for this was lost/untested at close —
treat that gap as open if you're relying on the wrapped form, not just the bare one.

## Who runs a full or broad suite

Only the EM. A dispatched subagent never does, nor any subagent of a cloud EM: a dispatch or a
plan step is not assent, whatever it says. A session the PM addresses directly as EM is the EM,
whatever brief started it. On a local box the EM runs it under a live `tier-u-grant-cli` grant,
because peers share the box. On a cloud box no one runs it: `check_test_suite_invocation` denies a
Tier-U or Tier-F run for every caller there (`_is_cloud_box`, the harness rung read off the
caller's env), and `docs/claude-md/environment.cloud.md` states the same. This supersedes the
2026-09-30 carve-out that let a cloud EM run it with no grant (a 4-core cloud suite run hit the
7200s cap at 88%). The venue is visible only where the caller's env reaches the guard: the cold
path and the warm door carry `CLAUDE_CODE_REMOTE`; the http hook leg does not, so there the grant
leg still decides. See
`state/improvement-queue/2026-09-30-tier-u-guard-has-no-cloud-em-carve-out.yaml`.

## A green suite re-run is not new evidence

Re-running a broad test suite is not new evidence on a shared branch. Decide what a targeted
scoped run must prove before spending it, and never re-run the full tier just to "confirm" a
pass — a full-tier re-run buys no new information over the prior run.

## Brief dispatched agents with file/node-id pytest targets, never directory arguments

The DR-088 R9 guard refuses directory arguments outright. Briefing a dispatched agent with a
directory-scoped pytest invocation costs a truncated verification and an EM round trip when the
guard denies it — brief with explicit file or node-id targets instead.
## The interpreter targeted tests run under — `targeted_test_python`

Engine-composed scoped-test and ledger-check prompts name the target repo's pinned interpreter
instead of bare `python3`. `coordinator_core/ops/dispatch_emit/interpreter_pin.py ::
resolve_targeted_test_python` resolves it, first hit wins:

1. `targeted_test_python:` in the target repo's `coordinator.local.md` frontmatter.
2. The leading token of `fast_test_cmd:`, only when it is an explicit interpreter: an absolute path
   or a versioned name such as `python3.14`. Bare `python`/`python3` is not a pin.
3. None: the prompt keeps the portable text (`python3`, falling back to `python`).

Accepted value forms: an absolute path (`/usr/local/bin/python3.14`) or a bare name resolved on
`PATH`. Resolution is in-process (`os.access`, `shutil.which`); no process is spawned.

A declared pin that does not resolve on the host is an `interpreter-resolution-defect:`, not a test
failure: the agent runs nothing and reports `status: error`, `tests_run: 0`. A verdict with
`tests_run: 0` is recorded as `not-run` and withholds no file as `targeted-tests-failed`.

## Always-run guards — `always_run_guards`

`always_run_guards:` in `coordinator.local.md` frontmatter is an inline list of pytest node ids
(`always_run_guards: ["tests/test_guard.py::test_no_literal", "tests/test_other.py"]`). Every
landing selects each declared guard, whatever files it touches. A guard already red at HEAD is
judged by new hits in the touched files (tree count above the HEAD-export count), not by its
verdict: a new hit is refused, a baseline-only red passes. A hand-land calls
`landing.guard_selection` (`{repo_root, paths}`) and runs the returned `retest_command`.

## Perf ratchets measure process time, not wall clock

An upper-bound assertion on an elapsed-time value is process time by default; wall clock is a
declared exception, never a fallback. `deliberate_wall_clock` is the fourth marker (registered
alongside `cadence`/`pending_fix`/`designed_red` in `pyproject.toml`'s `markers` list) and applies
at function, class, or module level with a non-empty `reason=` naming the behaviour CPU time
cannot see — a bare marker does not discharge.

Discriminator, in three lines:
- In-process, no subprocess → time it with `coordinator_core/benchmarks/process_time.py`'s
  process-time primitive.
- Spawns a subprocess → count spawns and/or sum their process time through the same module.
- Neither is possible (the thing under test genuinely is wall-bound, e.g. a lock's real-world
  wait) → `pytest.mark.deliberate_wall_clock(reason=...)`.

`coordinator_core/tests/test_no_wall_clock_ratchets.py` is the enforcing guard: an unmarked
upper-bound assertion on a tainted wall-clock duration anywhere under `testpaths` fails, naming
the file/function and the two routes above. See `CLAUDE.md` § The brightline and
`docs/wiki/machine-load-norm.md` for why wall clock is the wrong axis on this box.
