"""
coordinator_core.ops — IPC operation handlers package.

Purpose: Namespace for all coordinator_core op implementations. Each sub-module
self-registers its handler(s) via register_op() at import time. Importing this
bare package NEVER populates the op-registry — registration is lazy
unconditionally, with no flag or channel to arm it: a caller must trigger a
targeted per-op import itself (see coordinator_core.ipc._lazy_import_and_lookup),
or call _eager_import_all() directly for the rare full-registration need.

Op registration list is maintained in coordinator_core/op_scopes.py::_OP_KEY_SCOPE
(coordinator_core/ipc.py:441 only imports it from there).
Review: code-reviewer — replaced stale hand-enumeration (8 of 19+ ops) with a canonical
reference to _OP_KEY_SCOPE, which is kept current as each op lands.

Lazy op registration (F6 / claude-klabauter-windows-portability § C4, made unconditional
2026-08-22 — the import-path-costs-nothing sprint): dispatching a single op via
coordinator_core.invoke used to unconditionally `import coordinator_core.ops`,
which (because Python always executes a package's __init__.py in full before any
of its submodules) forced ALL ~55 op modules to compile/import to run just ONE op
— a ~150-250ms Windows cold-compile tax per invoke (no __pycache__ warm-up there).
A two-channel flag (`COORDINATOR_CORE_LAZY_OPS` env var / `sys._coordinator_core_lazy_ops`
in-process attribute) used to gate this package's eager-import block; both
channels, and the writers that armed the in-process one, are retired — every
consumer of a bare `import coordinator_core.ops` (including the ~50 test modules
that assert the registry at import time) now goes through the targeted-import or
SAFE FALLBACK paths below instead of relying on package-init to populate the
registry as a side effect.

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
  - The _eager_import_all() path is the one reached by ipc.py's registry-miss
    SAFE FALLBACK and by the handful of callers that force full registration
    explicitly (the census enumerators, the warm server's preload) — this is
    exactly the path the reported defect broke, so it MUST be resilient: one
    broken module must never take ~8000 unrelated tests down with it. Before
    2026-08-22 this was the package-init default, reached whenever neither lazy
    channel was armed; package-init now registers nothing at all.
  - The actual PRODUCTION dispatch path (the one-shot CLI, DR-215) does a
    TARGETED import via OP_MODULE_MAP — it needs no arming, since lazy is the
    only mode; it reaches _eager_import_all() only via the SAFE FALLBACK, and
    only for the one op being dispatched. Making package-init "fail hard"
    would not make production stricter (production doesn't take that path);
    it would only reintroduce the test-collision collapse this fix removes.
  - Production strictness instead lives at the DISPATCH boundary: an op whose
    owning module is poisoned still errors out on every call to that op — now
    with the real cause attached instead of a misleading "unknown op" — while
    ops in HEALTHY modules that used to be collaterally killed by the same
    import failure now correctly continue to work. That is a strict
    improvement over today, not added laxness: no op that worked before now
    silently no-ops, and no failure that used to be visible becomes invisible.

Spec backlink: pln-pcore-03-beachhead-coordinator-core-fecdbb § C1b
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
    ("coordinator_core.ops.invoke_from_argv", 'registers "invoke.from_argv"'),
    ("coordinator_core.ops.cutover_gate", 'registers "cutover.gate"'),
    ("coordinator_core.ops.cutover_advance", 'registers "cutover.advance"'),
    (
        "coordinator_core.ops.handoff_children",
        'registers "handoff.blocked_by_dependents" (handoff.has_live_children was '
        'DELETED 2026-08-27, kill ledger K-113; the module stays eager for the '
        'surviving op and for the undecorated compute in-process callers use)',
    ),
    (
        "coordinator_core.hooks",
        "coordinator_core.hooks is ITSELF lazy (2026-08-22, mirroring this package's own "
        "C6 retirement) -- a bare import of the package registers nothing; only its own "
        "_eager_import_all() forces its 19 hooks.* ops to register. Handled as a special "
        "case in the loop below, not a plain importlib.import_module() call.",
    ),
    (
        "coordinator_core.frontmatter.schema_cli",
        'registers "schema.describe", "schema.validate" (C7 byte-parity CLI + JSON-RPC ops)',
    ),
    ("coordinator_core.ops.emit.recorder", 'registers "backlog.record"'),
    ("coordinator_core.ops.goal_append", 'registers "goal.append"'),
    ("coordinator_core.ops.goal_kr_status", 'registers "goal.set_kr_status"'),
    ("coordinator_core.ops.goal_close_day", 'registers "goal.close_day", "goal.close_day_apply"'),
    ("coordinator_core.orientation.regenerate_cache", 'registers "orientation.regenerate_cache"'),
    ("coordinator_core.ops.fleet.plan_handoffs", 'registers "fleet.handoffs_for_plan"'),
    ("coordinator_core.ops.fleet.work_state", 'registers "fleet.work_state"'),
    ("coordinator_core.ops.fleet.record_history", 'registers "fleet.record_history"'),
    ("coordinator_core.ops.fleet.archive_terminal_handoffs", 'registers "fleet.archive_completed_handoffs"'),
    ("coordinator_core.ops.handoff_housekeeping", 'registers "handoff.housekeeping"'),
    ("coordinator_core.ops.fleet.capability_index", 'registers "fleet.aggregate_capability_index"'),
    ("coordinator_core.ops.fleet.sweep_status", 'registers "fleet.archive_sweep_status"'),
    ("coordinator_core.ops.fleet.archive_actioned_memos", 'registers "fleet.archive_actioned_memos"'),
    ("coordinator_core.ops.fleet.mode_control", 'registers "fleet.mode_set", "fleet.mode_show"'),
    ("coordinator_core.ops.commit_anchors", 'registers "commit.anchors"'),
    ("coordinator_core.ops.ceremony.commit_exec_bit", 'registers "commit.exec_bit_change"'),
    ("coordinator_core.ops.ceremony.commit_v2", 'registers "ceremony.commit_v2"'),
    ("coordinator_core.ops.memo_transition", 'registers "memo.transition"'),
    ("coordinator_core.ops.handoff_transition", 'registers "handoff.transition"'),
    ("coordinator_core.ops.handoff_stamp", 'registers "handoff.stamp"'),
    ("coordinator_core.ops.handoff_correct_body", 'registers "handoff.correct_body"'),
    ("coordinator_core.ops.handoff_discharge_criteria", 'registers "handoff.discharge_criteria"'),
    ("coordinator_core.ops.handoff_author_lint", 'registers "handoff.author_lint"'),
    ("coordinator_core.ops.handoff_append_session_ledger", 'registers "handoff.append_session_ledger"'),
    ("coordinator_core.ops.propagate_body", 'registers "handoff.propagate"'),
    ("coordinator_core.ops.handoff_phase_stamp", 'registers "handoff.stamp_phase"'),
    ("coordinator_core.ops.handoff_ship_archive", 'registers "handoff.ship_and_archive"'),
    ("coordinator_core.ops.handoff_backfill_claim_stamp", 'registers "handoff.backfill_claim_stamp"'),
    ("coordinator_core.ops.handoff_repoint_origin", 'registers "handoff.repoint_origin"'),
    ("coordinator_core.ops.handoff_normalize", 'registers "handoff.normalize"'),
    ("coordinator_core.ops.goals_match", 'registers "goal.match_candidates"'),
    ("coordinator_core.ops.plan_match", 'registers "plan.match_candidates"'),
    ("coordinator_core.ops.plan_capture_persist", 'registers "plan.persist_capture"'),
    ("coordinator_core.ops.handoff_match", 'registers "handoff.match_candidates"'),
    ("coordinator_core.ops.initiatives_serve", 'registers "initiative.serve_set"'),
    ("coordinator_core.ops.roadmap_link_stubs", 'registers "roadmap.link_stubs"'),
    ("coordinator_core.ops.queue_append", 'registers "queue.append"'),
    ("coordinator_core.ops.decision_record_mint",
     'registers "decision_record.mint_id" + "decision_record.release_id"'),
    ("coordinator_core.ops.peer_notice_send", 'registers "peer_notice.send"'),
    ("coordinator_core.ops.peer_notice_check", 'registers "peer_notice.check"'),
    ("coordinator_core.ops.queue_promote", 'registers "queue.promote"'),
    ("coordinator_core.ops.queue_cluster", 'registers "queue.cluster"'),
    ("coordinator_core.ops.queue_scaffold_baton", 'registers "handoff.scaffold_from_queue"'),
    ("coordinator_core.ops.updatedocs_gates", 'registers "updatedocs.gates"'),
    ("coordinator_core.ops.fleet.memo_list", 'registers "memo.list"'),
    ("coordinator_core.ops.fleet.memo_list_outbox", 'registers "memo.list_outbox"'),
    ("coordinator_core.ops.fleet.memo_check_addressee", 'registers "memo.check_addressee"'),
    ("coordinator_core.ops.fleet.memo_draft", 'registers "memo.draft" (C7 wiring)'),
    ("coordinator_core.ops.fleet.memo_compose", 'registers "memo.compose" (C7 wiring)'),
    ("coordinator_core.ops.fleet.memo_send", 'registers "memo.send" (rebuilt 2026-08-25, C2)'),
    ("coordinator_core.ops.fleet.memo_reconcile_outbox", 'registers "memo.reconcile_outbox"'),
    ("coordinator_core.ops.fleet.memo_blitz_buckets", 'registers "memo.blitz_buckets"'),
    ("coordinator_core.ops.push_outstanding", 'registers "push.outstanding"'),
    ("coordinator_core.ops.deliverable_rollup", 'registers "deliverable.rollup"'),
    ("coordinator_core.ops.delegation_check", 'registers "delegation.check"'),
    (
        "coordinator_core.ops.spec_backlink_resolve",
        'registers "spec_backlink.resolve", "spec_backlink.rewrite"',
    ),
    (
        "coordinator_core.ops.sizing_decline",
        'registers "sizing.decline" (2026-08-10, single-target applier for the sizing-object '
        '`declined` terminal status)',
    ),
    (
        "coordinator_core.ops.sizing_ship",
        'registers "sizing.ship" (2026-08-13, single-target applier for the sizing-object '
        '`shipped` terminal status when no plan was ever minted for the routed work)',
    ),
    (
        "coordinator_core.ops.sizing_spike_verdict",
        'registers "sizing.record_spike_verdict" (2026-08-14, single-target applier for '
        'the sizing-object `premise.spike_verdict` pointer — the missing producer for the '
        '`plan⇄spike` back-edge\'s trampoline gate)',
    ),
    (
        "coordinator_core.ops.cascade_backstop_sweep",
        'registers "deliverable.cascade_backstop_sweep" (C6c read-only backstop sweep, AC6d)',
    ),
    (
        "coordinator_core.ops.cascade_retract",
        'registers "deliverable.cascade_retract" (C6d retraction/revision, AC6f)',
    ),
    (
        "coordinator_core.ops.deliverable_fork_detect",
        'registers "deliverable.fork_detect" (C7 report-only slug-prefix fork-family '
        "detector, AC12 — reachable by name, from no boot/commit-path trigger)",
    ),
    (
        "coordinator_core.ops.ceremony.post_commit_tail",
        'registers "ceremony.post_commit_tail" (C3a, 2026-07-23 wsc-tail-slim-down: '
        "extraction of wsc_tail's steps 5c/5d into one standalone op, still invoked "
        "in-process by wsc_tail.py)",
    ),
    ("coordinator_core.session_ledger.aggregate_chain_loe", 'registers "session_ledger.aggregate_chain_loe"'),
    ("coordinator_core.ops.records_query", 'registers "records.query"'),
    ("coordinator_core.ops.record_history", 'registers "records.history"'),
    (
        "coordinator_core.ops.read_sizing_object_fields",
        'registers "sizing.read_object_fields" -- shipped PRESENT-BUT-DEAD: '
        "decorated and listed in `_registry_map.py`, but absent here, so "
        "`import coordinator_core.ops` never registered it and "
        "`coordinator-invoke sizing.read_object_fields` could not resolve it. "
        "Its own suite asserted registry membership and stayed GREEN "
        "throughout, because that test file imports the module directly and "
        "the decorator fires as an import side effect -- a guard or test that "
        "imports what it audits cannot observe declared-but-unreachable. "
        "Caught 2026-08-21 by `test_registry_fast_path_matches_live_registry`, "
        "not by the op's own tests.",
    ),
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
        # `completion.reconcile_commits` was struck from this annotation on
        # 2026-08-26. It was KILLED and rebuilt from scratch per PM ruling
        # 2026-08-23 (see that module's own docstring, which has said so since),
        # but the eager-import table went on advertising it for three days. A
        # registration table that names an op the registry does not serve is the
        # failure MEMORY.md records twice over -- a killed op name living on in a
        # string-keyed surface -- and here it was the surface a reader would
        # trust FIRST to learn what exists.
        'registers "completion.flip_to_released", "plan.append_session" (strang-10 B, DR-216)',
    ),
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
    (
        "coordinator_core.ops.fleet.backfill_memo_disposition",
        'registers "fleet.backfill_dispositionless_memos"',
    ),
    ("coordinator_core.ops.backfill_reference_edges", 'registers "fleet.backfill_reference_edges"'),
    ("coordinator_core.ops.fleet.reap_unintegrated_findings", 'registers "fleet.reap_unintegrated_findings"'),
    ("coordinator_core.ops.fleet.reap_integrated_findings", 'registers "fleet.reap_integrated_findings"'),
    ("coordinator_core.ops.session.reap", 'registers "session.reap", "session.reap_claims_for_repos", "session.audit_unreapable"'),
    ("coordinator_core.ops.session.guard_settings_integrity", 'registers "session.guard_settings_integrity"'),
    ("coordinator_core.ops.session.record_pickup", 'registers "session.record_pickup"'),
    ("coordinator_core.ops.session.scope_report", 'registers "session.scope_report"'),
    ("coordinator_core.ops.session.safe_commit_offer", 'registers "session.safe_commit_offer"'),
    ("coordinator_core.ops.session_resolve_address", 'registers "session.resolve_address"'),
    ("coordinator_core.ops.session_peer_roster", 'registers "session.peer_roster"'),
    ("coordinator_core.ops.session_work_state", 'registers "session.work_state"'),
    ("coordinator_core.ops.session_artifact_owner", 'registers "session.artifact_owner"'),
    ("coordinator_core.ops.handoff_author_fork", 'registers "handoff.author_fork"'),
    ("coordinator_core.ops.handoff_lineage_ancestry", 'registers "handoff.lineage_ancestry"'),
    ("coordinator_core.ops.plan_tasks_mutate", ""),
    ("coordinator_core.ops.plan_tasks_grouping_digest", 'registers "plan.tasks.grouping_digest"'),
    (
        "coordinator_core.ops.plan_tasks_spine_drift_check",
        'registers "plan.tasks.spine_drift_check" (read-only spine-vs-tree drift '
        "check, reusing close_out_and_stamp's commit-coverage oracle)",
    ),
    ("coordinator_core.ops.engine_drift", 'registers "engine.drift"'),
    ("coordinator_core.plugin_health.drift", 'registers "plugin_health.drift"'),
    ("coordinator_core.plugin_health.scan", 'registers "plugin_health.scan"'),
    ("coordinator_core.plugin_health.sentinel", 'registers "plugin_health.sentinel"'),
    ("coordinator_core.plugin_health.forwarder_drift", 'registers "plugin_health.forwarder_drift"'),
    ("coordinator_core.ops.cartography_tree", 'registers "cartography.tree"'),
    ("coordinator_core.ops.cartography_file_index", 'registers "cartography.file_index"'),
    ("coordinator_core.ops.cartography_symbols", 'registers "cartography.symbols"'),
    ("coordinator_core.ops.cartography_edges", 'registers "cartography.edges", "cartography.count_references"'),
    ("coordinator_core.ops.cartography_op_edges", 'registers "cartography.op_edges"'),
    ("coordinator_core.ops.memo_triage", 'registers "memo.triage"'),
    ("coordinator_core.ops.distill_scope", 'registers "distill.scope"'),
    ("coordinator_core.ops.distill_workflow_input", 'registers "distill.workflow_input"'),
    ("coordinator_core.ops.workflow_validate", 'registers "workflow.validate"'),
    ("coordinator_core.ops.workflow_scaffold", 'registers "workflow.scaffold"'),
    ("coordinator_core.ops.compute_layer_scaffold.op", 'registers "compute_layer.scaffold"'),
    ("coordinator_core.ops.dispatch_emit.op", 'registers "dispatch.emit"'),
    (
        "coordinator_core.ops.workflow_fire.op",
        'registers "workflow.fire", "workflow.fire_status"',
    ),
    ("coordinator_core.ops.review_mint.op", 'registers "review.mint_workflow"'),
    ("coordinator_core.ops.strategic_generate", 'registers "strategic.generate"'),
    ("coordinator_core.ops.strategic_emit", 'registers "strategic.emit"'),
    ("coordinator_core.ops.handoff_close_origin_stub", 'registers "handoff.close_origin_stub"'),
    ("coordinator_core.ops.session_hierarchy_derive", 'registers "session_hierarchy.derive"'),
    ("coordinator_core.goals.reassess_krs", ""),
    ("coordinator_core.ops.deferral_detect_orphan_memo", 'registers "deferral.detect_orphan_memo"'),
    ("coordinator_core.ops.deferral_detect_partial_strangle", 'registers "deferral.detect_partial_strangle"'),
    ("coordinator_core.ops.fleet.archive_release_accumulator", 'registers "fleet.archive_release_accumulator"'),
    ("coordinator_core.ops.fleet.archive_paper_trail", 'registers "fleet.archive_paper_trail"'),
    ("coordinator_core.ops.fleet.archive_queue_entry", 'registers "fleet.archive_queue_entry"'),
    ("coordinator_core.ops.fleet.migrate_handoff_vocabulary", 'registers "fleet.migrate_handoff_vocabulary"'),
    ("coordinator_core.ops.fleet.archive_sizings", 'registers "fleet.archive_terminal_sizings"'),
    (
        "coordinator_core.ops.orphan_branch_sweep",
        'registers "git_branch.compute_descendant_tip", "git_branch.detect_unpushed_commits", '
        '"git_branch.list_unmerged_work", "git_branch.verify_commit_in_review_window"',
    ),
    ("coordinator_core.ops.bootstrap_repo", 'registers "repo_setup.validate_target_root"'),
    ("coordinator_core.ops.ensure_python3_exe_shim", 'registers "install.detect_python3_appx_stub"'),
    ("coordinator_core.ops.draft_plan_aging", 'registers "plan.list_stale_executing", "plan.list_orphaned"'),
    ("coordinator_core.ops.plan_suggest_completion_steps", 'registers "plan.suggest_completion_steps"'),
    ("coordinator_core.ops.ceremony.chunk_commits", 'registers "ceremony.chunk_commits"'),
    ("coordinator_core.ops.session_commits", 'registers "session.commits"'),
    ("coordinator_core.ops.session_baton_mint", 'registers "session_baton.mint"'),
    ("coordinator_core.ops.session_baton_promote", 'registers "session_baton.promote"'),
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
        "no longer actuated from session.boot_sweep as of the C3/C5 boot-backstop "
        "rebuild, docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md — "
        "relocated to a ceremony-gate call site (coordinator-claude side, per "
        "C3's cross-repo-memo wiring), opt-in-by-existence only)",
    ),
    (
        "coordinator_core.ops.tracker.mint_person",
        'registers "tracker.mint_person" (sat-06 C4, DR-241-affirmed producer-'
        "facing op that mints a person through the sovereign-tracker person "
        "registry, per-repo, no cross-tree write)",
    ),
    (
        "coordinator_core.ops.tracker.assign",
        'registers "tracker.assign" (the first production caller of the '
        "item_person edge — writes/retracts through tracker_entities' "
        "existing emit_item_person_added/emit_item_person_retracted, "
        "per-repo, no cross-tree write)",
    ),
    (
        "coordinator_core.ops.tracker.render_status",
        'registers "tracker.render_status" (sat-06 C3 — the read-only status '
        "projection, classified MUTATING by DR-241's 2026-08-20 amendment as "
        "conservative-by-construction rather than descriptive)",
    ),
    (
        "coordinator_core.ops.tracker.completion_policy",
        'registers "tracker.assert_code_complete" (C11 — DR-318 D2\'s routed '
        "op surface for sat-04's tracker_completion_policy; wraps "
        "emit_code_complete_assert, a real write via tracker_transitions."
        "emit_transition, classified MUTATING on the merits)",
    ),
    (
        "coordinator_core.ops.tracker.push_suggestion",
        'registers "tracker.push_suggestion" (sat-06 C4 — the producer-'
        "facing write op: resolves ownership via tracker_holder."
        "write_root_for, then routes a local write through the store's own "
        "append entrypoint "
        "vs a DR-338 delivery envelope committed into a peer repo's "
        "cross-repo/inbox/, never a direct cross-tree open())",
    ),
    (
        "coordinator_core.ops.tracker.fold_ownership",
        'registers "tracker.fold_ownership" (a read op answering who owns/is '
        "assigned one item — folds item_person retractions and person_merged "
        "resolution via tracker_projection, classified MUTATING per DR-241's "
        "2026-08-20 conservative-by-construction amendment)",
    ),
    ("coordinator_core.ops.priority_set", 'registers "priority.set"'),
    ("coordinator_core.ops.priority_drain", 'registers "priority.drain"'),
    ("coordinator_core.ops.distill_curate_clusters", 'registers "distill.curate_clusters"'),
    (
        "coordinator_core.ops.diagnostics_probes",
        'registers "diagnostics.always_succeeds", "diagnostics.always_refuses", '
        '"diagnostics.always_structural_pin" (write-free transport-failure probes)',
    ),
    (
        "coordinator_core.ops.gate_validate_invocable",
        'registers "gate.validate_invocable" (merge-gate DoD checker skeleton, '
        "docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C1)",
    ),
    (
        "coordinator_core.ops.gate_liveness.resolve",
        'registers "gate_liveness.resolve" (external_gate closure_key join '
        "reader, docs/plans/2026-08-21-a-discharged-gate-tells-the-row-waiting.md § C1)",
    ),
    (
        "coordinator_core.ops.gate_liveness.reconcile",
        'registers "gate_liveness.reconcile" (dry-run-default, precondition-'
        "checked external_gate cleared: true flip through stamp, "
        "docs/plans/2026-08-21-a-discharged-gate-tells-the-row-waiting.md § C2)",
    ),
    (
        "coordinator_core.ops.cmd_autorun_guard",
        'registers "install.detect_cmd_autorun_coverage", '
        '"install.write_cmd_autorun_guard", "install.strip_cmd_autorun_guard" '
        "(cmd.exe AutoRun coverage-gap probe/write/strip)",
    ),
    (
        "coordinator_core.ops.app_session",
        'registers "app_session.launch", "app_session.census", '
        '"app_session.teardown" (docs/plans/2026-08-15-app-session-launch-'
        "census-teardown-ops.md § C3)",
    ),
    (
        "coordinator_core.ops.op_budget_breaches",
        'registers "op_census.breaches" (the budget-breach surface — DR-344-the-'
        "brightline-process-budget-for-claude-klabauter.md)",
    ),
    (
        "coordinator_core.ops.warm_guard_evaluate",
        'registers "warm_guard.evaluate" (the warm-side bash-guard chain — state/'
        "handoffs/2026-08-23-the-warm-guard-op-gets-registered.md)",
    ),
    (
        "coordinator_core.merge_assemble.ops",
        'registers "merge_assemble.apply" (chunk C6, '
        "docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md). "
        'merge_assemble.brief was DELETED 2026-08-27 (kill ledger K-114) and is '
        "struck from this annotation: the module stays eager for apply, and an "
        "advertised-but-absent op reads to the annotation guard as a name "
        "committed ahead of its op.",
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
    on demand, regardless of which submodules (if any) are already imported.
    (Before 2026-08-22 this also read "regardless of the
    COORDINATOR_CORE_LAZY_OPS flag"; there is no such flag now.)

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
            if module_path == "coordinator_core.hooks":
                # This sub-package is itself lazy — a bare import registers
                # none of its 19 hooks.* ops. Force ITS eager-import routine
                # rather than just importing the package (see the entry's
                # note above and coordinator_core/hooks/__init__.py's own
                # _eager_import_all() docstring).
                hooks_module = importlib.import_module(module_path)
                hooks_module._eager_import_all()
                for poisoned_path, poisoned_exc in hooks_module.get_poisoned_modules().items():
                    _POISONED_MODULES[poisoned_path] = poisoned_exc
            else:
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


# Lazy is the only mode: importing this bare package never eagerly registers
# any op. The former `_lazy_ops_requested()` gate (COORDINATOR_CORE_LAZY_OPS
# env var / sys._coordinator_core_lazy_ops in-process attribute) is retired —
# there is no longer a flag to read or a channel to arm, so no conditional
# call to _eager_import_all() happens here. Callers reach registration
# through the targeted per-op import (ipc.py's registry-miss path) or, for the
# rare full-registration need, by calling _eager_import_all() directly.
