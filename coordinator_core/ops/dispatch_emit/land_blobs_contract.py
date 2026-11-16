"""Frozen contract of the rebuilt landing op (`land_blobs.py`, registered as `dispatch.land_blobs` until the
cutover rebinds `dispatch.land_run`): request and reply shapes, refusal kinds, the spawn ledger, the pinned
baseline and the cutover census.

Imports nothing beyond the stdlib so a caller, the bar profile and the bar test read it without loading the op.
"""

from __future__ import annotations

from typing import Final

#: The name callers keep; the cutover rebinds it from `land_run` to `land_blobs`.
OP_NAME: Final = "dispatch.land_run"
#: Registered name until the cutover.
TEMP_OP_NAME: Final = "dispatch.land_blobs"
#: `land(params, repo_root)`: `repo_root` is the git common dir, exactly as `run_landing.land_run` receives it.
ENTRY: Final = "coordinator_core.ops.dispatch_emit.land_blobs:land"

# --- request ---------------------------------------------------------------------------------------------

#: Params the op reads. Unchanged from `land_run`, so the warm server's caller needs no edit. The run's
#: declared paths come from the script's commit-request marker; the recorded set comes from the run record's
#: `kept.json` (`chunks.<id>.paths.<path> = blob sha`), never from params.
REQUEST_REQUIRED: Final = ("script_path", "run_id", "inline_review")
REQUEST_OPTIONAL: Final = ("incomplete_chunks", "landed_chunks", "session_id", "run_base_sha", "run_token")
#: A call carrying any of these is a record mode, served by `land_run_contract.MODE_MODULES` (which the cutover
#: keeps): `land_blobs` lands the product branch only and replies `mode-ambiguous` otherwise.
MODE_KEYS: Final = ("halted_by", "fanout", "grind")

# --- reply -----------------------------------------------------------------------------------------------

#: Landed: `{committed: True, sha, landed_paths, changed_paths}`. Nothing to land: `{committed: False,
#: nothing_to_commit: True}`. Refused: `{committed: False, sha: None, error, refused: <kind>, ...detail}`.
REPLY_LANDED_KEYS: Final = ("committed", "sha", "landed_paths", "changed_paths")
REPLY_REFUSED_KEYS: Final = ("committed", "sha", "error", "refused")

UNREVIEWED: Final = "unreviewed"
SECRET_IN_DIFF: Final = "secret-in-diff"
HEAD_MOVED: Final = "head-moved"
INDEX_LOCK_BUSY: Final = "index-lock-busy"
DISPATCH_ABANDONED: Final = "dispatch-abandoned"
MODE_AMBIGUOUS: Final = "mode-ambiguous"
#: Guard 1: a declared path's staged blob differs from the run's recorded blob, so the landing would carry a
#: hunk outside the run's recorded set. Detail keys: `foreign_paths` (sorted), `foreign_runs` (run ids whose
#: record holds a blob for one of those paths, possibly empty).
FOREIGN_HUNK: Final = "foreign-hunk"
#: Guard 2: a halt could not write the base blob and the run blob of every declared path to the run record
#: (`HALT_RECORD_FILE`); the halt replies this kind instead of a halt. Detail key: `unrecorded_paths`.
#: Enforced only by `land_blobs_halt.halt_with_record`, which no halt path in `land_blobs.py` calls yet.
HALT_RECORD_INCOMPLETE: Final = "halt-record-incomplete"

GUARD_REFUSALS: Final = (FOREIGN_HUNK, HALT_RECORD_INCOMPLETE)
REFUSAL_KINDS: Final = (UNREVIEWED, SECRET_IN_DIFF, HEAD_MOVED, INDEX_LOCK_BUSY, DISPATCH_ABANDONED,
                        MODE_AMBIGUOUS, *GUARD_REFUSALS)
#: Retried inside the op (bounded, re-reading HEAD each time) before the refusal reaches the caller.
RETRIED_REFUSALS: Final = (HEAD_MOVED, INDEX_LOCK_BUSY)
RETRY_ATTEMPTS: Final = 8

#: Halt record, under the run's record dir (written by `stranded_sibling.write`): `{path: {"base": sha|None,
#: "run": sha|None}}`; `None` base is a path HEAD lacks, `None` run is a declared deletion.
HALT_RECORD_FILE: Final = "touched-blobs.json"

# --- requirements kept (ids from docs/reference/run-landing-invariants.md) --------------------------------

