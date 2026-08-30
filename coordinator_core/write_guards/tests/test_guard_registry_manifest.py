"""Manifest test — closes the `_discover_guards()` silent-import-failure hole.

Half 1 (CI-visible) of the two-part discharge in
docs/plans/2026-07-29-hook-fan-in-write-path.md chunk C12. Before this fix, a
guard module that raised on import was simply absent from the discovered set
— no operator signal, fail-open for that one guard, every other guard kept
working. This test asserts the discovered guard-name set equals a pinned
manifest, so a syntax error or broken sibling import in ANY guard module
fails a test instead of silently degrading a PM-approval boundary to absent.

What this does NOT cover: an environment-dependent import failure (an
unresolvable claude-klabauter root, a missing third-party dependency, a Windows-only
import quirk present on one operator's machine but not in CI). CI runs on one
environment and cannot see a failure that only reproduces on another. That
gap is why `engine.discover_guard_names()` also exists as a runtime-visible
signal (half 2) — see `coordinator/hooks/scripts/preuse-write-dispatch.py`
(DoE-claude repo), which emits a stderr line naming any guard that failed to
import at actual hook-invocation time, on whatever machine that turns out to
be.

Maintenance note: adding, removing, or renaming a guard module is expected to
require updating `_EXPECTED_GUARD_NAMES` below in the same commit — that
manual step IS the test's job; it converts a should-have-been-noticed change
into a change that must be noticed.
"""

from __future__ import annotations

from coordinator_core.write_guards.engine import discover_guard_names

_EXPECTED_GUARD_NAMES = frozenset(
    {
        "block_completion_monolith_write",
        "block_confined_agent_write",
        "block_consumed_handoff_edit",
        "block_duplicate_decision_record_id",
        "bump_out_of_repo_tool_write",
        "guard_memory_store_cap",
        "block_cutover_phase_hand_edit",
        "block_derived_global_doctrine_write",
        "block_dev_repo_sentinel_write",
        "block_dev_side_mirror_wiki",
        "block_disarm_marker_sentinel_write",
        "block_em_hand_edit_pending_review_integration",
        "block_fleet_delegation_write",
        "block_goals_log_hand_write",
        "block_home_dir_memo_delivery",
        "block_illegal_filename",
        "block_memo_status_hand_edit",
        "block_oss_mirror_memo_delivery",
        "block_priority_ledger_edit",
        "block_subagent_archive_write",
        "block_subagent_grant_record_write",
        "block_subagent_guard_grant_write",
        "block_subagent_plan_body_write",
        "block_unauthorized_claude_md_write",
        "block_worktree_sentinel_write",
        "check_claude_md_size",
        "guard_concrete_path_citations",
        "guard_doctrine_surface_edits",
        "guard_settings_json_write",
        "nudge_baton_body_bar",
        "nudge_em_code_dispatch",
        "nudge_handoff_ac_shape",
        "nudge_improvement_queue_write",
        "nudge_new_sh_file_naked_python",
        "nudge_outbox_draft_frontmatter_shape",
        "nudge_peer_notice_unread",
        "nudge_plan_sidecar_family_split",
        "nudge_private_git_fact_resolver",
        "nudge_prose_queue_append",
        "nudge_prose_queue_creation",
        "nudge_sentinel_retained_review_sidecar",
        "nudge_session_display_name_as_identifier",
        "nudge_shell_shaped_spawn",
        "nudge_tasks_state_folder_split",
        "nudge_terminal_artifact_edit",
        "nudge_unmarked_spawning_test",
        "nudge_windows_subprocess_popup",
        "validate_frontmatter_schema_advisory",
        "validate_frontmatter_schema_deny",
    }
)


def test_discovered_guard_set_matches_manifest() -> None:
    loaded, import_failed = discover_guard_names()
    assert import_failed == [], (
        f"guard module(s) failed to import and were silently omitted: {import_failed}"
    )
    assert set(loaded) == _EXPECTED_GUARD_NAMES, (
        "discovered guard set diverged from the pinned manifest — "
        f"missing={_EXPECTED_GUARD_NAMES - set(loaded)}, "
        f"unexpected={set(loaded) - _EXPECTED_GUARD_NAMES}"
    )


def test_no_guard_names_collide_with_intentionally_skipped_modules() -> None:
    loaded, _import_failed = discover_guard_names()
    assert "engine" not in loaded
    assert "__main__" not in loaded
    assert not any(name.startswith("_") for name in loaded)
