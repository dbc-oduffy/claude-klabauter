"""
coordinator_core.tests.test_fleet_mode_reader_set — the floor: a positive
allowlist of every module whose TRANSITIVE IMPORT CLOSURE reaches the fleet
record (``coordinator_core.session.fleet_mode`` / ``.mode_resolution``),
red in both directions.

WHY AN ALLOWLIST, NOT A PROHIBITION. This plan's exit criterion ("no value
in that file can change how a denial-shaped guard behaves") holds only
while the reader set stays closed: an absent reader is a real property
here because a mode key has exactly the consumers this plan wired and no
others. The moment a consumer outside that set reads the file, the
absence stops being an absence and the floor is gone with no other signal.
The allowlist below is the assertion of that precondition — a prohibition
("no guard may read the fleet file") asserts something strictly weaker and
cannot detect the precondition failing, because it says nothing about a
non-guard module quietly becoming a reader. See
``state/lessons/2026-08-28-a-guard-surface-is-not-a-boundary.md`` and
``guard_unlock_sentinel``'s own standing negative-spec ("must never
generalize into 'all guards off for this session'") — both are about the
same shape of mistake: treating a guard's ABSENCE from a surface as a
boundary the surface itself enforces, when nothing enforces it but this
test.

WHY THE WALK IS TRANSITIVE, NOT DIRECT-IMPORT-ONLY. A module one import hop
away from the record (an indirect reader) is exactly what a direct-import
walk misses, and reachability — not directness — is the property this test
asserts. ``coordinator_core.write_guards.nudge_em_code_dispatch`` is the
concrete case: its own source names neither ``fleet_mode`` nor
``mode_resolution`` (confirmed by probe: importing it places
``coordinator_core.hooks.nudge_em_code_dispatch`` in ``sys.modules``), so a
direct-import-or-basename walk would report it clean while it transitively
wraps the exact hook C3 converts.

WHY ``nudge_em_code_dispatch`` IS ALLOWLISTED RATHER THAN A REASON TO
RESTRUCTURE C3. Its own module docstring declares its CLASS advisory — it
offers a better path via ``additionalContext`` and never denies — so it is
neither an irreversible-harm nor an ask-before-external-action guard, and
restructuring C3 to dodge a shim would distort the hook conversion for a
naming artifact rather than a safety one. This is the one allowlist
addition this chunk is authorized to make; no other module may be added to
EXPECTED_READERS without the same allowlist-vs-restructure analysis
recorded inline (not merely re-run).

"GUARD" IS DEFINED BY CLASS, NOT BY DIRECTORY. The prime exit criterion is
a statement about DENIAL-SHAPED guards, not about every module under
``write_guards/``/``bash_guards/`` — those directories also hold
advisory-class modules, one of which (above) legitimately reaches the
record. ``test_no_denial_shaped_guard_reaches_the_record`` below derives
each candidate's module-level ``CLASS`` STATICALLY (never by importing it)
and fails loud — not "treat as advisory" — when a guard-directory module in
the reader closure has no derivable ``CLASS`` at all: an unclassifiable
module is an unresolved question, matching the guard directories' own
stated convention ("DEFAULT POSTURE ON AMBIGUITY IS DENY",
``coordinator_core/bash_guards/block_disarm_marker_sentinel_creation.py``).

THE RESIDUAL THIS STATIC WALK CANNOT CLOSE. The basename leg of the walk
sees only a LITERAL ``fleet-mode.json`` string in source text — it cannot
see a path handed across a runtime boundary: a module that receives the
fleet-mode.json path as a string from ``fleet_mode_path()``, re-exports it,
or composes it from ``settings_home()`` at runtime, names neither the
module nor the basename statically. This is an accepted residual, not a
claim of exhaustiveness — it is exactly why C1 exposes
``fleet_mode_path()`` as a function rather than a raw string constant, and
why no module outside EXPECTED_READERS may re-export it.

IMPLEMENTATION CONSTRAINTS THAT MAKE "RED IN BOTH DIRECTIONS" REAL RATHER
THAN TAUTOLOGICAL:
    1. ``EXPECTED_READERS`` below is a HARDCODED LITERAL TUPLE, never
       derived from the walk itself — a derived-then-compared set would
       make the assertion pass forever, which is the original vacuity
       wearing the allowlist's clothes.
    2. ``reader_closure()`` is a FUNCTION TAKING A ROOT PATH, never a
       module that hardcodes the repo root — this is the seam that lets
       ``TestReaderClosureSelfVerification`` below run the red-in-both-
       directions property against a FIXTURE tree, never the real one.

HARD CONSTRAINTS THIS MODULE OBSERVES: no commit, no edit to any guard
module (this file only observes them), static analysis only (no import of
any guard module, no execution of any guard).

Spec backlink: state/dispatch-briefs/2026-08-28-the-fleet-gets-one-file-
and-the-floor-moves-to-the-reader/C5.md
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# --- targets ----------------------------------------------------------------

_TARGET_MODULES = (
    "coordinator_core.session.fleet_mode",
    "coordinator_core.session.mode_resolution",
)
_TARGET_BASENAME = "fleet-mode.json"

# The floor. Hardcoded literal tuple -- never derived from the walk below.
#
# Beyond C3's two hooks, C4's op, and the two hooks'/op's/record's own direct
# unit tests (the bucket the brief named), a full static walk of the real
# tree surfaces THREE further clusters -- discovered by this chunk, not
# pre-disposed by the brief, and disposed here by the same allowlist-vs-
# restructure analysis the brief modeled for the one edge it did anticipate
# (`write_guards.nudge_em_code_dispatch`). Restructuring any of them is out
# of this chunk's file scope regardless (writes: is this one test file), so
# allowlist is the only available disposition; each cluster is analyzed on
# its own merits below, not merely because restructuring is unavailable.
#
#   1. PERF/BENCHMARK HARNESSES measuring the two hooks' entry cost
#      (`coordinator_core.benchmarks.*`). These import the hooks to time
#      them, never to gate a tool call -- structurally identical in kind to
#      "the module's own tests" (measurement of the hook, not consumption of
#      a resolved mode value to change runtime behaviour).
#
#   2. GUARD-CORPUS / MESSAGE-REGISTER / FIRING-SHAPE META-TESTS
#      (`coordinator_core.bash_guards._firing_shape` and its dependents
#      under `bash_guards/tests/`, plus `write_guards.tests.
#      test_windows_platform_simulation`, `message_register.tests.
#      test_register`, `tests.test_hooks_roundtrip`). These validate
#      cross-cutting invariants (message format, firing-shape
#      classification, corpus registration completeness) over the FULL
#      guard/hook corpus, and reach the record only because
#      `nudge_em_code_dispatch` -- already allowlisted, advisory, non-
#      denial -- is one specimen among many they iterate. None of them is
#      itself registered as a guard (no CLASS/MATCHERS/PRIORITY); they
#      consume guard modules as static data, the same non-executing
#      relationship a test has to the code it tests.
#
#      `bash_guards._firing_shape` ALSO reaches the record and sits at
#      TOP LEVEL of `bash_guards/` (unlike its `tests/` dependents), which
#      puts it in scope for `test_no_denial_shaped_guard_reaches_the_record`
#      below. It carries no module-level `CLASS` (confirmed by static
#      probe) -- it is not itself a registered guard (no MATCHERS/PRIORITY
#      either), so it cannot execute as a denial-shaped gate, but per this
#      file's own "fail loud, never default to advisory" rule it is
#      correctly flagged there as an unresolved classification question,
#      not silently passed. That failure is intentional and not remediated
#      by this chunk (file scope is this test only) -- see the executor's
#      run report for the disposition this surfaces.
#
# C3's two hooks, C4's op, the four modules' own direct unit tests, and the
# two clusters above, plus the one pre-disposed advisory shim:
EXPECTED_READERS = (
    "coordinator_core.bash_guards._firing_shape",
    "coordinator_core.bash_guards.tests.guard_message_corpus",
    "coordinator_core.bash_guards.tests.guard_message_register_lint",
    "coordinator_core.bash_guards.tests.test_confinement_deny_band_shape",
    "coordinator_core.bash_guards.tests.test_corpus_audience_axis",
    "coordinator_core.bash_guards.tests.test_firing_shape_gate",
    "coordinator_core.bash_guards.tests.test_guard_corpus_registration_invariants",
    "coordinator_core.bash_guards.tests.test_guard_message_corpus",
    "coordinator_core.bash_guards.tests.test_guard_message_register_lint",
    "coordinator_core.bash_guards.tests.test_guard_message_size",
    "coordinator_core.bash_guards.tests.test_no_machine_absolute_path_in_guard_messages",
    "coordinator_core.bash_guards.tests.test_override_route_inventory",
    "coordinator_core.benchmarks.bash_dispatch_probe",
    "coordinator_core.benchmarks.hook_entry_cost",
    "coordinator_core.benchmarks.tests.test_bash_dispatch_process_time_gate",
    "coordinator_core.benchmarks.tests.test_hook_entry_cost",
    "coordinator_core.hooks.nudge_em_code_dispatch",
    "coordinator_core.hooks.postuse_advisory_dispatch",
    # --- 2026-09-06 cluster: the `_resolve_subagent_identity` re-use chain ---
    #
    # Nine modules arrived at once, and every one of them reaches the record
    # through a SINGLE edge measured, not assumed:
    #
    #   hooks.runtime_tripwire_em_check
    #       -> hooks.postuse_advisory_dispatch   (already allowlisted, C3)
    #       -> session.mode_resolution
    #
    # and `hooks.stop_dispatch` reaches it only by importing
    # `runtime_tripwire_em_check._handler`; the seven test modules below reach
    # it only by importing one of those two. So this is ONE edge, counted nine
    # times by module granularity.
    #
    # ALLOWLIST-VS-RESTRUCTURE, done rather than re-run. The edge is
    # `from coordinator_core.hooks.postuse_advisory_dispatch import
    # _resolve_subagent_identity` -- a deliberate re-use, recorded in
    # `runtime_tripwire_em_check`'s own docstring as "imported rather than
    # carrying its own copy". `_resolve_subagent_identity` does NOT call
    # `resolve_mode`: the only `resolve_mode` call in
    # `postuse_advisory_dispatch` is in its compaction-warnings band, a
    # different function entirely. NO MODE VALUE REACHES EITHER HOOK'S
    # DECISION -- the reachability is module-granularity, not data flow.
    #
    # That distinction matters here more than for the clusters above, because
    # `stop_dispatch` IS denial-capable: it aggregates eight legs and returns
    # `deny("Stop", ...)` when any leg blocks. It is not in
    # `test_no_denial_shaped_guard_reaches_the_record`'s scope (that test
    # enumerates top-level `write_guards/`/`bash_guards/` modules), so the
    # analysis is recorded here instead of being left to a check that would
    # not have asked. The plan's exit criterion -- no value in the fleet
    # record can change how a denial-shaped guard behaves -- still holds, on
    # the symbol-level fact above and not on the directory `stop_dispatch`
    # lives in.
    #
    # THE RESTRUCTURE THAT WOULD RETIRE THE WHOLE CLUSTER, named so it is a
    # choice and not an omission: move `_resolve_subagent_identity` into a
    # leaf module that imports neither target, and both hooks plus all seven
    # tests drop out of the closure. Not done here -- it edits two per-turn
    # hot-path hooks, which is a change that needs its own owner, and this
    # file's writes are this test only. If that lands, delete this whole
    # cluster rather than repointing it.
    "coordinator_core.hooks.runtime_tripwire_em_check",
    "coordinator_core.hooks.stop_dispatch",
    "coordinator_core.hooks.test_auto_push",
    "coordinator_core.hooks.test_postuse_advisory_dispatch",
    "coordinator_core.hooks.tests.test_fleet_mode_reaches_the_hooks",
    "coordinator_core.hooks.tests.test_nudge_em_code_dispatch",
    "coordinator_core.hooks.tests.test_post_executor_verification",
    "coordinator_core.hooks.tests.test_postuse_advisory_dispatch",
    "coordinator_core.hooks.tests.test_postuse_context_pressure",
    "coordinator_core.hooks.tests.test_project_orientation_scratch_banner",
    "coordinator_core.hooks.tests.test_runtime_tripwire_em_check",
    "coordinator_core.hooks.tests.test_stop_dispatch",
    "coordinator_core.hooks.tests.test_watchdog_undischarged_next_move",
    "coordinator_core.message_register.tests.test_register",
    "coordinator_core.ops.dispatch_emit.tests.conftest",
    "coordinator_core.ops.dispatch_emit.tests.test_agent_type_host",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_em_brief",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_end_to_end",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_resume",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_size_then_plan",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_ceo_xl_exit",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_seed",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_m_first_stage",
    "coordinator_core.ops.dispatch_emit.tests.test_brief_outranks_relayed_chat",
    "coordinator_core.ops.dispatch_emit.tests.test_checkpoint_commit_and_push",
    "coordinator_core.ops.dispatch_emit.tests.test_checkpoint_rows_are_plan_scoped",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_ask_brief",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_ask_stop_after",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_brightline",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_dag",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_predispatch_gating",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_script_opens_plan_once",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_script_size_cap",
    "coordinator_core.ops.dispatch_emit.tests.test_emitted_script_carries_no_host_path",
    "coordinator_core.ops.dispatch_emit.tests.test_external_gate_shapes",
    "coordinator_core.ops.dispatch_emit.tests.test_gate_baseline",
    "coordinator_core.ops.dispatch_emit.tests.test_grind_compose",
    "coordinator_core.ops.dispatch_emit.tests.test_grind_integration",
    "coordinator_core.ops.dispatch_emit.tests.test_grind_pm_adjudication",
    "coordinator_core.ops.dispatch_emit.tests.test_halt_on_unstarted_dependents",
    "coordinator_core.ops.dispatch_emit.tests.test_memo_send_row",
    "coordinator_core.ops.dispatch_emit.tests.test_needs_slot_narration",
    "coordinator_core.ops.dispatch_emit.tests.test_part_script_name_matches_part_id",
    "coordinator_core.ops.dispatch_emit.tests.test_predecessor_state_in_row_prompt",
    "coordinator_core.ops.dispatch_emit.tests.test_review_only_test_scope_is_plan_surface",
    "coordinator_core.ops.dispatch_emit.tests.test_row_prompt_names_its_spec",
    "coordinator_core.ops.dispatch_emit.tests.test_run_base_at_fire",
    "coordinator_core.ops.dispatch_emit.tests.test_seam_wave_boundary",
    "coordinator_core.ops.dispatch_emit.tests.test_shared_blocks_keep_prompts_byte_identical",
    "coordinator_core.ops.dispatch_emit.tests.test_sibling_split",
    "coordinator_core.ops.dispatch_emit.tests.test_size_then_plan_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_sizing_fire_accept_and_token_args",
    "coordinator_core.ops.dispatch_emit.tests.test_stop_rule_halt_scopes_to_plan",
    "coordinator_core.ops.dispatch_emit.tests.test_unroutable_row_survives_an_override",
    "coordinator_core.ops.dispatch_emit.tests.test_unverifiable_enricher_row",
    "coordinator_core.ops.dispatch_emit.tests.test_vitest_cap_and_row_local_block",
    "coordinator_core.ops.dispatch_emit.tests.test_write_set_shared_once",
    "coordinator_core.ops.dispatch_emit.tests.test_writes_under",
    "coordinator_core.ops.dispatch_emit.tests.test_writes_under_carry_through",
    "coordinator_core.ops.dispatch_emit.tests.test_writes_under_gitignored_prefix",
    "coordinator_core.ops.fleet.mode_control",
    "coordinator_core.ops.plan_chain.tests._chain_repo",
    "coordinator_core.ops.tests.test_fleet_mode_control",
    "coordinator_core.ops.tests.test_sizing_record_xl_exit_apm",
    "coordinator_core.session.tests.test_fleet_mode",
    "coordinator_core.session.tests.test_mode_resolution",
    # Names "fleet-mode.json" as a literal in its own docstring/source (same
    # basename-reader shape as this file's own _SELF_MODULE exclusion, but
    # this module is not itself the reader_closure() walker so it does not
    # get that exclusion) -- a basename reader, not an importer of either
    # target module. Pre-existing gap found while integrating this diff's
    # review; not one of the reviewer's three findings.
    "coordinator_core.session.tests.test_unarchive_on_resume",
    "coordinator_core.sizing_assemble.conftest",
    "coordinator_core.sizing_assemble.test_clause_register_refusal",
    "coordinator_core.sizing_assemble.test_intent_turn",
    "coordinator_core.sizing_assemble.test_plan_hand_off",
    "coordinator_core.sizing_assemble.test_register_cli",
    "coordinator_core.sizing_assemble.test_verbatim_embed_report",
    "coordinator_core.sizing_assemble.test_write_records_touch",
    "coordinator_core.tests.test_fleet_mode_process_boundary",
    "coordinator_core.tests.test_hooks_roundtrip",
    "coordinator_core.write_guards.nudge_em_code_dispatch",
    "coordinator_core.write_guards.tests.test_windows_platform_simulation",
    # --- C6 (2026-09-27-sizing-carries-exit-criterion-and-interaction-mode) ---
    #
    # `sizing_assemble.route()`'s `_validate_interaction_mode` and `main()`'s
    # `_resolve_interaction_mode_and_source` both do a FUNCTION-LOCAL (lazy)
    # import of `mode_resolution` -- never at module top level, so a bare
    # `import coordinator_core.sizing_assemble` stays import-light (module
    # docstring's "READ-ONLY, by construction" note). This static AST walk
    # still finds the import (it walks the WHOLE tree, not just module-level
    # statements -- see `_module_imports`'s own docstring), so the module
    # reaches this closure regardless of laziness. `sizing_assemble` is
    # advisory-class (it never denies -- it computes a routing decision an
    # EM/skill acts on, the same non-denial shape as
    # `nudge_em_code_dispatch`'s own allowlisting above) and it is not under
    # `write_guards/`/`bash_guards/`, so it is out of
    # `test_no_denial_shaped_guard_reaches_the_record`'s CLASS-derivation
    # scope entirely -- it only has to appear here.
    "coordinator_core.sizing_assemble",
    "coordinator_core.sizing_assemble.test_interaction_mode_cli",
    "coordinator_core.sizing_assemble.test_interaction_mode_touchpoints",
    # --- job-mode announce / sessionstart cluster ---------------------------
    #
    # `session_start_announce_job_mode` calls `resolve_mode("job_mode")` to
    # announce the resolved mode as SessionStart `additionalContext`; it is
    # advisory (context only, never a tool-call gate) and needs the resolver,
    # so allowlisting is the disposition -- restructuring would mean
    # re-deriving the resolver. `sessionstart_dispatch` reaches the record
    # only by dispatching to it, with the same class. Consumers that need only
    # a constant (`autonomous_go`, `sizing_accept_exit_criterion`, the warm
    # door's `env_forwarding`) were restructured onto the import-free leaf
    # `session.job_mode_env` and are NOT listed.
    "coordinator_core.hooks.session_start_announce_job_mode",
    "coordinator_core.hooks.sessionstart_dispatch",
    "coordinator_core.hooks.tests.test_arrival_w4_c10",
    "coordinator_core.hooks.tests.test_postuse_bash_commit_releases_claims",
    "coordinator_core.bash_guards.tests.guard_message_exemptions",
    "coordinator_core.benchmarks.shim_prototype_dispatcher",
    "coordinator_core.ops.tests.test_sizing_spike_verdict",
    "coordinator_core.tests.test_composition_budget_doc_scaffold",
    # --- plan_assemble cluster ----------------------------------------------
    #
    # `plan_assemble.predicates.substrate_seven_dim` reads
    # `sizing_assemble._PREMISE_DETENT_TSHIRTS` (a constant defined inside
    # `sizing_assemble`, and monkeypatched there by its liveness test), so it
    # reaches the record only through `sizing_assemble`, already allowlisted
    # above as advisory. The predicate computes a report field for plan
    # assembly and never gates a tool call. Moving the constant out would
    # break that liveness test's premise, so the cluster is allowlisted.
    "coordinator_core.plan_assemble",
    "coordinator_core.plan_assemble.predicates.substrate_seven_dim",
    "coordinator_core.plan_assemble.predicates.test_context",
    "coordinator_core.plan_assemble.predicates.test_substrate_seven_dim",
    "coordinator_core.plan_assemble.residue",
    "coordinator_core.plan_assemble.test_residue",
    "coordinator_core.plan_assemble.test_residue_admission",
    "coordinator_core.plan_assemble.test_scaffold_directive_parity",
    "coordinator_core.sizing_assemble.test_scaffold_directive_parity",
    "coordinator_core.test_sizing_assemble",
    # --- new-module cluster (marker-parity/allowlist sweep, bb-20261002a C38) ---
    #
    # Each group reaches the record through an edge measured by
    # `reader_closure`, not assumed.
    #
    # (a) `sizing_assemble` importers. `ops.sizing_resize` and
    # `ops.dispatch_emit.ask_gate` each import `sizing_assemble` (a function-
    # local `route` import) to compute a sizing route; `sizing_assemble`
    # reaches the record through its own lazy `mode_resolution` import and is
    # already allowlisted above as advisory. Every `dispatch_emit` module
    # (`op`, `cli`, `ask_compose`, `mark_landed`), `workflow_fire.op`, and
    # their tests reach it only by importing `ask_gate` / `op` -- ONE edge,
    # counted per module by the transitive walk. None of them is under
    # `write_guards/`/`bash_guards/`, none gates a tool call: they compute and
    # emit a dispatch script or a sizing arm, so no mode value reaches a
    # denial. Restructure alternative (move `route` behind a leaf that does
    # not import `mode_resolution`) edits `sizing_assemble`, which is another
    # owner's surface; allowlisting is the available disposition here.
    "coordinator_core.ops.dispatch_emit.ask_compose",
    "coordinator_core.ops.dispatch_emit.ask_gate",
    "coordinator_core.ops.dispatch_emit.cli",
    "coordinator_core.ops.dispatch_emit.mark_landed",
    "coordinator_core.ops.dispatch_emit.op",
    "coordinator_core.ops.dispatch_emit.tests.test_agent_type_resolution",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_plan_blitz_args",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_single_exit",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_records_ruling",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_interaction_mode_single_source",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_shape_handback",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_zero_headless",
    "coordinator_core.ops.dispatch_emit.tests.test_cli",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_admission",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_mark_landed",
    "coordinator_core.ops.dispatch_emit.tests.test_cloud_spawn_brief",
    "coordinator_core.ops.dispatch_emit.tests.test_doe_port_fidelity_and_scope",
    "coordinator_core.ops.dispatch_emit.tests.test_emit",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_spawn_count",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest",
    "coordinator_core.ops.dispatch_emit.tests.test_emitted_script_states_node_check_does_not_apply",
    "coordinator_core.ops.dispatch_emit.tests.test_every_emit_route_composes_review",
    "coordinator_core.ops.dispatch_emit.tests.test_inventory_mint",
    "coordinator_core.ops.dispatch_emit.tests.test_mark_landed",
    "coordinator_core.ops.dispatch_emit.tests.test_new_module_importer_window",
    "coordinator_core.ops.dispatch_emit.tests.test_op",
    "coordinator_core.ops.dispatch_emit.tests.test_op_emission_receipt",
    "coordinator_core.ops.dispatch_emit.tests.test_op_returns_fire_args",
    "coordinator_core.ops.dispatch_emit.tests.test_op_sizing_emit",
    "coordinator_core.ops.dispatch_emit.tests.test_queue_emit",
    "coordinator_core.ops.dispatch_emit.tests.test_queue_emit_composes_review",
    "coordinator_core.ops.dispatch_emit.tests.test_restamp",
    "coordinator_core.ops.dispatch_emit.tests.test_sizing_fire_routes",
    "coordinator_core.ops.dispatch_emit.tests.test_sizing_fire_wiring",
    "coordinator_core.ops.sizing_resize",
    "coordinator_core.ops.tests.test_sizing_resize",
    # (b) `plan_chain` cluster and later `dispatch_emit` importers. `plan_chain.plan_stage`
    # and `.driver` import `dispatch_emit.op` (the emission receipt and the bind op), the
    # same ONE edge into the `ask_gate` -> `sizing_assemble` closure group (a) records;
    # the rest import `op`, `plan_stage` or `driver` or a test helper that does. The chain
    # computes and fires a plan/execute script and halts; no mode value reaches a denial,
    # and none is under `write_guards/`/`bash_guards/`. The size rule
    # (`ops.sizing_acceptance`) is an import-free leaf and adds no edge.
    "coordinator_core.hooks.tests.test_nudge_hand_written_plan",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_usage_limit",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_research_route",
    "coordinator_core.ops.dispatch_emit.tests.test_hold_rows",
    "coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_gaps",
    "coordinator_core.ops.dispatch_emit.tests.test_op_certified_plans",
    "coordinator_core.ops.dispatch_emit.tests.test_pipeline_resume_missing",
    "coordinator_core.ops.dispatch_emit.tests.test_pipeline_validator",
    "coordinator_core.ops.dispatch_emit.tests.test_plan_blitz_call_contract",
    "coordinator_core.ops.dispatch_emit.tests.test_review_close_routes",
    "coordinator_core.ops.dispatch_emit.tests.test_review_fix_reverification_and_cli_line",
    "coordinator_core.ops.dispatch_emit.tests.test_typecheck_leg",
    "coordinator_core.ops.plan_chain",
    "coordinator_core.ops.plan_chain.driver",
    "coordinator_core.ops.plan_chain.plan_stage",
    "coordinator_core.ops.plan_chain.tests.test_chain_budget",
    "coordinator_core.ops.plan_chain.tests.test_chain_end_to_end",
    "coordinator_core.ops.plan_chain.tests.test_contract",
    "coordinator_core.ops.plan_chain.tests.test_digest",
    "coordinator_core.ops.plan_chain.tests.test_driver",
    "coordinator_core.ops.plan_chain.tests.test_execute_result",
    "coordinator_core.ops.plan_chain.tests.test_phase1_checks",
    "coordinator_core.ops.plan_chain.tests.test_phase1_dead_session_gate",
    "coordinator_core.ops.plan_chain.tests.test_phase1_gates",
    "coordinator_core.ops.plan_chain.tests.test_plan_stage",
    "coordinator_core.session.tests.test_autonomous_sentinel_fleet_move",
    "coordinator_core.sizing_assemble.test_research_flags",
    "coordinator_core.tests.test_sizing_assemble_click_path",
    "coordinator_core.ops.workflow_fire.op",
    "coordinator_core.ops.workflow_fire.tests.test_end_to_end_fire",
    "coordinator_core.ops.workflow_fire.tests.test_fired_drift_guard",
    # (b) New test modules importing an already-allowlisted dispatcher or
    # corpus module (`stop_dispatch`, `sessionstart_dispatch`,
    # `postuse_advisory_dispatch`, `guard_message_corpus`,
    # `test_guard_message_size`). Tests consume these as data or measure their
    # spawn count; they are the same class as the test modules listed above.
    "coordinator_core.bash_guards.tests.test_guard_message_size_claude_klabauter_corpus",
    "coordinator_core.bash_guards.tests.test_platform_conditioned_deny_reachability",
    "coordinator_core.hooks.tests.test_postuse_advisory_dispatch_spawn_count",
    "coordinator_core.hooks.tests.test_sessionstart_dispatch_leg_parity",
    "coordinator_core.hooks.tests.test_stop_dispatch_spawn_count",
    # (c) `sizing_assemble`'s own write-back test.
    "coordinator_core.sizing_assemble.test_write_back",
    # (d) Importers of an already-allowlisted dispatcher/sizing module, measured by
    # `reader_closure`: `ops.dispatch_emit.ask_roadmap` (imported by `ask_compose`),
    # `roadmap.blitz_stage` / `ops.roadmap_blitz_stage` (a `sizing_assemble` `route`/
    # `write_back` import) -- advisory-class, computing a sizing/roadmap fold that no
    # tool call gates on -- and the test modules that import `dispatch_emit.op`/`cli`,
    # `ask_compose`/`ask_gate`, `sizing_assemble` or `blitz_stage`. Neither lives under
    # `write_guards/`/`bash_guards/`; the one `bash_guards/tests` entry is a test.
    "coordinator_core.bash_guards.tests.test_guard_message_corpus_no_process_control",
    "coordinator_core.frontmatter.tests.test_approval_body_sha_engine_bookkeeping",
    "coordinator_core.frontmatter.tests.test_approved_body_sha",
    "coordinator_core.hooks.tests.test_block_workflow_foreign_emission_pipeline",
    "coordinator_core.ops.dispatch_emit.ask_roadmap",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_gated_and_halt",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_baton_input",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_apm_accept",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_roadmap",
    "coordinator_core.ops.dispatch_emit.tests.test_inventory_default_out",
    "coordinator_core.ops.dispatch_emit.tests.test_inventory_optional_columns",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_stage_scaffold_gate",
    "coordinator_core.ops.dispatch_emit.tests.test_chatty_emit",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_lanes",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_pipeline",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_reverify_delivery_empty",
    "coordinator_core.ops.dispatch_emit.tests.test_dirty_write_set_spawns",
    "coordinator_core.ops.dispatch_emit.tests.test_inventory_split_cap_and_refusal",
    "coordinator_core.ops.dispatch_emit.tests.test_landed_reconcile",
    "coordinator_core.ops.dispatch_emit.tests.test_op_lanes",
    "coordinator_core.ops.dispatch_emit.tests.test_op_pipeline_route",
    "coordinator_core.ops.dispatch_emit.tests.test_pipeline_doe_manifests",
    "coordinator_core.ops.dispatch_emit.tests.test_pipeline_route_budget",
    "coordinator_core.ops.dispatch_emit.tests.test_pipeline_route_parity",
    "coordinator_core.ops.dispatch_emit.tests.test_resume_from_continuance",
    "coordinator_core.ops.dispatch_emit.tests.test_review_only_emit",
    "coordinator_core.ops.roadmap_blitz_stage",
    "coordinator_core.ops.tests.test_roadmap_blitz_stage_spawn_count",
    "coordinator_core.ops.tests.test_sizing_record_pm_resolution",
    "coordinator_core.ops.tests.test_sizing_record_xl_exit",
    "coordinator_core.roadmap.blitz_stage",
    "coordinator_core.roadmap.tests.test_blitz_stage",
    #
    # --- engine-ask submission cluster (the same ONE edge as (a), measured) ---
    #
    # `hooks.submit_engine_ask` and `hooks.submit_bug_blitz` import
    # `ops.dispatch_emit.op` (to emit the warp run), `ops.dispatch_emit.plan_route`
    # imports `dispatch_emit.cli`, and the dispatch_emit / hooks / sizing_assemble
    # tests import those or `ask_gate`/`ask_compose`/`sizing_assemble`: every
    # module below reaches the record only through that already-allowlisted
    # `dispatch_emit` -> `sizing_assemble` -> `mode_resolution` edge. The two
    # `hooks.submit_*` modules rewrite or deny a `Workflow` submission on its
    # own args shape (malformed ask, live run, unmet `after`); no mode value
    # reaches either decision, and neither is under `write_guards/` or
    # `bash_guards/`. Restructure alternative (move `route` behind a leaf)
    # edits `sizing_assemble`, another owner's surface -- unchanged from (a).
    #
    # The WIDER edge this cluster once rode is NOT allowlisted but cut:
    # `hooks.block_unenumerated_agent_type` (imported by guards and hooks
    # across the tree) used to import `hooks.submit_engine_ask` for
    # `sizing_scout_deny`, which put ~3,600 modules in the closure; it now
    # imports the leaf `hooks.sizing_scout_deny`. Likewise
    # `dispatch_emit.terminal_commit` read `op._recorded_run_base` (pulling `op`
    # into every `ops.review_stamp` importer); it now reads
    # `ask_fanout_contract.recorded_run_base`.
    "coordinator_core.hooks.submit_bug_blitz",
    "coordinator_core.hooks.submit_engine_ask",
    "coordinator_core.hooks.tests.test_dispatch_escape_in_run_report",
    "coordinator_core.hooks.tests.test_multi_ask_sibling_runs_e2e",
    "coordinator_core.hooks.tests.test_submit_bug_blitz",
    "coordinator_core.hooks.tests.test_submit_engine_ask",
    "coordinator_core.hooks.tests.test_submit_engine_ask_real_emit",
    "coordinator_core.hooks.tests.test_submit_engine_ask_research_sizing",
    "coordinator_core.hooks.tests.test_submit_engine_ask_sizing_context",
    "coordinator_core.hooks.tests.test_workflow_run_record_capture",
    "coordinator_core.hooks.tests.test_workflow_stall_resume",
    "coordinator_core.hooks.tests.test_workflow_stall_stop_op",
    "coordinator_core.ops.dispatch_emit.plan_route",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_bundle_split_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_acceptance_rows",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_commit_doc_paths",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_cross_repo_evidence",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_design_brief",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_design_met_by",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_halt_commits_record",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_in_run_close",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_judge_evidence_and_run_base",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_ledger_resume",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_m_spine_direct",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_mechanical_revise",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_no_silent_end",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_research_classification",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_research_fire",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_resume_all_landed",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_test_leg_verdict",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_verify_only_skips_review",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_wave_barrier",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_design_registry",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_research_route",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_spawn_count",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_halt_detail",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_intake_sizing_submission",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_commit_pending_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_owed_handback",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_research_classification",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_roadmap_fanout_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_stage_blinded_reviewer_row",
    "coordinator_core.ops.dispatch_emit.tests.test_check_leg_cut",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_queue_route_run_token",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_research_multi_ask",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_research_parts",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_stdout_single_json",
    "coordinator_core.ops.dispatch_emit.tests.test_commit_legs_are_haiku",
    "coordinator_core.ops.dispatch_emit.tests.test_cross_repo_engine_ask",
    "coordinator_core.ops.dispatch_emit.tests.test_em_mailbox_watch",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_register_pin_drift",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_verify_leg",
    "coordinator_core.ops.dispatch_emit.tests.test_emitted_prompts_name_no_skill_to_invoke",
    "coordinator_core.ops.dispatch_emit.tests.test_engine_owned_rows_judged_at_engine_root",
    "coordinator_core.ops.dispatch_emit.tests.test_gate_baseline_owed_gates",
    "coordinator_core.ops.dispatch_emit.tests.test_grind_improvement_queue",
    "coordinator_core.ops.dispatch_emit.tests.test_halt_commit_relay_layout_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_halted_record_withholds_peer_claims",
    "coordinator_core.ops.dispatch_emit.tests.test_halted_rows_commit",
    "coordinator_core.ops.dispatch_emit.tests.test_held_halt_reviews_landed_rows",
    "coordinator_core.ops.dispatch_emit.tests.test_judge_reason_falls_back_to_observation",
    "coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_names_only_register_rows",
    "coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_pointer_written",
    "coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_recovery_text",
    "coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate",
    "coordinator_core.ops.dispatch_emit.tests.test_leg_register_ratchet",
    "coordinator_core.ops.dispatch_emit.tests.test_no_run_leaves_a_dirty_tree",
    "coordinator_core.ops.dispatch_emit.tests.test_only_incomplete_own_leftover",
    "coordinator_core.ops.dispatch_emit.tests.test_only_incomplete_review_base",
    "coordinator_core.ops.dispatch_emit.tests.test_plan_route",
    "coordinator_core.ops.dispatch_emit.tests.test_plan_route_resume_rows",
    "coordinator_core.ops.dispatch_emit.tests.test_reachability_fix_keeps_met_judge_met",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_echoes_revisions",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_reason_agrees_with_criterion",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_resumes_halted_run",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_xs_recorded_writes",
    "coordinator_core.ops.dispatch_emit.tests.test_relayed_ask_registers_only_its_text",
    "coordinator_core.ops.dispatch_emit.tests.test_research_close_params_grounding",
    "coordinator_core.ops.dispatch_emit.tests.test_research_emit",
    "coordinator_core.ops.dispatch_emit.tests.test_research_emit_script_cap",
    "coordinator_core.ops.dispatch_emit.tests.test_research_emit_single_script",
    "coordinator_core.ops.dispatch_emit.tests.test_research_emit_split",
    "coordinator_core.ops.dispatch_emit.tests.test_research_gate_end_to_end",
    "coordinator_core.ops.dispatch_emit.tests.test_research_halt_fire_routes",
    "coordinator_core.ops.dispatch_emit.tests.test_research_multi_ask_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_research_multi_repo_end_to_end",
    "coordinator_core.ops.dispatch_emit.tests.test_research_route_scout_questions",
    "coordinator_core.ops.dispatch_emit.tests.test_research_three_source_segment_halt",
    "coordinator_core.ops.dispatch_emit.tests.test_reserved_run_refire",
    "coordinator_core.ops.dispatch_emit.tests.test_resume_counts_ledger_halt_landed_chunks",
    "coordinator_core.ops.dispatch_emit.tests.test_review_only_brief_drops_deleted_tests",
    "coordinator_core.ops.dispatch_emit.tests.test_review_wave_blocked_halts_run",
    "coordinator_core.ops.dispatch_emit.tests.test_seam_leg_executes_declared_paths",
    "coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_cli_escape",
    "coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record",
    "coordinator_core.ops.dispatch_emit.tests.test_typecheck_criterion_routing",
    "coordinator_core.ops.review_mint.tests.test_roster",
    "coordinator_core.ops.tests.test_roadmap_blitz_stage_params",
    "coordinator_core.sizing_assemble.test_acceptance_rows",
    "coordinator_core.sizing_assemble.test_suite_tier_rewrite",
    "coordinator_core.sizing_assemble.test_write_terse_default",
    "coordinator_core.tests.test_cited_records_not_in_scratch",
    #
    # --- later test-module cluster (measured by `reader_closure`) -----------
    #
    # Every module below is a test that imports an already-allowlisted
    # `dispatch_emit` (`op`/`cli`/`ask_gate`/`ask_compose`), `submit_engine_ask`
    # or `sizing_assemble` module: the same ONE edge as groups (a)-(d) above.
    # No production module is added; tests consume these as the code under test,
    # none gates a tool call, and none sits under `write_guards/`/`bash_guards/`.
    "coordinator_core.hooks.tests.test_engine_ask_route_preference_e2e",
    "coordinator_core.hooks.tests.test_sibling_hold_release_and_supersession_e2e",
    "coordinator_core.hooks.tests.test_size_stage_guard_run_scoped",
    "coordinator_core.hooks.tests.test_submit_engine_ask_live_overlap",
    # All-landed / resume test modules: none names fleet-mode.json or imports
    # the resolver; each reaches the record only through an importer of
    # `ask_gate` / `op` / `submit_engine_ask`, already allowlisted above.
    "coordinator_core.hooks.tests.test_submit_engine_ask_landed_predecessor",
    "coordinator_core.hooks.tests.test_submit_engine_ask_session_root",
    "coordinator_core.ops.dispatch_emit.tests.test_all_landed_refire_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_all_landed_refire_gate",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_all_landed_ledger",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume_stamps_landed_rows_coded",
    "coordinator_core.ops.dispatch_emit.tests.test_resume_landed_state_row_shas",
    "coordinator_core.hooks.tests.test_watchdog_research_sizing_repro",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_checkpoint_trailer_runtime",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_commit_halt_refire_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_commit_halt_next_action",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_design_route",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_ledger_resume_restage",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_pin_drift_halt",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_plan_blitz_ready_restage",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_relayed_manifest_admission",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_repeat_pull_halt",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_scout_not_run",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_source_memo_clause",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_fold_reads_disk_manifest_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_design_approval",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_design_route",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_ledger_stages",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume_ledger_discharges",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume_snapshot",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_route_preference",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_prompts_name_sidecar_dir",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_route_preference_emit",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_scout_design_route",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_stage_folds_no_mechanical_leg",
    "coordinator_core.ops.dispatch_emit.tests.test_checkpoint_lines_on_every_emitted_checkpoint",
    "coordinator_core.ops.dispatch_emit.tests.test_commit_halt_pointer_written",
    "coordinator_core.ops.dispatch_emit.tests.test_commit_halt_resume_gate",
    "coordinator_core.ops.dispatch_emit.tests.test_compose_script_front_end_wave_leg",
    "coordinator_core.ops.dispatch_emit.tests.test_composed_chunk_ids_are_strings",
    "coordinator_core.ops.dispatch_emit.tests.test_design_review_after_plan",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_prompts_name_sidecar_dir",
    "coordinator_core.ops.dispatch_emit.tests.test_every_emitted_leg_names_its_sidecar",
    "coordinator_core.ops.dispatch_emit.tests.test_grind_prompts_name_sidecar_dir",
    "coordinator_core.ops.dispatch_emit.tests.test_halted_record_landing_integrity",
    "coordinator_core.ops.dispatch_emit.tests.test_invoke_prompts_carry_reconcile_clause",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_executes_unmet_rows_incident",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_after_partial_ledger_halt_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_carries_met_rows_across_halts",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_ledger_halt_incident_falsifier",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_ledger_halt_keeps_met_rows_and_runs_remaining",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_ledger_halted_sizing_resumes",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_replan_added_rows_dispatch_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_rejudge_agent_brief",
    "coordinator_core.ops.dispatch_emit.tests.test_research_emit_seeds",
    "coordinator_core.ops.dispatch_emit.tests.test_research_route_probe_targets",
    "coordinator_core.ops.dispatch_emit.tests.test_review_only_inventory_repo_root",
    "coordinator_core.ops.dispatch_emit.tests.test_revise_from_peer",
    "coordinator_core.ops.dispatch_emit.tests.test_row_verify_folds_into_terminal",
    "coordinator_core.ops.dispatch_emit.tests.test_ruled_out_filters_resume_and_prompt",
    "coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift",
    "coordinator_core.ops.review_mint.tests.test_review_prompts_name_sidecar_dir",
    "coordinator_core.sizing_assemble.test_design_route_flags",
    #
    # --- test modules added after the last sweep (measured by `reader_closure`) ---
    #
    # Each imports an already-allowlisted `dispatch_emit` module (`ask_gate`, `op`,
    # `plan_route`, `cli`) or a sibling test that does: the same ONE existing edge
    # (`ask_gate` -> `sizing_assemble` -> `mode_resolution`), counted per module.
    # No production module is added. Restructure alternative: none per test -- a test
    # that exercises `ask_gate`/`op` has to import it. The wider edge the ipc closure
    # once rode (`sizing_fire` lazily importing `mode_resolution`, ~4,000 modules) is
    # CUT, not allowlisted: the fleet mode now arrives through
    # `sizing_fire.set_fleet_mode_provider`, installed by `dispatch_emit.cli.main`,
    # which is already a reader. `ops.research_reground` was likewise cut (it now
    # resolves the content root itself, not through `dispatch_emit.op`).
    "coordinator_core.hooks.tests.test_submit_engine_ask_held_slice",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_commit_stage_reconcile_clause",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_all_ruled_out_passes_ledger_check",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_bound_sizing_path",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_fanout_groups",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_review_reverify",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_routed_rows",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_scout_values_assemble",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_compose_sizing_path_single_source",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_echoes_bound_sizing",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_gate_s_resume_stages",
    "coordinator_core.ops.dispatch_emit.tests.test_ask_source_help_text",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_inventory_accounting",
    "coordinator_core.ops.dispatch_emit.tests.test_cli_research_per_group",
    "coordinator_core.ops.dispatch_emit.tests.test_deviation_reason_end_to_end",
    "coordinator_core.ops.dispatch_emit.tests.test_direct_sizing_inherits_interaction_mode",
    "coordinator_core.ops.dispatch_emit.tests.test_dirty_write_set",
    "coordinator_core.ops.dispatch_emit.tests.test_dispatch_emit_fanout_reply",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_artifact_naming_and_out",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_fanout_groups",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_inventory_accounting",
    "coordinator_core.ops.dispatch_emit.tests.test_emit_work_class_routing",
    "coordinator_core.ops.dispatch_emit.tests.test_fanout_exit_criterion",
    "coordinator_core.ops.dispatch_emit.tests.test_grind_compose_fanout_meta",
    "coordinator_core.ops.dispatch_emit.tests.test_grind_review_reverify",
    "coordinator_core.ops.dispatch_emit.tests.test_grind_terminal_commit_reconcile_clause",
    "coordinator_core.ops.dispatch_emit.tests.test_grind_verify_covered_flip",
    "coordinator_core.ops.dispatch_emit.tests.test_grind_verify_dedupe_close",
    "coordinator_core.ops.dispatch_emit.tests.test_grind_verify_mode",
    "coordinator_core.ops.dispatch_emit.tests.test_halted_record_own_claims_landing",
    "coordinator_core.ops.dispatch_emit.tests.test_halted_record_targeted_tests",
    "coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes",
    "coordinator_core.ops.dispatch_emit.tests.test_only_incomplete_row_subset",
    "coordinator_core.ops.dispatch_emit.tests.test_op_research_per_group",
    "coordinator_core.ops.dispatch_emit.tests.test_refire_credit_counts_landed",
    "coordinator_core.ops.dispatch_emit.tests.test_research_spike_refusal",
    "coordinator_core.ops.dispatch_emit.tests.test_resume_ledger_carries_ledger_ids",
    "coordinator_core.ops.dispatch_emit.tests.test_stop_rule_refire_reemits_row_e2e",
    "coordinator_core.ops.dispatch_emit.tests.test_stop_rule_row_not_landed",
    "coordinator_core.ops.dispatch_emit.tests.test_stranded_run_reland",
    "coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_own_checkpoints_satisfy_since_halt",
    "coordinator_core.ops.dispatch_emit.tests.test_unmarked_ask_source_derivation",
)

# The one allowlist addition the brief itself pre-disposed (see module
# docstring's "WHY nudge_em_code_dispatch IS ALLOWLISTED" section). It is
# also the only EXPECTED_READERS member that sits at the TOP LEVEL of a
# guard directory, which is the scope `test_no_denial_shaped_guard_reaches_
# the_record` below actually enumerates CLASS over -- see that test's own
# docstring for why top-level-only mirrors the brief's own measurement.
_ALLOWLISTED_TOP_LEVEL_GUARD_MODULE = "coordinator_core.write_guards.nudge_em_code_dispatch"

# Top-level-only: matches exactly `write_guards/*.py` / `bash_guards/*.py`
# (no further dot), the same non-recursive scope the brief's own "71
# classified / 15 unclassified" measurement was taken over. A module under
# `write_guards/tests/` or `bash_guards/tests/` is test-support code, never
# itself a registered guard (no MATCHERS/PRIORITY), so it is out of this
# check's scope by construction, not by omission.
_GUARD_TOP_LEVEL_PREFIXES = (
    "coordinator_core.write_guards.",
    "coordinator_core.bash_guards.",
)

_DENIAL_SHAPED_CLASSES = frozenset({"hard-deny"})


def _is_top_level_guard_module(dotted: str) -> bool:
    for prefix in _GUARD_TOP_LEVEL_PREFIXES:
        if dotted.startswith(prefix) and "." not in dotted[len(prefix):]:
            return True
    return False


#: The structural mark of a module that can actually be DISPATCHED as a guard.
#: Living in `write_guards/` or `bash_guards/` does not make a module a guard --
#: both directories also hold mechanism and helper modules that no dispatcher
#: can ever fire. `MATCHERS` is what the dispatcher keys on, so declaring it is
#: the decidable, structural answer to "is this a guard", and it does not depend
#: on a naming convention or a docstring.
_GUARD_DISPATCH_MARKER = "MATCHERS"


# The one module this walk exempts from the inverted default below: a
# mechanism helper at the TOP LEVEL of a guard directory whose absence of a
# dispatch surface is UNAMBIGUOUS, not merely undetected. `_firing_shape.py`
# carries no MATCHERS and no PRIORITY -- it is the classification mechanism
# the firing-shape gate itself runs, not a registered guard -- and it reaches
# the fleet record only through a function-local import of
# `nudge_em_code_dispatch` used as a test specimen (see module docstring's
# "GUARD-CORPUS / MESSAGE-REGISTER / FIRING-SHAPE META-TESTS" cluster note).
# No other module may be added here without the same unambiguous-absence
# analysis recorded inline.
_UNAMBIGUOUS_NON_GUARD_MODULES = frozenset({
    "coordinator_core/bash_guards/_firing_shape.py",
})


def _declares_guard_dispatch_surface(path: Path) -> bool:
    """True unless `path` can be shown to POSITIVELY LACK a dispatchable
    ``MATCHERS`` surface -- i.e. ambiguity resolves to IS-A-GUARD, never to
    not-a-guard, matching the guard directories' own stated convention
    ("default posture on ambiguity is deny",
    ``coordinator_core/bash_guards/block_disarm_marker_sentinel_creation.py``).

    WHY THIS EXISTS, so it is not "simplified" back into a directory check.
    Without it, `_module_class()` returning None collapses two facts that carry
    opposite obligations: "this is a guard whose CLASS I could not read" (an
    unresolved question -- fail loud, default posture on ambiguity is deny) and
    "this is not a guard, so it has no CLASS to read" (nothing to resolve).

    INVERTED DEFAULT (review finding, 2026-08-29). The original version of
    this function matched only a literal top-level `MATCHERS = ...` /
    `MATCHERS: T = ...` statement in `tree.body`, and returned False -- "not a
    guard" -- for everything else, including a `MATCHERS` bound by
    `from ... import MATCHERS`, by a conditional top-level assignment inside
    an `if`/`try` block, or dynamically via `globals()["MATCHERS"] = ...`.
    Any of those shapes lands the module in `non_guard_readers`, where CLASS
    is never checked at all -- exactly the "validated key instead of an
    absent reader" failure this file's docstring says it exists to prevent,
    and once such a module is allowlisted the tests go permanently green with
    zero further pushback on its denial-shaped nature. The fix is not to
    enumerate more binding forms and hope the list is complete: this function
    now returns True (treat as guard, subject to the CLASS check) for
    anything it cannot positively prove lacks a MATCHERS surface. The only
    modules exempted from that default are named explicitly in
    `_UNAMBIGUOUS_NON_GUARD_MODULES` above, with the reasoning recorded
    inline -- never inferred structurally.

    This narrows the SUBJECT of the denial-shaped check, never its STRENGTH: a
    module declaring MATCHERS with no derivable CLASS still fails loud below.
    Measured over the tree at the time of writing, `write_guards/` has 48
    dispatchable guards and every one declares CLASS; `bash_guards/` has 29, of
    which exactly two (`guard_grep_via_bash`, `guard_powershell_via_bash`)
    declare no CLASS -- those two are precisely what the fail-loud path is for,
    and neither reaches the fleet record today.

    Non-guard modules are NOT thereby exempt from the floor: they still have to
    appear in `EXPECTED_READERS`, so a helper newly reaching the record still
    turns `test_reader_set_matches_allowlist_exactly` red. Only the CLASS
    question stops applying to them, because they have no CLASS to ask about.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return True  # unreadable -- cannot prove absence, default to guard
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return True  # unparseable -- cannot prove absence, default to guard

    # Positive detection, over the WHOLE tree (not just tree.body), so a
    # conditional top-level assignment inside `if`/`try` is not missed: a
    # direct MATCHERS bind, a `from ... import MATCHERS` re-export, or the
    # bare string literal "MATCHERS" appearing anywhere (a dynamic bind via
    # `globals()["MATCHERS"] = ...` / `setattr(..., "MATCHERS", ...)` cannot
    # be proven a bind statically, so its mere presence is treated as
    # ambiguous-therefore-guard rather than an attempt to prove intent).
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == _GUARD_DISPATCH_MARKER:
                    return True
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if _GUARD_DISPATCH_MARKER in (alias.name, alias.asname):
                    return True
        elif isinstance(node, ast.Constant) and node.value == _GUARD_DISPATCH_MARKER:
            return True

    posix_path = path.as_posix().replace("\\", "/")
    if any(posix_path.endswith(exempt) for exempt in _UNAMBIGUOUS_NON_GUARD_MODULES):
        return False
    return True


