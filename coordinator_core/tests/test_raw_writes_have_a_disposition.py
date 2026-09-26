"""
coordinator_core.tests.test_raw_writes_have_a_disposition — the gate (D2):
every non-test ``coordinator_core`` module that carries a raw file-write
primitive must carry a recorded disposition, or the gate goes RED.

Spec backlink: docs/plans/2026-09-11-state-writers-claim-through-one-seam.md
§ C8, § D2. Register seeded from state/audits/2026-09-11-state-writer-census.md
(C3) at this chunk's commit.

Why this exists
================
Before this gate, "does this module's raw write have an owner?" was a
question nobody was forced to answer. A module could write to ``state/``
outside the seam (``coordinator_core/session/claimed_write.py``) and nothing
noticed, because the census that would have caught it (state/audits/
2026-09-11-state-writer-census.md, C3) is a point-in-time document, not a
standing check. The remembered claim — "we'll migrate it later" — failed by
construction: there was no artifact forcing the remembering. This gate is
that artifact. From this commit forward, a peer's new raw writer cannot land
undecided while the migration batches (C5-C7) run, because the gate is live
and its register is the ONLY thing that turns it green.

Why bytes, not AST
==================
Census row 3 measured an AST-parse gate at ~1.8s of process time to parse
just the pre-filtered hits, before any analysis — over 3.5x this repo's
500ms brightline before the gate does any work. The bytes-level walk (census
row 2) measured 297ms for the whole ``coordinator_core`` package. That is
still over CLAUDE.md's "one process over 200ms needs a fix, not a
rationale" line, so this gate's own walk (below) is a three-phase
literal-prefilter over the same vocabulary rather than one alternation
regex run against every file's full bytes — same population, cheaper to
compute. Measured at authoring time, three repeated runs:

  - the original single-regex walk (census row 2's exact pattern): ~332ms
    (mean of 3 runs; range 332-337ms).
  - this gate's three-phase prefiltered walk (identical vocabulary, same
    281-module population): ~184ms (mean of 3 runs; range 182-187ms).
  - a `git grep --untracked -lE` spawn over the same vocabulary, measured
    for comparison: ~1469ms of child process time with a naive pathspec
    that (unlike the walk) does not exclude ``tests``/``testing``/
    ``benchmarks``/``__pycache__`` or ``test_*.py``/``conftest.py`` — a
    `git grep` variant that replicated the walk's exclusions was not
    pursued further once the python-side prefilter alone already beat the
    200ms bar, since census row 3's own finding is that a spawned process
    is a cost that "justifies itself per use" (CLAUDE.md) and this walk
    needs no spawn at all. The kept scan is therefore the prefiltered
    bytes-level Python walk, never a `git grep` spawn. `git grep` qualifies
    only if its module set equals the walk's; it was not re-measured with
    matching exclusions because the walk already cleared the budget.

The asserted budget is 200ms flat (never the repo's 500ms brightline): the
measured figure (~184ms) plus a ~16ms stated headroom for host variance,
landing exactly at the AC6 ceiling ("at or below 200ms"). If a future
change pushes the live scan over 200ms, the fix is to cut the walk's cost
further (fewer files touched twice, a cheaper prefilter) — a looser
assertion is not a fix.

The vocabulary (``_RAW_LITERALS`` / ``_RAW_OPEN_RE`` / ``_RAW_MODE_RE``
below) is census row 2's ``RAW`` pattern, decomposed into a literal-substring
prefilter plus two narrower regexes covering only the ``open(...)`` and
``mode=...`` call shapes that need real pattern matching. It is stated once,
here, and is the same vocabulary the census used to build the population
this gate's register covers.

What the gate checks, and what it does not
============================================
The gate never decides whether a raw write actually targets ``state/``. It
requires only that a decision — a disposition — exists for every module its
scan flags. That is the "prove, don't decide" shape of
docs/plans/2026-09-11-invert-the-write-detector-prove-read-only.md, applied
at module grain instead of call-site grain.

A flagged module passes only if ONE of the following covers it:

  - a **rule**: it is the seam module itself
    (``coordinator_core/session/claimed_write.py``), or one of the two
    primitives the seam delegates to (``coordinator_core/atomic_append.py``,
    ``coordinator_core/atomic_replace.py``); or it sits under
    ``coordinator_core/install/`` and declares a module-level
    ``WRITE_SURFACE``. The install-prefix scoping matters: a module outside
    ``coordinator_core/install/`` that happens to declare a variable named
    ``WRITE_SURFACE`` is NOT covered by this rule — it goes through the
    register like any other module. Six ``coordinator_core/ops/`` modules
    declare ``WRITE_SURFACE`` and are registered as ``claims-explicitly``
    below, not rule-covered, for exactly this reason.
  - a **register entry** in ``_DISPOSITIONS: dict[str, tuple[str, str]]``,
    keyed by repo-relative POSIX path, valued ``(category, reason)``. The
    category set is closed to exactly six members: ``claims-explicitly``,
    ``outside-repo``, ``git-internal``, ``ignored-target``,
    ``in-repo-non-state``, ``to-fix``.

Only two of the six are mechanically enforced. ``claims-explicitly`` is
TOKEN-checked: the gate greps the module's own bytes for one of the seam's
claim tokens (``declare_write(``, ``_scope_touch_paths``,
``touch_written_path(``, ``_SCOPE_TOUCH_PATHS_KEY``) and fails the entry if
none is present. Importing the seam is not itself a claim token — a module
that imports ``claimed_write`` but never calls ``declare_write`` could still
hold an unclaimed raw ``state/`` write elsewhere in the same module, so the
token check looks for an actual call/reference, not an import line.
``to-fix`` is CEILING-checked against ``_TO_FIX_CEILING``, a frozenset that
can only shrink (see "Ceiling shrink is review-time, not mechanical" below).
The other four labels (``outside-repo``, ``git-internal``, ``ignored-target``,
``in-repo-non-state``) are UNCHECKED — the gate verifies only that the
category is one of the closed six and that the reason is non-blank. The
label itself is C3's evidence, carried into the register; it is not an
assertion this gate makes about the module's actual write target.

A disposition describes a module's RESIDUAL raw write, never its seam
writes. A module that has moved its ``state/`` writes onto the seam but
still holds one raw write elsewhere (a tempfile, a write outside the repo)
is still flagged by the scan, and its register entry names that residual's
category (typically ``outside-repo`` or ``in-repo-non-state``), not
``claims-explicitly`` — because ``claims-explicitly``'s token check would
pass (the seam call is present) while the residual raw write stays
unaccounted for. This is deliberate: ``claims-explicitly`` means the
module's remaining raw-shaped bytes ARE the claim call itself, not that the
module also happens to import the seam elsewhere.

Ceiling shrink is review-time, not mechanical
================================================
``_TO_FIX_CEILING`` sits in this same file as ``_DISPOSITIONS`` and nothing
in this gate mechanically stops a future edit from WIDENING it — that
property is enforced by review (a peer checking a diff to this file
confirms the ceiling only shrank), not by a runtime assertion. What the gate
DOES enforce mechanically: every ``to-fix`` register entry's module must be
a ceiling member (an entry outside the ceiling is RED), and every ceiling
member must have a ``to-fix`` register entry naming a module that still
carries a raw token (a ceiling member with no entry, or whose module has
lost its raw token without the entry being deleted, is a STALE ceiling
member and is RED). That symmetry is what forces a migration batch (C5-C7)
to delete a module's register entry AND its ceiling membership in the same
commit — the stale-check on one side and the stale-ceiling check on the
other leave no way to land only one half.

The register-shape and stale-check precedent is
``coordinator_core/tests/test_session_dir_has_one_constructor.py``
(``_EXEMPT_SITES``, ``test_named_exemption_still_describes_a_real_site``): a
named exemption that stops describing a real site is deleted, not carried
forward, so a future refactor re-trips the gate instead of riding a dead
entry forever. The same discipline applies to ``_DISPOSITIONS`` entries
here: a stale entry (module gone, or the module no longer carries a raw
token) is RED, not silently ignored.

Named residuals (negative spec) — what this gate does NOT prove
===================================================================
1. **A dispositioned module can still add a second, unclaimed raw write and
   stay green.** ``claims-explicitly`` is a TOKEN check, not a per-site
   coverage proof: a module holding one claimed write and one brand-new
   unclaimed write passes as long as the claim token is present anywhere in
   its bytes. This gate proves that every raw-writing module has a recorded
   decision; it does NOT prove that every raw-write site inside a
   dispositioned module is covered. The runtime instrument (D3,
   ``coordinator_core/testing/state_write_audit.py``) is the on-demand,
   single-run re-check that answers that narrower question when it is
   asked — it is opt-in and never a standing gate, because an audit hook
   cannot be uninstalled once loaded.
2. **The raw-primitive vocabulary itself has a gap.** ``os.rename``,
   ``Path.rename``, ``Path.touch``, ``shutil.*``, ``sqlite3.connect`` and
   ``zipfile.ZipFile`` are not in ``_RAW_LITERALS``/``_RAW_OPEN_RE``/
   ``_RAW_MODE_RE``. A module whose only raw write goes through one of
   these calls is invisible to this gate's scan and needs no disposition to
   pass, even though it may write to ``state/``. Widening the vocabulary is
   a deliberate future change (it changes the scanned population and
   therefore the register), not a defect in this gate as authored.
"""