#: Everything else in `land_run` is dropped; a reply field or the record carries the fact where a later reader needs it.
KEPT_REQUIREMENTS: Final = (
    "RL-01", "RL-02", "RL-04", "RL-05", "RL-09", "RL-10", "RL-13", "RL-14", "RL-15", "RL-16", "RL-18", "RL-19",
    "RL-20", "RL-22",
)
#: RL-02 changes shape: a hunk outside the recorded set is refused (`FOREIGN_HUNK`), no longer left dirty.
#: RL-03 (ccc7d1) stays an open question for the APM; until ruled, `land_blobs` emits it as a reply field.

# --- spawn ledger -----------------------------------------------------------------------------------------

#: The default landing spawns nothing. A git subcommand may spawn only in a shape the in-process writer cannot
#: serve; each row is `(git subcommand, the question the process cannot answer)`.
SPAWN_LEDGER: Final = (
    ("commit-tree", "a signed commit (commit.gpgsign) needs the signing program, which only git invokes"),
    ("hash-object", "a clean filter the process cannot run decides the bytes git would hash"),
    ("reset", "a fallback commit leaves index entries to realign with HEAD in one batched call"),
)


def git_subcommand(argv: object) -> str:
    """The git subcommand of a spawned argv, skipping `-c <kv>` and `-C <dir>`; `""` when the spawn is not git."""
    if not isinstance(argv, (list, tuple)) or not argv:
        return ""
    exe = str(argv[0]).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if exe not in ("git", "git.exe"):
        return ""
    i = 1
    while i < len(argv):
        token = str(argv[i])
        if token in ("-c", "-C"):
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            return token
    return ""


def unledgered(spawned: list) -> list:
    """The spawned argvs whose git subcommand has no `SPAWN_LEDGER` row; a non-git spawn is always unledgered."""
    allowed = {sub for sub, _question in SPAWN_LEDGER}
    return [argv for argv in spawned if git_subcommand(argv) not in allowed]


# --- falsifier baseline -----------------------------------------------------------------------------------

#: `dispatch.land_run` of a clean landing at default params with 6 live sibling landings, run by
#: `land_run_leg_budget_profile.py --live-siblings 6 --json` against an export of `base_sha` (Windows, 24c, the
#: harness's 15.6ms process-time quantum). Pinned so the new op's figure has a fixed comparator.
BASELINE_LAND_RUN: Final = {
    "base_sha": "257fa64f72eb1ada6cfcce4b8b04b48eeb902088",
    "axis": "dispatch.land_run",
    "live_siblings": 6,
    "process_time_ms": 1015.625,
    "spawns": 0,
    "reads": 660,
    "leg_process_time_ms": {
        "land_run.script_read": 484.375,
        "halted_hold_release.without_released_paths": 140.625,
        "land_run.commit": 78.125,
        "peer_hunk_gate.sibling_spine": 31.25,
        "land_run._integrity.import_closure": 31.25,
    },
}
BAR_MS: Final = 500.0

# --- cutover census (the C5 write set) --------------------------------------------------------------------

#: Modules only `land_run` reaches (git grep of `.py` importers, non-test): deleted with it, their tests with them.
CUTOVER_DELETE: Final = (
    "coordinator_core/ops/dispatch_emit/land_run.py",
    "coordinator_core/ops/dispatch_emit/footprint_breach.py",
    "coordinator_core/ops/dispatch_emit/halt_gap_reverify_closure.py",
    "coordinator_core/ops/dispatch_emit/held_revisions.py",
    "coordinator_core/ops/dispatch_emit/land_byte_gate.py",
    "coordinator_core/ops/dispatch_emit/needed_path.py",
    "coordinator_core/ops/dispatch_emit/sibling_blockers.py",
)

#: Family modules the plan lists for deletion that other live code still reaches; excluded by name until their
#: importers are rewired or deleted in the same commit. `landing_integrity.py` is excluded for the same reason.
CUTOVER_KEEP_REACHED: Final = {
    "coordinator_core/ops/dispatch_emit/land_run_halt.py": (
        "coordinator_core/ops/dispatch_emit/halted_record_commit.py",
        "coordinator_core/ops/dispatch_emit/land_run_contract.py (MODE_MODULES)",
    ),
    "coordinator_core/ops/dispatch_emit/peer_hunk_gate.py": (
        "coordinator_core/git/checkpoint_hunks.py", "coordinator_core/ops/dispatch_emit/co_declared_settle.py",
        "coordinator_core/ops/dispatch_emit/halt_product_gates.py",
        "coordinator_core/ops/dispatch_emit/hunk_provenance.py",
        "coordinator_core/ops/dispatch_emit/sibling_claims.py",
        "coordinator_core/ops/dispatch_emit/terminal_commit.py",
        "coordinator_core/ops/dispatch_emit/terminal_commit_refusals.py",
    ),
    "coordinator_core/ops/dispatch_emit/land_deadline.py": (
        "coordinator_core/authoring_leaks/import_closure.py", "coordinator_core/ipc.py",
        "coordinator_core/ops/ceremony/commit_v2.py", "coordinator_core/ops/warp_run_deliverable.py",
        "coordinator_core/warm/server.py", "coordinator_core/ops/dispatch_emit/ask_compose.py",
        "coordinator_core/ops/dispatch_emit/terminal_commit.py",
        "(more importers: git grep -l land_deadline -- '*.py')",
    ),
    "coordinator_core/ops/dispatch_emit/landing_integrity.py": (
        "coordinator_core/git/since_base_overlap.py", "coordinator_core/ops/ceremony/close_background.py",
        "coordinator_core/ops/dispatch_emit/halted_record_commit.py",
        "(more importers: git grep -l landing_integrity -- '*.py')",
    ),
}

