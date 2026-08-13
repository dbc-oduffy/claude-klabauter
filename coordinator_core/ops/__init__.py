"""
coordinator_core.ops — IPC operation handlers package.

Purpose: Namespace for all coordinator_core op implementations. Each sub-module
self-registers its handler(s) via register_op() at import time. Importing this
package is the trigger that populates the op-registry before the IPC server
begins accepting connections — UNLESS lazy mode is active (see below), in which
case the eager import list is skipped and callers must trigger targeted
per-op imports themselves (see coordinator_core.ipc._lazy_import_and_lookup).

Op registration list is maintained in coordinator_core/op_scopes.py::_OP_KEY_SCOPE
(coordinator_core/ipc.py:441 only imports it from there).
Review: code-reviewer — replaced stale hand-enumeration (8 of 19+ ops) with a canonical
reference to _OP_KEY_SCOPE, which is kept current as each op lands.

Lazy op registration (F6 / claude-klabauter-windows-portability § C4): dispatching a single
op via coordinator_core.invoke used to unconditionally `import coordinator_core.ops`,
which (because Python always executes a package's __init__.py in full before any
of its submodules) forced ALL ~55 op modules to compile/import to run just ONE op
— a ~150-250ms Windows cold-compile tax per invoke (no __pycache__ warm-up there).

The eager-import list below is gated behind `_lazy_ops_requested()` (see its
docstring for the two channels and their precedence):
  - NEITHER CHANNEL ARMED (default) — behavior is UNCHANGED from before this
    chunk: importing this package (bare `import coordinator_core.ops`, as ~50
    existing test files do) eagerly imports every op module and populates the
    FULL registry, exactly as today. This preserves every existing consumer's
    contract untouched.
  - LAZY REQUESTED — the eager-import block is SKIPPED, so that a targeted
    per-op import (driven by coordinator_core.ops._registry_map.OP_MODULE_MAP
    via ipc.py's registry-miss path) is the ONLY import that happens — cheap,
    because this package's own __init__.py no longer forces the other 54
    modules to compile first. Armed by the one-shot command-type CLI dispatcher
    (coordinator_core.invoke.__main__) and by the shared trampoline seam
    (coordinator/bin/lib/cc_invoke.py), each for its OWN process only, before
    any op is looked up. Safe because both are short-lived processes with no
    other consumer depending on the full-eager side-effect (DR-215
    command-type execution model).

_eager_import_all() is also exposed for ipc.py's registry-miss SAFE FALLBACK: if
an op is absent from OP_MODULE_MAP (or a mapped import didn't register it — map
drift), the fallback calls this function directly to force full registration
regardless of the lazy flag or of whatever partial state this package is
already in — idempotent, since re-importing an already-imported submodule is a
cheap no-op. This is what makes an incomplete/stale map degrade to today's
correctness rather than to a broken dispatch.

Resilient-and-loud per-module import (2026-07-21 break-class fix — one op
module's ImportError used to abort ALL ~80 modules' registration in one shot;
demonstrated live when a concurrent session's single deleted symbol
(`sh_argv`, imported transitively) produced 146 pytest collection errors from
ONE missing name). _eager_import_all() now imports each module in
_EAGER_OP_MODULES independently: a single module's failure no longer prevents
the other ~79 from registering (§ resilience), but every failure is printed to
stderr immediately, by name, with the real exception (§ loudness) — see
"Negative-spec" below for the anti-pattern this deliberately avoids. Failed
modules are recorded in _POISONED_MODULES so that a later DISPATCH of one of
that module's ops (coordinator_core.ipc.dispatch_message, via OP_MODULE_MAP)
re-surfaces the ORIGINAL exception instead of a generic "Method not found" —
see coordinator_core/ipc.py's METHOD_NOT_FOUND branch for that half of the fix.

Failure-mode analysis (why this design, not a stricter one):
  - The package-init default path (neither lazy channel armed) is the one
    hit by test collection and any ad-hoc `import coordinator_core.ops` — this
    is exactly the path the reported defect broke, so it MUST be resilient:
    one broken module must never take ~8000 unrelated tests down with it.
  - The actual PRODUCTION dispatch path (the one-shot CLI, DR-215) already
    arms lazy registration and does a TARGETED import via
    OP_MODULE_MAP — it only reaches _eager_import_all() via the SAFE FALLBACK,
    and only for the one op being dispatched. Making package-init "fail hard"
    would not make production stricter (production doesn't take that path);
    it would only reintroduce the test-collision collapse this fix removes.
  - Production strictness instead lives at the DISPATCH boundary: an op whose
    owning module is poisoned still errors out on every call to that op — now
    with the real cause attached instead of a misleading "unknown op" — while
    ops in HEALTHY modules that used to be collaterally killed by the same
    import failure now correctly continue to work. That is a strict
    improvement over today, not added laxness: no op that worked before now
    silently no-ops, and no failure that used to be visible becomes invisible.

Spec backlink: docs/plans/2026-07-02-pcore-03-beachhead-coordinator-core.md § C1b
                docs/plans/2026-07-14-claude-klabauter-windows-portability.md § C4
                (2026-07-21 resilient-eager-import fix — no dedicated plan doc;
                PM-authorized same-day break-class fix, see session record)

Negative-spec (hard-won — do NOT reintroduce):
    - `try: import X\n    except ImportError: pass` (swallow-and-continue) was
      explicitly REJECTED. It trades a LOUD failure (today's total collapse)
      for a SILENT one: the module's ops just don't register, and a later
      `invoke` of one of them fails with a confusing generic "unknown op"
      instead of the real ImportError — strictly worse than collapse, because
      collapse at least tells you immediately and unambiguously. Every catch
      in this module prints the real module name + exception to stderr AND
      records it in _POISONED_MODULES so dispatch-time lookups can re-surface
      the true cause — resilience without silence.
"""