from __future__ import annotations

import os
import pathlib
import re
import time
from typing import Dict, FrozenSet, List, Optional, Tuple

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_SCAN_ROOT = "coordinator_core"
_EXCLUDED_DIRS = frozenset({"tests", "testing", "benchmarks", "__pycache__"})

_RAW_LITERALS: Tuple[bytes, ...] = (
    b".write_text(",
    b".write_bytes(",
    b"os.fdopen(",
    b"json.dump(",
    b"os.replace(",
    b"O_CREAT",
    b"append_line(",
    b"mkstemp(",
)
_RAW_OPEN_RE = re.compile(rb"""open\([^)\n]*['"][wax]b?\+?['"]""")
_RAW_MODE_RE = re.compile(rb"""mode\s*=\s*['"][wax]""")

_CLAIM_TOKEN_RE = re.compile(
    rb"declare_write\(|_scope_touch_paths|touch_written_path\(|_SCOPE_TOUCH_PATHS_KEY"
)

#: A module-level ``WRITE_SURFACE = ...`` / ``WRITE_SURFACE: ... = ...``
_WRITE_SURFACE_RE = re.compile(rb"^WRITE_SURFACE\s*[:=]", re.M)

_CATEGORIES = frozenset(
    {
        "claims-explicitly",
        "outside-repo",
        "git-internal",
        "ignored-target",
        "in-repo-non-state",
        "to-fix",
    }
)

_RULE_SEAM_MODULES = frozenset(
    {
        "coordinator_core/session/claimed_write.py",
        "coordinator_core/atomic_append.py",
        "coordinator_core/atomic_replace.py",
    }
)

#: The install-prefix a module must sit under for the ``WRITE_SURFACE`` rule
#: to apply. A module outside this prefix that declares ``WRITE_SURFACE`` is
_INSTALL_PREFIX = "coordinator_core/install/"

#: (seam/primitive), 15 rule-covered (WRITE_SURFACE/install), and 263 with an
#: RECATEGORIZED (never deleted) to the residual's own category —
#: to-fix members held no actual `state/` write and are RECATEGORIZED

