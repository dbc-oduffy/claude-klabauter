"""Structural pin for the write-guard family's hard-deny/advisory split (M1,
2026-08-06 -- B5 write-guard classification pass).

Mirrors `bash_guards/tests/test_guard_band_membership.py`'s mechanism, NOT
its two-attribute (band + fail_closed) shape -- this package's `CLASS`
attribute already IS a real, consumed classification (`engine.py`'s
hard-deny phase runs first and short-circuits, advisory phase runs only if
no hard-deny fired; see `engine.evaluate`), unlike the bash family's own
`CLASS`, which `dispatch.py` does not read at all (the chain order is
hand-built literal registration, and `CLASS` there governs nothing -- see
that module's own docstring, "WHY A SUBCLASS" section, and the vestigial-ness
this test's sibling exists to avoid reproducing). What was missing on this
side was not a REAL classification (the engine already enforces one) but a
GATED, visible artifact recording what each guard's classification IS and
WHY -- so a new guard landing with the wrong `CLASS`, or an existing one
drifting, fails a test instead of requiring a 38-module reading pass to
notice.

Reads the live registration via the engine's own private `_discover_guards()`
(same precedent as the bash sibling reading `dispatch._build_guard_chain`
directly rather than through a public re-export) -- `check()` closures are
never invoked here, so this stays a STRUCTURAL registration check.

Spec backlink: docs/plans/2026-08-06-apply-guard-class-census.md, under
DR-277 (docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md).
DR-277 REPLACES the B5-era irreversible-harm-only hard-deny bar this
docstring previously argued for: the ruling is guards are advisory BY
DEFAULT, with hard-deny reserved for three named carve-outs -- (1) a
genuinely silent/total/irreversible loss with no error at write time AND
no error at the moment of loss (a derived doctrine copy overwritten
silently; a settings.json poison that locks every tool fleet-wide with no
in-session repair path; an NTFS-illegal filename that breaks
`git checkout` on every Windows machine; a memo delivered to an inbox
nobody ever reads), (2) a fleet-wide resource-exhaustion risk that takes
the machine down (the `check_test_suite_invocation` carve-out), or (3) a
handful of PM-ratified exceptions recorded in the census itself. Everything
that does not clear one of those three carve-outs is advisory, including
guards this file previously pinned hard-deny under the superseded B5 bar
(the nine C2-C12 flips: `check_claude_md_size`,
`block_cutover_phase_hand_edit`, `block_tracker_edit`,
`block_priority_ledger_edit`,
`block_em_hand_edit_pending_review_integration`,
`nudge_prose_queue_creation`, `nudge_improvement_queue_write`,
`guard_memory_store_cap`, `block_dev_repo_sentinel_write` -- disk-truth
wires and dev-side mirrors whose harm is reversible and
individually-correctable, not the silent-and-total shape the DR-277
hard-deny bar reserves). The guards remaining hard-deny below are the
carve-out set DR-277 names, not a residual of the old bar.
"""
from __future__ import annotations

from typing import Dict, List

from coordinator_core.write_guards import engine

# Every hard-deny guard, alphabetical. A hard-deny here means: the harm this
# guard prevents is silent-and-total (no error at write time AND no error at
# the moment of loss), fleet-wide-and-compounding in a way after-the-fact
# review cannot hold down, or a disk-truth wire whose corruption an
# automated downstream consumer trusts before any human reviews it.
HARD_DENY_NAMES = [
    "block_consumed_handoff_edit",
    "block_derived_global_doctrine_write",
    "block_disarm_marker_sentinel_write",
    "block_goals_log_hand_write",
    "block_home_dir_memo_delivery",
    "block_illegal_filename",
    "block_memo_status_hand_edit",
    "block_oss_mirror_memo_delivery",
    "block_subagent_archive_write",
    "block_subagent_plan_body_write",
    "block_unauthorized_claude_md_write",
    "block_worktree_sentinel_write",
    "guard_doctrine_surface_edits",
    "guard_settings_json_write",
    "validate_frontmatter_schema_deny",
]