# This test module inevitably names "fleet-mode.json" in its own docstring
# and error messages -- exclude it from its own basename-reader detection
# rather than let prose about the mechanism count as use of it.
_SELF_MODULE = "coordinator_core.tests.test_fleet_mode_reader_set"


# --- static import-graph walk (never imports anything) ----------------------


def _dotted_module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(module_name: str, *, is_package: bool) -> str:
    if is_package:
        return module_name
    parts = module_name.split(".")
    return ".".join(parts[:-1])


def _resolve_from_import(current_module: str, is_package: bool, node: ast.ImportFrom) -> set[str]:
    targets: set[str] = set()
    if node.level == 0:
        base = node.module or ""
    else:
        pkg_parts = _package_of(current_module, is_package=is_package).split(".")
        # level=1 -> same package; level=2 -> one package up; etc.
        up = node.level - 1
        anchor_parts = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
        anchor = ".".join(p for p in anchor_parts if p)
        base = f"{anchor}.{node.module}" if node.module else anchor
    if not base:
        return targets
    targets.add(base)
    for alias in node.names:
        if alias.name == "*":
            continue
        targets.add(f"{base}.{alias.name}")
    return targets


def _module_imports(path: Path, root: Path) -> tuple[set[str], bool]:
    """Returns (imported-module-candidates, names-the-fleet-basename-literal)."""
    source = path.read_text(encoding="utf-8")
    names_basename = _TARGET_BASENAME in source
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return set(), names_basename

    current_module = _dotted_module_name(root, path)
    is_package = path.name == "__init__.py"
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dotted = alias.name
                imports.add(dotted)
                pieces = dotted.split(".")
                for i in range(1, len(pieces)):
                    imports.add(".".join(pieces[:i]))
        elif isinstance(node, ast.ImportFrom):
            imports |= _resolve_from_import(current_module, is_package, node)
    return imports, names_basename