_DISPOSITIONS: Dict[str, Tuple[str, str]] = {
    'coordinator_core/async_hook_status.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: record_failure'),
    'coordinator_core/authz/classification.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: <module-level>'),
    'coordinator_core/authz/token.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_tokens'),
    'coordinator_core/bash_guards/_alternative_liveness.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: <module-level>, _trigger_destructive_git_revert, _trigger_destructive_git_revert_advisory, _trigger_host_subagent_policy_guard'),
    'coordinator_core/bash_guards/_dialect.py': ('to-fix', 'raw-write site(s): _log_dialect_parser_unavailable; runtime observed=yes (n=24), sample=/tmp/pytest-of-root/pytest-716/home-quarantine21712/.coordinator-claude-settings/state/dialect-parser-unavailable.log'),
    'coordinator_core/bash_guards/_write_bump_session_start.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_session_start_record'),
    'coordinator_core/bash_guards/_write_bump_sink_shapes.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _bound_literal_paths, _strip_comments_and_docstrings'),
    'coordinator_core/bash_guards/_write_bump_stand_down.py': ('to-fix', 'raw-write site(s): _mirror_to_durable_sink, log_environment_stand_down; runtime observed=yes (n=15), sample=/tmp/pytest-of-root/pytest-705/test_deny_grant_allow_consumed0/anchor/state/stand-downs/foreign-repo-write.log'),
    'coordinator_core/bash_guards/block_approval_sentinel_creation.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/bash_guards/block_subagent_commit.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _open_call_is_read_only'),
    'coordinator_core/bash_guards/block_subagent_destructive_action.py': ('outside-repo', 'C6-migrated: _log_fail_open now routes through session/claimed_write.py::append_claimed_line (entry wrapped at bash_guards/dispatch.py::main); residual raw-write site(s) near tempdir/home/settings-home construct: _rotate_fail_open_log_if_oversized'),
    'coordinator_core/bash_guards/block_subagent_plan_body_bash_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_block_log'),
    'coordinator_core/bash_guards/chain_arrival_ledger.py': ('to-fix', 'raw-write site(s): <module-level>, _rotate_if_oversize, record_chain_arrival; runtime observed=yes (n=1462), sample=/tmp/pytest-of-root/pytest-705/test_deny_grant_allow_consumed0/isolated-settings-home/state/chain-arrival-ledger/em-grant-e2e-f3ffc92aee9c/chain-arrival-ledger.jsonl'),
    'coordinator_core/bash_guards/commit_tripwires.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _log_pathspec_divergence_override'),
    'coordinator_core/bash_guards/dispatch_checks.py': ('claims-explicitly', 'claim token in check_validate_commit'),
    'coordinator_core/bash_guards/guard_doctrine_surface_bash_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _fold_literal_joins, _interpreter_write_sinks_are_ungoverned'),
    'coordinator_core/block_discharge.py': ('to-fix', 'raw-write site(s): _append_record; runtime observed=not-scanned (n=0), sample=n/a'),
    'coordinator_core/ceremony_common/_phantom_sweep_providers.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: sweep_review_assemble writes its fixture .md files under a pytest tmp_path, never a tracked path in this repo'),
    'coordinator_core/claims_emit.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_atomic_pair'),
    'coordinator_core/commit_ledger/store.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: append_entry._append, record_predecessor_pointer -- target is <git-common-dir>/coordinator-sessions/.commit-ledger/<handoff_id>.jsonl, not state/'),
    'coordinator_core/contract/cockpit_schema/emit_conformance_fixture.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: generate'),
    'coordinator_core/contract/cockpit_schema/emit_schema.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: emit_schemas'),
    'coordinator_core/contract/cockpit_schema/entities/competitor_summary.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/contract/cockpit_schema/provenance.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/contract/emit_memo_schema.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: emit_schemas -- generated schema file, matching contract/cockpit_schema/emit_schema.py and emit_conformance_fixture.py'),
    'coordinator_core/diagnostics/contained_run.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_spawn_script'),
    'coordinator_core/distill/_common.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: active_reference_guard_many'),
    'coordinator_core/distill/log_append.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: append_row, append_rows'),
    'coordinator_core/distill/log_normalize.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: normalize_arrow_dialects_log, normalize_log'),
    'coordinator_core/engine_root_census.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, record_fallback_read'),
    'coordinator_core/execute_plan_assemble/close_out_and_stamp.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _dry_run_scratch_plan, _stamp_plan_landed, apply_tracker_reconciliation, close_out_and_stamp'),
    'coordinator_core/frontmatter/author_dependence.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: main -- writes only to the committed golden fixture coordinator_core/frontmatter/tests/_goldens/author_dependence_labels.json, gated behind --write'),
    'coordinator_core/git/commit_trailers.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: apply_missing_trailers'),
    'coordinator_core/git/eol_declared.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: repair_declared_eol_drift'),
    'coordinator_core/git/git_dir.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/git/git_objects.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _replace_with_retry, append_reflog, cas_ref, write_object'),
    'coordinator_core/git/index_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: splice_index'),
    'coordinator_core/goals/reassess_krs.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _write_goal_file_atomic'),
    'coordinator_core/group_em/atomic_record.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: holder_lock, write_json_atomic'),
    'coordinator_core/group_em/baseline.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _write_atomic'),
    'coordinator_core/group_em/nomination.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _write_json_atomic'),
    'coordinator_core/group_em/obligations.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: record'),
    'coordinator_core/group_em/watch_heartbeat.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: write_atomic'),
    'coordinator_core/group_em/watch_spool.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: prune'),
    'coordinator_core/guard_advisory_counter.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: record_advisory_fire, record_deny_fire'),
    'coordinator_core/handoff_creation_guard.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: assert_no_archived_twin'),
    'coordinator_core/hooks/agent_completion_log.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _append_audit_entry'),
    'coordinator_core/hooks/auto_push.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: log_failure'),
    'coordinator_core/hooks/cater_subagent_start.py': ('claims-explicitly', 'claim token in _write_miss_sentinel'),
    'coordinator_core/hooks/context_pressure_precompact.py': ('to-fix', "raw-write site(s): _write_sentinel, _write_state_snapshot; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/hooks/em_report_altitude.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _mark_fired'),
    'coordinator_core/hooks/nudge_em_code_dispatch.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_pending_dispatch_artifact'),
    'coordinator_core/hooks/nudge_harness_directive_dispatch.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _claim_fire'),
    'coordinator_core/hooks/nudge_unrouted_sizing.py': ('to-fix', "raw-write site(s): _claim_fire; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/hooks/plan_persistence_check.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _append_readme_row, _handler'),
    'coordinator_core/hooks/platform_localize.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: atomic_write'),
    'coordinator_core/hooks/postuse_advisory_dispatch.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _check_group_em_watch_arm_sync, _check_group_em_watch_arm_sync._touch_checked_sentinel, _check_runtime_tripwire_sync, _persist_workflow_run_record, _save_advisory_state'),
    'coordinator_core/hooks/runtime_tripwire_em_check.py': ('to-fix', "raw-write site(s): _check_hooks_json_staleness, _check_push_failures; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/hooks/subagent_zero_tool_use.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _append_record_sync, _persist_final_report_sync'),
    'coordinator_core/hooks/track_dispatched_agents.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _process_dispatched_sync, _write_backpointer_sync'),
    'coordinator_core/hooks/ue_knowledge_distrust.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _run_bootstrap'),
    'coordinator_core/hooks/watchdog_undischarged_next_move.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_records'),
    'coordinator_core/housekeeping/archive_index.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: save_index'),
    'coordinator_core/learn_lessons_pipeline/run_stamp.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: stamp_run_complete -- target is <claude_home>/tasks/learn-lessons-<date>/COMPLETE via ops/learn_lessons_cutoff.resolve_runs_dir, outside every repo'),
    'coordinator_core/install/_shared.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: settings_hook_identity_inverse_strip'),
    'coordinator_core/install/door_install.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _replace_possibly_running_image, install_named_forwarder'),
    'coordinator_core/install/door_uninstall.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_uninstall_fallback_cmd_forwarder'),
    'coordinator_core/install/host_sampler_scheduler.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: register_host_sampler_task'),
    'coordinator_core/install/sandbox_check.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _tier1_filesystem_shape, _tier1b_mirror_and_cold_tier, _tier1b_pointer_and_shim, _tier1c_publish_repo_parity, _write_claude_doe_argv_stub'),
    'coordinator_core/locked_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: held_lock, locked_rmw, replace_with_retry'),
    'coordinator_core/machine_resolver.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: registry_set'),
    'coordinator_core/op_census/module_summary.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: save_index'),
    'coordinator_core/op_census/spawn_bearing_ops.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: save_spawn_index'),
    'coordinator_core/ops/app_session.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_handle'),
    'coordinator_core/ops/archive_auto_memory_rows.py': ('to-fix', 'raw-write site(s): main; runtime observed=yes (n=5), sample=/tmp/pytest-of-root/pytest-716/test_own_body_and_index_row_la0/repo/state/auto-memory-archive/2026-09-19-self-session.md'),
    'coordinator_core/ops/assert_no_dangling_plan_backlinks.py': ('claims-explicitly', "claim token in _fix_file [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/backfill_deliverable_spine.py': ('claims-explicitly', 'claim token in _stamp_file, _stamp_yaml_document'),
    'coordinator_core/ops/backfill_initiative_fk.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _acquire_lock, _attach_batch'),
    'coordinator_core/ops/bootstrap_orchestrate.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _coordinator_currency_write'),
    'coordinator_core/ops/cartography_chunk_table.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: write_chunk_table [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/cartography_symbols.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: write_symbols_artifact'),
    'coordinator_core/ops/cartography_tree.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/ops/ceremony/consumed_handoff_stamp.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _commit_and_push_follow_up'),
    'coordinator_core/ops/ceremony/detached_spawn.py': ('to-fix', 'raw-write site(s): _append_to_failures_archive, _log_spawn_failure, advance_failures_cursor, clear_failures_log, record_child_failure; runtime observed=yes (n=158), sample=/tmp/pytest-of-root/pytest-705/test_exhausts_retries_and_logs0/state/housekeeping-failures.log'),
    'coordinator_core/ops/ceremony/git_native.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _apply_trailers, _write_pathspec_file'),
    'coordinator_core/ops/ceremony/housekeeping_liveness.py': ('to-fix', 'raw-write site(s): stamp_liveness; runtime observed=yes (n=24), sample=/tmp/pytest-of-root/pytest-705/test_stamp_liveness_writes_par0/state/housekeeping-liveness.json'),
    'coordinator_core/ops/ceremony/post_commit_tail.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: _commit_and_push_origin_stub_close, _fold_sha_into_entry_on_disk, _run_completion_entry_fold [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/ceremony/receipt_emit.py': ('claims-explicitly', 'claim token in emit_receipt'),
    'coordinator_core/ops/ceremony/renderers.py': ('to-fix', 'raw-write site(s): _process_file, refresh_roadmap_callout; runtime observed=yes (n=12), sample=/tmp/pytest-of-root/pytest-705/test_matches_real_node_output0/state/roadmap/testroadmap/STUB-INDEX.md'),
    'coordinator_core/ops/ceremony/snapshot_diff_and_head.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _handler'),
    'coordinator_core/ops/changelog_ops.py': ('claims-explicitly', 'claim token in append_day, backfill_gaps'),
    'coordinator_core/ops/check_competitor_positioning_nudge.py': ('claims-explicitly', 'claim token in _record_decline'),
    'coordinator_core/ops/check_posix_exec_assumptions.py': ('to-fix', "raw-write site(s): <module-level>; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/completion_ops.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: append_plan_session [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/coordinator_complete_entry.py': ('claims-explicitly', 'claim token in _write_entry'),
    'coordinator_core/ops/coordinator_setup_state.py': ('claims-explicitly', 'claim token in _atomic_write'),
    'coordinator_core/ops/cruft_sweep.py': ('claims-explicitly', "claim token in _append_log_row [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/decision_record_mint.py': ('to-fix', 'raw-write site(s): <module-level>, mint_next_dr_id; runtime observed=yes (n=9), sample=/tmp/pytest-of-root/pytest-716/test_mint_first_number_in_empt0/state/decision-record-reservations/DR-1.reserved'),
    'coordinator_core/ops/deliverable_cascade.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: _commit_mutated_paths [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/deliverable_equivalence.py': ('to-fix', "raw-write site(s): _is_immutable_path_local; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/deliverable_ledger_write.py': ('to-fix', 'raw-write site(s): _restore_original_content, upsert_deliverable_ledger_rows; runtime observed=yes (n=93), sample=/tmp/pytest-of-root/pytest-716/test_header_bytes_preserved_ve0/state/deliverable-equivalence.yaml.ledger-write.tmp.24772'),
    'coordinator_core/ops/dev_sync.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _sync_plugin'),
    'coordinator_core/ops/dispatch_emit/op.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _dispatch_emit, _write_emission_receipt, restamp'),
    'coordinator_core/ops/distill_apply_disposal.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _delete_tracked_and_append_log, _write_denormalizations, write_apply_receipt'),
    'coordinator_core/ops/distill_disposal_manifest.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: write_disposal_manifest'),
    'coordinator_core/ops/distill_scope.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: write_scope_manifest'),
    'coordinator_core/ops/distill_stamp_disposal.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: write_stamped_manifest'),
    'coordinator_core/ops/docindex_emit.py': ('to-fix', "raw-write site(s): _docindex_emit; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/doctor.py': ('claims-explicitly', 'claim token in _fix_bare_hook_commands'),
    'coordinator_core/ops/dod_floor_ratchet.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_floor'),
    'coordinator_core/ops/edit_live_hook.py': ('claims-explicitly', 'claim token in cmd_commit'),
    'coordinator_core/ops/emit/lma_cache.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _store'),
    'coordinator_core/ops/emit_artifact_shape_contract.py': ('claims-explicitly', 'claim token in _emit'),
    'coordinator_core/ops/emit_withheld_knobs.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/ensure_vscode_readonly.py': ('claims-explicitly', 'claim token in _merge_settings'),
    'coordinator_core/ops/extract_cited_sidecars.py': ('to-fix', 'raw-write site(s): main; runtime observed=yes (n=6), sample=/tmp/pytest-of-root/pytest-716/test_main_writes_both_audit_fi0/state/audits/2026-09-02-cited-subagent-share-sidecars.md'),
    'coordinator_core/ops/fleet/_common.py': ('claims-explicitly', 'claim token in declare_move_claims'),
    'coordinator_core/ops/fleet/_sweep_receipt.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _truncate_if_oversized, record_sweep_outcome'),
    'coordinator_core/ops/fleet/archive_actioned_memos.py': ('to-fix', "INDIRECTION (D3 caught, static missed): os.open(...O_CREAT|O_EXCL...) lock-file create in _acquire lock helper (~L693/708); state write missed by static regex because the target is a Path variable, not a literal 'state/'/STATE_ROOT token nearby"),
    'coordinator_core/ops/fleet/archive_plans.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _acquire_sweep_lock, _apply_untracked_sidecar_moves'),
    'coordinator_core/ops/fleet/archive_terminal_handoffs.py': ('to-fix', "raw-write site(s): _acquire_sweep_lock, apply_sweep; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/fleet/memo_compose.py': ('claims-explicitly', 'claim token in _memo_compose'),
    'coordinator_core/ops/fleet/memo_draft.py': ('claims-explicitly', 'claim token in _memo_draft'),
    'coordinator_core/ops/fleet/memo_heal.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _restore_one, _write_msg_file'),
    'coordinator_core/ops/fleet/memo_reconcile_outbox.py': ('claims-explicitly', 'claim token in _memo_reconcile_outbox, _reconcile'),
    'coordinator_core/ops/fleet/memo_send.py': ('claims-explicitly', "claim token in _memo_send [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/fleet/migrate_handoff_vocabulary.py': ('to-fix', 'raw-write site(s): apply_migration; runtime observed=yes (n=8), sample=/tmp/pytest-of-root/pytest-705/test_idempotent_second_run_is_1/state/handoffs/a.md'),
    'coordinator_core/ops/fleet_machinery_sweep.py': ('to-fix', "raw-write site(s): _write_ignore_block, append_audit, main; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/gen_claude_doe_launcher.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/gen_claude_doe_shim.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/gen_doe_root_pointer.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/generate_exec_summary.py': ('claims-explicitly', "claim token in main [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/generator_provenance.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _call_is_write, _is_excluded_base, _is_fdopen_of_scratch_fd, _promoted_tmp_names, _replace_destination_exprs, _scratch_mkstemp_fds, _tmp_var_info, _write_target_expr'),
    'coordinator_core/ops/generator_scan_cache.py': ('to-fix', "raw-write site(s): save, save_content_cache; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/guard_message_audit.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: main'),
    'coordinator_core/ops/handoff_archive_transition.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: _commit_retained_supersede_flip [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/handoff_repoint_origin.py': ('to-fix', "raw-write site(s): _handler; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/install_doe_claude_precommit_hook.py': ('claims-explicitly', 'claim token in _atomic_write'),
    'coordinator_core/ops/install_lfs_pre_push_hook.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: install'),
    'coordinator_core/ops/install_meta_repo_precommit_hook.py': ('claims-explicitly', 'claim token in _atomic_write'),
    'coordinator_core/ops/install_publish_repo_precommit_hook.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/install_shell_init_guard_seam.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/list_reverse_drift_cmds.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _run'),
    'coordinator_core/ops/memo_fate_partition.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _atomic_write_json'),
    'coordinator_core/ops/memo_transition.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: _commit_terminal_write [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/name_personas.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: main'),
    'coordinator_core/ops/new_project_scaffold.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/normalize_claimed_frontmatter.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/normalize_env.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _ne_darwin_bash_profile_repair, _ne_write_backup_file'),
    'coordinator_core/ops/peer_notice_send.py': ('to-fix', 'raw-write site(s): _peer_notice_send; runtime observed=yes (n=48), sample=/tmp/pytest-of-root/pytest-716/test_send_writes_notice_file0/state/peer-notices/peer-abc/.438833fec0d44a83ab3ac1525b682cdf.json.tmp'),
    'coordinator_core/ops/percolate_build_token_index.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_cursor'),
    'coordinator_core/ops/percolate_preflight_scratch_publish.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: run_scratch_publish, self_test'),
    'coordinator_core/ops/plan_capture_persist.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: persist_captured_plan'),
    'coordinator_core/ops/plan_status_transition.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: _commit_plan_flip, _stamp_implemented, _stamp_reopened, _stamp_review_verified, _stamp_rung, _stamp_superseded [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/propagate_body.py': ('to-fix', 'raw-write site(s): _rollback_delivery; runtime observed=yes (n=2), sample=/tmp/pytest-of-root/pytest-716/test_commit_delivery_failure_r0/repo/state/handoffs/2026-08-01-rollback.md'),
    'coordinator_core/ops/prune_resolved_queue_entries.py': ('claims-explicitly', 'claim token in _atomic_replace'),
    'coordinator_core/ops/queue_append.py': ('claims-explicitly', 'claim token in _queue_append_handler'),
    'coordinator_core/ops/queue_promote.py': ('claims-explicitly', "claim token in _queue_promote_handler [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/reap_orphaned_agent_dirs.py': ('to-fix', "raw-write site(s): _write_audit; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/reap_stale_locks.py': ('claims-explicitly', 'claim token in _append_log'),
    'coordinator_core/ops/register_coordinator_mirror.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: register'),
    'coordinator_core/ops/release_tagging.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _publish_release'),
    'coordinator_core/ops/render_posture_overlay.py': ('claims-explicitly', 'claim token in run'),
    'coordinator_core/ops/render_template.py': ('claims-explicitly', 'claim token in _render_and_write_in_place, main'),
    'coordinator_core/ops/review_freeze_diff.py': ('claims-explicitly', 'claim token in freeze_diff'),
    'coordinator_core/ops/review_mint/op.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _review_mint_workflow'),
    'coordinator_core/ops/rewrite_spec_backlinks.py': ('claims-explicitly', 'claim token in rewrite_file'),
    'coordinator_core/ops/roadmap_link_stubs.py': ('to-fix', "raw-write site(s): _roadmap_id_lock; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/run_shellcheck_sweep.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _lint_files'),
    'coordinator_core/ops/scope_soak_enable.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: enable'),
    'coordinator_core/ops/scope_warning_resolve.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: resolve'),
    'coordinator_core/ops/self_persist_findings.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/ops/session/fix_concrete_path_citations.py': ('claims-explicitly', "claim token in _write_preserving_newlines [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/session/guard_hook_generation_self_probe.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _atomic_write_text, run_self_probe'),
    'coordinator_core/ops/session/guard_settings_integrity.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _atomic_copy, evaluate_settings_integrity'),
    'coordinator_core/ops/session/legacy_touch_corpus_migrate.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/ops/session/migrate_touched_prefix.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: run_migration'),
    'coordinator_core/ops/session/record_pickup.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: _record_pickup_sync, _try_claim_lock_dir [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/session_baton_promote.py': ('to-fix', 'INDIRECTION (D3 caught, static missed): handoff_path.write_text(...) writes the promoted baton handoff (~L215/239); target built from a Path variable, static regex missed the nearby state-signal token'),
    'coordinator_core/ops/session_hierarchy_derive.py': ('claims-explicitly', "claim token in _atomic_write_json [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/session_hierarchy_query.py': ('to-fix', "raw-write site(s): main; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/setup_rag_decision.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_tripwire'),
    'coordinator_core/ops/setup_seed_health_ledger.py': ('to-fix', 'raw-write site(s): seed_health_ledger; runtime observed=yes (n=4), sample=/tmp/pytest-of-root/pytest-716/test_fresh_repo_seeds_ledger0/state/health-ledger.md'),
    'coordinator_core/ops/shim_usage_census.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, record_invocation'),
    'coordinator_core/ops/strategic/draft_writer.py': ('to-fix', "raw-write site(s): write_draft; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/strategic/emit_writer.py': ('to-fix', "raw-write site(s): emit_strategic_feed; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/tracker/advance_status.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, advance_status'),
    'coordinator_core/ops/tracker/push_suggestion.py': ('to-fix', "INDIRECTION (D3 caught, static missed): _write_envelope_file: os.open(...O_CREAT|O_EXCL...)+os.fdopen (~L456-457) creates the tracker envelope file under state/; static regex missed the state-signal token near this indirection"),
    'coordinator_core/ops/verify_subagent_sandbox_preamble_sync.py': ('claims-explicitly', 'claim token in insert_block, rewrite_block'),
    'coordinator_core/ops/workday_complete_step2_5_dirty_tree.py': ('claims-explicitly', "claim token in _append_to_gitignore [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/workflow_fire/fire.py': ('to-fix', "raw-write site(s): _acquire_cap_lock, _write_record, fire_workflow; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/workweek_trail_scope.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/write_identity_file.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/ops/write_workday_start_marker.py': ('claims-explicitly', "claim token in write_marker [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/orientation/expired_grant_signal.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_index_atomically -- target is .coordinator-local/cache/queue-grants-index.json, a best-effort orientation cache, not state/'),
    'coordinator_core/orientation/regenerate_cache.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _atomic_replace'),
    'coordinator_core/p4/register.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _author_gitignore, _ensure_p4ignore, _pin_gitattributes, _upsert_local_md_keys'),
    'coordinator_core/percolate/engine.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _build_inject_scrub_callback._scrub_one_file, run_content_transform_sweep -- target_root is the percolate publish destination tree, never a tracked path in this repo'),
    'coordinator_core/percolate/inject.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: backup_destination_native -- target is the publish destination / consumer_state_home(), never a tracked path in this repo'),
    'coordinator_core/percolate/manifest.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_manifest'),
    'coordinator_core/percolate/prune_dead_registrations.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _prune_registry_map, prune_dead_registrations'),
    'coordinator_core/percolate/rewrite_basename.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _atomic_write_text, write_rename_ledger'),
    'coordinator_core/percolate/round.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: stamp_engine_row'),
    'coordinator_core/percolate/token_index.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: serialize_index'),
    'coordinator_core/plugin_health/sentinel.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_sentinel'),
    'coordinator_core/publish/time_transform.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: rename_codename_basenames, rewrite_plugin_paths, run_fix'),
    'coordinator_core/registry_fallback_counter.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: record_registry_fallback'),
    'coordinator_core/review_trail/reviewed_set.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _append_lines'),
    'coordinator_core/session/claims.py': ('git-internal', 'raw-write site(s) within the claim-directory bookkeeping (pid, session_id, claimed_at, stage files) under the coordinator-sessions hub -- session-internal, per this batch\'s row body: routing session/claims.py through the seam would recurse through the very claim machinery the seam feeds'),
    'coordinator_core/session/claude_md_grant.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_claude_md_write_grant'),
    'coordinator_core/session/context_usage_sidecar.py': ('git-internal', 'raw-write site within 3 lines of a coordinator-sessions/meta.json reference'),
    'coordinator_core/session/core.py': ('git-internal', 'raw-write site within 3 lines of a coordinator-sessions/meta.json reference'),
    'coordinator_core/session/day_branch_cut_lock.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _try_create'),
    'coordinator_core/session/em_guard_grant.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_em_guard_grant'),
    'coordinator_core/session/fleet_delegation.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_fleet_delegation'),
    'coordinator_core/session/fleet_mode.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_fleet_mode'),
    'coordinator_core/session/grant.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_tier_u_grant'),
    'coordinator_core/session/grant_scope.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_tier_u_grant_scope'),
    'coordinator_core/session/receiver_state.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_receiver_state'),
    'coordinator_core/session/scope.py': ('claims-explicitly', 'claim token in _restate_single_path_claim, _restate_tree_claims, touch, touch_written_path'),
    'coordinator_core/session/shape.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _try_claim_lock, session_shape_set'),
    'coordinator_core/session/touch_record.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _rotate_oversized, append_event, compact_record'),
    'coordinator_core/session_baton/store.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: merge_baton, write_baton'),
    'coordinator_core/snippet_sync/verify.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _insert_block, _rewrite_block, run'),
    'coordinator_core/subagent_sandbox/provision_report.py': ('claims-explicitly', 'claim token in _provision, _provision_plan_derivable_doc'),
    'coordinator_core/telemetry/host_sampler.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: sample_once'),
    'coordinator_core/telemetry/log_rotation.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: rotate_if_needed'),
    'coordinator_core/telemetry/op_latency.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _append_line, _write_entry'),
    'coordinator_core/text/refresh_queries.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: process_file'),
    'coordinator_core/warm/breadcrumb.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: try_claim_boot, write_breadcrumb'),
    'coordinator_core/warm/cookie.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_private_atomically'),
    'coordinator_core/warm/door/build.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: build, write_provenance, write_sidecar'),
    'coordinator_core/warm/door/build_posix.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_provenance'),
    'coordinator_core/warm/door_credential.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: ensure_secret'),
    'coordinator_core/warm/election.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _acquire_election_lock'),
    'coordinator_core/warm/front_door.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: flush, write_discovery'),
    'coordinator_core/warm/front_door_routing.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_routing_table'),
    'coordinator_core/warm/push_cadence.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _try_create_sweep_lock'),
    'coordinator_core/warm/server.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _bind_null_std_streams, _wrap_handle'),
    'coordinator_core/warm/skew.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_currency_cache, write_engine_stamp'),
    'coordinator_core/warm/supervisor.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_discovery'),
    'coordinator_core/warm/telemetry.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: flush, record_client_boot_wait, record_client_cold_fallback, record_degrade, record_election_lost, record_publish_warm_attempt, record_server_boot, record_worker_pool_depth'),
    'coordinator_core/workday_complete/autonomous_verb.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: main'),
    'coordinator_core/workflow_watch/stamp.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: stamp_terminal'),
    'coordinator_core/workstream_complete/directives_lessons_plan.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _spool_body_to_file'),
    'coordinator_core/workstream_complete/directives_review.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: record_gate_memo'),
    'coordinator_core/write_guards/block_subagent_archive_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_block_log'),
    'coordinator_core/write_guards/block_subagent_plan_body_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_block_log, _write_hook_emit_log'),
    'coordinator_core/write_guards/guard_concrete_path_citations.py': ('git-internal', 'raw-write site within 3 lines of a coordinator-sessions/meta.json reference'),
    'coordinator_core/write_guards/guard_doctrine_surface_edits.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_repo_identity_advisory_log'),
}