# Every advisory guard, alphabetical. Includes the two 2026-08-06 B5
# conversions from hard-deny (`block_completion_monolith_write`,
# `block_dev_side_mirror_wiki`) -- the harm each flags is a plainly
# reversible, individually-correctable mistake, not the irreversible-harm
# shape the hard-deny band is reserved for; see their own module docstrings.
ADVISORY_NAMES = [
    "block_completion_monolith_write",
    "block_cutover_phase_hand_edit",
    "block_dev_repo_sentinel_write",
    "block_dev_side_mirror_wiki",
    "block_em_hand_edit_pending_review_integration",
    "block_priority_ledger_edit",
    "block_tracker_edit",
    "bump_out_of_repo_tool_write",
    "check_claude_md_size",
    "guard_concrete_path_citations",
    "guard_memory_store_cap",
    "nudge_baton_body_bar",
    "nudge_em_code_dispatch",
    "nudge_improvement_queue_write",
    "nudge_new_sh_file_naked_python",
    "nudge_plan_sidecar_family_split",
    "nudge_prose_queue_append",
    "nudge_prose_queue_creation",
    "nudge_sentinel_retained_review_sidecar",
    "nudge_shell_shaped_spawn",
    "nudge_tasks_state_folder_split",
    "nudge_terminal_artifact_edit",
    "nudge_windows_subprocess_popup",
    "validate_frontmatter_schema_advisory",
]

_EXPECTED_CLASS_BY_NAME: Dict[str, str] = {
    **{name: "hard-deny" for name in HARD_DENY_NAMES},
    **{name: "advisory" for name in ADVISORY_NAMES},
}


def _guards() -> "List":
    guards, import_failed = engine._discover_guards()
    assert import_failed == [], (
        f"guard module(s) failed to import: {import_failed} -- this test cannot "
        "trust the discovered set while an import is silently broken"
    )
    return guards


def test_chain_is_readable_and_non_trivial():
    """Guard the guard: if `_discover_guards` stops returning entries, every
    assertion below would pass vacuously on an empty list."""
    guards = _guards()
    assert len(guards) > 10, guards
    names = {g.name for g in guards}
    assert "block_illegal_filename" in names
    assert "nudge_baton_body_bar" in names


def test_all_named_guards_are_actually_registered():
    """A typo in either list above would silently weaken every other
    assertion in this file to a no-op against a name that was never
    checked."""
    registered = {g.name for g in _guards()}
    all_named = set(HARD_DENY_NAMES) | set(ADVISORY_NAMES)
    missing = all_named - registered
    assert not missing, (
        f"named in this test but not registered/discoverable: {sorted(missing)} -- "
        "either the guard was renamed/removed, or this test has gone stale."
    )


def test_no_registered_guard_is_unclassified_by_this_test():
    """The mirror image of the check above: every discovered guard must be
    named in exactly one of the two lists -- a new guard module landing
    without updating this file is exactly the "unclassified guard, visible
    only by reading 38 modules" failure mode B5 exists to close."""
    registered = {g.name for g in _guards()}
    all_named = set(HARD_DENY_NAMES) | set(ADVISORY_NAMES)
    unclassified = registered - all_named
    assert not unclassified, (
        f"discovered but not named in either classification list here: "
        f"{sorted(unclassified)} -- a new guard must be classified explicitly, "
        "not left to default."
    )


def test_each_guard_carries_its_expected_class():
    wrong = []
    for g in _guards():
        expected = _EXPECTED_CLASS_BY_NAME.get(g.name)
        if expected is not None and g.cls != expected:
            wrong.append((g.name, g.cls, expected))
    assert not wrong, (
        "guards whose runtime CLASS diverged from this pinned classification "
        "(name, actual, expected): %r" % wrong
    )


def test_no_name_appears_in_both_lists():
    overlap = set(HARD_DENY_NAMES) & set(ADVISORY_NAMES)
    assert not overlap, f"guard(s) named in both classification lists: {sorted(overlap)}"
