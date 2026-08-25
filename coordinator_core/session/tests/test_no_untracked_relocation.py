"""Repo-wide guard: no raw file relocation strands a touch-claim.

The defect this guards: a touch-claim names a path, and a path is not a
file. When something moves a claimed file with a bare ``shutil.move`` /
``os.rename`` / ``os.replace`` / ``Path.rename`` / ``Path.replace``, the
claim keeps pointing at a path that no longer exists, and the file's new
location -- the one actually holding the session's work -- reads as
"untouched by this session" and drops out of the safe-commit offer. See
``docs/plans/2026-08-06-relocation-re-declares-the-touch-claim.md`` for the
full mechanism, and ``coordinator_core.session.scope.relocate_touched_path``
(that plan's C1) for the fix: it performs the move AND, when the source is
currently T-claimed by this session, re-declares the claim on the
destination before moving.

**This is a STATIC AST scan, not a runtime checker.** Whether a given
source path was ACTUALLY claimed by a live session at the moment of a given
call is a fact about that session's ``touched.txt``, not something a
static scan of source code can ever observe. That claimed-source property
is what the plan's C3 chunk's discriminator decided, site by site, when it
built the allow-list below (`## Site classification` in the plan body,
committed at ``15a61ab69``) -- this test does not re-derive that judgment,
it only enforces the mechanical half: "route through
``relocate_touched_path`` (directly or via a documented fail-open fallback
that calls it first), or be a reasoned, enumerated exception." A green run
of this guard is not proof no claim ever strands at runtime; it is proof
that every relocation call site in scope is one C3 examined and either
migrated onto the helper or explained.

Negative-spec -- scope of this guard, read before extending it:
    - Static AST scan only. It does not import or execute any scanned
      module. Do NOT widen it into a runtime/execution check.
    - Test files ARE included in the scan (unlike the sibling guard
      ``test_no_bare_argv0_script_launch.py``, which excludes them). A raw
      relocation performed by a test's own setup fixture is still a raw
      relocation, and the plan's C3 classification pass deliberately
      classified several of them (pytest ``tmp_path`` fixtures resolve
      outside any real worktree, so nothing is claimable there) plus one
      load-bearing exception: ``test_claims.py``'s own characterization
      test, which uses a raw ``shutil.move`` ON PURPOSE to document the
      pre-fix stranded shape this whole plan exists to close. That test
      must keep working, so its call site is allow-listed by name, not
      excluded by directory.
    - ``.rename(...)`` / ``.replace(...)`` (no module prefix) are matched
      ONLY when called with exactly one positional argument and no
      keywords -- ``Path.rename(target)`` / ``Path.replace(target)``'s
      exact shape. ``str.replace(old, new)`` always takes >= 2 positional
      arguments and so never matches; this keeps the scan from drowning in
      unrelated string-replace call sites while still catching every
      ``Path``-shaped rename/replace an eyeball can find in this tree.
      This is a heuristic, not a type check -- an exotic single-argument
      ``.rename()``/``.replace()`` on some other class would false-positive
      and need its own allow-list entry with a reason, same as any other
      finding.
    - Scope is ``coordinator_core/`` and ``coordinator/bin/`` (matching the
      plan's own C3 sweep root) -- files under either tree, any extension
      (``coordinator/bin`` has extensionless scripts).
    - Import-alias resolution (``import shutil as sh``, ``from os import
      rename``, ``m = shutil; m.move(...)``) is handled by tracing each
      module's own ``Import``/``ImportFrom`` nodes plus one level of local
      reassignment (the same ``_find_assignment``/enclosing-scope approach
      ``test_no_bare_argv0_script_launch`` uses) -- see ``_build_alias_map``/
      ``_resolve_module_base`` below. What is still OUT OF SCOPE, disclosed
      rather than silently missed: a module object reached through anything
      other than a simple local ``name = expr`` chain -- attribute access via
      ``getattr(shutil, "move")``, wildcard imports (``from os import *``),
      a module object passed in as a function *parameter* rather than
      assigned in the same scope, or a tuple/multi-target assignment
      (``mod_a, mod_b = os, shutil``) -- none of these are traced, and a call
      routed through one of them escapes this scan the same way the
      pre-fix aliasing blind spot did.

Spec backlink: ``pln-a-relocated-file-re-declares-i-b9d010``
(C6), its `## Site classification` section (the provenance for every
allow-list entry below), and
``coordinator_core.test_no_bare_argv0_script_launch`` (the house pattern
this guard's shape and drift test are copied from).
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAN_ROOTS = (_REPO_ROOT / "coordinator_core", _REPO_ROOT / "coordinator" / "bin")

# {(root-name, relative-path-from-that-root, enclosing-function-name,
#   callee-kind, call-signature, ordinal): reason}
#
# Keyed on the enclosing function rather than a line number -- see
# `_find_enclosing_function_name`/`_key_for_call` below for the exact
# resolution rule, including the `_MODULE_LEVEL` sentinel. The call's own
# identity -- `call-signature`, an `ast.unparse` of the call node itself --
# is what disambiguates two-or-more same-kind calls in one function, NOT a
# positional ordinal: a positional ordinal is re-keyed by inserting an
# unrelated same-kind call anywhere earlier in the group, which silently
# re-maps an already-approved entry onto the new call while reporting the
# untouched original site as "new" (see `test_ordinal_insertion_does_not_
# silently_reassign_an_approved_entry` below for the reproduction). Keying on
# the call's own text means inserting a neighbour changes NOTHING for any
# existing entry -- no key depends on what else is in its group. `ordinal`
# is now a narrow residual: it disambiguates two BYTE-IDENTICAL calls (same
# `call-signature`) inside one function, which is a real and legitimate
# collision (see the three `#2` entries below) -- those calls are
# interchangeable by construction, so sharing one reason across them is
# acceptable, and the same insertion hazard applies only within that
# narrower same-signature sub-group. A pure line shift above a guarded call
# site (an unrelated edit, a docstring line added earlier in the file, ...)
# does not change this key; a function rename, or a change to the call's own
# arguments, does, on purpose -- both are real semantic changes worth
# re-ratifying.
#
# Every entry's reason is the provenance for that entry -- either quoted
# from the plan's `## Site classification` table (the no-strand sites, and
# the two written directory-relocation residuals), or naming the plan chunk
# that added the call site (C1's own move; C4/C5's fail-open fallbacks; the
# C2/characterization test). An entry with no reason, or a bare path with no
# mapped value, fails `test_allowlist_entries_carry_a_reason` below -- the
# allow-list is data this test reads, not a place to silence a finding by
# appending a string.
_ALLOWED: Dict[Tuple[str, str, str, str, str, int], str] = {
    # --- C1: relocate_touched_path IS the move; seeded first, per the plan ---
    ("coordinator_core", "session/scope.py", "relocate_touched_path", "shutil.move", "shutil.move(src_abs, dst_abs)", 1):
        "relocate_touched_path's own shutil.move -- this function IS the "
        "re-declare-then-move mechanism the rest of this allow-list migrates "
        "onto (plan C1)",

    # --- Fail-open fallbacks the plan added: deliberate raw call taken only ---
    # --- when the session engine / relocate_touched_path is unreachable.   ---
    ("coordinator_core", "percolate/rewrite_basename.py", "_do_rename", "Path.rename", "old_path.rename(new_path)", 1):
        "rewrite_basename._do_rename's fail-open fallback -- tries "
        "relocate_touched_path first, falls back to a plain Path.rename only "
        "if the helper or session_id is unavailable, or the helper call "
        "itself raises; a rename this module is already committed to "
        "performing must not fail on a claim-bookkeeping nicety",
    ("bin", "workweek-complete-close.py", "_relocate_or_move", "shutil.move", "shutil.move(str(src), str(dst))", 1):
        "workweek-complete-close._relocate_or_move's fail-open fallback -- "
        "tries relocate_touched_path first, falls back to a plain "
        "shutil.move; an archive that ran is better than one that failed on "
        "bookkeeping",
    ("coordinator_core", "ops/priority_drain.py", "_plain_rename", "Path.rename", "path.rename(dest)", 1):
        "priority_drain._move's fail-open fallback -- tries "
        "relocate_touched_path first (most drains have no live claim to "
        "restate anyway), falls back to a plain Path.rename",
    ("coordinator_core", "ops/migrate_cross_repo_layout.py", "_move_one", "shutil.move", "shutil.move(src, dst)", 1):
        "_move_one's fail-open fallback for the untracked branch (plan C5d) "
        "-- tries relocate_touched_path first when a session id resolved, "
        "falls back to a plain shutil.move when session_id is empty/"
        "unresolvable (could hold no claim) or the helper's own internal "
        "bookkeeping raises; a migration step already committed to moving "
        "the file must not fail, or behave differently, on a claim-"
        "bookkeeping nicety -- same pattern as the priority_drain.py and "
        "rewrite_basename.py entries above",
    ("bin", "cross-repo-memo.py", "_archive_sent_outbox_draft", "shutil.move", "shutil.move(outbox_path, dest)", 1):
        "cross-repo-memo._archive_sent_outbox_draft's fail-open fallback -- "
        "tries relocate_touched_path first (this is the confirmed strand "
        "instance the plan's source memo reported), falls back to a plain "
        "shutil.move so a delivered/sent memo is never lost to unreachable "
        "claim bookkeeping",

    # --- The characterization test: a raw move ON PURPOSE, to document the ---
    # --- exact pre-fix stranded shape this whole plan exists to close.     ---
    ("coordinator_core", "session/tests/test_claims.py", "test_raw_shutil_move_strands_the_new_path_unclaimed", "shutil.move", "shutil.move(str(Path(repo) / 'a.py'), str(Path(repo) / 'b.py'))", 1):
        "characterization test for the pre-fix stranded shape (plan C2) -- "
        "uses a raw shutil.move ON PURPOSE so the test documents what a raw "
        "move does (A stays claimed, B reads 'untouched by this session'); "
        "allow-listing it is mandatory, not an oversight -- this guard must "
        "not break the test that proves why it exists",

    # --- C3's no-strand classification: atomic tmp->final writes, where the ---
    # --- temp source was never claimed, so there is nothing to strand.     ---
    ("coordinator_core", "authz/token.py", "write_tokens", "os.replace", "os.replace(tmp, path)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "distill/log_normalize.py", "normalize_log", "os.replace", "os.replace(tmp_path, log_path)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "distill/log_normalize.py", "normalize_arrow_dialects_log", "os.replace", "os.replace(tmp_path, log_path)", 1): "atomic tmp->final rename; temp source never claimed, same pattern as normalize_log's own entry above",
    ("coordinator_core", "distill/wiki_log_migrate.py", "migrate_wiki_log", "os.replace", "os.replace(tmp_path, wiki_log_path)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "goals/reassess_krs.py", "_write_goal_file_atomic", "os.replace", "os.replace(tmp, goal_file)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "hooks/auto_push.py", "_write_pending_record", "os.replace", "os.replace(tmp, target)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "hooks/platform_localize.py", "atomic_write", "os.replace", "os.replace(tmp, path)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "hooks/postuse_advisory_dispatch.py", "_save_advisory_state", "os.replace", "os.replace(tmp_path, path)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "hooks/track_dispatched_agents.py", "_write_backpointer_sync", "os.replace", "os.replace(tmp, em_backpointer)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "hooks/track_dispatched_agents.py", "_process_dispatched_sync", "os.replace", "os.replace(tmp, dispatched)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "hooks/ue_knowledge_distrust.py", "_run_bootstrap", "os.replace", "os.replace(tmp_path, settings_path)", 1): "atomic tmp->final; temp source never claimed (fresh-write branch)",
    ("coordinator_core", "hooks/ue_knowledge_distrust.py", "_run_bootstrap", "os.replace", "os.replace(tmp_path, settings_path)", 2): "atomic tmp->final; temp source never claimed (merge branch)",
    ("coordinator_core", "install/_shared.py", "atomic_write_bytes", "os.replace", "os.replace(tmp_name, str(target))", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "install/dep_check.py", "visited_set_append", "os.replace", "os.replace(tmp_path, visited_file)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "install/detect_test_cmd.py", "upsert_frontmatter_key", "os.replace", "os.replace(tmp_path, file)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "install/gen_settings_hooks.py", "_atomic_write_json", "os.replace", "os.replace(tmp_name, target)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "install/substrate.py", "_register_hardware_concern", "os.replace", "os.replace(tmp, registry_live)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "install/uninstall_legs.py", "_atomic_write_text", "os.replace", "os.replace(tmp_name, target)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "install/uninstall_legs.py", "uninstall_strip_settings_hooks", "os.replace", "os.replace(tmp_out, settings_json)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/backfill_deliverable_spine.py", "_stamp_file", "os.replace", "os.replace(tmp_path, path)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/bootstrap_orchestrate.py", "_coordinator_currency_write", "os.replace", "os.replace(tmp_path, stamp_path)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/changelog_ops.py", "_atomic_write", "os.replace", "os.replace(tmp, path)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/coordinator_setup_state.py", "_atomic_write", "os.replace", "os.replace(tmp_path, target)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/completion_ops.py", "append_plan_session", "os.replace", "os.replace(tmp_path, str(path))", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/distill_apply_disposal.py", "write_apply_receipt", "os.replace", "os.replace(tmp_path, str(target))", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/distill_disposal_manifest.py", "write_disposal_manifest", "os.replace", "os.replace(tmp_path, str(target))", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/distill_stamp_disposal.py", "write_stamped_manifest", "os.replace", "os.replace(tmp_path, str(manifest_path))", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/distill_scope.py", "write_scope_manifest", "os.replace", "os.replace(tmp_path, str(target))", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/ensure_vscode_readonly.py", "_merge_settings", "Path.replace", "tmp.replace(settings_path)", 1): "atomic tmp->final rename; temp source never claimed (fresh sibling tmp, not a pre-existing tracked path)",
    ("coordinator_core", "ops/gen_claude_doe_launcher.py", "main", "os.replace", "os.replace(tmp_dest, dest)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/gen_claude_doe_shim.py", "main", "os.replace", "os.replace(tmp_shim, shim_dest)", 1): "atomic tmp->final rename; temp source never claimed (render-shim block)",
    ("coordinator_core", "ops/gen_claude_doe_shim.py", "main", "os.replace", "os.replace(tmp_rc, target_rc)", 1): "atomic tmp->final rename; temp source never claimed (rc-wiring block)",
    ("coordinator_core", "ops/gen_doe_root_pointer.py", "main", "os.replace", "os.replace(tmp_live, pointer_file)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/install_claude_klabauter_precommit_hook.py", "_atomic_write", "os.replace", "os.replace(tmp_path, path)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/install_meta_repo_precommit_hook.py", "_atomic_write", "os.replace", "os.replace(tmp_path, path)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/memo_fate_partition.py", "_atomic_write_json", "os.replace", "os.replace(tmp, path)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/normalize_env.py", "_ne_darwin_bash_profile_repair", "os.replace", "os.replace(tmp_name, bp_path)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/prune_resolved_queue_entries.py", "_atomic_replace", "os.replace", "os.replace(tmp_path, path)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/register_coordinator_mirror.py", "register", "os.replace", "os.replace(tmp, reg_path)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/queue_append.py", "_write_out_path_overwrite", "os.replace", "os.replace(tmp_path, out_path)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/render_posture_overlay.py", "run", "os.replace", "os.replace(tmp_name, str(target_path))", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/queue_promote.py", "promote_lesson", "os.replace", "os.replace(tmp_path, out_path)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/render_template.py", "main", "os.replace", "os.replace(tmp_path, output_path)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/scope_warning_resolve.py", "resolve", "os.replace", "os.replace(tmp_path, log_file)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/session_hierarchy_derive.py", "_atomic_write_json", "os.replace", "os.replace(tmp, path)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/ceremony/receipt_emit.py", "_atomic_write_json", "os.replace", "os.replace(tmp_str, str(path))", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/ceremony/tail_ops.py", "cs_archive", "shutil.move", "shutil.move(str(sdir), str(archive_dir))", 1): "source lives inside the .git subtree, explicitly outside claimable space (cs_archive)",
    ("coordinator_core", "ops/emit/lma_cache.py", "_store", "os.replace", "os.replace(tmp_name, path)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/fleet/memo_compose.py", "_memo_compose", "os.replace", "os.replace(tmp_path, str(target_path))", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/tracker/advance_status.py", "advance_status", "os.replace", "os.replace(tmp_path, tracker_file)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "ops/session/guard_hook_generation_self_probe.py", "_atomic_write_text", "os.replace", "os.replace(tmp_name, path)", 1): "atomic tmp->final rename; temp source never claimed (marker file, outside worktree)",
    ("coordinator_core", "ops/session/guard_settings_integrity.py", "_atomic_copy", "os.replace", "os.replace(tmp_path, dst)", 1): "atomic tmp->final rename; temp source never claimed",
    ("coordinator_core", "ops/session/guard_settings_integrity.py", "evaluate_settings_integrity", "os.replace", "os.replace(head_tmp, settings)", 1): "atomic tmp->final rename; temp source never claimed, and source resolves outside the worktree entirely",
    ("coordinator_core", "ops/session/reap.py", "_reap_stale_sessions", "Path.rename", "sdir.rename(archive_dest)", 1): "source lives under the .git/ subtree, categorically outside claimable space (_reap_stale_sessions)",
    ("coordinator_core", "ops/session/reap.py", "_reap_stale_agents", "Path.rename", "adir.rename(archive_dest)", 1): "source lives under the .git/ subtree, categorically outside claimable space (_reap_stale_agents)",
    ("coordinator_core", "ops/session/record_pickup.py", "_record_pickup_sync", "os.replace", "os.replace(tmp_path, str(shape_file))", 1): "atomic tmp->final rename; temp never claimed, both paths under .git/",
    ("coordinator_core", "session/claude_md_grant.py", "write_claude_md_write_grant", "os.replace", "os.replace(tmp_name, grant_file)", 1): "atomic tmp->final rename; temp never claimed, destination under .git/",
    ("coordinator_core", "session/context_usage_sidecar.py", "write_usage", "os.replace", "os.replace(tmp_path, target)", 1): "atomic tmp->final rename; temp source (a sibling .{name}.{uuid}.tmp under tempfile.gettempdir()) never claimed -- not a repo path at all",
    ("coordinator_core", "session/core.py", "update_meta_field", "os.replace", "os.replace(tmp_name, meta_path)", 1): "atomic tmp->final rename; temp never claimed, destination under .git/",
    ("coordinator_core", "session/grant.py", "write_tier_u_grant", "os.replace", "os.replace(tmp_name, grant_file)", 1): "atomic tmp->final rename; temp never claimed, destination under .git/",
    ("coordinator_core", "session/scope.py", "archive", "shutil.move", "shutil.move(sdir, archive_dir)", 1): "archive(): source lives under the .git/ subtree, categorically outside claimable space",
    ("coordinator_core", "session/shape.py", "session_shape_set", "os.replace", "os.replace(tmp_name, shape_file)", 1): "atomic tmp->final rename; temp never claimed, destination under .git/",
    ("coordinator_core", "testing/suite_mutex.py", "_write_meta", "os.replace", "os.replace(str(tmp_path), str(path / _META_FILENAME))", 1): "atomic tmp->final rename; temp never claimed, both paths sit outside the worktree entirely",
    ("coordinator_core", "orientation/regenerate_cache.py", "_atomic_replace", "os.replace", "os.replace(tmp, cache_file)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "percolate/rewrite_basename.py", "write_rename_ledger", "os.replace", "os.replace(tmp, path)", 1): "write_rename_ledger: atomic tmp->final; temp source never claimed",
    ("coordinator_core", "plugin_health/sentinel.py", "_write_sentinel", "os.replace", "os.replace(tmp, sentinel_path)", 1): "atomic tmp->final; temp source never claimed",
    ("coordinator_core", "publish/time_transform.py", "rename_codename_basenames", "Path.rename", "old_path.rename(new_path)", 1): "rename_codename_basenames operates on the meta-repo/publish-repo trees, neither of which is this worktree -- normalize_touch_path returns None, no claim expressible; also invoked out-of-process via a CLI trampoline",
    ("coordinator_core", "ops/research_archive_workdir.py", "_archive_workdir_sync", "os.rename", "os.rename(tmp, dest)", 1): "EXDEV-fallback leg: tmp is a freshly-created copytree, never itself claimed; the original src is removed separately via shutil.rmtree, not renamed",
    ("coordinator_core", "ops/edit_live_hook.py", "cmd_commit", "os.replace", "os.replace(scratch_path, hook_path)", 1): "cmd_commit's os.replace(scratch_path, hook_path) swap -- scratch_path is created fresh by shutil.copy2 into a never-before-existing scratch name, never authored through an agent Edit/Write, so no claim was ever written for it regardless of what the module imports (Wave B resolution, stands independent of the refuted 'never imports touch()' argument)",

    # --- C3's no-strand: pytest tmp_path fixtures resolve outside the real ---
    # --- worktree of this process, so nothing there is claimable.           ---
    ("coordinator_core", "ops/test_bootstrap_repo.py", "test_resolve_scaffold_manifest_root_rung_one_miss_rung_two_hit", "os.replace", "os.replace(os.path.join(fallback_root, name), doe_root / 'coordinator' / name)", 1): "source resolves inside an isolated pytest tmp_path fixture tree, never the process's real worktree",
    ("coordinator_core", "ops/test_install_claude_klabauter_precommit_hook.py", "test_hook_survives_the_repo_being_relocated", "Path.rename", "repo.rename(moved)", 1): "synthetic fixture repo under pytest tmp_path, not the running process's real worktree",
    ("coordinator_core", "ops/test_research_dir_restructure.py", "test_crash_between_renames_rerun_completes_pending_step", "Path.rename", "result_md.rename(topic_dir / f'{DATE}-result.md')", 1): "fixture-simulated worktree lives under pytest tmp_path, not the real worktree of this process",

    # --- Category 4: additional test fixtures the plan's C3 table did not ---
    # --- enumerate, found by this guard's own scan -- same pytest         ---
    # --- tmp_path reasoning as the three sites above, reported explicitly ---
    # --- rather than silently folded in.                                  ---
    ("coordinator_core", "distill/tests/test_harvest_debt.py", "test_renamed_away_log_fails_loud", "Path.rename", "log_path.rename(renamed_path)", 1): "test_renamed_away_log_fails_loud: log_path.rename under pytest tmp_path -- not the process's real worktree, and the test's own point is to assert FAIL-LOUD on a renamed-away log, not to exercise touch-claim bookkeeping",
    ("coordinator_core", "ops/emit/tests/test_ac_priority_spinoff_wall.py", "test_resolution_unchanged_when_explicit_ancestor_archives", "shutil.move", "shutil.move(str(live_handoffs / 'ancestor.md'), str(archive_dir / 'ancestor.md'))", 1): "shutil.move under pytest tmp_path, simulating a real archival git-mv shape for a fixture handoff file -- fixture tree, not the process's real worktree",
    ("coordinator_core", "session/tests/test_liveness.py", "test_archived_sid_absent_from_map_reads_unknown_and_not_live", "Path.rename", "sdir.rename(archive_dir / 'was-live-2026-08-03')", 1): "sdir.rename under pytest tmp_path fixture repo, simulating session archival -- not the process's real worktree",
    ("coordinator_core", "tests/test_dag_git_path_ever_tracked_cache.py", "test_renamed_path_present_in_widened_cache", "Path.rename", "(root / 'state/handoffs/old-name.md').rename(root / 'state/handoffs/new-name.md')", 1): "rename under pytest tmp_path fixture repo, exercising git rename-detection cache behaviour -- not the process's real worktree",
    ("coordinator_core", "tests/test_dag_git_path_ever_tracked_cache.py", "test_widened_cache_short_circuits_the_per_path_fallback_spawn", "Path.rename", "(root / 'state/handoffs/old-name.md').rename(root / 'state/handoffs/new-name.md')", 1): "same fixture-repo rename, second test in the same class -- not the process's real worktree",

    # --- These two os.rename calls are DIRECTORY relocations, migrated onto ---
    # --- restate_touched_tree immediately ahead of the rename (Part B of   ---
    # --- the same diff that landed this allow-list) -- no longer the      ---
    # --- unmigrated forward-work residual an earlier draft of this entry  ---
    # --- described. Allow-listed here (rather than requiring the guard to ---
    # --- special-case restate_touched_tree call sequencing) because the   ---
    # --- guard is a static AST scan of the relocation call alone; it has  ---
    # --- no way to verify a *preceding* restatement call covers it.       ---
    ("coordinator_core", "ops/research_archive_workdir.py", "_archive_workdir_sync", "os.rename", "os.rename(src, dest)", 1):
        "_archive_workdir_sync direct-rename leg -- relocates a real, "
        "populated working DIRECTORY inside the worktree; "
        "restate_touched_tree(session_id, str(src), str(dest), cwd=...) runs "
        "immediately before this os.rename (see the function body), "
        "re-declaring every claim nested under src onto its post-move path "
        "under dest -- this is the directory-aware relocation, not the "
        "residual that used to be forward work",
    ("coordinator_core", "ops/research_dir_restructure.py", "_restructure_sync", "os.rename", "os.rename(src, dest)", 1):
        "_restructure_sync's os.rename(src, dest) for the paper_trail "
        "(directory) step -- restate_touched_tree(session_id, str(src), "
        "str(dest), cwd=cwd) runs immediately before this os.rename, "
        "re-declaring claims nested under src onto dest; the sibling "
        "dated_result (file) step is covered by restate_touched_path instead "
        "-- same pairing, not the unmigrated residual an earlier draft of "
        "this entry described",

    # --- coordinator/bin no-strand sites (atomic tmp->final, or external- ---
    # --- destination trees outside this worktree entirely).               ---
    ("bin", "lib/git_hook_install.py", "_atomic_write", "os.replace", "os.replace(tmp, path)", 1): "atomic tmp->final; temp source never claimed",
    ("bin", "coordinator-initiative.py", "_cmd_create", "os.replace", "os.replace(tmp, target)", 1): "atomic tmp->final; temp source never claimed (_cmd_create)",
    ("bin", "coordinator-initiative.py", "_cmd_attach", "os.replace", "os.replace(tmp, artifact_path)", 1): "atomic tmp->final; temp source never claimed (_cmd_attach)",
    ("bin", "coordinator-tasks-mirror.py", "cmd_init", "os.replace", "os.replace(tmp_path, mirror_file)", 1): "atomic tmp->final; temp source never claimed (cmd_init)",
    ("bin", "coordinator-tasks-mirror.py", "cmd_update", "os.replace", "os.replace(tmp_path, mirror_file)", 1): "atomic tmp->final; temp source never claimed (cmd_update)",
    ("bin", "claude-ue-bootstrap.py", "bootstrap", "Path.replace", "tmp_path.replace(settings_path)", 1): "atomic tmp->final; temp source never claimed",
    ("bin", "claude-ue-bootstrap.py", "bootstrap", "Path.replace", "tmp_path.replace(settings_path)", 2): "atomic tmp->final; temp source never claimed",
    ("bin", "seed-skill-overrides.py", "_atomic_write", "os.replace", "os.replace(tmp, settings_path)", 1): "atomic tmp->final; temp source never claimed",
    ("bin", "coordinator-queue-append.py", "_write_out_path_overwrite", "os.replace", "os.replace(tmp_path, out_path)", 1): "atomic tmp->final; temp source never claimed",
    ("bin", "workday-start-handoff-triage.py", "trim_orphan_sweep_notes", "Path.replace", "tmp_path.replace(path)", 1): "atomic tmp->final; temp source never claimed",
    ("bin", "seed-marketplace-enabledplugins.py", "_atomic_write", "os.replace", "os.replace(tmp, target)", 1): "atomic tmp->final; temp source never claimed",
    ("bin", "gen-claude-klabauter-root-pointer.py", "main", "os.replace", "os.replace(tmp_live, pointer_file)", 1): "atomic tmp->final; temp source never claimed",
    ("bin", "repomap/generate-repomap.py", "save_cache", "os.replace", "os.replace(tmp_path, cache_path)", 1): "atomic tmp->final; temp source never claimed (save_cache)",
    ("bin", "repomap/generate-repomap.py", "generate_task_scoped_map", "os.replace", "os.replace(tmp_path, output_path)", 1): "atomic tmp->final; temp source never claimed (generate_task_scoped_repomap)",
    ("bin", "repomap/generate-repomap.py", "generate_repomap", "os.replace", "os.replace(tmp_path, output_path)", 1): "atomic tmp->final; temp source never claimed (main, final write)",
    ("bin", "refresh-plugin-live-install.py", "_replace_restore", "Path.rename", "staging.rename(live_path)", 1): "live_path resolves under the plugin plugins_dir (the CLI's live plugin-install tree), outside this worktree -- not claimable by touch() in this process",
    ("bin", "publish.py", "_swap_publish_staging_into_dest", "os.rename", "os.rename(dest_dir, prior_backup)", 1): "dest_dir points at an external sibling-repo publish destination, outside this worktree entirely",
    ("bin", "publish.py", "_swap_publish_staging_into_dest", "os.rename", "os.rename(staging_dir, dest_dir)", 1): "dest_dir points at an external sibling-repo publish destination, outside this worktree entirely",
    ("bin", "publish.py", "_swap_publish_staging_into_dest", "os.rename", "os.rename(prior_backup, dest_dir)", 1): "recovery rename: prior_backup/dest_dir, same external-destination reasoning",
    # The root-dest branch (2026-08-15): a flat-mirror row whose dest_subdir is
    # empty has dest_dir == the mirror ROOT, and renaming that root fails on
    # Windows whenever any handle is open anywhere beneath it -- which, for a
    # mirror that is also a live engine install, is always. That branch swaps
    # entry-by-entry instead, so its renames operate one level lower than the
    # four above. Same external-destination reasoning: every path here is under
    # the sibling-repo publish destination, never this worktree.
    ("bin", "publish.py", "_swap_publish_staging_entry", "os.rename", "os.rename(dest_entry, prior)", 1): "per-entry swap under an external sibling-repo publish destination; rename-aside so a partial failure stays recoverable",
    ("bin", "publish.py", "_swap_publish_staging_entry", "os.rename", "os.rename(prior, dest_entry)", 1): "recovery rename of the aside-copy, same external-destination reasoning",
    ("bin", "publish.py", "_swap_publish_staging_entry", "os.rename", "os.rename(staging_entry, dest_entry)", 1): "directory entry moved into the external publish destination; staging source is this round's own copy, never claimed",
    ("bin", "publish.py", "_swap_publish_staging_entry", "os.replace", "os.replace(staging_entry, dest_entry)", 1): "file entry replaced in the external publish destination; staging source is this round's own copy, never claimed",
    ("bin", "publish.py", "write_publish_provenance_record", "os.replace", "os.replace(tmp_path, record_path)", 1): "atomic tmp->final rename of the publish provenance record; the temp source is a fresh pid-suffixed sibling written moments earlier by this call and never claimed by any session",

    # --- Category 4: sites the plan's C3 table does not cover, found by  ---
    # --- this guard's own scan and classified here against the same      ---
    # --- source-side discriminator C3 used. Reported explicitly in the   ---
    # --- C6 execution report -- not silently added.                      ---
    ("coordinator_core", "async_hook_status.py", "surface_and_clear", "Path.rename", "marker_path.rename(claimed)", 1): "surface_and_clear's atomic claim-rename: marker_path (a .cache marker file outside the worktree) is renamed to an immediately-consumed '.claimed.<pid>' sibling then unlinked -- ephemeral bookkeeping file, never touch()-claimed",
    ("coordinator_core", "claims_emit.py", "_write_atomic_pair", "os.replace", "os.replace(claims_path, backup_claims)", 1): "_write_atomic_pair: os.replace(claims_path, backup_claims) -- moves a pre-existing destination aside to its own temp backup before the real write; backup is never itself claimed",
    ("coordinator_core", "claims_emit.py", "_write_atomic_pair", "os.replace", "os.replace(meta_path, backup_meta)", 1): "_write_atomic_pair: os.replace(meta_path, backup_meta) -- same backup-aside pattern",
    ("coordinator_core", "claims_emit.py", "_write_atomic_pair", "os.replace", "os.replace(tmp_claims, claims_path)", 1): "_write_atomic_pair: os.replace(tmp_claims, claims_path) -- atomic tmp->final; temp source never claimed",
    ("coordinator_core", "claims_emit.py", "_write_atomic_pair", "os.replace", "os.replace(tmp_meta, meta_path)", 1): "_write_atomic_pair: os.replace(tmp_meta, meta_path) -- atomic tmp->final; temp source never claimed",
    ("coordinator_core", "claims_emit.py", "_write_atomic_pair", "os.replace", "os.replace(backup_claims, claims_path)", 1): "_write_atomic_pair's unwind: os.replace(backup_claims, claims_path) restores the pre-existing file back to where it always was -- not a relocation, a revert",
    ("coordinator_core", "claims_emit.py", "_write_atomic_pair", "os.replace", "os.replace(backup_meta, meta_path)", 1): "_write_atomic_pair's unwind: os.replace(backup_meta, meta_path) restores the pre-existing file back to where it always was -- not a relocation, a revert",
    ("coordinator_core", "install/maximalist.py", "_install_claude_doe_wrapper", "os.replace", "os.replace(tmp_dst, wrapper_dst)", 1): "claude_doe_wrapper symlink swap: os.replace(tmp_dst, wrapper_dst) is an atomic tmp->final symlink swap; temp source never claimed",
    ("coordinator_core", "locked_write.py", "locked_rmw", "os.replace", "os.replace(tmp_path, str(target_path))", 1): "locked_rmw's atomic tmp->final write; temp source never claimed",
    ("coordinator_core", "ops/fleet/_common.py", "archive_and_commit", "Path.rename", "move.dst.rename(move.src)", 1): "archive_and_commit: move.dst.rename(move.src) reverses a git-mv that landed on disk before the index update failed -- restores content to the ORIGINAL path, so any pre-existing claim on src (the restored destination) stays correctly attributed; this is an undo, not a forward relocation",
    ("coordinator_core", "ops/fleet/_common.py", "archive_and_commit", "Path.rename", "move.dst.rename(move.src)", 2): "archive_and_commit: move.dst.rename(move.src) reverses ALL acted renames after a failed batch commit -- same undo-to-original-path reasoning as above",

    # --- Category 5 (R6): sites the plan's C3 sweep never saw -- added to  ---
    # --- the tree after the sweep (or after the allow-list was last       ---
    # --- regenerated) and only surfaced once the Windows separator bug    ---
    # --- (fixed above, see `_scan_root`'s `rel = path.relative_to(root).  ---
    # --- as_posix()`) stopped masking every subdirectory finding. Same    ---
    # --- source-side discriminator as Category 4 above; none are hazards.---
    ("bin", "lib/halted_marker.py", "sync_halted_marker", "os.replace", "os.replace(tmp_path, marker_path)", 1): "atomic tmp->final rename; temp source is a fresh `.tmp-<pid>` sibling never touch()-claimed",
    ("bin", "publish.py", "_swap_publish_staging_into_dest", "os.rename", "os.rename(prior_backup / '.git', dest_dir / '.git')", 1): "dest_dir/prior_backup point at an external sibling-repo publish destination, outside this worktree entirely -- same reasoning as this function's other three already-allowlisted renames",
    ("bin", "tests/test_publish_swap_preserves_dest_git.py", "_stranded_prior_dir", "Path.rename", "stray.rename(prior)", 1): "test fixture: stray/prior both resolve under pytest tmp_path -- not the process's real worktree",
    ("bin", "tests/test_publish_swap_preserves_dest_git.py", "test_arm_h_stranded_prior_glob_metachar_dest_name_ignores_lookalike_sibling", "Path.rename", "lookalike.rename(lookalike_prior)", 1): "test fixture: lookalike/lookalike_prior both resolve under pytest tmp_path -- not the process's real worktree",
    ("bin", "tests/test_publish_swap_preserves_dest_git.py", "test_arm_j_non_matching_directory_is_untouched", "Path.rename", "prior_shaped.rename(prior)", 1): "test fixture: prior_shaped/prior both resolve under pytest tmp_path -- not the process's real worktree",
    ("coordinator_core", "cartography/tests/test_churn.py", "test_op_churn_ratio_bounded_with_deletion_and_rename", "Path.rename", "(src / 'old_name.py').rename(src / 'new_name.py')", 1): "test fixture: src resolves under pytest tmp_path's synthetic git repo -- not the process's real worktree",
    ("coordinator_core", "install/ensure_venv.py", "_swap_in_new_venv", "os.rename", "os.rename(venv_dir, stale_dir)", 1): "venv_dir/stale_dir both resolve under <settings_home>/.coordinator-venv*, outside this worktree entirely -- never touch()-claimed",
    ("coordinator_core", "install/ensure_venv.py", "_swap_in_new_venv", "os.rename", "os.rename(build_dir, venv_dir)", 1): "build_dir/venv_dir both resolve under <settings_home>/.coordinator-venv*, outside this worktree entirely -- never touch()-claimed",
    ("coordinator_core", "install/maximalist.py", "_install_claude_doe_wrapper", "os.replace", "os.replace(tmp_dst, wrapper_dst)", 2): "second claude_doe_wrapper os.replace call in this function, same atomic tmp->final symlink-swap shape as ordinal #1 above; temp source never claimed",
    ("coordinator_core", "ops/cartography_chunk_table.py", "write_chunk_table", "os.replace", "os.replace(tmp_path, str(target))", 1): "atomic tmp->final write of a run-scoped scratch artifact under state/scratch/; temp source (tempfile.mkstemp in the same run dir) never claimed",
    ("coordinator_core", "ops/deliverable_ledger_write.py", "_restore_original_content", "os.replace", "os.replace(restore_tmp_path, artifact_path)", 1): "atomic tmp->final rewrite that RESTORES artifact_path to its pre-write content after a failed rendered-write -- the temp restore file is a fresh sibling never claimed, and artifact_path itself is never relocated, only rewritten in place",
    ("coordinator_core", "ops/deliverable_ledger_write.py", "upsert_deliverable_ledger_rows", "os.replace", "os.replace(tmp_path, artifact_path)", 1): "atomic tmp->final rewrite under held_lock; temp source never claimed, artifact_path rewritten in place (not relocated)",
    ("coordinator_core", "ops/install_doe_claude_precommit_hook.py", "_atomic_write", "os.replace", "os.replace(tmp_path, path)", 1): "atomic tmp->final write of the installed pre-commit hook; temp source is a fresh `.tmp.<pid>` sibling never claimed",
    ("coordinator_core", "ops/reap_orphaned_agent_dirs.py", "_archive_candidate", "Path.rename", "agent_dir.rename(archive_dest)", 1): "agent_dir lives under .git/coordinator-sessions/.agents/, categorically outside claimable space -- same reasoning as session/reap.py's already-allowlisted _reap_stale_agents entry, which this function deliberately mirrors",
    ("coordinator_core", "ops/rewrite_spec_backlinks.py", "rewrite_file", "os.replace", "os.replace(tmp_path, str(target_path))", 1): "atomic tmp->final rewrite of the file being edited in place; temp source never claimed, target_path rewritten in place (not relocated)",
    ("coordinator_core", "ops/test_assert_no_dangling_plan_backlinks.py", "test_archive_round_trip_id_citation_survives_and_gate_stays_clean", "os.rename", "os.rename(os.path.join(root, plan_rel), os.path.join(root, dest_rel))", 1): "test fixture: root resolves under pytest tmp_path -- not the process's real worktree",
    ("coordinator_core", "ops/test_install_doe_claude_precommit_hook.py", "test_hook_survives_the_repo_being_relocated", "Path.rename", "repo.rename(moved)", 1): "test fixture: repo/moved both resolve under pytest tmp_path -- not the process's real worktree",
    ("coordinator_core", "ops/tests/test_deliverable_equivalence.py", "test_seed_zero_write_guard_negative_control_replace", "os.replace", "os.replace(src, violating_dst)", 1): "negative-control test: os.replace is monkeypatched to assert it never reaches an archive/ path, then deliberately called once under pytest tmp_path to prove the guard fires -- not a real relocation, the guard's own probe",
    ("coordinator_core", "session/claims.py", "relocate_artifact_claim", "os.replace", "os.replace(old_claim_dir, new_claim_dir)", 1): "THE sanctioned entrypoint for relocating a claim DIRECTORY (a physical mkdir lock under <base>/<class>-claims/) alongside an artifact rename -- parallel purpose to relocate_touched_path but a different claim mechanism (directory move, not an appended touch-claim event); this IS the re-declare-then-move helper for this claim type",
    ("coordinator_core", "session/core.py", "update_meta_fields", "os.replace", "os.replace(tmp_name, meta_path)", 1): "atomic tmp->final rename; meta_path resolves under .git/coordinator-sessions/<sid>/, outside claimable space, and temp source never claimed",
    ("coordinator_core", "session/em_guard_grant.py", "write_em_guard_grant", "os.replace", "os.replace(tmp_name, grant_file)", 1): "atomic tmp->final rename; grant_file resolves under .git/coordinator-sessions/<sid>/, outside claimable space, and temp source never claimed",
    ("coordinator_core", "session/receiver_state.py", "write_receiver_state", "os.replace", "os.replace(tmp_name, path)", 1): "atomic tmp->final rename; path resolves under .git/coordinator-sessions/<sid>/receiver-state.json, outside claimable space, and temp source never claimed",
    ("coordinator_core", "session/scope.py", "_drop_owned_agent_dirs", "os.rename", "os.rename(agent_dir, archive_dest)", 1): "agent_dir lives under .git/coordinator-sessions/.agents/, categorically outside claimable space -- same reasoning as this module's own already-allowlisted archive() entry",
    ("coordinator_core", "tests/test_locked_write_held_lock.py", "test_git_dir_rename_succeeds_when_lock_sidecar_is_outside_it", "os.rename", "os.rename(str(renamed_repo / '.git'), str(staging / '.git'))", 1): "test fixture: renamed_repo/staging both resolve under pytest tmp_path -- not the process's real worktree",
    ("coordinator_core", "workstream_complete/directives_review.py", "record_gate_memo", "os.replace", "os.replace(tmp_str, str(path))", 1): "atomic tmp->final write of a gate memoisation marker; temp source (tempfile.mkstemp in the same dir) never claimed",

    # --- 2026-08-19: sites that landed after this allow-list was last
    # --- curated. All classified no-strand (atomic tmp->final writes whose
    # --- temp source no session ever touched, git-internal telemetry sinks,
    # --- a directory publish-swap, and machine-managed notice files).
    ('coordinator_core', 'benchmarks/shim_fanin_measure.py', 'run_and_record', 'os.replace', 'os.replace(tmp_path, RECORD_PATH)', 1):
        "atomic tmp->final benchmark-record write (C3 no-strand class) -- "
        "the temp source is created by this function microseconds earlier "
        "and was never touched by any session, so there is no claim to "
        "restate onto the destination",
    ('coordinator_core', 'benchmarks/shim_inprocess_measure.py', 'run_and_record', 'os.replace', 'os.replace(tmp_path, RECORD_PATH)', 1):
        "atomic tmp->final benchmark-record write (C3 no-strand class) -- "
        "same shape as shim_fanin_measure.run_and_record above; unclaimed "
        "temp source",
    ('coordinator_core', 'benchmarks/shim_prototype_measure.py', 'run_and_record', 'os.replace', 'os.replace(tmp_path, RECORD_PATH)', 1):
        "atomic tmp->final benchmark-record write (C3 no-strand class) -- "
        "same shape as shim_fanin_measure.run_and_record above; unclaimed "
        "temp source",
    ('coordinator_core', 'install/fleet_env.py', '_atomic_write_registry_unlocked', 'os.replace', 'os.replace(tmp_path, str(registry_path))', 1):
        "atomic tmp->final registry write (C3 no-strand class) -- the temp "
        "source is this function's own, never session-touched; routing it "
        "through relocate_touched_path would also re-enter the session "
        "engine from inside the fleet-env install path, which must stay "
        "self-contained",
    ('coordinator_core', 'install/fleet_env.py', '_swap_in_new_env', 'os.rename', 'os.rename(build_dir, env_root)', 1):
        "publish-by-rename of an environment DIRECTORY tree, not a tracked "
        "file -- the two os.rename calls are the metadata-only swap that "
        "keeps a concurrent reader executing out of the old tree coherent "
        "(see the function's own docstring); neither operand is a repo path "
        "a session can hold a T-claim on",
    ('coordinator_core', 'install/fleet_env.py', '_swap_in_new_env', 'os.rename', 'os.rename(env_root, stale_dir)', 1):
        "publish-by-rename of an environment DIRECTORY tree, not a tracked "
        "file -- the two os.rename calls are the metadata-only swap that "
        "keeps a concurrent reader executing out of the old tree coherent "
        "(see the function's own docstring); neither operand is a repo path "
        "a session can hold a T-claim on",
    ('coordinator_core', 'install/fleet_env.py', '_write_pth_unlocked', 'os.replace', 'os.replace(tmp_path, str(dest))', 1):
        "atomic tmp->final .pth write (C3 no-strand class) -- unclaimed "
        "temp source, same reasoning as _atomic_write_registry_unlocked "
        "above",
    ('coordinator_core', 'ops/app_session.py', '_write_handle', 'Path.replace', 'tmp.replace(_handle_path(repo_root, key))', 1):
        "atomic tmp->final app-session handle write (C3 no-strand class) -- "
        "unclaimed temp source",
    ('coordinator_core', 'ops/peer_notice_send.py', '_peer_notice_send', 'Path.replace', 'tmp_path.replace(notice_path)', 1):
        "atomic tmp->final peer-notice write (C3 no-strand class) -- the "
        "tmp file is created and replaced inside this call so a concurrent "
        "reader never sees a partial notice; the temp source is never "
        "session-touched",
    ('coordinator_core', 'ops/tests/test_peer_notice_channel.py', 'test_delivered_notice_excluded_from_unread', 'Path.replace', 'notice_path.replace(delivered_dir / notice_path.name)', 1):
        "test fixture -- moves a notice into .delivered/ to set up the "
        "excluded-from-unread assertion; a fixture arranging its own tmp "
        "corpus has no claim to strand",
    ('coordinator_core', 'ops/workflow_fire/fire.py', '_write_record', 'os.replace', 'os.replace(tmp, path)', 1):
        "atomic tmp->final workflow-fire record write (C3 no-strand class) "
        "-- unclaimed temp source",
    ('coordinator_core', 'telemetry/log_rotation.py', 'rotate_if_needed', 'os.replace', 'os.replace(sink, _generation_path(sink, 1))', 1):
        "log-generation cascade under .git/coordinator-sessions/logs/ -- "
        "git-internal telemetry sinks, never a tracked repo path, so no "
        "T-claim can exist on either operand; this also runs on the "
        "op-latency hot path, where re-entering the session engine per "
        "rotation is exactly the amplification this repo's cost doctrine "
        "forbids",
    ('coordinator_core', 'telemetry/log_rotation.py', 'rotate_if_needed', 'os.replace', 'os.replace(src, dst)', 1):
        "log-generation cascade under .git/coordinator-sessions/logs/ -- "
        "git-internal telemetry sinks, never a tracked repo path, so no "
        "T-claim can exist on either operand; this also runs on the "
        "op-latency hot path, where re-entering the session engine per "
        "rotation is exactly the amplification this repo's cost doctrine "
        "forbids",
    ('coordinator_core', 'write_guards/nudge_peer_notice_unread.py', '_deliver', 'Path.replace', 'path.replace(delivered_dir / path.name)', 1):
        "machine-managed notice sink moved into .delivered/ from inside a "
        "PreToolUse hook -- the notice is written by peer_notice.send and "
        "consumed by this guard, never touched by an EM session, so there "
        "is no claim to restate; and this runs on the hook hot path where "
        "pulling in the session engine is the cost the guard chain is built "
        "to avoid. The move is already fully fail-open (OSError swallowed) "
        "so a bookkeeping failure could not surface here anyway",

}

_METHODS = {"move", "rename", "replace"}


_GITIGNORED_SCRATCH_GLOBS = ("*.wip.*.bak", "*.your-wip.*.bak")
"""Patterns from the repo's own ``.gitignore`` (its ``*.wip.*.bak`` /
``*.your-wip.*.bak`` lines) for editor/session scratch backup files that can
land beside real source on a shared, concurrently-edited tree. This is a raw
filesystem walk, not ``git ls-files``, so without this filter a peer
session's untracked scratch backup reads as a real module and gets AST-
parsed for relocation calls -- it is never a real call site, just a stale
copy of one that already has (or will have) its own real-file finding."""


def _iter_files(root: Path):
    import fnmatch

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if any(fnmatch.fnmatch(fn, pat) for pat in _GITIGNORED_SCRATCH_GLOBS):
                continue
            yield Path(dirpath) / fn


def _build_alias_map(tree: ast.Module) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return (module_aliases, func_aliases) from this module's own
    ``import``/``from ... import`` statements.

    ``module_aliases`` maps a local name to the module it refers to
    (``"shutil"`` or ``"os"``) -- covers ``import shutil``, ``import shutil
    as sh``, ``import os as _os``. ``func_aliases`` maps a local name to a
    kind string directly, for a name bound to the function object itself --
    covers ``from shutil import move``, ``from os import rename as rn``.
    """
    module_aliases: Dict[str, str] = {}
    func_aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("shutil", "os"):
                    module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module == "shutil":
                for alias in node.names:
                    if alias.name == "move":
                        func_aliases[alias.asname or alias.name] = "shutil.move"
            elif node.module == "os":
                for alias in node.names:
                    if alias.name in ("rename", "replace"):
                        func_aliases[alias.asname or alias.name] = f"os.{alias.name}"
    return module_aliases, func_aliases