_TO_FIX_CEILING: FrozenSet[str] = frozenset({
    'coordinator_core/bash_guards/_dialect.py',
    'coordinator_core/bash_guards/_write_bump_stand_down.py',
    'coordinator_core/bash_guards/chain_arrival_ledger.py',
    'coordinator_core/block_discharge.py',
    'coordinator_core/hooks/context_pressure_precompact.py',
    'coordinator_core/hooks/nudge_unrouted_sizing.py',
    'coordinator_core/hooks/runtime_tripwire_em_check.py',
    'coordinator_core/ops/archive_auto_memory_rows.py',
    'coordinator_core/ops/ceremony/detached_spawn.py',
    'coordinator_core/ops/ceremony/housekeeping_liveness.py',
    'coordinator_core/ops/ceremony/renderers.py',
    'coordinator_core/ops/check_posix_exec_assumptions.py',
    'coordinator_core/ops/decision_record_mint.py',
    'coordinator_core/ops/deliverable_equivalence.py',
    'coordinator_core/ops/deliverable_ledger_write.py',
    'coordinator_core/ops/docindex_emit.py',
    'coordinator_core/ops/extract_cited_sidecars.py',
    'coordinator_core/ops/fleet/archive_actioned_memos.py',
    'coordinator_core/ops/fleet/archive_terminal_handoffs.py',
    'coordinator_core/ops/fleet/migrate_handoff_vocabulary.py',
    'coordinator_core/ops/fleet_machinery_sweep.py',
    'coordinator_core/ops/generator_scan_cache.py',
    'coordinator_core/ops/handoff_repoint_origin.py',
    'coordinator_core/ops/peer_notice_send.py',
    'coordinator_core/ops/propagate_body.py',
    'coordinator_core/ops/reap_orphaned_agent_dirs.py',
    'coordinator_core/ops/roadmap_link_stubs.py',
    'coordinator_core/ops/session_baton_promote.py',
    'coordinator_core/ops/session_hierarchy_query.py',
    'coordinator_core/ops/setup_seed_health_ledger.py',
    'coordinator_core/ops/strategic/draft_writer.py',
    'coordinator_core/ops/strategic/emit_writer.py',
    'coordinator_core/ops/tracker/push_suggestion.py',
    'coordinator_core/ops/workflow_fire/fire.py',
})


