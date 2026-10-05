"""A bare wiki-path citation that does not resolve in this repo fails unless baselined.

A bare `docs/wiki/<page>.md` names this repo's wiki; a coordinator-content-repo page is cited
`coordinator-content-repo coordinator/docs/wiki/<subdir>/<page>.md`. The baseline holds the
`<file>::<page-path>` residue (test fixtures, non-docstring string literals, pages absent
from DoE too); it only shrinks. Needs no DoE clone.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

REPO_ROOT = Path(__file__).resolve().parents[2]
_THIS = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
_WIKI = "docs" + "/wiki/"
_CITATION = re.compile(r"(?<![\w/.\-])" + re.escape(_WIKI) + r"((?:[\w.\-]+/)*[\w.\-]+?\.md)")

BASELINE = frozenset(
    {
        "coordinator/bin/check-bin-sh-polyglot.py::cross-platform-shell-portability.md",
        "coordinator/bin/check-doctrine-citations.py::foo.md",
        "coordinator/bin/check-doctrine-citations.py::x.md",
        "coordinator/bin/coordinator-doc-new.py::lessons-outbox-schema.md",
        "coordinator/bin/coordinator-doc-new.py::spinoff-handoffs.md",
        "coordinator/bin/coordinator-doc-new.py::writing-plans.md",
        "coordinator/bin/coordinator-lesson-promote.py::coordinator/skills/pickup/SKILL.md",
        "coordinator/bin/coordinator-lesson-promote.py::foo.md",
        "coordinator/bin/coordinator-lesson-promote.py::lessons-outbox-schema.md",
        "coordinator/bin/coordinator-lesson-promote.py::some-wiki.md",
        "coordinator/bin/coordinator-queue-append.py::cross-repo-commitments-schema.md",
        "coordinator/bin/fan-out-dispatch.py::dispatching-parallel-agents.md",
        "coordinator/bin/gen-launcher-shim.py::install-surface-completeness.md",
        "coordinator/bin/lib/target_wiki_canon.py::coordinator/skills/pickup/SKILL.md",
        "coordinator/bin/lib/target_wiki_canon.py::foo.md",
        "coordinator/bin/lib/test_emit_lesson_summaries.py::lessons-born-attributable.md",
        "coordinator/bin/lib/test_emit_lesson_summaries.py::other.md",
        "coordinator/bin/lib/test_emit_lesson_summaries.py::test-lesson-a.md",
        "coordinator/bin/lib/test_emit_lesson_summaries.py::test.md",
        "coordinator/bin/publish.py::versioning-convention.md",
        "coordinator/bin/standup.py::machine-local-registry.md",
        "coordinator/bin/test_check_doctrine_citations.py::does-not-exist-anywhere.md",
        "coordinator/bin/test_check_doctrine_citations.py::foo.md",
        "coordinator/bin/test_check_doctrine_citations.py::only-in-coordinator.md",
        "coordinator/bin/test_check_doctrine_citations.py::only-in-claude-klabauter.md",
        "coordinator/bin/test_check_doctrine_citations.py::x.md",
        "coordinator/bin/test_coordinator_lesson_promote.py::brand-new-wiki.md",
        "coordinator/bin/test_coordinator_lesson_promote.py::concurrent-em-hazards.md",
        "coordinator/bin/test_coordinator_lesson_promote.py::executor-discipline.md",
        "coordinator/bin/test_coordinator_lesson_promote.py::roundtrip-multiline.md",
        "coordinator/bin/test_coordinator_lesson_promote.py::roundtrip-test.md",
        "coordinator/bin/test_coordinator_lesson_promote.py::some-real-target.md",
        "coordinator/bin/test_coordinator_lesson_promote.py::some.md",
        "coordinator/bin/test_coordinator_queue_append_parity.py::executor-discipline.md",
        "coordinator/bin/test_coordinator_queue_append_parity.py::some.md",
        "coordinator/bin/test_lessons_outbox_drain.py::learn-lessons-routing.md",
        "coordinator/bin/test_lessons_outbox_drain.py::some-doc.md",
        "coordinator/bin/test_lessons_outbox_drain.py::some-other-doc.md",
        "coordinator/bin/test_lessons_outbox_drain.py::test-design-discipline.md",
        "coordinator/bin/tests/test_coordinator_lesson_promote_prose.py::test-wiki.md",
        "coordinator/bin/tests/test_content_root_routing.py::test.md",
        "coordinator/bin/tests/test_harvest_content_root_machine_local_leg.py::some-central-doc.md",
        "coordinator/bin/tests/test_harvest_content_root_machine_local_leg.py::some-doctrine-doc.md",
        "coordinator/bin/tests/test_lesson_promote.py::coordinator-tripwires/a-nested-tripwire.md",
        "coordinator/bin/tests/test_lesson_promote.py::does-not-exist-yet.md",
        "coordinator/bin/tests/test_lesson_promote.py::skills-corpus/computed-skills.md",
        "coordinator/bin/tests/test_lesson_promote.py::target.md",
        "coordinator/bin/tests/test_lesson_promote.py::test-wiki.md",
        "coordinator/bin/tests/test_parallel_review_gate_decision.py::b.md",
        "coordinator/bin/tests/test_parallel_review_gate_decision.py::tiered-context-loading.md",
        "coordinator/bin/update-docs-probes.py::produce-not-prescribe.md",
        "coordinator/bin/workweek-start-goal-and-priorities.py::install-surface-completeness.md",
        "coordinator/lib/coordinator-artifact-subject.py::state-placement-law.md",
        "coordinator/lib/test_coordinator_artifact_subject.py::mcp-server-configuration.md",
        "coordinator/scripts/chain-walk.py::machine-local-registry.md",
        "coordinator/scripts/setup-github-auth-1password.py::github-auth-setup.md",
        "coordinator/tests/test_arrival_generate_claudemeta_manifest.py::another.md",
        "coordinator/tests/test_arrival_generate_claudemeta_manifest.py::keep-me.md",
        "coordinator/tests/test_check_no_monolith_completion_append.py::completion-log.md",
        "coordinator/tests/test_extract_scope_paths.py::some-doc.md",
        "coordinator/tests/test_lesson_promote_legacy_collision.py::test-wiki.md",
        "coordinator/tests/test_lesson_promote_node_enum.py::some-wiki.md",
        "coordinator/tests/test_lesson_promote_node_enum.py::test-wiki.md",
        "coordinator/tests/test_percolate_ignore_composition.py::private.md",
        "coordinator/tests/test_publish_allowlist_bin_row_trampoline_closure.py::py-glob-partition-silently-omits-extensionless-clis.md",
        "coordinator/tests/test_sync_plugin_wiki_mirror_guard.py::another-wiki.md",
        "coordinator/tests/test_sync_plugin_wiki_mirror_guard.py::test-wiki.md",
        "coordinator/tests/test_sync_plugin_wiki_mirror_guard.py::transitive-wiki.md",
        "coordinator/tests/test_verify_publish_targets_portable_sync.py::a.md",
        "coordinator/tests/test_verify_publish_targets_portable_sync.py::extra.md",
        "coordinator_core/artifact_subject.py::state-placement-law.md",
        "coordinator_core/attribution/tests/test_detector.py::some-topic.md",
        "coordinator_core/backlog_grind_assemble/tests/test_readers_debt_no_improvement_leg.py::foo.md",
        "coordinator_core/bash_guards/commit_tripwires.py::cross-platform-shell-portability.md",
        "coordinator_core/bash_guards/guard_offer_git_c.py::tool-output-flakiness-protocol.md",
        "coordinator_core/bash_guards/tests/guard_message_corpus.py::governed-thing.md",
        "coordinator_core/bash_guards/tests/test_firing_shape_gate.py::dispatching-parallel-agents.md",
        "coordinator_core/bash_guards/tests/test_governed_surfaces_manifest_miss.py::x.md",
        "coordinator_core/citation_graph.py::some-page.md",
        "coordinator_core/distill/tests/test_delete_guard.py::captured.md",
        "coordinator_core/distill/tests/test_delete_guard.py::does-not-exist.md",
        "coordinator_core/distill/tests/test_delete_guard.py::empty-capture.md",
        "coordinator_core/distill/tests/test_harvest_debt.py::foo.md",
        "coordinator_core/distill/tests/test_log_normalize.py::agent-hierarchy.md",
        "coordinator_core/distill/tests/test_log_normalize.py::harvest-target.md",
        "coordinator_core/distill/tests/test_log_normalize.py::second-batch.md",
        "coordinator_core/distill/tests/test_log_normalize.py::some-guide.md",
        "coordinator_core/distill/tests/test_log_normalize.py::x.md",
        "coordinator_core/distill/tests/test_log_normalize.py::y.md",
        "coordinator_core/distill/tests/test_manifest_schema.py::some-guide.md",
        "coordinator_core/execute_plan_assemble/tests/test_close_out_and_stamp.py::widget.md",
        "coordinator_core/frontmatter/schema_validate.py::schema-version-gate.md",
        "coordinator_core/frontmatter/tests/test_schema_validate.py::writing-plans.md",
        "coordinator_core/hooks/coordinator_reminder.py::delegate-execution.md",
        "coordinator_core/hooks/runtime_tripwire_em_check.py::coordinator-tripwires/related.md",
        "coordinator_core/hooks/support/tests/test_support_runners.py::foo.md",
        "coordinator_core/hooks/support/tests/test_support_runners.py::x.md",
        "coordinator_core/install/sandbox_check.py::install-surface-completeness.md",
        "coordinator_core/install/tests/test_receipt.py::foo.md",
        "coordinator_core/ops/check_machine_local_regeneratability.py::install-surface-completeness.md",
        "coordinator_core/ops/check_machine_local_regeneratability.py::machine-local-registry.md",
        "coordinator_core/ops/check_version_consistency.py::versioning-convention.md",
        "coordinator_core/ops/coordinator_setup_state.py::coordinator-setup-state-receipt.md",
        "coordinator_core/ops/dispatch_emit/tests/test_wave_map.py::x.md",
        "coordinator_core/ops/doc_content_verify.py::x.md",
        "coordinator_core/ops/edit_live_hook.py::concurrent-em-hazards.md",
        "coordinator_core/ops/emit_artifact_shape_contract.py::canonical-artifact-shapes.md",
        "coordinator_core/ops/fleet/tests/test_delete_superseded_decisions.py::note.md",
        "coordinator_core/ops/fleet/tests/test_delete_superseded_decisions.py::stray.md",
        "coordinator_core/ops/generate_exec_summary.py::exec-summary-artifact.md",
        "coordinator_core/ops/list_reverse_drift_cmds.py::machine-local-registry.md",
        "coordinator_core/ops/memo/tests/test_memo_transition_distill_fate.py::some-topic.md",
        "coordinator_core/ops/test_check_no_monolith_completion_append.py::completion-log.md",
        "coordinator_core/ops/test_doc_content_verify.py::delegate-execution.md",
        "coordinator_core/ops/test_extract_scope_paths.py::some-doc.md",
        "coordinator_core/ops/test_sync_plugin_wiki.py::alpha.md",
        "coordinator_core/ops/test_sync_plugin_wiki.py::beta.md",
        "coordinator_core/ops/test_sync_plugin_wiki.py::delta.md",
        "coordinator_core/ops/test_sync_plugin_wiki.py::foo.md",
        "coordinator_core/ops/test_sync_plugin_wiki.py::gamma.md",
        "coordinator_core/ops/tests/test_distill_scope.py::fam.md",
        "coordinator_core/ops/tests/test_distill_scope.py::fam/_index.md",
        "coordinator_core/ops/tests/test_distill_scope.py::fam/child.md",
        "coordinator_core/ops/tests/test_distill_workflow_input.py::foo.md",
        "coordinator_core/ops/tests/test_plan_tasks_mutate.py::some-wiki.md",
        "coordinator_core/ops/tests/test_queue_parity.py::explicit-content-root-test.md",
        "coordinator_core/ops/tests/test_queue_parity.py::handler-content-root-test.md",
        "coordinator_core/ops/tests/test_queue_parity.py::some-wiki.md",
        "coordinator_core/ops/tests/test_queue_parity.py::test-parity-wiki.md",
        "coordinator_core/ops/tests/test_queue_parity.py::test.md",
        "coordinator_core/ops/tests/test_queue_promote_concurrency.py::collision-test-a.md",
        "coordinator_core/ops/tests/test_queue_promote_concurrency.py::collision-test-b.md",
        "coordinator_core/ops/tests/test_queue_promote_concurrency.py::digest-divergence.md",
        "coordinator_core/ops/tests/test_queue_promote_concurrency.py::digest-idempotency.md",
        "coordinator_core/ops/tests/test_queue_promote_concurrency.py::idempotent-rerun.md",
        "coordinator_core/ops/tests/test_queue_promote_content_root_param.py::x.md",  # private-name-ok: historical-filename
        "coordinator_core/ops/validate_install_contract.py::agent-install-contract.md",
        "coordinator_core/ops/verify_coverage.py::example-retrieval-repo.md",
        "coordinator_core/ops/workweek_reverse_drift_gate.py::machine-local-registry.md",
        "coordinator_core/orientation/regenerate_cache.py::DIRECTORY_GUIDE.md",
        "coordinator_core/subagent_sandbox/provision_report.py::plan-coverage-checker.md",
        "coordinator_core/test_artifact_subject.py::mcp-server-configuration.md",
        "coordinator_core/test_sizing_assemble.py::bar.md",
        "coordinator_core/test_sizing_assemble.py::foo.md",
        "coordinator_core/test_state_root.py::foo.md",
        "coordinator_core/tests/guard_witnesses/bash_confinement_b.py::governed-thing.md",
        "coordinator/bin/tests/test_citation_integrity_cli.py::old-location/renamed.md",
        "coordinator_core/tests/test_citation_graph_anchor.py::old/renamed.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::a.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::ceremony-calibration/workstream-complete-conversion-archaeology.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::ceremony-calibration/workstream-complete-review.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::concurrent-em-git-operations/concurrent-em-hazards.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::coordinator-tripwires/a-number-keeps-travelling-after-its-instrument-is-forgotten.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::coordinator-tripwires/git-commit-tree-bypasses-the-session-id-trailer-and-review-trail-refuses-the-commit-later.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::cross-repo-communication/cross-repo-memo-lifecycle.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::cross-repo-communication/cross-repo-op-ownership-discriminator.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::doctrine-authoring/named-contracts-vs-incidental-flags.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::em-operating-model/verification-before-completion.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::hook-best-practices/state-placement-law.md",
        "coordinator_core/tests/test_doctrine_prose_op_names_resolve.py::skills-corpus/architecture-survey-residue.md",
        "coordinator_core/tests/test_citation_graph.py::does-not-exist.md",
        "coordinator_core/tests/test_citation_graph.py::nowhere.md",
        "coordinator_core/tests/test_citation_graph.py::only-under-plugin-root.md",
        "coordinator_core/tests/test_citation_graph.py::only-under-repo-root.md",
        "coordinator_core/tests/test_citation_graph.py::other-page.md",
        "coordinator_core/tests/test_citation_graph.py::portable-page.md",
        "coordinator_core/tests/test_citation_graph.py::target-page.md",
        "coordinator_core/tests/test_coverage_bookkeeping_partition.py::example.md",
        "coordinator_core/tests/test_ipc_scope_touch_self_report.py::wire-path-test.md",
        "coordinator_core/tests/test_review_brightline_gate.py::foo.md",
        "coordinator_core/tracker_store.py::tracker-observed-set-and-concurrency.md",
        "coordinator_core/workstream_complete/directives_session_hygiene.py::install-surface-completeness.md",
        "coordinator_core/write_guards/tests/test_block_subagent_archive_write.py::foo.md",
        "coordinator_core/write_guards/tests/test_block_subagent_plan_body_write.py::x.md",
        "coordinator_core/write_guards/tests/test_block_unauthorized_claude_md_write.py::some-page.md",
        "docs/plans/2026-09-26-adopt-navigable-wiki-hierarchy.migrate.py::x.md",
    }
)


def _dangling() -> set[str]:
    listed = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.split()
    found: set[str] = set()
    for rel in listed:
        if rel.startswith(("tasks/", "state/")) or rel == _THIS:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _CITATION.finditer(text):
            if not (REPO_ROOT / _WIKI / match.group(1)).exists():
                found.add(f"{rel}::{match.group(1)}")
    return found


def test_no_new_bare_wiki_citation_that_misses_this_repo():
    new = sorted(_dangling() - BASELINE)
    assert not new, (
        "bare wiki citation resolves to nothing here; cite a DoE page as "
        "'coordinator-content-repo coordinator/docs/wiki/<subdir>/<page>.md': " + ", ".join(new)
    )


def test_baseline_has_no_stale_entries():
    stale = sorted(BASELINE - _dangling())
    assert not stale, "remove resolved entries from BASELINE: " + ", ".join(stale)