def reader_closure(root: Path, package: str = "coordinator_core") -> frozenset[str]:
    """Static, non-importing walk of every ``.py`` file under ``root/package``.

    Returns the dotted-module-name set of every module whose TRANSITIVE
    import closure reaches one of ``_TARGET_MODULES``, unioned with every
    module that names ``_TARGET_BASENAME`` as a literal in its own source.
    Takes a root path (never hardcodes the repo root) so it can run against
    a fixture tree -- see ``TestReaderClosureSelfVerification`` below.
    """
    pkg_root = root / package
    forward_edges: dict[str, set[str]] = {}
    basename_readers: set[str] = set()

    for path in sorted(pkg_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module_name = _dotted_module_name(root, path)
        imports, names_basename = _module_imports(path, root)
        forward_edges[module_name] = imports
        if names_basename:
            basename_readers.add(module_name)

    reverse_edges: dict[str, set[str]] = {}
    for importer, targets in forward_edges.items():
        for target in targets:
            reverse_edges.setdefault(target, set()).add(importer)

    reached: set[str] = set()
    frontier = list(_TARGET_MODULES)
    seen_targets = set(_TARGET_MODULES)
    while frontier:
        current = frontier.pop()
        for importer in reverse_edges.get(current, ()):
            if importer in _TARGET_MODULES:
                continue
            if importer not in reached:
                reached.add(importer)
                if importer not in seen_targets:
                    seen_targets.add(importer)
                    frontier.append(importer)

    reached |= {m for m in basename_readers if m not in _TARGET_MODULES}
    reached.discard(_SELF_MODULE)
    return frozenset(reached)


# --- CLASS derivation (static, never imports) --------------------------------


def _module_class(path: Path) -> str | None:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "CLASS" for t in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path(root: Path, dotted: str) -> Path:
    return root.joinpath(*dotted.split(".")).with_suffix(".py")


# --- the floor, over the real tree -------------------------------------------


def test_reader_set_matches_allowlist_exactly():
    """RED in both directions: a new reader appearing, or a listed reader
    disappearing, both fail this equality -- never a subset/superset check."""
    root = _repo_root()
    computed = reader_closure(root)
    expected = frozenset(EXPECTED_READERS)

    unexpected = computed - expected
    missing = expected - computed

    assert not unexpected, (
        f"module(s) {sorted(unexpected)} now reach the fleet record "
        "(coordinator_core.session.fleet_mode / .mode_resolution) but are "
        "not in EXPECTED_READERS -- someone wired a new reader. Either "
        "widen EXPECTED_READERS with the same allowlist-vs-restructure "
        "analysis this file's docstring records for "
        f"{_ALLOWLISTED_TOP_LEVEL_GUARD_MODULE}, or remove the new import."
    )
    assert not missing, (
        f"module(s) {sorted(missing)} are in EXPECTED_READERS but no "
        "longer reach the fleet record -- the reader was deleted (or its "
        "import removed) and the floor became vacuous again without "
        "anyone noticing. Update EXPECTED_READERS only if the removal was "
        "deliberate."
    )


def test_no_denial_shaped_guard_reaches_the_record():
    """The complement this plan's first unknown resolved to: no TOP-LEVEL
    module under write_guards/ or bash_guards/ (the non-recursive scope the
    brief's own "71 classified / 15 unclassified" measurement was taken
    over -- a module under either directory's tests/ subpackage is test-
    support code, never itself a registered guard) whose CLASS is denial-
    shaped may reach the fleet record, and an unclassifiable top-level
    guard-directory module in the reader closure fails loud rather than
    being treated as advisory by default."""
    root = _repo_root()
    computed = reader_closure(root)

    # Living in a guard directory is not being a guard. Split the reachers on
    # the dispatch marker BEFORE asking any of them about CLASS -- see
    # `_declares_guard_dispatch_surface` for why collapsing these two is what
    # made this test fail loud on a mechanism module.
    guard_dir_readers = sorted(m for m in computed if _is_top_level_guard_module(m))
    guard_readers = [
        m for m in guard_dir_readers if _declares_guard_dispatch_surface(_module_path(root, m))
    ]
    non_guard_readers = [m for m in guard_dir_readers if m not in guard_readers]

    # Non-guard modules in these directories are out of the CLASS question's
    # subject, never out of the floor: each one still has to be in
    # EXPECTED_READERS, which `test_reader_set_matches_allowlist_exactly`
    # enforces. Assert that here too so this test names its own residual
    # rather than silently ignoring a set it deliberately does not grade.
    unlisted_non_guards = sorted(set(non_guard_readers) - set(EXPECTED_READERS))
    assert not unlisted_non_guards, (
        f"non-guard module(s) {unlisted_non_guards} under write_guards/ or "
        "bash_guards/ reach the fleet record without appearing in "
        "EXPECTED_READERS. They carry no CLASS to grade, but they are still "
        "readers and the allowlist has to name them."
    )

    # CLASS derivation runs BEFORE the list-equality assert below, so the
    # specific "CLASS cannot be statically derived" message can actually
    # fire -- previously this loop ran AFTER `assert guard_readers == [...]`,
    # which already failed the test for any non-allowlisted entry, making
    # this branch dead code (review finding: the docstring's claim about how
    # this fails loud was false because the branch could never execute).
    non_allowlisted_guard_readers = [
        m for m in guard_readers if m != _ALLOWLISTED_TOP_LEVEL_GUARD_MODULE
    ]
    for dotted in non_allowlisted_guard_readers:
        path = _module_path(root, dotted)
        cls = _module_class(path)
        if cls is None:
            # Every other dispatchable guard reaching the record must fail
            # loud: an unclassifiable guard is an unresolved question, never
            # silently treated as advisory (default posture on ambiguity is
            # deny).
            pytest.fail(
                f"{dotted} reaches the fleet record but its module-level CLASS "
                "cannot be statically derived -- an unclassifiable module is an "
                "unresolved question, never silently treated as advisory "
                "(default posture on ambiguity is deny). This is a known, "
                "unremediated finding (see this chunk's run report) -- "
                "disposing of it (classify or restructure the import away) is "
                "out of this file's scope."
            )

    assert guard_readers == [_ALLOWLISTED_TOP_LEVEL_GUARD_MODULE], (
        f"dispatchable guard(s) reaching the fleet record: {guard_readers}. "
        f"Only {_ALLOWLISTED_TOP_LEVEL_GUARD_MODULE} is allowlisted, on the "
        "allowlist-vs-restructure analysis this file's module docstring "
        "records. A guard that is not on this list must not reach the record "
        "at all -- the floor is an absent reader, not a validated key."
    )

    allowlisted_path = _module_path(root, _ALLOWLISTED_TOP_LEVEL_GUARD_MODULE)
    allowlisted_cls = _module_class(allowlisted_path)
    assert allowlisted_cls == "advisory", (
        f"{_ALLOWLISTED_TOP_LEVEL_GUARD_MODULE} is allowlisted on the "
        f"assumption its CLASS is 'advisory'; it is now {allowlisted_cls!r}. "
        "Re-run the allowlist-vs-restructure analysis in this file's module "
        "docstring -- the allowlisting was conditioned on this CLASS."
    )


# --- self-verification: the mechanism itself is red in both directions ------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestReaderClosureSelfVerification:
    """Proves reader_closure()'s red-in-both-directions property against a
    disposable fixture tree -- never the real repo -- per this file's own
    HARD CONSTRAINTS (the walk takes a root path for exactly this reason)."""

    def _base_fixture(self, tmp_path: Path) -> Path:
        root = tmp_path / "fixture_repo"
        _write(root / "coordinator_core" / "__init__.py", "")
        _write(root / "coordinator_core" / "session" / "__init__.py", "")
        _write(
            root / "coordinator_core" / "session" / "fleet_mode.py",
            "def read_fleet_mode():\n    return {}\n",
        )
        _write(
            root / "coordinator_core" / "session" / "mode_resolution.py",
            "from coordinator_core.session.fleet_mode import read_fleet_mode\n",
        )
        _write(root / "coordinator_core" / "unrelated" / "__init__.py", "")
        _write(
            root / "coordinator_core" / "unrelated" / "other.py",
            "import json\n",
        )
        return root

    def test_absent_reader_is_absent(self, tmp_path):
        root = self._base_fixture(tmp_path)
        closure = reader_closure(root)
        assert "coordinator_core.unrelated.other" not in closure

    def test_direct_reader_appears(self, tmp_path):
        root = self._base_fixture(tmp_path)
        _write(
            root / "coordinator_core" / "unrelated" / "direct_reader.py",
            "from coordinator_core.session.fleet_mode import read_fleet_mode\n",
        )
        closure = reader_closure(root)
        assert "coordinator_core.unrelated.direct_reader" in closure

    def test_indirect_reader_one_hop_away_appears(self, tmp_path):
        """The direct-import-only failure mode this walk is built to avoid:
        a module that imports a reader, not the record itself."""
        root = self._base_fixture(tmp_path)
        _write(
            root / "coordinator_core" / "unrelated" / "direct_reader.py",
            "from coordinator_core.session.fleet_mode import read_fleet_mode\n",
        )
        _write(
            root / "coordinator_core" / "unrelated" / "indirect_reader.py",
            "from coordinator_core.unrelated.direct_reader import read_fleet_mode\n",
        )
        closure = reader_closure(root)
        assert "coordinator_core.unrelated.indirect_reader" in closure

    def test_basename_only_reader_appears(self, tmp_path):
        root = self._base_fixture(tmp_path)
        _write(
            root / "coordinator_core" / "unrelated" / "basename_reader.py",
            'PATH = "fleet-mode.json"\n',
        )
        closure = reader_closure(root)
        assert "coordinator_core.unrelated.basename_reader" in closure

    def test_removed_reader_disappears(self, tmp_path):
        root = self._base_fixture(tmp_path)
        reader_path = root / "coordinator_core" / "unrelated" / "direct_reader.py"
        _write(
            reader_path,
            "from coordinator_core.session.fleet_mode import read_fleet_mode\n",
        )
        assert "coordinator_core.unrelated.direct_reader" in reader_closure(root)

        # Remove the import (the reader is deleted / no longer reaches the
        # record) -- the floor's second direction: a listed reader must be
        # able to fall OUT of the closure, or the walk is a one-way ratchet.
        _write(reader_path, "import json\n")
        assert "coordinator_core.unrelated.direct_reader" not in reader_closure(root)

    def test_expected_readers_tuple_is_not_derived_from_the_walk(self):
        """Guards against the exact vacuity this file's docstring names:
        EXPECTED_READERS must be assigned from a literal tuple of string
        constants, never a call into reader_closure() (or anything else
        computed). If this ever becomes `EXPECTED_READERS =
        reader_closure(...)`, the equality test above would pass forever."""
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(Path(__file__)))
        assign_node = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EXPECTED_READERS" for t in node.targets
            ):
                assign_node = node
                break
        assert assign_node is not None, "EXPECTED_READERS assignment not found"
        assert isinstance(assign_node.value, ast.Tuple), (
            "EXPECTED_READERS must be a literal tuple expression, not a "
            f"computed value ({type(assign_node.value).__name__})."
        )
        for element in assign_node.value.elts:
            assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
                "every EXPECTED_READERS element must be a literal string "
                f"constant, found {ast.dump(element)}"
            )