_SCAN_MS_BUDGET = 200.0


def _is_raw_writer(content: bytes) -> bool:
    if any(lit in content for lit in _RAW_LITERALS):
        return True
    if b"open(" in content and _RAW_OPEN_RE.search(content):
        return True
    if b"mode" in content and _RAW_MODE_RE.search(content):
        return True
    return False


def scan_raw_writers(root: pathlib.Path) -> List[str]:
    """Return sorted POSIX paths (rooted at ``coordinator_core/...``, matching
    the register's keying) of every non-test ``.py`` module under ``root /
    "coordinator_core"`` whose bytes match the raw-write vocabulary. ``root``
    is the repo root (real ``_REPO_ROOT`` or a synthetic ``tmp_path`` holding
    its own ``coordinator_core/`` subtree). Exposed (not private) so the AC6
    proof tests can drive it against a synthetic tree.

    The inner loop avoids ``pathlib`` per-file overhead (``relative_to``,
    ``as_posix``) in favour of plain string joins — that alone is the
    difference between the ~184ms and ~335ms figures in the module
    docstring's cost section, at identical output."""
    scan_root_str = os.path.join(str(root), _SCAN_ROOT)
    if not os.path.isdir(scan_root_str):
        return []
    root_str = str(root)
    prefix_len = len(root_str) + 1 if not root_str.endswith(os.sep) else len(root_str)
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(scan_root_str):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for filename in filenames:
            if (
                not filename.endswith(".py")
                or filename.startswith("test_")
                or filename == "conftest.py"
            ):
                continue
            full = os.path.join(dirpath, filename)
            try:
                with open(full, "rb") as fh:
                    content = fh.read()
            except OSError:
                continue
            if _is_raw_writer(content):
                found.append(full[prefix_len:].replace(os.sep, "/"))
    return sorted(found)