_MODULE_LEVEL = "<module>"
"""Sentinel enclosing-function name for a call that sits outside any
`def`/`async def` -- a module-level allow-list key never crashes on
``None``/empty-string, and stays visually distinct from any real
function name."""


def _find_enclosing_scope(tree: ast.AST, lineno: int) -> List[ast.stmt]:
    """Statement list of the innermost function containing `lineno`, else
    module body -- ported from ``test_no_bare_argv0_script_launch``'s
    identically-named helper."""
    best = None

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            nonlocal best
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                best = node.body
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    _Visitor().visit(tree)
    return best if best is not None else list(getattr(tree, "body", []))


def _find_enclosing_function_name(tree: ast.AST, lineno: int) -> str:
    """Name of the innermost function containing `lineno`, or
    `_MODULE_LEVEL` if `lineno` sits outside every `def`/`async def`.

    Same descent rule as `_find_enclosing_scope` (innermost match wins),
    returning the function's name instead of its statement list -- this is
    the allow-list key's function component. A function *rename* changes
    this value and so is meant to fail the drift test (a rename is a real
    semantic change worth re-ratifying); a line shift anywhere else in the
    file does not touch it.
    """
    best = None

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            nonlocal best
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                best = node.name
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    _Visitor().visit(tree)
    return best if best is not None else _MODULE_LEVEL