from __future__ import annotations

import importlib
import logging as _logging
import os as _os
import sys as _sys
import traceback as _traceback
from typing import Dict, List, Tuple

_logger = _logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Eager-import table: (dotted module path, human-readable "registers ..." note)
# One entry per op module that used to be a bare `from X import Y` statement.
# Kept as data (not individual import statements) so _eager_import_all() can
# wrap EACH import independently in its own try/except — a data-driven loop
# is the only way to get per-module isolation without ~80 duplicated
# try/except blocks. The note column preserves the original inline
# "registers ..." documentation from each import's trailing comment.
# ---------------------------------------------------------------------------
_EAGER_OP_MODULES: List[Tuple[str, str]] = [
    ("coordinator_core.ops.ping", 'registers "ping"'),
    ("coordinator_core.ops.coverage_gate", 'registers "coverage.gate"'),
    ("coordinator_core.ops.cutover_gate", 'registers "cutover.gate"'),
    ("coordinator_core.ops.cutover_advance", 'registers "cutover.advance"'),
    (
        "coordinator_core.ops.handoff_children",
        'registers "handoff.has_live_children" and "handoff.blocked_by_dependents"',
    ),
    ("coordinator_core.ops.artifact_emit", 'registers "artifact.emit"'),
    ("coordinator_core.hooks", "registers the 15 hooks.* ops (6 advisory + 8 bookkeeping + 1 arrival-check)"),
    (
        "coordinator_core.frontmatter.schema_cli",
        'registers "schema.describe", "schema.validate" (C7 byte-parity CLI + JSON-RPC ops)',
    ),
    ("coordinator_core.ops.emit.recorder", 'registers "backlog.record"'),
    ("coordinator_core.ops.emit_cadence", 'registers "emit.cadence"'),
    ("coordinator_core.ops.goal_append", 'registers "goal.append"'),
    ("coordinator_core.ops.goal_kr_status", 'registers "goal.set_kr_status"'),
    ("coordinator_core.ops.goal_close_day", 'registers "goal.close_day", "goal.close_day_apply"'),
    ("coordinator_core.orientation.regenerate_cache", 'registers "orientation.regenerate_cache"'),
    ("coordinator_core.ops.fleet.archive_plans", 'registers "fleet.archive_completed_plans"'),
    ("coordinator_core.ops.fleet.plan_handoffs", 'registers "fleet.handoffs_for_plan"'),
    ("coordinator_core.ops.fleet.archive_handoffs", 'registers "fleet.archive_completed_handoffs"'),
    ("coordinator_core.ops.fleet.prune_bugs", 'registers "fleet.prune_closed_bugs"'),
    ("coordinator_core.ops.reap_chain_ancestry_waivers", 'registers "chain_ancestry_waivers.reap"'),
    ("coordinator_core.ops.commit_anchors", 'registers "commit.anchors"'),
    ("coordinator_core.ops.memo_transition", 'registers "memo.transition"'),
    ("coordinator_core.ops.handoff_transition", 'registers "handoff.transition"'),
    ("coordinator_core.ops.handoff_stamp", 'registers "handoff.stamp"'),
    ("coordinator_core.ops.handoff_correct_body", 'registers "handoff.correct_body"'),
    ("coordinator_core.ops.handoff_discharge_criteria", 'registers "handoff.discharge_criteria"'),
    ("coordinator_core.ops.propagate_body", 'registers "handoff.propagate"'),
    ("coordinator_core.ops.handoff_phase_stamp", 'registers "handoff.stamp_phase"'),
    ("coordinator_core.ops.handoff_ship_archive", 'registers "handoff.ship_and_archive"'),
    ("coordinator_core.ops.handoff_archive_transition", 'registers "handoff.archive_transition"'),
    ("coordinator_core.ops.handoff_reconcile_close_terminal", 'registers "handoff.reconcile_close_terminal"'),
    ("coordinator_core.ops.handoff_backfill_claim_stamp", 'registers "handoff.backfill_claim_stamp"'),
    ("coordinator_core.ops.handoff_repoint_origin", 'registers "handoff.repoint_origin"'),
    ("coordinator_core.ops.handoff_normalize", 'registers "handoff.normalize"'),
    ("coordinator_core.ops.goals_match", 'registers "goal.match_candidates"'),
    ("coordinator_core.ops.plan_match", 'registers "plan.match_candidates"'),
    ("coordinator_core.ops.handoff_match", 'registers "handoff.match_candidates"'),
    ("coordinator_core.ops.initiatives_serve", 'registers "initiative.serve_set"'),
    ("coordinator_core.ops.roadmap_serve", 'registers "roadmap.serve"'),
    ("coordinator_core.ops.roadmap_link_stubs", 'registers "roadmap.link_stubs"'),
    ("coordinator_core.ops.queue_append", 'registers "queue.append"'),
    ("coordinator_core.ops.queue_close", 'registers "queue.close"'),
    ("coordinator_core.ops.queue_promote", 'registers "queue.promote"'),
    ("coordinator_core.ops.queue_cluster", 'registers "queue.cluster"'),
    ("coordinator_core.ops.queue_age_ping", 'registers "queue.age_ping"'),
    ("coordinator_core.ops.queue_scaffold_baton", 'registers "handoff.scaffold_from_queue"'),
    ("coordinator_core.ops.updatedocs_gates", 'registers "updatedocs.gates"'),
    ("coordinator_core.ops.fleet.memo_send", 'registers "memo.send"'),
    ("coordinator_core.ops.fleet.memo_list", 'registers "memo.list"'),
    ("coordinator_core.ops.fleet.memo_list_outbox", 'registers "memo.list_outbox"'),
    ("coordinator_core.ops.fleet.memo_check_addressee", 'registers "memo.check_addressee"'),
    ("coordinator_core.ops.fleet.memo_draft", 'registers "memo.draft" (C7 wiring)'),
    ("coordinator_core.ops.fleet.memo_compose", 'registers "memo.compose" (C7 wiring)'),
    ("coordinator_core.ops.fleet.memo_blitz_buckets", 'registers "memo.blitz_buckets"'),
    ("coordinator_core.ops.deliverable_rollup", 'registers "deliverable.rollup"'),
    (
        "coordinator_core.ops.deliverable_cascade",
        'registers "deliverable.cascade_terminal" (C6 terminal-state-propagation cascade)',
    ),
    (
        "coordinator_core.ops.sizing_decline",
        'registers "sizing.decline" (2026-08-10, single-target applier for the sizing-object '
        '`declined` terminal status)',
    ),
    (
        "coordinator_core.ops.cascade_backstop_sweep",
        'registers "deliverable.cascade_backstop_sweep" (C6c read-only backstop sweep, AC6d)',
    ),
    (
        "coordinator_core.ops.cascade_retract",
        'registers "deliverable.cascade_retract" (C6d retraction/revision, AC6f)',
    ),
    ("coordinator_core.ops.ceremony.wsc_tail", 'registers "ceremony.wsc_tail"'),
    (
        "coordinator_core.ops.ceremony.post_commit_tail",
        'registers "ceremony.post_commit_tail" (C3a, 2026-07-23 wsc-tail-slim-down: '
        "extraction of wsc_tail's steps 5c/5d into one standalone op, still invoked "
        "in-process by wsc_tail.py)",
    ),
    ("coordinator_core.ops.ceremony.session_instructions", 'registers "ceremony.session_instructions"'),
    ("coordinator_core.ops.ceremony.render_handoff_tracker", 'registers "ceremony.render_handoff_tracker"'),
    ("coordinator_core.session_ledger.aggregate_chain_loe", 'registers "session_ledger.aggregate_chain_loe"'),
    ("coordinator_core.ops.records_query", 'registers "records.query"'),
    (
        "coordinator_core.ops.handoff_columns_query",
        'registers "handoff.columns" (2026-08-11 pull-surface-four-columns C3 — '
        "batch-computed status/deployment_state/predecessor/shipped_in over live "
        "plus opt-in archived handoffs)",
    ),
    (
        "coordinator_core.ops.changelog_ops",
        'registers "changelog.append_day", "changelog.backfill_gaps", '
        '"changelog.compute_day_fields", "changelog.upsert_reviewed" (strang-10 A, DR-216)',
    ),
    ("coordinator_core.ops.cruft_sweep", ""),
    (
        "coordinator_core.ops.completion_ops",
        'registers "completion.reconcile_commits", "plan.append_session" (strang-10 B, DR-216)',
    ),
    ("coordinator_core.ops.review_trail_write", 'registers "review_trail.write" (strang-10 B, DR-216)'),
    (
        "coordinator_core.ops.review_trail_readjudication_report",
        'registers "review_trail.readjudication_report" '
        "(state/improvement-queue/2026-07-27-wire-review-trail-readjudication-report-628d6e5848a9.yaml)",
    ),
    (
        "coordinator_core.ops.review_freeze_diff",
        'registers "review.freeze_diff" (cross-repo/inbox/2026-07-23-claude-central-em-'
        "review-diff-freeze-op-wanted.md)",
    ),
    ("coordinator_core.ops.fleet.archive_shipped_handoffs", 'registers "fleet.archive_shipped_handoffs"'),
    ("coordinator_core.ops.fleet.archive_actioned_memos", 'registers "fleet.archive_actioned_memos"'),
    (
        "coordinator_core.ops.fleet.backfill_memo_disposition",
        'registers "fleet.backfill_dispositionless_memos"',
    ),
    ("coordinator_core.ops.fleet.reap_unintegrated_findings", 'registers "fleet.reap_unintegrated_findings"'),
    ("coordinator_core.ops.fleet.reap_integrated_findings", 'registers "fleet.reap_integrated_findings"'),
    ("coordinator_core.ops.session.reap", 'registers "session.reap", "session.reap_claims_for_repos"'),
    ("coordinator_core.ops.session.boot_sweep", 'registers "session.boot_sweep"'),
    (
        "coordinator_core.ops.session.sweep_consumed_handoffs",
        'registers "session.sweep_consumed_handoffs" (C21 on-demand single-family '
        "consumed-handoff sweep, wraps session.boot_sweep's own consumed-leg internal)",
    ),
    ("coordinator_core.ops.session.guard_settings_integrity", 'registers "session.guard_settings_integrity"'),
    ("coordinator_core.ops.session.record_pickup", 'registers "session.record_pickup"'),
    ("coordinator_core.ops.session.scope_report", 'registers "session.scope_report"'),
    ("coordinator_core.ops.handoff_author_fork", 'registers "handoff.author_fork"'),
    ("coordinator_core.ops.handoff_lineage_ancestry", 'registers "handoff.lineage_ancestry"'),
    ("coordinator_core.ops.plan_tasks_mutate", ""),
    ("coordinator_core.ops.plan_tasks_grouping_digest", 'registers "plan.tasks.grouping_digest"'),
    ("coordinator_core.ops.engine_drift", 'registers "engine.drift"'),
    ("coordinator_core.plugin_health.drift", 'registers "plugin_health.drift"'),
    ("coordinator_core.plugin_health.scan", 'registers "plugin_health.scan"'),
    ("coordinator_core.plugin_health.sentinel", 'registers "plugin_health.sentinel"'),
    ("coordinator_core.plugin_health.forwarder_drift", 'registers "plugin_health.forwarder_drift"'),
    ("coordinator_core.probes.fork_census", 'registers "probes.fork_census"'),
    ("coordinator_core.ops.cartography_tree", 'registers "cartography.tree"'),
    ("coordinator_core.ops.cartography_file_index", 'registers "cartography.file_index"'),
    ("coordinator_core.ops.cartography_churn", 'registers "cartography.churn"'),
    ("coordinator_core.ops.cartography_symbols", 'registers "cartography.symbols"'),
    ("coordinator_core.ops.cartography_edges", 'registers "cartography.edges", "cartography.count_references"'),
    ("coordinator_core.ops.cartography_op_edges", 'registers "cartography.op_edges"'),
    ("coordinator_core.ops.memo_triage", 'registers "memo.triage"'),
    ("coordinator_core.ops.distill_scope", 'registers "distill.scope"'),
    ("coordinator_core.ops.distill_workflow_input", 'registers "distill.workflow_input"'),
    ("coordinator_core.ops.workflow_validate", 'registers "workflow.validate"'),
    ("coordinator_core.ops.workflow_scaffold", 'registers "workflow.scaffold"'),
    ("coordinator_core.ops.strategic_generate", 'registers "strategic.generate"'),
    ("coordinator_core.ops.strategic_emit", 'registers "strategic.emit"'),
    ("coordinator_core.ops.handoff_reconcile", 'registers "handoff.reconcile_open"'),
    ("coordinator_core.ops.handoff_close_origin_stub", 'registers "handoff.close_origin_stub"'),
    ("coordinator_core.ops.session_hierarchy_derive", 'registers "session_hierarchy.derive"'),
    ("coordinator_core.goals.reassess_krs", ""),
    ("coordinator_core.ops.testing_full_runner", 'registers "testing.full_runner"'),
    ("coordinator_core.ops.deferral_detect_orphan_memo", 'registers "deferral.detect_orphan_memo"'),
    ("coordinator_core.ops.deferral_detect_partial_strangle", 'registers "deferral.detect_partial_strangle"'),
    ("coordinator_core.ops.fleet.archive_release_accumulator", 'registers "fleet.archive_release_accumulator"'),
    ("coordinator_core.ops.fleet.archive_paper_trail", 'registers "fleet.archive_paper_trail"'),
    ("coordinator_core.ops.fleet.archive_queue_entry", 'registers "fleet.archive_queue_entry"'),
    ("coordinator_core.ops.fleet.migrate_handoff_vocabulary", 'registers "fleet.migrate_handoff_vocabulary"'),
    (
        "coordinator_core.ops.orphan_branch_sweep",
        'registers "git_branch.compute_descendant_tip", "git_branch.detect_unpushed_commits", '
        '"git_branch.list_unmerged_work", "git_branch.verify_commit_in_review_window"',
    ),
    ("coordinator_core.ops.bootstrap_repo", 'registers "repo_setup.validate_target_root"'),
    ("coordinator_core.ops.ensure_python3_exe_shim", 'registers "install.detect_python3_appx_stub"'),
    ("coordinator_core.ops.draft_plan_aging", 'registers "plan.list_stale_executing", "plan.list_orphaned"'),
    ("coordinator_core.ops.ceremony.scoped_git_commit", 'registers "ceremony.scoped_git_commit"'),
    ("coordinator_core.ops.ceremony.chunk_commits", 'registers "ceremony.chunk_commits"'),
    ("coordinator_core.ops.self_persist_findings", 'registers "findings.self_persist_fallback"'),
    ("coordinator_core.ops.workday_stitch_sidecar_summary", 'registers "workday.stitch_sidecar_into_summary"'),
    ("coordinator_core.ops.workday_drain_pending_push", 'registers "workday.drain_pending_push"'),
    ("coordinator_core.ops.write_identity_file", 'registers "install.write_identity_file"'),
    ("coordinator_core.install.clone_sibling_repo", 'registers "install.clone_idempotent"'),
    (
        "coordinator_core.install.prereq_probe",
        'registers "install.probe_skill_frontmatter_valid", "install.probe_windows_terminal_presence"',
    ),
    ("coordinator_core.install.shell_rc_guard", 'registers "install.write_shell_rc_guard_block"'),
    ("coordinator_core.install.wrapper_onto_path", 'registers "install.wrapper_onto_path"'),
    (
        "coordinator_core.ops.assert_doctrine_cross_reference_counts",
        'registers "doctrine.assert_cross_reference_counts"',
    ),
    ("coordinator_core.ops.cartography_stack", 'registers "cartography.stack"'),
    ("coordinator_core.ops.cartography_chunk_table", 'registers "cartography.chunk_table"'),
    ("coordinator_core.ops.ceremony.snapshot_diff_and_head", 'registers "review.snapshot_diff_and_head"'),
    (
        "coordinator_core.ops.detect_changed_dependency_manifests",
        'registers "dependency.detect_changed_manifests"',
    ),
    ("coordinator_core.ops.detect_plugin_layout", 'registers "detect.plugin_layout"'),
    ("coordinator_core.ops.detect_primary_languages", 'registers "detect.primary_languages"'),
    ("coordinator_core.ops.init_anchor_injection_state", 'registers "ceremony.init_anchor_injection_state"'),
    (
        "coordinator_core.ops.lessons_filter",
        'registers "lessons.filter_undated_universal", "lessons.reject_orphan_strip_entries"',
    ),
    ("coordinator_core.ops.list_files_newer_than_marker", 'registers "percolate.list_files_newer_than_marker"'),
    ("coordinator_core.ops.merge_quiet_activity_gate", 'registers "merge.quiet_activity_gate"'),
    (
        "coordinator_core.ops.parse_cli_args",
        'registers "cli.parse_flag", "cli.parse_date_flags"',
    ),
    ("coordinator_core.ops.probe_fresh_repo_noop", 'registers "update_docs.probe_fresh_repo_noop"'),
    ("coordinator_core.ops.schema_drift_gate", 'registers "schema.drift_gate"'),
    (
        "coordinator_core.ops.release_tagging",
        'registers "release.cut_tag", "release.cut_tag_and_publish"',
    ),
    ("coordinator_core.ops.repo_bootstrap", 'registers "repo.clone_and_register"'),
    ("coordinator_core.ops.resolve_mcp_server_cli_path", 'registers "mcp.resolve_server_cli_path"'),
    ("coordinator_core.ops.resolve_swept_baton", 'registers "baton.resolve_swept_in_archive"'),
    ("coordinator_core.ops.run_pip_audit", 'registers "ci.run_pip_audit"'),
    ("coordinator_core.ops.run_semgrep_scan", 'registers "ci.run_semgrep_scan"'),
    ("coordinator_core.ops.run_shellcheck_sweep", 'registers "ci.run_shellcheck_sweep"'),
    ("coordinator_core.ops.scan_unresolved_ubt_records", 'registers "review_trail.scan_unresolved_ubt"'),
    (
        "coordinator_core.ops.verify_scout_inventory_completeness",
        'registers "research.verify_scout_inventory_completeness"',
    ),
    ("coordinator_core.ops.research_archive_workdir", 'registers "research.archive_workdir"'),
    (
        "coordinator_core.ops.research_dir_restructure",
        'registers "research.restructure_for_repeat_topic"',
    ),
    (
        "coordinator_core.ops.session.rotate_orphan_sweep_log",
        'registers "session.rotate_orphan_sweep_log"',
    ),
    ("coordinator_core.ops.create_github_remote", 'registers "repo.create_and_push_remote"'),
    (
        "coordinator_core.ops.merge_branch_into_workstream",
        'registers "branch.merge_into_workstream"',
    ),
    (
        "coordinator_core.ops.copy_plugin_template",
        'registers "repo_setup.copy_console_subprocess_tripwire"',
    ),
    (
        "coordinator_core.ops.workday_surface_auto_push_failure_stats",
        'registers "workday.surface_auto_push_failure_stats"',
    ),
    (
        "coordinator_core.ops.push_failure_verdict",
        'registers "git.push_failure_verdict"',
    ),
    (
        "coordinator_core.ops.verify_fix_files_changed",
        'registers "bug_sweep.verify_fix_files_changed"',
    ),
    ("coordinator_core.ops.hibernate_machine", 'registers "machine.hibernate"'),
    ("coordinator_core.ops.ceremony.commit_exec_bit", 'registers "commit.exec_bit_change"'),
    ("coordinator_core.ops.run_pre_ci_hooks", 'registers "percolate.run_pre_ci_hooks"'),
    ("coordinator_core.ops.scan_content_leakage", 'registers "percolate.scan_content_leakage_tiers"'),
    (
        "coordinator_core.ops.session.resolve_chain_terminal_disposition",
        'registers "session.resolve_chain_terminal_disposition"',
    ),
    ("coordinator_core.ops.resolve_baton_path", 'registers "baton.resolve_path_and_repo"'),
    ("coordinator_core.ops.poll_scratch_dir", 'registers "fanout.poll_scratch_dir"'),
    ("coordinator_core.ops.scratchpad_sweep", 'registers "scratchpad.sweep"'),
    ("coordinator_core.ops.memo_fate_partition", 'registers "memo.fate_partition"'),
    ("coordinator_core.ops.memo_fate_backfill", 'registers "memo.fate_backfill"'),
    ("coordinator_core.ops.distill_curation_status", 'registers "distill.curation_status"'),
    (
        "coordinator_core.ops.distill_disposal_manifest",
        'registers "distill.assemble_disposal_manifest"',
    ),
    ("coordinator_core.ops.distill_stamp_disposal", 'registers "distill.stamp_disposal"'),
    ("coordinator_core.ops.distill_apply_disposal", 'registers "distill.apply_disposal"'),
    ("coordinator_core.ops.crossrepo_closure_status", 'registers "crossrepo.closure_status"'),
    ("coordinator_core.ops.ceremony.update_docs_scan", 'registers "ceremony.update_docs_scan"'),
    (
        "coordinator_core.ops.tracker.advance_status",
        'registers "tracker.advance_status" (provisional — see module docstring\'s '
        '"Provisional classification" section: no ratified DR carve-out yet)',
    ),
    (
        "coordinator_core.ops.tracker.fold_observed_set",
        'registers "tracker.fold_observed_set" (sat-01b C5, DR-241-affirmed; '
        "actuated from session.boot_sweep, opt-in-by-existence only)",
    ),
    (
        "coordinator_core.ops.tracker.mint_person",
        'registers "tracker.mint_person" (sat-06 C4, DR-241-affirmed producer-'
        "facing op that mints a person through the sovereign-tracker person "
        "registry, per-repo, no cross-tree write)",
    ),
    ("coordinator_core.ops.priority_set", 'registers "priority.set"'),
    ("coordinator_core.ops.priority_drain", 'registers "priority.drain"'),
    ("coordinator_core.ops.distill_curate_clusters", 'registers "distill.curate_clusters"'),
    (
        "coordinator_core.ops.write_surface_manifest",
        'registers "write_surface.emit_manifest"',
    ),
    (
        "coordinator_core.ops.diagnostics_probes",
        'registers "diagnostics.always_succeeds", "diagnostics.always_refuses", '
        '"diagnostics.always_structural_pin" (write-free transport-failure probes)',
    ),
]