def _rule_covers(rel: str, content: bytes) -> Optional[str]:
    """Return the rule name covering ``rel``, or ``None``. The two
    structural rules from D2: the seam/primitive modules by exact path, and
    a ``WRITE_SURFACE``-declaring module scoped to ``coordinator_core/
    install/`` only — a module outside that prefix declaring the same
    variable is NOT covered (see module docstring)."""
    if rel in _RULE_SEAM_MODULES:
        return "seam/primitive"
    if rel.startswith(_INSTALL_PREFIX) and _WRITE_SURFACE_RE.search(content):
        return "WRITE_SURFACE/install"
    return None


def _evaluate(
    root: pathlib.Path,
    register: Dict[str, Tuple[str, str]],
    ceiling: FrozenSet[str],
) -> List[str]:
    violations: List[str] = []
    found = scan_raw_writers(root)
    found_set = set(found)

    for rel in found:
        content = (root / rel).read_bytes()
        if _rule_covers(rel, content) is not None:
            continue
        if rel not in register:
            violations.append(f"undispositioned: {rel}")

    for rel, (category, reason) in register.items():
        if category not in _CATEGORIES:
            violations.append(f"unknown category {category!r}: {rel}")
        if not reason.strip():
            violations.append(f"blank reason: {rel}")

    for rel in register:
        target = root / rel
        if not target.is_file():
            violations.append(f"stale entry (file gone): {rel}")
            continue
        if rel not in found_set:
            violations.append(f"stale entry (no raw token): {rel}")

    for rel, (category, _reason) in register.items():
        if category != "claims-explicitly":
            continue
        target = root / rel
        if not target.is_file():
            continue
        content = target.read_bytes()
        if not _CLAIM_TOKEN_RE.search(content):
            violations.append(f"claims-explicitly without claim token: {rel}")

    to_fix_entries = {rel for rel, (cat, _r) in register.items() if cat == "to-fix"}
    for rel in to_fix_entries:
        if rel not in ceiling:
            violations.append(f"to-fix entry outside ceiling: {rel}")

    for rel in ceiling:
        if rel not in to_fix_entries:
            violations.append(f"stale ceiling member (no to-fix entry): {rel}")

    return violations