def _find_assignment(scope_stmts: List[ast.stmt], name: str, before_lineno: int) -> Optional[ast.stmt]:
    """Nearest preceding simple `name = ...` in `scope_stmts` -- ported from
    ``test_no_bare_argv0_script_launch``'s identically-named helper."""
    candidates = []
    wrapper = ast.Module(body=scope_stmts, type_ignores=[])
    for stmt in ast.walk(wrapper):
        if isinstance(stmt, ast.Assign) and stmt.lineno < before_lineno:
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    candidates.append(stmt)
    if not candidates:
        return None
    candidates.sort(key=lambda s: s.lineno)
    return candidates[-1]


def _resolve_module_base(
    node: ast.AST,
    tree: ast.AST,
    before_lineno: int,
    module_aliases: Dict[str, str],
    depth: int = 0,
) -> Optional[str]:
    """Trace a `Name` node back through local reassignment (`m = shutil`) to
    decide whether it ultimately refers to the `shutil` or `os` module
    object. Depth-limited the same way the house pattern's `_classify` is."""
    if depth > 5 or not isinstance(node, ast.Name):
        return None
    if node.id in module_aliases:
        return module_aliases[node.id]
    if node.id in ("shutil", "os"):
        return node.id
    scope = _find_enclosing_scope(tree, before_lineno)
    assign = _find_assignment(scope, node.id, before_lineno)
    if assign is None or getattr(assign, "value", None) is None:
        return None
    return _resolve_module_base(assign.value, tree, assign.lineno, module_aliases, depth + 1)