# module dotted-path -> the exception raised the last time we tried to import
# it. Populated by _eager_import_all() on a per-module ImportError/Exception;
# cleared on a subsequent successful import of that same module (self-healing
# if the module is fixed mid-process, e.g. under pytest --looponfail). Read by
# coordinator_core.ipc's dispatch_message to turn a registry MISS on a
# poisoned module's op into the real cause instead of a generic "Method not
# found" (see ipc.py's METHOD_NOT_FOUND branch).
_POISONED_MODULES: Dict[str, BaseException] = {}


def get_poisoned_modules() -> Dict[str, BaseException]:
    """Return a shallow copy of {module dotted-path: last import exception}.

    Purpose: read-only seam for ipc.py's dispatch path to check whether a
    registry-miss op belongs to a module that failed to import, so it can
    re-surface the REAL cause instead of a generic "Method not found".
    """
    return dict(_POISONED_MODULES)


def _eager_import_all() -> None:
    """Import every production op module, firing each one's register_op(...)
    side-effect. This is the exact set of imports that used to run
    unconditionally at package-init time; it is now also independently
    callable (by ipc.py's registry-miss fallback) to force full registration
    on demand, regardless of the COORDINATOR_CORE_LAZY_OPS flag or of which
    submodules (if any) are already imported.

    Resilient-and-loud (2026-07-21): each module is imported independently.
    A single module's import failure is:
      1. NOT allowed to prevent any other module from registering (resilience).
      2. Printed to stderr immediately, naming the module and the real
         exception (loudness) — never swallowed, never merely debug-logged.
      3. Recorded in _POISONED_MODULES so a later dispatch of one of that
         module's ops can raise the real cause (see coordinator_core.ipc).
    See the module docstring's "Negative-spec" for the try/except-pass
    anti-pattern this deliberately avoids.
    """
    for module_path, _note in _EAGER_OP_MODULES:
        try:
            importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 — intentional broad catch, see docstring
            _POISONED_MODULES[module_path] = exc
            # ERROR-severity logging call (§ FUNCTION gate C4C brief "make the
            # silent swallow observable") ALONGSIDE the pre-existing stderr
            # print below — control flow is UNCHANGED (still resilient: no
            # raise, every other module still gets its own import attempt).
            # This is purely about making a per-module import failure land
            # in anything that watches Python's logging machinery (e.g. a
            # log-aggregation handler attached to the root logger), which a
            # bare stderr print to an unread hermetic subprocess (§
            # `coordinator_core/percolate/engine.py` `run_function_gate`,
            # which only inspects stdout for "GATE_OK"/stderr for a
            # "GATE_FAIL:" marker it never emits here) does not reach.
            _logger.error(
                "coordinator_core.ops: FAILED to import %r (%s: %s) — its "
                "op(s) will NOT be registered",
                module_path,
                type(exc).__name__,
                exc,
            )
            print(
                f"coordinator_core.ops: FAILED to import {module_path!r} "
                f"({type(exc).__name__}: {exc}) — its op(s) will NOT be "
                f"registered; the other {len(_EAGER_OP_MODULES) - 1} op "
                f"modules are unaffected. Dispatching any op owned by this "
                f"module will re-raise this exact cause instead of "
                f"'unknown op'.",
                file=_sys.stderr,
            )
            _traceback.print_exc(file=_sys.stderr)
        else:
            _POISONED_MODULES.pop(module_path, None)