def test_the_live_gate_is_green():
    violations = _evaluate(_REPO_ROOT, _DISPOSITIONS, _TO_FIX_CEILING)
    assert violations == [], (
        "The disposition gate is RED. Every raw-writing module needs a rule "
        "or a _DISPOSITIONS entry (category, reason). See this file's module "
        "docstring for the closed category set and how to add an entry.\n  "
        + "\n  ".join(violations)
    )


def test_to_fix_ceiling_is_an_exact_mirror_of_the_to_fix_set():
    to_fix_entries = {rel for rel, (cat, _r) in _DISPOSITIONS.items() if cat == "to-fix"}
    assert to_fix_entries == _TO_FIX_CEILING


def test_at_close_out_the_ceiling_is_empty():
    """AC4: 'At close-out `_TO_FIX_CEILING` and the register's `to-fix` set
    are both empty.' This chunk (C8) seeds the ceiling with C3's full
    migration list; C5-C7 shrink it. It is NOT empty yet — that assertion
    belongs to C9's close-out, not this gate. This test is deliberately the
    inverse: it documents, and pins, that the ceiling is non-empty here (its
    size shrinks as each migration batch lands -- C5 moved
    coordinator_core/ops/goal_append.py onto the seam, dropping C3's seeded
    56 to 55; C6 moved coordinator_core/bash_guards/block_subagent_
    destructive_action.py's append onto the seam and recategorized its
    residual-rotation entry to outside-repo (55 to 54), and moved
    coordinator_core/write_guards/validate_frontmatter_schema_deny.py's
    forensics write onto the seam with no residual raw-write bytes left, so
    that entry was deleted outright (54 to 53); C7 moved ten more modules'
    writes onto the seam (entries/ceiling membership deleted) and
    recategorized eight more to-fix members that held no actual `state/`
    write to the category their real write target names (53 to 35 -- see
    the register's C7 comment block for the full per-module breakdown); retiring the
    backlog-history shard deleted one to-fix module outright (35 to 34)), so
    a reader of this file at this commit does not mistake a non-empty
    ceiling for a defect."""
    assert len(_TO_FIX_CEILING) == 34