def _classify_call(
    node: ast.Call,
    tree: ast.Module,
    module_aliases: Dict[str, str],
    func_aliases: Dict[str, str],
) -> str | None:
    """Return a kind string ("shutil.move" | "os.rename" | "os.replace" |
    "Path.rename" | "Path.replace") if `node` is a relocation call this
    guard cares about, else None."""
    f = node.func

    # A direct from-import binding of the function itself:
    # `from shutil import move; move(a, b)`.
    if isinstance(f, ast.Name) and f.id in func_aliases:
        return func_aliases[f.id]

    if not isinstance(f, ast.Attribute) or f.attr not in _METHODS:
        return None

    base = _resolve_module_base(f.value, tree, node.lineno, module_aliases)
    if base == "shutil" and f.attr == "move":
        return "shutil.move"
    if base == "os" and f.attr == "rename":
        return "os.rename"
    if base == "os" and f.attr == "replace":
        return "os.replace"

    # Bare `.rename(...)` / `.replace(...)` -- ambiguous at the syntax level
    # (str.replace, dict-like .replace, pandas .rename, ...) without type
    # inference. Narrow to Path.rename(target)/Path.replace(target)'s exact
    # call shape: exactly one positional argument and no keywords, OR the
    # single-keyword form `target=...`. str.replace always takes >= 2
    # positional arguments, so neither shape ever matches it.
    if f.attr in ("rename", "replace"):
        if len(node.args) == 1 and not node.keywords:
            return f"Path.{f.attr}"
        if not node.args and len(node.keywords) == 1 and node.keywords[0].arg == "target":
            return f"Path.{f.attr}"

    return None