def _lazy_ops_requested() -> bool:
    """Whether eager op registration is suppressed for this process.

    Two channels, environment-first:

      - `COORDINATOR_CORE_LAZY_OPS` is the OPERATOR override, settable from
        outside the process and authoritative in BOTH directions — "1" forces
        lazy, any other value forces eager, whatever the in-process channel
        says. `coordinator/tests/test_install_substrate.sh` Test 15 depends on
        the eager direction: it compares a trampoline's output under
        `LAZY_OPS=1` against a `LAZY_OPS=0` baseline, and an in-process channel
        able to override "0" would turn that into lazy-vs-lazy — a test that
        passes while detecting nothing.
      - `sys._coordinator_core_lazy_ops` is the IN-PROCESS channel, set by the
        two writers that only ever need lazy registration for their own
        process: coordinator_core.invoke.__main__ (the one-shot CLI dispatch,
        DR-215) and coordinator/bin/lib/cc_invoke.py (the shared trampoline
        seam). Neither can import the other, so the attribute name is repeated
        at each writer rather than shared from here — this reader must not be
        importable-before-arming, which is the whole ordering constraint.

    Why the in-process channel is not an environment variable (2026-07-28):
    both writers used to set `os.environ`, which every child spawned without an
    explicit `env=` inherits. 59 test modules in this tree assert the op
    registry at import time, so any pytest child of such a process failed
    collection on a green tree. A `sys` attribute is inherited by nothing,
    making the leak structurally impossible instead of stripped per spawn site.
    Scoping study: docs/research/2026-07-28-lazy-ops-import-side-effect-scope.md § 6 (c).
    """
    operator_override = _os.environ.get("COORDINATOR_CORE_LAZY_OPS")
    if operator_override is not None:
        return operator_override == "1"
    return getattr(_sys, "_coordinator_core_lazy_ops", False) is True


# Default behavior (neither channel armed) — UNCHANGED from before this chunk:
# importing this package eagerly registers every op.
if not _lazy_ops_requested():
    _eager_import_all()