#: Importers of `land_run.py` outside `CUTOVER_DELETE`, with the helpers each takes from it. Every one is
#: rewired (the helper moves to a surviving module) or deleted in the cutover commit; none is a caller of the op.
LAND_RUN_HELPER_IMPORTERS: Final = {
    "coordinator_core/ops/dispatch_emit/terminal_commit.py": ("_recovery_chunks", "_remnant_record"),
    "coordinator_core/ops/dispatch_emit/terminal_commit_ledger.py": ("_remnant_record",),
    "coordinator_core/ops/dispatch_emit/terminal_commit_refusals.py": ("_recovery_backing", "_peer_held"),
    "coordinator_core/ops/dispatch_emit/halted_record_commit.py": ("foreign_halted_withheld",),
    "coordinator_core/ops/dispatch_emit/hunk_provenance.py": ("UNREADABLE_HALT_RECORD", "foreign_halted_withheld"),
    "coordinator_core/ops/dispatch_emit/peer_hunk_gate.py": ("UNREADABLE_HALT_RECORD", "foreign_halted_withheld"),
    "coordinator_core/ops/dispatch_emit/land_intent.py": ("already_landed_reply", "landed_on_head"),
    "coordinator_core/ops/dispatch_emit/land_run_halt.py": ("_DELIVERED_REPORT_RE", "_UNDELIVERED_REPORT_RE",
                                                          "_worktree_blob", "_sizing_stamp_floor"),
    "coordinator_core/ops/dispatch_emit/ask_stage.py": ("_DELIVERED_REPORT_RE", "_UNDELIVERED_REPORT_RE"),
    "coordinator_core/ops/dispatch_emit/ask_stage_plan.py": ("_DELIVERED_REPORT_RE", "_UNDELIVERED_REPORT_RE"),
    "coordinator_core/ops/dispatch_emit/cli.py": ("params_from_task_output",),
    "coordinator_core/ops/dispatch_emit/retest_targeted.py": ("params_from_task_output",),
    "coordinator_core/ops/dispatch_emit/measured_rev.py": ("_manifest_declared",),
    "coordinator_core/ops/dispatch_emit/plan_residue.py": ("_worktree_blob",),
    "coordinator_core/ops/dispatch_emit/shipped_claims.py": ("_FILTERED", "_worktree_blob"),
    "coordinator_core/ops/dispatch_emit/refire_inherited_scope.py": ("_fixes_integrated_at",),
    "coordinator_core/ops/dispatch_emit/run_terminality.py": ("_RECORDED_EXIT_OUTCOMES",),
    "coordinator_core/ops/dispatch_emit/run_write_set.py": ("_firing_sessions", "_peer_held", "_prior_pass_sessions"),
    "coordinator_core/ops/ceremony/close_background.py": ("_close_kind",),
    "coordinator_core/ops/review_prep.py": ("_bounded_claim_lookup", "_firing_sessions", "_prior_pass_sessions",
                                          "foreign_halted_withheld"),
    "coordinator_core/ops/review_reverify.py": ("UNREADABLE_HALT_RECORD", "foreign_halted_withheld"),
    "coordinator_core/ops/_registry_map.py": ("the dispatch.land_run registration line",),
    "coordinator_core/ops/__init__.py": ("the dispatch.land_run import",),
}
#: Benchmarks that import `land_run` (fixtures and profiles): deleted or re-pointed at `land_blobs` in the cutover commit.
LAND_RUN_BENCHMARK_IMPORTERS: Final = (
    "grind_terminal_commit_full_path", "halted_record_commit_full_path", "hunk_split_large_file_budget",
    "land_credited_profile", "land_gate_reach_profile", "land_legs_pre_integrity_profile",
    "land_run_dirty_scan_bytes", "land_run_leg_budget_profile", "land_run_slow_disk_profile",
    "liveness_record_scan_budget", "review_prep_dirty_worktree", "terminal_commit_ab", "terminal_commit_full_path",
)