def _scan_root(root: Path) -> List[Tuple[str, str, int, int, str, str]]:
    """Return `(relpath, enclosing_function, lineno, col_offset, kind,
    call_signature)` for every relocation call site found under `root`,
    un-ordinalled -- `_scan_all` below groups these by `(root, relpath,
    enclosing_function, kind, call_signature)` and assigns the residual
    ordinal (rank in `(lineno, col_offset)` order, within one same-signature
    sub-group) once, across both roots. `call_signature` is `ast.unparse` of
    the call node itself -- the call's own text, not its position, is what
    makes this key stable under an unrelated insertion (see `_scan_all`)."""
    findings: List[Tuple[str, str, int, int, str, str]] = []
    for path in _iter_files(root):
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Broad on purpose: the method name itself ("move"/"rename"/
        # "replace") is always present as text regardless of whether the
        # call reaches it via a bare `shutil.move` literal or an aliased/
        # from-imported binding -- narrower literal tokens (`"shutil.move"`,
        # `"os.rename"`) would re-open the exact aliasing blind spot this
        # scan closes.
        if not any(tok in src for tok in ("move", "rename", "replace")):
            continue
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(root).as_posix()
        module_aliases, func_aliases = _build_alias_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                kind = _classify_call(node, tree, module_aliases, func_aliases)
                if kind is not None:
                    func_name = _find_enclosing_function_name(tree, node.lineno)
                    try:
                        sig = ast.unparse(node)
                    except Exception:
                        # Best-effort only -- an unparseable call node still
                        # needs a signature to key on; fall back to its
                        # position so it still surfaces as a real (if oddly
                        # labelled) finding rather than vanishing silently.
                        sig = f"<unparseable@{node.lineno}:{node.col_offset}>"
                    findings.append((rel, func_name, node.lineno, node.col_offset, kind, sig))
    return findings


def _scan_all() -> Dict[Tuple[str, str, str, str, str, int], str]:
    """Return `{(root-name, relpath, enclosing_function, kind,
    call_signature, ordinal): kind}` for every relocation call site found
    across both scan roots.

    Identity is the call's own text (`call_signature`, an `ast.unparse` of
    the call node), NOT a position-derived ordinal -- adding, removing, or
    reordering ANY other call anywhere in the group never touches an
    existing entry's key, because no key depends on its neighbours. `ordinal`
    is a narrow residual that only disambiguates two-or-more BYTE-IDENTICAL
    calls (same `call_signature`) inside one `(root, relpath,
    enclosing_function, kind)` group -- genuinely interchangeable calls by
    construction, so sharing one allow-list reason across them is
    acceptable. Inserting a new same-signature call between two identical
    existing ones still has the old insertion hazard *within that narrow
    sub-group only*; `test_group_membership_change_forces_full_group_re_
    review` below closes that residual by failing the WHOLE
    `(root, relpath, function, kind)` group loud the moment its call-site
    membership changes at all, rather than trusting the ordinal alone.
    """
    per_group: Dict[Tuple[str, str, str, str], List[Tuple[int, int, str]]] = {}
    for root in _SCAN_ROOTS:
        for rel, func_name, lineno, col, kind, sig in _scan_root(root):
            per_group.setdefault((root.name, rel, func_name, kind), []).append((lineno, col, sig))

    found: Dict[Tuple[str, str, str, str, str, int], str] = {}
    for (root_name, rel, func_name, kind), entries in per_group.items():
        sig_rank: Dict[str, int] = {}
        for lineno, col, sig in sorted(set(entries), key=lambda e: (e[0], e[1])):
            sig_rank[sig] = sig_rank.get(sig, 0) + 1
            found[(root_name, rel, func_name, kind, sig, sig_rank[sig])] = kind
    return found


def _group_key(entry_key: Tuple[str, str, str, str, str, int]) -> Tuple[str, str, str, str]:
    """The `(root, relpath, function, kind)` group a full 6-tuple key
    belongs to -- drops `call_signature`/`ordinal`, the two components that
    vary within one group. Used by the group-membership guard below."""
    root, rel, func, kind, _sig, _ordinal = entry_key
    return (root, rel, func, kind)


def test_no_relocation_outside_the_reasoned_allowlist():
    """Every raw shutil.move/os.rename/os.replace/Path.rename/Path.replace
    call site outside coordinator_core.session.scope.relocate_touched_path
    must already be a reasoned entry in `_ALLOWED` -- any other hit is a new
    (or reintroduced) call site that can silently strand a touch-claim on
    relocation."""
    found = _scan_all()
    unexpected = set(found.keys()) - set(_ALLOWED.keys())
    assert not unexpected, (
        "Call coordinator_core.session.scope."
        "relocate_touched_path(session_id, src_rel, dst_rel, cwd=...) "
        "instead of moving/renaming the path directly (it performs the move "
        "AND re-declares any T-claim held on the source onto the "
        "destination, so a relocated file never falls out of the "
        "safe-commit offer) -- new raw relocation call site(s) detected "
        "outside the reasoned allow-list:\n"
        + "\n".join(
            f"  {root}/{rel} in {func}() [{kind}, {sig!r}#{ordinal}] -- {kind}"
            for (root, rel, func, kind, sig, ordinal), kind in sorted(found.items())
            if (root, rel, func, kind, sig, ordinal) in unexpected
        )
    )


def test_allowlist_has_not_gone_stale():
    """If an allow-listed relocation call site is migrated onto
    relocate_touched_path (or otherwise removed), its allow-list entry must
    be removed in the same change -- otherwise the allow-list silently
    drifts from what is actually still a raw call, and a future regression
    reintroduced at that same line would be masked by a stale entry."""
    found_keys = set(_scan_all().keys())
    stale = set(_ALLOWED.keys()) - found_keys
    assert not stale, (
        "Allow-list entries no longer match a real relocation call site -- "
        "remove them from _ALLOWED (the call appears migrated, removed, its "
        "enclosing function renamed, or its own arguments changed):\n"
        + "\n".join(
            f"  {root}/{rel} in {func}() [{kind}, {sig!r}#{ordinal}]"
            for root, rel, func, kind, sig, ordinal in sorted(stale)
        )
    )


def test_allowlist_entries_carry_a_reason():
    """Grandfather-list discipline: every entry must carry a real,
    non-empty one-line reason. A test that can be silenced by appending a
    bare path with an empty string is not a guard."""
    empty = [key for key, reason in _ALLOWED.items() if not reason or not reason.strip()]
    assert not empty, (
        "These allow-list entries carry no reason -- every entry must state "
        "why the relocation there is safe (or, for a documented residual, "
        "why it is knowingly not yet migrated):\n"
        + "\n".join(
            f"  {root}/{rel} in {func}() [{kind}, {sig!r}#{ordinal}]"
            for root, rel, func, kind, sig, ordinal in sorted(empty)
        )
    )


def test_group_membership_change_forces_full_group_re_review():
    """Belt-and-braces on top of the call-signature key: if the SET of
    matching calls in any `(root, relpath, function, kind)` group differs at
    all from what `_ALLOWED` records for that group, every entry in that
    group must fail -- not just the ones whose own key changed.

    Without this, the narrow same-signature ordinal residual (see `_scan_all`
    docstring) could still, in principle, silently re-key an entry within its
    own signature sub-group. This guard makes that structurally impossible:
    a false re-ratification costs one extra reading of an unchanged site: a
    silently re-mapped allow-list entry is a permanent, invisible exemption.
    Fail loud toward re-ratification, always.
    """
    found = _scan_all()
    found_groups: Dict[Tuple[str, str, str, str], set] = {}
    for key in found:
        found_groups.setdefault(_group_key(key), set()).add(key)
    allowed_groups: Dict[Tuple[str, str, str, str], set] = {}
    for key in _ALLOWED:
        allowed_groups.setdefault(_group_key(key), set()).add(key)

    mismatched_groups = {
        group
        for group in set(found_groups) | set(allowed_groups)
        if found_groups.get(group, set()) != allowed_groups.get(group, set())
    }
    affected_entries = {
        key for key in _ALLOWED if _group_key(key) in mismatched_groups
    }
    assert not affected_entries, (
        "The set of relocation call sites in these (root, relpath, function, "
        "kind) groups no longer matches _ALLOWED exactly -- every entry in "
        "an affected group must be re-reviewed and re-keyed, even entries "
        "whose own call_signature/ordinal did not change, because a "
        "membership change anywhere in the group means at least one entry's "
        "assumptions about its neighbours are now stale:\n"
        + "\n".join(
            f"  group {group}"
            for group in sorted(mismatched_groups)
        )
    )


def test_ordinal_insertion_does_not_silently_reassign_an_approved_entry():
    """Regression test for the reviewer-confirmed defect: reproduce two
    allow-listed calls in one function, insert a third same-kind call
    BETWEEN them, and assert the guard does not silently accept the new call
    under either original entry's key.

    Under the OLD positional-ordinal scheme this failed silently: A(1),
    B(2) plus an inserted C between them re-sorted to A(1), C(2), B(3) --
    ordinal 2's key already existed (B's, with B's reason), so it now
    silently referred to C with zero re-review, while unchanged B was
    misreported as a brand-new site under ordinal 3. Keying on the call's
    own `ast.unparse` text instead of position means C's insertion changes
    NOTHING about A's or B's keys -- this test proves that directly, without
    relying on the production `_ALLOWED` table or scan roots at all.
    """
    src_before = (
        "def f(a, b, c):\n"
        "    shutil.move(a, 'first')\n"
        "    shutil.move(b, 'second')\n"
    )
    src_after_insertion = (
        "def f(a, b, c):\n"
        "    shutil.move(a, 'first')\n"
        "    shutil.move(c, 'inserted')\n"
        "    shutil.move(b, 'second')\n"
    )

    def _scan_signatures(src: str) -> Dict[Tuple[str, int], str]:
        tree = ast.parse(src)
        module_aliases, func_aliases = _build_alias_map(tree)
        out: Dict[Tuple[str, int], str] = {}
        rank: Dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                kind = _classify_call(node, tree, module_aliases, func_aliases)
                if kind is not None:
                    sig = ast.unparse(node)
                    rank[sig] = rank.get(sig, 0) + 1
                    out[(sig, rank[sig])] = kind
        return out

    before = _scan_signatures(src_before)
    after = _scan_signatures(src_after_insertion)

    # Both pre-existing calls' keys are untouched by the insertion -- this is
    # the property the old ordinal scheme violated.
    for key in before:
        assert key in after, f"insertion silently removed/re-keyed {key}"
        assert after[key] == before[key]

    # The inserted call gets its OWN new key, distinct from both existing
    # entries -- it does not, and cannot, collide with either one.
    inserted_keys = set(after) - set(before)
    assert inserted_keys == {("shutil.move(c, 'inserted')", 1)}


# ---------------------------------------------------------------------------
# Self-tests for the scanner itself -- prove the detector actually detects.
# ---------------------------------------------------------------------------


def _classify_src(src: str) -> List[str]:
    """Test helper: parse `src`, build its alias map, and return the kind
    for every Call node found (in source order)."""
    tree = ast.parse(src)
    module_aliases, func_aliases = _build_alias_map(tree)
    kinds = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kinds.append(_classify_call(node, tree, module_aliases, func_aliases))
    return kinds


def test_scanner_flags_a_bare_shutil_move(tmp_path):
    src = "import shutil\ndef f(a, b):\n    shutil.move(a, b)\n"
    mod = tmp_path / "probe_mod.py"
    mod.write_text(src)
    assert _classify_src(src) == ["shutil.move"]


def test_scanner_flags_a_bare_path_rename():
    src = "def f(p, dst):\n    p.rename(dst)\n"
    assert _classify_src(src) == ["Path.rename"]


def test_scanner_flags_a_bare_path_replace():
    src = "def f(p, dst):\n    p.replace(dst)\n"
    assert _classify_src(src) == ["Path.replace"]


def test_scanner_ignores_str_replace_two_args():
    """str.replace(old, new) always takes >= 2 positional args -- the
    single-positional-arg heuristic must not flag it."""
    src = "def f(s):\n    return s.replace('a', 'b')\n"
    assert _classify_src(src) == [None]


def test_scanner_accepts_os_rename_and_os_replace():
    src = "import os\ndef f(a, b, c, d):\n    os.rename(a, b)\n    os.replace(c, d)\n"
    assert set(_classify_src(src)) == {"os.rename", "os.replace"}


def test_scanner_flags_a_from_import_move():
    """`from shutil import move; move(a, b)` used to escape entirely -- the
    aliasing blind spot this dispatch closes."""
    src = "from shutil import move\ndef f(a, b):\n    move(a, b)\n"
    assert _classify_src(src) == ["shutil.move"]


def test_scanner_flags_an_aliased_module_import():
    """`import shutil as sh; sh.move(a, b)` -- module import-alias."""
    src = "import shutil as sh\ndef f(a, b):\n    sh.move(a, b)\n"
    assert _classify_src(src) == ["shutil.move"]


def test_scanner_flags_an_aliased_os_import():
    """`import os as _os; _os.rename(a, b)` -- module import-alias for os."""
    src = "import os as _os\ndef f(a, b):\n    _os.rename(a, b)\n"
    assert _classify_src(src) == ["os.rename"]


def test_scanner_flags_a_rebound_local_name():
    """`m = shutil; m.move(a, b)` -- one level of local reassignment must
    still resolve back to the `shutil` module object."""
    src = "import shutil\ndef f(a, b):\n    m = shutil\n    m.move(a, b)\n"
    assert _classify_src(src) == ["shutil.move"]


def test_scanner_flags_path_rename_keyword_target():
    """`p.rename(target=dst)` -- the single-keyword form, previously
    excluded by the positional-only heuristic."""
    src = "def f(p, dst):\n    p.rename(target=dst)\n"
    assert _classify_src(src) == ["Path.rename"]


def test_scanner_flags_path_replace_keyword_target():
    src = "def f(p, dst):\n    p.replace(target=dst)\n"
    assert _classify_src(src) == ["Path.replace"]


@pytest.mark.parametrize("root", _SCAN_ROOTS, ids=lambda p: p.name)
def test_scan_roots_exist(root):
    """Guards against a silent no-op scan if a root gets renamed later."""
    assert root.is_dir(), f"expected scan root {root} to exist"