def test_scan_cost_is_at_or_below_the_budget():
    t0 = time.process_time()
    scan_raw_writers(_REPO_ROOT)
    elapsed_ms = (time.process_time() - t0) * 1000.0
    assert elapsed_ms <= _SCAN_MS_BUDGET, (
        f"Live scan took {elapsed_ms:.1f}ms, over the {_SCAN_MS_BUDGET}ms budget "
        "(measured figure + headroom, per this file's module docstring). Cut "
        "the walk's cost; do not loosen this assertion."
    )


def _write(root: pathlib.Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_an_undispositioned_raw_writer_goes_red(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/newwriter.py",
        "def write_something(p):\n    open(p, 'w').write('x')\n",
    )
    violations = _evaluate(tmp_path, {}, frozenset())
    assert any("undispositioned: coordinator_core/newwriter.py" in v for v in violations)


def test_a_seam_only_writer_goes_green(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/claimer.py",
        "from coordinator_core.session.claimed_write import declare_write\n"
        "def write_something(p):\n    declare_write(p)\n    open(p, 'w').write('x')\n",
    )
    register = {"coordinator_core/claimer.py": ("claims-explicitly", "claim token in write_something")}
    violations = _evaluate(tmp_path, register, frozenset())
    assert violations == []


def test_a_stale_entry_goes_red_when_the_file_is_gone(tmp_path):
    (tmp_path / "coordinator_core").mkdir(parents=True)
    register = {"coordinator_core/ghost.py": ("in-repo-non-state", "no longer exists")}
    violations = _evaluate(tmp_path, register, frozenset())
    assert any("stale entry (file gone): coordinator_core/ghost.py" in v for v in violations)


def test_a_stale_entry_goes_red_when_the_raw_token_is_gone(tmp_path):
    _write(tmp_path, "coordinator_core/migrated.py", "def x():\n    return 1\n")
    register = {"coordinator_core/migrated.py": ("in-repo-non-state", "no longer a raw writer")}
    violations = _evaluate(tmp_path, register, frozenset())
    assert any("stale entry (no raw token): coordinator_core/migrated.py" in v for v in violations)


def test_a_claims_explicitly_entry_without_a_claim_token_goes_red(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/unclaimed.py",
        "def write_something(p):\n    open(p, 'w').write('x')\n",
    )
    register = {"coordinator_core/unclaimed.py": ("claims-explicitly", "claim token in write_something")}
    violations = _evaluate(tmp_path, register, frozenset())
    assert any("claims-explicitly without claim token: coordinator_core/unclaimed.py" in v for v in violations)


def test_a_to_fix_entry_outside_the_ceiling_goes_red(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/migrate_me.py",
        "def write_something(p):\n    open(p, 'w').write('x')\n",
    )
    register = {"coordinator_core/migrate_me.py": ("to-fix", "raw-write site(s): write_something")}
    violations = _evaluate(tmp_path, register, frozenset())
    assert any("to-fix entry outside ceiling: coordinator_core/migrate_me.py" in v for v in violations)


def test_a_ceiling_member_with_no_to_fix_entry_goes_red(tmp_path):
    (tmp_path / "coordinator_core").mkdir(parents=True)
    violations = _evaluate(tmp_path, {}, frozenset({"coordinator_core/nothing_here.py"}))
    assert any(
        "stale ceiling member (no to-fix entry): coordinator_core/nothing_here.py" in v
        for v in violations
    )


def test_a_to_fix_entry_inside_the_ceiling_is_fine(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/migrate_me.py",
        "def write_something(p):\n    open(p, 'w').write('x')\n",
    )
    register = {"coordinator_core/migrate_me.py": ("to-fix", "raw-write site(s): write_something")}
    ceiling = frozenset({"coordinator_core/migrate_me.py"})
    violations = _evaluate(tmp_path, register, ceiling)
    assert violations == []


def test_a_write_surface_module_under_install_passes_by_rule(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/install/some_installer.py",
        "WRITE_SURFACE = True\n\ndef install(p):\n    open(p, 'w').write('x')\n",
    )
    violations = _evaluate(tmp_path, {}, frozenset())
    assert violations == []


def test_the_same_write_surface_module_outside_install_goes_red(tmp_path):
    """The install-prefix scoping matters (Review F2 in the plan): a module
    outside ``coordinator_core/install/`` that declares ``WRITE_SURFACE`` is
    NOT rule-covered — it needs a register entry like any other module."""
    _write(
        tmp_path,
        "coordinator_core/ops/some_op.py",
        "WRITE_SURFACE = True\n\ndef run(p):\n    open(p, 'w').write('x')\n",
    )
    violations = _evaluate(tmp_path, {}, frozenset())
    assert any("undispositioned: coordinator_core/ops/some_op.py" in v for v in violations)


def test_a_migrated_module_that_regains_a_raw_write_goes_red(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/session/claimed_write.py",
        "def declare_write(p):\n    pass\n",
    )
    _write(
        tmp_path,
        "coordinator_core/migrated_then_regressed.py",
        "from coordinator_core.session.claimed_write import declare_write\n"
        "def write_something(p):\n    declare_write(p)\n",
    )
    violations_before = _evaluate(tmp_path, {}, frozenset())
    assert violations_before == []
    assert "coordinator_core/migrated_then_regressed.py" not in scan_raw_writers(tmp_path)

    _write(
        tmp_path,
        "coordinator_core/migrated_then_regressed.py",
        "from coordinator_core.session.claimed_write import declare_write\n"
        "def write_something(p):\n    declare_write(p)\n"
        "def new_raw_write(p):\n    open(p, 'w').write('y')\n",
    )
    violations_after = _evaluate(tmp_path, {}, frozenset())
    assert any(
        "undispositioned: coordinator_core/migrated_then_regressed.py" in v
        for v in violations_after
    )


def test_a_test_module_is_never_scanned(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/tests/test_something.py",
        "def test_x():\n    open('/tmp/x', 'w').write('y')\n",
    )
    assert scan_raw_writers(tmp_path) == []


def test_a_conftest_is_never_scanned(tmp_path):
    _write(tmp_path, "coordinator_core/conftest.py", "open('/tmp/x', 'w').write('y')\n")
    assert scan_raw_writers(tmp_path) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

