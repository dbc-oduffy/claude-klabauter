"""
coordinator_core.ops.dispatch_emit.terminal_commit -- JSON-RPC
"dispatch.terminal_commit" operation.

Purpose: § Design D3 of docs/plans/2026-09-27-emitter-dag-terminal-commit-
wake-digest.md. The emitted DAG script (C12) commits each wave through a
checkpoint commit (``ask_compose`` wires it per wave) and also writes ONE
terminal-commit-request marker (D2, ``commit_request.py``) recording what
the run promises the terminal commit. This op is fired once, after the run finishes, by the
driver (C7 for a headless fire, DoE D1/D2 for the EM) and lands exactly one
``ceremony.commit_v2`` call over the run's DONE chunks.

Keying scope: common_dir (mirrors ``ceremony.commit_v2`` itself, which this
handler calls in-process) -- the caller's own worktree, never a
``params.repo_root`` override (D3 consistency semantics only, same as
commit_v2).

Negative-spec:
  - Does NOT call ``ceremony.commit_v2`` more than once, bar one exception: a lost ref
    compare-and-swap is re-attempted (``git.ref_cas_retry``, bounded) while no peer commit touched a
    landing path. Every DONE chunk's paths and prefix-claimed files fold into ONE call.
  - Does NOT infer prefix-claimed files from ``git status`` or prose -- only
    a DONE chunk's OWN report, read verbatim, under its OWN declared
    prefixes (mirrors emit.py's ``_prefix_commit_rule`` executor contract).
  - Declares to ``commit_v2`` only deletions a DONE chunk declared: a path
    in a chunk's own ``paths`` (its declared ``writes``) that is gone from
    the worktree but tracked at HEAD is passed as ``deleted_paths``. Any
    other absent tracked path (a prefix claim, the bookkeeping record) is an
    undeclared deletion and refuses the WHOLE commit; an absent path
    untracked at HEAD is dropped (nothing to declare).
  - Does NOT compare head_sha: peers commit to the shared branch mid-run.
    Only the branch NAME is checked (detached, unreadable, or differing from
    the marker's ``expected_branch`` refuses before any write).
  - Lands the run's candidate set (``run_artifacts.candidate_paths``: record dir, cited sizing,
    baton, plan, evidence and seam sidecars, delivery note, slice sizings, superseded predecessors'
    record dirs, the docs stage's ``doc_paths``) beside the declared writes, each only when dirty,
    not gitignored and not held by a live peer's claim. In a repo that has not opted in
    (``process_artifact_policy.hosts_coordinator_state`` False: no ``archive/``, no
    ``state/workstreams/``) the whole set, the bookkeeping record, ask note and plan are withheld
    as ``repo-not-opted-in`` and excluded locally, and the stamp commit and archive moves are
    skipped. A withheld path is named in
    ``run_artifacts.withheld`` and the commit proceeds. A path the EM ruled out through
    ``workflow.revise withhold_paths`` (``withhold.jsonl`` in this run's record dir or a
    superseded predecessor's) is left unstaged and dirty, checked before the peer-claim and
    blob-drift gates, and recorded in ``run_artifacts.withheld`` as ``withhold_paths: <reason>``.
    The sizing and an ask run's plan rewritten
    after the product commit ride the stamp commit. A candidate still dirty afterwards sets
    ``ok: false`` and ``run_artifact_residue``. Never for an anchor-only run; the pathspec is never
    widened to the dirty tree.
  - A warp ask run (its script carries the ask-run-manifest marker) lands
    PRODUCT plus its candidate set: its declared writes, including the stage's
    ``docs`` chunk (the delivery note and the cited sizing's
    ``doc_consumers``/``ask_conflicts``), under the same in-scope and
    ``undeclared_dirty`` test as every code row, and the changed files in
    ``state/warp/<run_id>/`` (nothing from an in-flight run's ``scratch/warp``
    directory), plus its own ``docs/delivery/<run_id>.md`` when that file is on disk
    (declared by no chunk). ``params.doc_paths`` is a candidate list only. When its plan is stageable (set, not gitignored) a second,
    plan-only stamp commit lands the plan, completion entry and shipped sizing; a
    gitignored plan keeps the stamps on disk and the run makes one commit. Sidecars and
    completion receipts stay on disk (receipts under ``.coordinator-local/``) and are
    named by trailers (``Completion-Receipt``, ``Review``, ``Falsifier``,
    ``Resolves-Sizing``). When the run is
    ledger-bearing, the commit refuses unless ``ledger_verdict`` passes
    ``requirements_ledger.judge`` against the live rows of the sizing's
    ``requirement_register``, and writes that verdict back onto the rows once it
    lands. The ledger is an extra gate: it never stands in for the delivery
    verdict, the terminal judge or falsifier integrity, at any size.
  - A ``run_token`` is redeemed before anything else is read, so a racing second
    caller is refused. It admits one LANDED commit: a landed commit consumes it;
    every return where none landed (an error, a refusal, nothing to commit, an
    exception before commit_v2 returned) releases it for a retry.
  - A call whose ``halted_by`` is a dict with a ``halted`` kind is a halted run's record-only
    commit (``halted_record_commit``): it lands the run's candidate set in one commit_v2
    call, the halt in the message. It never consumes
    the run token (redeems, then releases) and is not a delivery: no receipt, sizing ship, plan
    stamp or close job.
  - A call whose ``fanout`` is ``{plans: [...]}`` is a roadmap run's record commit
    (``roadmap_fanout_commit``): one commit_v2 over the record files and each plan's sizing and
    baton, then ``ask_fanout`` over the plans' sizings. It consumes the token on a landed commit.
    Carrying ``halted_by`` too is refused.
  - A call whose ``grind`` is a dict with a ``rows`` list is a grind's terminal commit
    (``land_run_grind``): one landing over the finished rows' declared paths, a
    ``Grind-Row:`` trailer per landed row; it drops ledger-root paths and deletions git cannot
    see, reports them, and carries no ``stranded``/``undeclared_dirty``. Carrying ``halted_by`` too is refused.
  - Lands a file the run's review stage wrote outside the declared writes as review residue (named
    files only, admitted by a review-stage touch record; ``Review-Residue:`` trailers), or refuses
    ``review-edit-unlanded`` naming the files; never leaves it silently dirty.
  - Names dirty engine-owned paths outside the landing under ``engine_residue`` (``residue_classes``;
    ``engine_residue_total`` when capped, ``None`` when unreadable): advisory, never staged.
  - Does NOT commit a dirty file no chunk declares and the run does not own: ``undeclared_dirty``
    reports it, because widening the pathspec would hide a wrong spine. The product commit
    is declared-only (``carry_own_left_behind: False``, no landing-set exception). A path the
    committing session itself holds a write-claim on and no live peer holds lands in a follow-on
    commit after it (parent is the product sha, ``Own-Left-Behind-Of:`` trailer; reply key
    ``own_left_behind_commit`` ``{sha, paths}`` or ``{error}``; a refusal never un-lands the
    product commit); the follow-on is skipped without a session_id and on an anchor-only
    call; a path a halted, unresumed in-window run declares it writes is
    never carried by either, and the dirty ones are named under ``held_by_halted_run``
    (``{path: [run_id, ...]}``). The commit passes ``carry_stopped_runs: False``: another run's
    residue is never swept into this run's commit, it stays dirty and is reported by
    ``undeclared_dirty``. A dirty file in a
    declared directory that this run itself touched (its chunk reports, review ``slice_files``,
    run artifacts) and no live peer contests is ``run_owned`` and rides the one commit_v2 call
    (ask route and plan-route DAG); a sibling run's dirt of the same session in a shared
    directory is never staged and surfaces in ``undeclared_dirty``. ``plan_chain``,
    ``workflow_fire`` and the execute-review fix keep the
    declared-only behaviour: a chain's claims split across child sessions, the other two have
    no commit of their own.
  - Does NOT write a receipt when there is nothing to commit.
  - Drops the expectation on the run's own record-named run-artifact paths (its sizing, record dir:
    ``run_artifact_ownership.own_artifact_paths``), re-reads them at commit and names them in
    ``run_artifacts.reread``; every other path keeps its expectation, the delivery note included
    (declared at the bytes this call left, so it is never re-read).
  - Declares ``expected_content_sha256`` per committed path from the newer of the run session's
    last recorded write and the run's newest ``review.prep`` reply (``reviewed_sha256`` at
    ``recorded_at``; a tie goes to the session write; a refire's newest review supplies it; a path
    the run session wrote after that review is declared at its current bytes), never the receipts, allowlist or
    bookkeeping record this call writes. The delivery note is declared at the sha256 of the bytes
    this call just left after its ``## Verdicts`` append. A drift refuses the run's commit with
    ``blob_drift``, never drops paths; a drifted ``.py`` path whose bytes are AST-equal (docstrings
    kept) to the review's pre-review snapshot is re-landed once with a ``Comment-Only-Drift``
    trailer, and anything unprovable refuses.
  - Does NOT retry or catch commit_v2's structured refusals -- returned to
    the caller unmodified in substance, same posture commit_v2 itself takes
    toward ``commit_paths``; the lost-ref-CAS re-attempt is the one exception, and a lost CAS whose
    peer commit touched a landing path returns ``refused: ref-moved-overlap``.

DR-208 five-question affirmation (MUTATING; citing this handler, modelled on
``ceremony.commit_v2``'s own affirmation, coordinator_core/authz/
classification.py):
  1. Writes, deletes, or reorders any state file, queue, or git object?   YES.
     Indirectly: the one in-process ``ceremony.commit_v2`` call this handler
     makes writes git objects and moves the branch ref.
  2. Writes into rag's relational store?                                  No.
  3. Opens any file for write (including sentinel creation)?              YES.
     Exclusive-creates the run's completion-receipt files
     (``completion_receipt.write_run_receipts``) before the commit; all other
     writes (index splice, object write) are delegated to ``ceremony.commit_v2``.
  4. Mutates shared mutable state outside its own module?                 YES.
     The landed commit and moved ref, via the delegated commit_v2 call, are
     read by every subsequent dispatch against this repo.
  5. Persistent state changes observable across process boundaries?      YES.
     The commit sha and ref move persist across the whole box.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
Spec: docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md § D3
"""

from __future__ import annotations

import ast
import dataclasses
import glob as _glob
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Optional, Sequence

from coordinator_core.git.git_state import head_branch
from coordinator_core.git.machine_local_paths import machine_local_paths
from coordinator_core.git import residue_classes
from coordinator_core.git.path_canon import canonical_repo_relative_path_refusal
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.session.claimed_write import replace_text
from coordinator_core.session.record_homes import home_dir
from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch
from coordinator_core.ops.dispatch_emit import bug_close, cohold_refusal, credit_gate, gate_owed, halt_baseline, pre_halt_credit, process_artifact_policy, review_floor  # noqa: E402 -- sibling-package group
from coordinator_core.ops.dispatch_emit.land_run import (  # noqa: E402
    _em_discharged_rows, _halt_gap_lines, _leg_footprint_writes, _recovery_backing, _unclosed_halt_gaps,
    foreign_halted_withheld, held_revision_refusal,
)
from coordinator_core.ops.dispatch_emit.halted_run_route import halted_refusal, route_suffix  # noqa: E402
from coordinator_core.ops.dispatch_emit.refire_inherited_scope import inherited_scope  # noqa: E402
from coordinator_core.ops.dispatch_emit.ledger_recovery import RECOVERY_WITHHELD_PARAM  # noqa: E402
from coordinator_core.ops.dispatch_emit.halt_landing import (  # noqa: E402
    landed_before_halt as _landed_before_halt,
    since_halt_close as _since_halt_close,
)
from coordinator_core.ops.dispatch_emit.pre_halt_ancestry import (  # noqa: E402
    CITED_DETAIL_KEY, cited_at_or_before, verified_post_commit_credit,
)
from coordinator_core.ops.dispatch_emit.ask_contract import (
    DELIVERY_NOTE_DIR,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_UNLANDED_FIELD,
    RESUMED_RUN_ID_KEY,
    RUN_DIR_ROOT,
    SPINE_SUFFIX as _SPINE_SUFFIX,
    TERMINAL_RESUME_LEDGER_PARAM,
    UNCHANGED_SINCE_HALT_REASON,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.reach_claims import REACH_CLAIM_REFUSED, reach_gate  # noqa: E402
from coordinator_core.ops.dispatch_emit.shipped_claims import refused_code, shipped_gate  # noqa: E402
from coordinator_core.ops.dispatch_emit.write_baseline import (  # noqa: E402
    DIRTY_BEFORE_RUN_KEY,
    MEANT_TO_LAND_KEY,
    NO_CHANGE_REFUSAL,
    pre_existing_dirt as _pre_existing_dirt_paths,
    prior_pass_written as _prior_pass_written,
    unwritten as _unwritten_paths,
)

POST_COMMIT_PATH_UNLANDED = "post-commit-path-unlanded"
from coordinator_core.ops.dispatch_emit.commit_request import (
    MARKER_PREFIX,
    PREFIX_CLAIM_LABEL,
    ChunkCommit,
    CommitRequest,
    MalformedCommitRequestError,
    parse_manifest_marker,
    parse_marker,
    plan_deliverable_id,
    plan_trailer_deliverable_ids,
    unmerged_sibling_scope,
    valid_deliverable_id,
)
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import (
    CHECKPOINT_PLAN_LINE,
    CHECKPOINT_SUBJECT,
    is_stamp_commit,
)
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.dispatch_emit.plan_row_coding import (  # noqa: F401 -- re-exported to the family and old tests
    MINTED_SPINE_ORIGIN as _MINTED_SPINE_ORIGIN,
    NOOP_DETAIL as _NOOP_DETAIL,
    REVIEW_WALK_CAP as _REVIEW_WALK_CAP,
    close_memo_rows_delivered as _close_memo_rows_delivered,
    flip_rows_coded as _flip_rows_coded,
    inventory_record_rel as _inventory_record_rel,
    outside_landing_attribution as _outside_landing_attribution,
    outside_landing_detail as _outside_landing_detail,
    outside_landings as _outside_landings,
    rows_landed_outside_executor,
    source_rows_by_plan as _source_rows_by_plan,
    yaml_load as _yaml_load,
)
from coordinator_core.ops.dispatch_emit.register_gate import (  # noqa: F401 -- re-exported to the family and old tests
    RESOLUTION_RUN_ID as _RESOLUTION_RUN_ID,
    register_gate as _register_gate,
    register_target as _register_target,
    register_writeback as _register_writeback,
    run_started_at as _run_started_at,
    sizing_file_rel as _sizing_file_rel,
)
from coordinator_core.ops.dispatch_emit.worktree_snapshot import (  # noqa: F401 -- re-exported to the family and old tests
    Snapshot as _Snapshot,
    read_rel as _read_rel,
    tracked_at_head as _tracked_at_head,
    worktree_snapshot as _worktree_snapshot,
)

from coordinator_core.ops.dispatch_emit.terminal_commit_closeout import (  # noqa: E402,F401 -- moved names, re-exported for importers
    _XS_SPINE_ORIGIN,
    _KIND_FIELD,
    _close_kind,
    _cited_sizing,
    _cited_baton,
    _derive_judge_status,
    _mint_mise_receipt,
    _SEAM_SIDECAR_SUFFIX,
    _EVIDENCE_SIDECAR_SUFFIX,
    _seam_sidecars,
    _MEMO_NO_SLUG,
    _memo_rows_without_receipt,
    _memo_rows_to_close,
    SIZING_LEDGER_REL,
    _resolving_sizing,
    _record_resolution,
    _ship_cited_sizing,
    _dropped_part_rows,
    USAGE_FILE,
    _write_usage,
    _run_id_of,
    CLOSE_JOB_SCRIPT,
    _CLOSE_JOB_WINDOW_MINUTES,
    _BACKGROUND_LEGS,
    _NOT_LANDED,
    _leg,
    _plan_source_memo,
    _deferred_rows,
    _run_own_baton,
    _pay_inline_legs,
    _restore_texts,
    _launch_close_job,
    _background_legs,
    _settle_close,
    _inline_paid_legs,
    _close_block,
    _release_siblings_at_landing,
)
from coordinator_core.ops.dispatch_emit.terminal_commit_checkpoints import (  # noqa: E402,F401 -- moved names, re-exported for importers
    _LANDS_CHUNKS_LINE,
    _predecessor_rows,
    _HALTED_WALK_CAP,
    _checkpoint_rows_from,
    _plan_checkpoint_touched,
    _halted_run_checkpoint_rows,
    _earlier_fire_checkpoint_rows,
    _checkpoint_attribution,
    _open_row_ids,
    _FULL_SHA_RE,
)
from coordinator_core.ops.dispatch_emit.terminal_commit_review import (  # noqa: E402,F401 -- moved names, re-exported for importers
    _delivery_not_rostered,
    _review_only_coding,
    _recorded_fingerprints,
    _review_write_events,
    _fixes_integrated_at,
    _VERIFIED_AT_RE,
    _LEDGER_BLOCK_RE,
    _reviewer_applied_fixes,
    _wave_slice_files,
    _epoch_of,
    _withhold_reasons,
    _RESOLUTION_SHA,
    _reconcile_prep,
    _write_resolution,
    _stage_returns,
    _mint_review_stamp,
    _review_since,
    _resumed_review_source,
    _post_checkpoint_named,
    _unclaimed_post_checkpoint,
    _recorded_post_checkpoint,
    _review_recorded_edits,
    _review_edit_verdict,
    _reverted_paths,
    _reviewed_files_if_passed,
    _clean_review_files,
)
from coordinator_core.ops.dispatch_emit.terminal_commit_refusals import (  # noqa: E402,F401 -- moved names, re-exported for importers
    _launcher_run_digest,
    _xs_zero_slice_review,
    _halted_commit_params,
    _params_from_task_output,
    _anchor_request,
    _new_file_rows,
    _run_arm,
    _peer_adjusted_rows,
    _xs_over_budget_refusal,
    _apply_xs_resize,
    _TEST_PATH,
    _NOT_RUN_TEXT,
    REFUSED_TESTS_NOT_RUN,
    _tests_not_run_refusal,
    _declared_reconcile,
    _review_floor_note,
    _error,
    _merge_landing_plan,
    _land_merge_artifacts,
    _retest_covers_targeted_set,
    _landing_integrity_gate,
    _missing_run_outcome,
    _measured_rev_drift_refusal,
    _readable_manifest,
    _pre_existing_dirt_refusal,
    _live_claimed_inherited,
    _drop_inherited_withheld,
    _no_change_refusal,
    _recovery_withheld_param,
    _recovery_withheld_reasons,
    _peer_held,
    _snapshot_unavailable_refusal,
    _unclaimed_refusal,
)
from coordinator_core.ops.dispatch_emit.terminal_commit_ledger import (  # noqa: E402,F401 -- moved names, re-exported for importers
    _parse_prefix_claims,
    _own_prefix_files,
    _expanded_prefix_files,
    _with_expansion,
    _routed_from_manifest,
    _PROCESS_STATE_CORPORA,
    _is_process_state,
    _manifest_declared,
    verified_only_rows,
    _every_staged_row_landed,
    _withheld_of,
    _worktree_differs_from_head,
    _withheld_work_pending,
    _unchanged_since_halt_failures,
    _VERDICTS_HEADING,
    _verdicts_section,
    _write_note_verdicts,
    _awaiting_operator,
    _ledger_gate,
    _ledger_writeback,
    _STATUS_FIELD,
    _DELIVERED_REPORT_RE,
    _UNDELIVERED_REPORT_RE,
    _PARTIAL_REPORT_RE,
    _UNDONE_LINE_RE,
    _CAUSE_HEADING_RE,
    _UNDONE_CAP,
    _cause_section,
    _undone_summary,
    _partial_chunks,
    _regradable_chunks,
    _BLOCKED_REPORT_RE,
    _is_delivered_report,
    _ledger_met_ids,
    _withheld_product_files,
    _lifecycle_owed,
    _blocking_open_rows,
    _ledger_met_observation,
    _ledger_not_stamped_reason,
    _ledger_met_regrades,
    _credit_regrades,
    _is_closeout_path,
    _xs_closeout_regrades,
    _own_evidence_append_chunks,
    _changed_paths,
    _changed,
    _commit_residue,
    _entangled_chunks,
)
from coordinator_core.ops.dispatch_emit.terminal_commit_stamp import (  # noqa: E402,F401 -- moved names, re-exported for importers
    _PYTEST_NODE,
    _archive_in_run,
    _undo_archive,
    _PRODUCT_ARTIFACT_CLASSES,
    _differs_from_commit,
    _stamp_coded_commit,
    _ask_trailers,
    _falsifier_broken_tells,
    _criterion_reason,
    _stamp_plan_implemented,
    _subject,
    _regenerate_landing_allowlist,
)


_PARAM_FIELDS = (
    Field("script_path", "nonempty_str", required=True),
    Field("incomplete_chunks", "str_list", required=True),
    Field("landed_chunks", "str_list"),
    Field("inline_review", "dict"),
    Field("task_output_path", "nonempty_str"),
    Field("plan_path", "nonempty_str"),
    Field("falsifier_broken", "list"),
    Field("held", "list"),
    Field("release_held", "list"),
    Field("withhold_paths", "list"),
    Field("run_id", "nonempty_str"),
    Field("run_token", "nonempty_str"),
    Field("ledger_verdict", "dict"),
    Field(RECOVERY_WITHHELD_PARAM, "str_list"),
    Field("widened", "list"),
    Field(TERMINAL_RESUME_LEDGER_PARAM, "dict"),
    Field("doc_paths", "str_list"),
    Field("allow_empty", "bool"),
    Field("declared_reverts", "str_list"),
)


#: Refusals raised before the run's incomplete set is known: ``stranded`` would
#: read ``{}`` ("nothing left behind") when it is really unknown, so the key is omitted.
_STRANDED_UNKNOWN_REFUSALS = frozenset({"missing-run-outcome", "unreviewed"})


_UNDECLARED_DIRTY_CAP = 100


def _glob_literal(text: str) -> str:
    return "".join("\\" + ch if ch in "*?[]\\" else ch for ch in text)


def _scope_pathspecs(request: CommitRequest) -> list:
    """Git pathspecs naming the directories a run's declared writes sit in:
    each declared path's own directory, non-recursive, and each declared
    prefix, recursive."""
    specs: dict = {}
    for chunk in request.chunks:
        for path in chunk.paths:
            parent = path.rpartition("/")[0]
            specs[f":(glob){_glob_literal(parent + '/') if parent else ''}*"] = None
        for prefix in chunk.prefixes:
            specs[f":(literal){prefix.rstrip('/')}/"] = None
    return list(specs)


def _live_run_claims(
    worktree_root: Path, candidates: Sequence[str], own_run_id: str, held_out: Optional[dict] = None,
) -> Optional[set[str]]:
    """The ``candidates`` another run than ``own_run_id`` holds: a live run's declared writes,
    whichever session holds that run (a sibling run of the committing session included), united with
    the declared writes of a halted, unresumed in-window run. ``None`` when either declaration is
    unreadable. The own session is not excluded: its other runs are the target. ``held_out``, when
    given, gains ``{path: [halted run ids]}`` for each candidate the halted leg held."""
    from coordinator_core.ops import warp_run_deliverable

    try:
        claims = warp_run_deliverable.live_run_write_claims(
            worktree_root, list(candidates), exclude_session="", cwd=str(worktree_root),
            exclude_run=own_run_id,
        )
        halted = warp_run_deliverable.halted_run_write_claims(
            worktree_root, list(candidates), exclude_run=own_run_id,
        )
    except Exception:  # noqa: BLE001 -- contest unknown: carry nothing
        return None
    if claims is None or halted is None:
        return None
    if held_out is not None:
        held_out.update(halted)
    return {*claims, *halted}


def _dirty_held_by_halted_run(worktree_root: Path, held: dict) -> dict:
    """``held`` ({path: halted run ids}) narrowed to the paths still dirty against the index.

    The sibling resolver runs before ``own_left_behind``'s dirty test, so a recorded hold may be
    clean. The run's worktree snapshot does not cover a path outside the run's scope, so the test is
    ``own_left_behind``'s own (index identity, zero subprocess) run without the resolver. An
    unreadable index reports every hold rather than none."""
    if not held:
        return {}
    from coordinator_core.ops.ceremony import commit_companion

    try:
        dirty = set(commit_companion.own_left_behind(worktree_root, {"-": list(held)}, "-", ()))
    except Exception:  # noqa: BLE001 -- surface the hold rather than hide it
        dirty = set(held)
    return {p: list(ids) for p, ids in sorted(held.items()) if p in dirty}


def _kept_blob_supply(
    worktree_root: Path, art_run_id: object, run_id: Optional[str], landing: Sequence[str],
    own_declared: Sequence[str], plan: Optional[str], minted_before: Optional[tuple],
) -> dict:
    """``{path: blob}`` for each landing path a live or halted peer co-declares AND the run's own
    ``kept.json`` (``chunks[*].paths``, ``post_checkpoint``) records a blob for: that blob lands in
    place of the worktree bytes, so the peer's hunks stay in the tree. A co-declared path with no
    recorded blob is absent here and keeps refusing in the integrity gate. No git spawn of its own;
    the hold lookup runs only when a landing path has a recorded blob."""
    if not isinstance(art_run_id, str) or not art_run_id:
        return {}
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_hold_release, kept_work
    from coordinator_core.ops.dispatch_emit import merge_ancestry_release, peer_hunk_gate

    try:
        data = kept_work.read(ask_contract.record_dir(worktree_root, art_run_id))
    except (OSError, ValueError):
        return {}
    blobs: dict = {}
    for entry in data["chunks"].values():
        paths = entry.get("paths") if isinstance(entry, dict) else None
        if isinstance(paths, dict):
            for rel, blob in paths.items():
                key = kept_work._norm(rel)
                if key is not None and isinstance(blob, str):
                    blobs[key] = blob
    for rel, blob in data[kept_work.POST_CHECKPOINT_KEY].items():
        key = kept_work._norm(rel)
        if key is not None:
            blobs[key] = blob
    candidates = [p for p in landing if p in blobs]
    if not candidates:
        return {}
    holds = halted_hold_release.without_released_paths(worktree_root, merge_ancestry_release.release(
        worktree_root,
        peer_hunk_gate.co_declared_peer_holds(
            worktree_root, run_id, candidates, own_declared, own_plan=plan, minted_before=minted_before,
        ),
    ))
    return {path: blobs[path] for path, _holder in holds if path in blobs}


KEPT_BLOB_STALE = "kept-blob-stale"


def _stale_kept_blobs(common_dir: Path, supplied: dict, expected: dict) -> list:
    """``[(path, blob)]`` for each supplied kept blob whose sha256 differs from the run's recorded
    sha256 for that path (``expected``, the run's latest own write). A path with no expectation, or
    a blob absent from the object store (commit_v2 refuses it), is not judged here. In-process
    object read: no git spawn."""
    import hashlib

    from coordinator_core.git.git_objects import _read_object

    out = []
    for path, blob in supplied.items():
        want = expected.get(path)
        stored = _read_object(common_dir, blob) if want else None
        if stored is not None and stored[0] == "blob" and hashlib.sha256(stored[1]).hexdigest() != want:
            out.append((path, blob))
    return out


def _own_run_seniority(worktree_root: Path, run_id: object, script_path_raw: object = None) -> Optional[tuple]:
    """`(minted_at, run_id)` of the committing run's claimed token. A plan run mints no token: its
    emission receipt's `emitted_at` (wall time, the clock `minted_at` uses) is then the moment it was
    minted, keyed by the script stem; never the script's filesystem mtime. None when neither
    resolves. Seniority only drops a junior peer's declaration; blob-drift still refuses
    any hunk that junior run already wrote into a file this commit carries."""
    from coordinator_core.ops.dispatch_emit import run_token

    if isinstance(run_id, str):
        record = run_token._read(run_token._record_path(worktree_root, run_id, claimed=True))
        if record is not None and isinstance(record.get("minted_at"), (int, float)):
            return (record["minted_at"], run_id)
    if not isinstance(script_path_raw, str) or not script_path_raw:
        return None
    script = Path(script_path_raw)
    script = script if script.is_absolute() else worktree_root / script
    key = run_id if isinstance(run_id, str) else script.stem
    from datetime import datetime

    from coordinator_core.ops.dispatch_emit.emission_receipt import _read_receipt

    receipt = _read_receipt(script)
    try:
        return (datetime.fromisoformat(receipt["emitted_at"]).timestamp(), key)
    except (TypeError, KeyError, ValueError):
        return None


def _claims_before_landing(worktree_root: Path, landed: Sequence[str]) -> dict:
    """``{session id: paths it holds}`` for every claimant of ``landed``, read before the product
    commit because landing releases those claims. Empty when the lookup fails."""
    from coordinator_core.session import claim_index

    try:
        return dict(claim_index.lookup(list(landed), cwd=str(worktree_root)).held_by or {})
    except Exception:  # noqa: BLE001 -- nothing is carried on a doubt
        return {}


def _session_left_behind(
    worktree_root: Path, session_id: str, landed: Sequence[str], held_by: dict, resolver,
) -> list:
    """The session's own dirty claimed paths outside ``landed``, under ``resolver``'s sibling-claim
    exclusion (which also fills the halted-hold map). Zero subprocess; any failure returns
    nothing rather than refusing the landed commit."""
    from coordinator_core.ops.ceremony import commit_companion

    try:
        if not held_by.get(session_id):
            return []
        with commit_companion.excluding_sibling_claims(resolver):
            return list(commit_companion.own_left_behind(worktree_root, held_by, session_id, landed))
    except Exception:  # noqa: BLE001 -- never un-land the product commit
        return []


_COLLAPSE_MIN = 8


def _ignored_among_left(
    worktree_root: Path, left: Sequence[str], snapshot: Optional[_Snapshot],
) -> Optional[list]:
    """The gitignored subset of ``left``: snapshot-covered paths answer from the snapshot (no
    spawn), machine-local paths are skipped (``own_carry_params`` drops them by prefix), and the
    remainder takes one batched ``git check-ignore`` only when non-empty. ``None`` when that spawn
    could not run, or could not answer for every path, so commit_v2 probes for itself.

    ``check-ignore`` costs paths-on-stdin times index entries, so a parent directory holding more
    than ``_COLLAPSE_MIN`` of the remainder is asked once as ``dir/``: an excluded directory
    excludes every untracked path under it, and git never reports a directory that has a tracked
    entry beneath it, so a force-added file keeps the directory unreported. A collapsed directory
    git did not report cannot answer for its paths, so the whole answer is ``None``."""
    from coordinator_core.git.machine_local_paths import machine_local_paths

    local = set(machine_local_paths(list(left)))
    covered, rest = [], []
    for p in left:
        if p in local:
            continue
        (covered if snapshot is not None and snapshot.covers(p) else rest).append(p)
    ignored = list(snapshot.ignored_among(covered)) if covered else []
    if rest:
        by_dir: dict = {}
        for p in rest:
            by_dir.setdefault(p.rpartition("/")[0], []).append(p)
        asked: list = []
        collapsed: dict = {}
        for directory, members in by_dir.items():
            if directory and len(members) > _COLLAPSE_MIN:
                collapsed[directory + "/"] = members
                asked.append(directory + "/")
            else:
                asked.extend(members)
        result = run_git(
            ["check-ignore", "-z", "--stdin"], cwd=str(worktree_root),
            input=b"\0".join(p.encode("utf-8", "surrogateescape") for p in asked),
        )
        if result.timed_out or result.returncode not in (0, 1):
            return None
        reported = {s.decode("utf-8", "surrogateescape") for s in (result.stdout_bytes or b"").split(b"\0") if s}
        for line, members in collapsed.items():
            if line not in reported:
                return None
            ignored += members
        ignored += [p for p in rest if p in reported]
    return ignored


def _land_own_left_behind(
    reply: dict, product_sha: str, session_id: str, worktree_root: Path, repo_root: Optional[Path],
    left: Sequence[str], withheld: Sequence[str] = (), snapshot: Optional[_Snapshot] = None,
) -> None:
    """Land the session's uncontested ``left`` claims in a follow-on commit and record it under
    ``own_carry.REPLY_KEY``. A refusal is recorded as ``{"error": ...}`` and never un-lands the
    product commit; nothing left makes no call. ``withheld`` paths (foreign-excluded, or beside a
    live sibling run's declared path) are never carried. Gitignored paths are dropped before the
    call: the snapshot answers for paths it covers, one batched ``check-ignore`` for the
    non-machine-local rest."""
    from coordinator_core.ops.dispatch_emit import own_carry
    from coordinator_core.session import scope as session_scope

    if not left:
        return
    contested = session_scope.contested_by_live_peers(list(left), session_id, cwd=str(worktree_root))
    if contested is None:
        return
    try:
        carry = own_carry.own_carry_params(
            list(left), {*contested, *withheld}, product_sha, session_id,
            ignored=_ignored_among_left(worktree_root, list(left), snapshot),
        )
        if carry is None:
            return
        out = reentrant_dispatch("ceremony.commit_v2", {**carry, "session_id": session_id}, repo_root=repo_root)
    except Exception as exc:
        reply[own_carry.REPLY_KEY] = {"error": f"{type(exc).__name__}: {exc}"}
        return
    if isinstance(out, dict) and out.get("committed") and out.get("sha"):
        reply[own_carry.REPLY_KEY] = {"sha": str(out["sha"]), "paths": list(carry["paths"])}
    else:
        err = out.get("error") if isinstance(out, dict) else None
        reply[own_carry.REPLY_KEY] = {"error": str(err or f"unexpected commit_v2 reply: {out!r}")}


_RELAND_CLASSES = frozenset({
    "record-dir", "ask-dir", "evidence-sidecar", "seam-sidecar", "baton", "slice-sizing",
    "superseded-record", "delivery-note",
})


def _reland_late_artifacts(
    reply: dict, worktree_root: Path, repo_root: Optional[Path], artifacts, fresh,
    skip: set, session_id: Optional[str], sha: str,
) -> list:
    """Land the run-dir and ask-dir files written after the landing's snapshot, in one follow-on
    ``ceremony.commit_v2`` over file pathspecs, and return the paths landed. ``fresh`` is the
    candidate set re-read after the commit. ``skip`` (landed, withheld or foreign paths) is never
    swept; a live peer's claim is never swept. A refusal is recorded under ``late_reland`` and never
    un-lands the commit."""
    classes = {p: (fresh.class_of(p) or artifacts.class_of(p)) for p in {*fresh.paths(), *artifacts.paths()}}
    cands = sorted(p for p, cls in classes.items() if cls in _RELAND_CLASSES and p not in skip)
    if not cands:
        return []
    dirty = _changed_paths(worktree_root, cands)
    if dirty is None:
        reply["late_reland"] = {"error": "git status could not answer"}
        return []
    paths = sorted(p for p in dirty if (worktree_root / p).is_file())
    if paths and session_id:
        from coordinator_core.session import scope as session_scope

        contested = session_scope.contested_by_live_peers(paths, session_id, cwd=str(worktree_root))
        if contested is None:
            return []
        paths = [p for p in paths if not contested.get(p)]
    ignored = set(_ignored_among_left(worktree_root, paths, None) or ()) if paths else set()
    paths = [p for p in paths if p not in ignored]
    if not paths:
        return []
    params: dict = {
        "paths": paths, "carry_stopped_runs": False,
        "message": f"run artifacts written after the landing\n\nRun-Artifacts-Late-Of: {sha}\n",
    }
    if session_id:
        params["session_id"] = session_id
    try:
        out = reentrant_dispatch("ceremony.commit_v2", params, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 -- never un-land the landed commit
        reply["late_reland"] = {"error": f"{type(exc).__name__}: {exc}"}
        return []
    if isinstance(out, dict) and out.get("committed") and out.get("sha"):
        reply["late_reland"] = {"sha": str(out["sha"]), "paths": paths}
        return paths
    err = out.get("error") if isinstance(out, dict) else None
    reply["late_reland"] = {"error": str(err or f"unexpected commit_v2 reply: {out!r}")}
    return []


def _cap_commit_reply(reply: dict) -> dict:
    """``reply`` bounded by ``commit_v2.cap_reply``, applied once every consumer of the full
    ``full_reply`` lists has read them."""
    from coordinator_core.ops.ceremony.commit_v2 import cap_reply

    return cap_reply(reply)


def _in_scope(path: str, request: CommitRequest) -> bool:
    """Whether ``path`` matches one of ``_scope_pathspecs(request)``."""
    parent = path.rpartition("/")[0]
    for chunk in request.chunks:
        if any(p.rpartition("/")[0] == parent for p in chunk.paths):
            return True
        if any(path.startswith(prefix.rstrip("/") + "/") for prefix in chunk.prefixes):
            return True
    return False


def _session_writes_outside_scope(
    worktree_root: Path, request: CommitRequest, session_id: Optional[str],
    landed: frozenset, snapshot: Optional[_Snapshot],
) -> set:
    """Dirty paths the committing session (or its sub-agents) write-claimed that sit outside every
    directory ``_in_scope`` covers and that nothing landed. They are invisible to the run's
    scoped snapshot, so without this a clean reply could leave an executor write uncommitted.
    Reads the session's own claim records and the index only (no git spawn); any failure yields
    nothing, never a refusal."""
    if not session_id:
        return set()
    try:
        from coordinator_core.ops.ceremony import commit_companion
        from coordinator_core.session import claims as session_claims
        from coordinator_core.session import core as session_core
        from coordinator_core.session import scope as session_scope

        sink = Path(session_core.session_dir(session_id, str(worktree_root))) / session_scope._TOUCH_RECORD_FILENAME
        own, _degraded = session_scope.project_self_write_scope(sink)
        claimed = {p.replace("\\", "/") for p in own}
        claimed.update(
            p.replace("\\", "/")
            for p in session_claims.my_agent_touched(session_id, "exact", str(worktree_root))
        )
        declared = {p for c in request.chunks for p in c.paths}
        candidates = sorted(
            p for p in claimed
            if p not in declared and p not in landed and not _in_scope(p, request)
            and not (snapshot is not None and snapshot.covers(p))
        )
        if not candidates:
            return set()
        return set(commit_companion.own_left_behind(worktree_root, {session_id: candidates}, session_id, ()))
    except Exception:  # noqa: BLE001 -- an unreadable claim record reports nothing extra
        return set()


def _undeclared_dirty(
    worktree_root: Path, request: CommitRequest,
    snapshot: Optional[_Snapshot] = None, landed: frozenset = frozenset(),
    *, run_id: Optional[str] = None, full_out: Optional[set] = None,
    session_id: Optional[str] = None,
) -> dict:
    """Dirty files beside the run's declared writes that no chunk declares.

    The terminal commit lands only declared paths and own-report prefix
    claims, so an executor-written file at an undeclared path is never staged
    by anything. It is reported, never committed: a wrong spine is the
    signal. One scoped ``git status`` spawn; the shared tree means a peer's
    dirt in the same directory is listed too, so a row is a candidate for
    inspection, not proof of this run's authorship. ``undeclared_dirty`` is
    ``None`` when git could not answer. ``full_out``, when given, receives the uncapped
    dirty set before the peer-held filter.
    """
    specs = _scope_pathspecs(request)
    if not specs:
        return {"undeclared_dirty": []}
    declared = {p for c in request.chunks for p in c.paths}
    if snapshot is not None:
        # The snapshot predates the commits: what they landed is no longer dirty.
        dirty = sorted(
            {p for p in snapshot.changed if _in_scope(p, request)} - declared - set(landed)
        )
    else:
        result = run_git(
            [
                "-C", str(worktree_root), "--no-optional-locks", "status",
                "--porcelain", "-z", "--no-renames", "--untracked-files=all", "--", *specs,
            ],
            binary=True,
        )
        if not result.ok:
            return {"undeclared_dirty": None}
        dirty = sorted(
            {
                entry[3:].decode("utf-8", "surrogateescape")
                for entry in result.stdout_bytes.split(b"\0")
                if len(entry) > 3
            }
            - declared
        )
    dirty = sorted({*dirty, *_session_writes_outside_scope(worktree_root, request, session_id, landed, snapshot)})
    if full_out is not None:
        full_out.update(dirty)
    peer_held = _peer_held(worktree_root, request.session_id, dirty, run_id=run_id)
    dirty = [p for p in dirty if p not in peer_held]
    out: dict = {"undeclared_dirty": dirty[:_UNDECLARED_DIRTY_CAP]}
    if peer_held:
        out["undeclared_dirty_peer_held"] = len(peer_held)
    if len(dirty) > _UNDECLARED_DIRTY_CAP:
        out["undeclared_dirty_total"] = len(dirty)
    return out


def _run_owned_dirt(
    worktree_root: Path, request: CommitRequest, snapshot: Optional[_Snapshot],
    session_id: Optional[str], all_paths: list, deleted_paths: list, touched: frozenset,
    *, left_out: Optional[dict] = None,
) -> tuple:
    """``(files, deletions)`` beside the run's declared writes (``_in_scope``, or named in
    ``touched`` whatever its directory) that join the one commit_v2 call, read from the
    pre-commit snapshot; both empty with no snapshot or session id.
    Files exist on disk, are in ``touched`` (this run's own touched set) and uncontested by a
    live peer. Deletions are changed paths absent on disk and tracked at HEAD that any request
    chunk (stranded included) declares or ``touched`` names, likewise uncontested; the HEAD
    classification is one zero-spawn ``partition_declared_deletions`` and an unreadable tree
    yields none (these are not declared deletions, so unknown never means tracked).
    Admission is the run's touched set, never a session claim: a sibling run's dirt in a shared
    directory is not staged and surfaces in ``undeclared_dirty``. ``session_id`` only keys the
    live-peer contest. ``left_out``, when given, receives the ``{path: [sid]}`` it dropped as
    contested."""
    if snapshot is None or not session_id:
        return [], []
    taken = set(all_paths) | set(deleted_paths)
    in_scope = sorted(
        p for p in snapshot.changed
        if p not in taken and (p in touched or _in_scope(p, request))
    )
    present = [p for p in in_scope if (worktree_root / p).is_file()]
    gone = [p for p in in_scope if not (worktree_root / p).exists()]
    if not present and not gone:
        return [], []
    from coordinator_core.session import scope as session_scope

    root_str = str(worktree_root)
    owned = touched
    declared = {p for c in request.chunks for p in c.paths}
    candidates = [p for p in present if p in owned] + [p for p in gone if p in owned or p in declared]
    if not candidates:
        return [], []
    contested = session_scope.contested_by_live_peers(candidates, session_id, cwd=root_str)
    contested = {} if contested is None else contested
    if left_out is not None:
        left_out.update({p: list(contested[p]) for p in candidates if p in contested})
    files = [p for p in present if p in owned and p not in contested]
    gone_ok = [p for p in gone if (p in owned or p in declared) and p not in contested]
    deletions: list = []
    if gone_ok:
        from coordinator_core.git.commit import partition_declared_deletions

        split = partition_declared_deletions(worktree_root, gone_ok)
        deletions = [] if split is None else sorted(split[0])
    return files, deletions


def _empty_diff_landing(worktree_root: Path, request: CommitRequest) -> Optional[dict]:
    """The ``already_landed`` reply naming HEAD when every file the request declares exists on disk
    and equals its HEAD blob (the run's diff is empty), else None. A declared path absent from disk
    (a deletion) is not empty. No next_action: nothing to refire."""
    from coordinator_core.git.git_state import head_sha

    paths = sorted({p for c in request.chunks for p in c.paths if p})
    if not paths:
        return None
    head = head_sha(worktree_root)
    if not head or any(
        not (worktree_root / p).is_file() or _differs_from_commit(worktree_root, head, p) for p in paths
    ):
        return None
    return {"committed": False, "nothing_to_commit": True, "already_landed": True, "sha": head, "paths": paths}


def _sole_run_review_writes(
    worktree_root: Path, session_id: Optional[str], run_id: Optional[str], inline_review: object
) -> frozenset:
    """Normalised in-worktree paths the session's review agents wrote while this run is the only
    one the session holds; a review write outside every declared path lands or halts instead of
    staying untracked."""
    from coordinator_core.ops.dispatch_emit import review_edit_source, review_edits

    try:
        raw = review_edit_source.sole_run_attested_writes(
            worktree_root, session_id, run_id, _review_since(worktree_root, run_id, inline_review)
        )
    except OSError:  # only an unreadable store attributes nothing; any other error is a bug and propagates
        return frozenset()
    normed = (review_edits.norm(p) for p in raw)
    return frozenset(p for p in normed if not review_edits.out_of_worktree(p))


def _ask_named_bug_ids(worktree_root: Path, ledger: object) -> tuple:
    """Ids of existing bug rows the ledger's ask rows or the bound sizing's ``em_rationale`` state closure
    intent for (``bug_close.closing_bug_ids``), whatever the route. In-process reads: no spawn."""
    if ledger is None:
        return ()
    texts = [str(getattr(i, "text", "") or "") for i in getattr(ledger, "items", ()) or ()]
    sizing_rel = getattr(ledger, "sizing_rel", None)
    raw = _read_rel(worktree_root, sizing_rel) if sizing_rel else None
    if raw:
        try:
            doc = _yaml_load(raw)
        except Exception:  # noqa: BLE001 -- an unreadable sizing contributes no ids
            doc = None
        analysis = doc.get("em_analysis") if isinstance(doc, dict) else None
        rationale = analysis.get("em_rationale") if isinstance(analysis, dict) else None
        if isinstance(rationale, str):
            texts.append(rationale)
    return bug_close.closing_bug_ids(worktree_root, "\n".join(texts))


def _close_marked_bugs(
    worktree_root: Path, chunks: Sequence, sha: str, snapshot: Optional[_Snapshot],
    report_cache: dict, ledger: object = None,
) -> tuple:
    """Close the bug rows the credited chunks' reports mark ``post-commit: bug_close <id>``, plus the
    existing rows the run's ask rows and the sizing's ``em_rationale`` name, at ``sha``. Returns
    ``(reply fields, rels the stamp commit carries, saved texts for rollback)``; the fields are empty
    when nothing names a row."""
    ids: dict = {}
    for bug_id in _ask_named_bug_ids(worktree_root, ledger):
        ids.setdefault(bug_id, None)
    for chunk in chunks:
        if not chunk.report:
            continue
        if chunk.report not in report_cache:
            report_cache[chunk.report] = _read_rel(worktree_root, chunk.report)
        for bug_id in bug_close.marker_ids(report_cache[chunk.report] or ""):
            ids.setdefault(bug_id, None)
    if not ids:
        return {}, [], {}
    rels = {i: bug_close.bug_row_rel(worktree_root, i) for i in ids}
    saved = {rel: _read_rel(worktree_root, rel) for rel in rels.values() if rel}
    result = bug_close.close_rows(worktree_root, tuple(ids), sha)
    written = list(result["closed"].values())
    if snapshot is not None:
        snapshot.note_written(written)
    fields: dict = {"bugs_closed": {**result["closed"], **result["already"]}}
    if result["refused"]:
        fields["bug_close_refused"] = result["refused"]
    return fields, written, {rel: saved[rel] for rel in written if rel in saved}


def _commit_siblings(
    sibling_paths: dict, subject: str, home_sha: Optional[str], session_id: Optional[str]
) -> list:
    """One ``ceremony.commit_v2`` per sibling repo, scoped to that repo's
    declared paths. A refusal is reported per repo, never raised."""
    out: list = []
    for root, paths in sibling_paths.items():
        message = subject + (f"\n\nCross-Repo-Of: {home_sha}" if home_sha else "")
        params: dict = {"paths": list(paths), "message": message, "carry_stopped_runs": False}
        if session_id is not None:
            params["session_id"] = session_id
        try:
            reply = reentrant_dispatch("ceremony.commit_v2", params, repo_root=Path(root) / ".git")
        except OpUnavailableError:
            reply = {"committed": False, "error": "ceremony.commit_v2 is not registered"}
        if not isinstance(reply, dict):
            reply = {"committed": False, "error": f"unexpected commit_v2 reply: {reply!r}"}
        out.append({"repo": Path(root).name, "paths": list(paths), **reply})
    return out


_INDEX_REFRESH_OPT_OUT_ENV = "COORDINATOR_INDEX_REFRESH_ON_COMMIT"
_INDEX_REFRESH_CODE = (
    "import sys; from pathlib import Path; "
    "from coordinator_core.workday_start.manifest import structural_index_refresh_admitted; "
    "structural_index_refresh_admitted(Path(sys.argv[1]))"
)


def _spawn_index_refresh(repo_root: str) -> bool:
    """One detached `python -c` child running the memory-admitted structural refresh.

    `detached_spawn.spawn_detached` only launches script files, so this reuses its
    interpreter, env and detach-flag helpers for the `-c` form. Never raises.
    """
    import subprocess

    from coordinator_core.ops.ceremony import detached_spawn as ds

    try:
        python_exe = ds._resolve_python_exe()
        if not python_exe:
            return False
        kwargs: dict = (
            {"start_new_session": True} if hasattr(os, "fork")
            else {"creationflags": ds._windows_detached_flags()}
        )
        subprocess.Popen(
            [python_exe, "-c", _INDEX_REFRESH_CODE, repo_root],
            cwd=repo_root, env=ds._child_env(repo_root, None),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, **kwargs,
        )
    except Exception:  # noqa: BLE001 -- best-effort; never alters the commit verdict
        return False
    return True


def _fire_index_refresh(repo_root: Optional[Path]) -> None:
    """After a landed terminal commit, spend one detached spawn on the admit-gated refresh.

    Skipped under pytest and when `COORDINATOR_INDEX_REFRESH_ON_COMMIT=0`, so tests that
    commit in temp repos launch no real child.
    """
    if os.environ.get(_INDEX_REFRESH_OPT_OUT_ENV) == "0" or "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        from coordinator_core.lifecycle import main_worktree_root

        _spawn_index_refresh(str(main_worktree_root(repo_root)))
    except Exception:  # noqa: BLE001
        pass


@register_op("dispatch.terminal_commit")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.terminal_commit" handler -- mutating, sync.

    Params:
        script_path (str, required unless plan_path and run_id are given without it)
                                          -- the emitted Workflow script to
                                             parse the terminal-commit-request
                                             marker (D2) out of. Guarded under
                                             the caller's own worktree before
                                             any read.
        incomplete_chunks (list[str], required unless task_output_path supplies it; may be empty) -- chunk ids the
                                             run did NOT finish DONE. Any id
                                             not present in the marker's
                                             request refuses the call.
        landed_chunks (list[str], optional)  -- chunk ids the digest listed
                                             incomplete that the driver landed
                                             during recovery (a resumed run
                                             replays their cached BLOCKED
                                             report). Treated as DONE: removed
                                             from the incomplete set before
                                             the commit and receipts are built.
        inline_review (dict, required when the script carries a marker) --
                                             ``{integration_stem, slices,
                                             fixes}``, relayed verbatim from
                                             the digest's
                                             ``next_action.params.inline_review``.
                                             Absent or missing stem/slices
                                             refuses (``refused="unreviewed"``);
                                             its trailer is appended to the
                                             commit message.
        task_output_path (str, optional) -- a Workflow task-output file; its
                                             ``next_action.params`` supplies
                                             script_path / incomplete_chunks /
                                             inline_review, explicit params win.
        session_id (str, optional)       -- forwarded to ``ceremony.commit_v2``
                                             when canonical-UUID shaped.
        ledger_verdict (dict, optional)  -- an ask run's final ledger-checker
                                             verdict ``{items: [{id, status,
                                             essential?, wired?, withheld_by?
                                             (a gated chunk id of the run's
                                             manifest: the not_met row passes,
                                             stays open and is named by a
                                             ``Requirement-Open`` trailer),
                                             superseded_by?,
                                             post_commit_acts?, remaining?}]}``; the reply
                                             carries ``post_commit_owed`` for
                                             each ``sha_memo`` item;
                                             required when the run is
                                             ledger-bearing, re-judged against
                                             the sizing's register rows
                                             (``refused`` is ``ledger-unjudged``
                                             / ``ledger-unmet``).
        withhold_paths (list, optional)  -- ``[{path, reason}]`` left unstaged and dirty; the
                                             withhold route for a run with no record dir.
        run_id, run_token (str, optional) -- a warp run's in-run commit:
                                             the token is redeemed once, here
                                             (``refused="run-token"`` otherwise).
        allow_empty (bool, optional)      -- default false; non-boolean is refused before any write.
                                             Forwarded to the product ``ceremony.commit_v2`` call
                                             only: a run whose content already landed (every
                                             declared write equals HEAD) lands one marker commit
                                             and the close legs run against its sha.
        declared_reverts (list[str], optional) -- a planned removal's restores: repo-relative paths
                                             the run deliberately rolls back to an older blob.
                                             Only those among the run's own staged paths reach
                                             ``ceremony.commit_v2``; the rest are named in the
                                             reply's ``declared_reverts_ignored``.

    Returns: ``ceremony.commit_v2``'s reply dict plus ``chunks_committed``
    (ids), ``dropped_absent`` (paths dropped as absent-and-untracked) and
    ``prefix_files`` (own-prefix-claimed files folded into the commit),
    ``incomplete_chunks`` (present only when ``landed_chunks`` was passed: the
    digest's incomplete list minus them) and
    ``rows_coded`` (``{plan path: [row ids]}`` flipped ``open`` -> ``coded``)
    and ``coded_sha`` (the second, plan-only commit), or ``coded_stamp_error``
    when that second step failed -- the product commit stands regardless.
    ``incomplete_reasons`` maps every incomplete id to its reason: a gated row's own
    ``external_gate`` / ``transitive_gate_closure``, or ``owed: <detail>`` (rows the ask
    manifest withheld; they carry no paths, so nothing is stranded),
    ``executor_partial: <Not-done lines>`` or ``incomplete_unreported``;
    ``index_stale`` lists committed paths whose index entry could not be
    spliced to the landed blob (``[]`` when every entry equals it); a
    non-empty list means the run is not clean.
    ``review_receipt`` (a mise run only) is the inventory record carrying the minted receipt;
    a refused receipt reports ``review_stamp: refused`` instead.
    ``review_stamp`` (``minted``/``refused``) reports the stamp, and on a
    minted stamp with no incomplete chunk ``plan_status`` reports the
    ``implemented`` flip (``implemented``/``refused``/``not-stamped``; every
    non-``implemented`` value carries ``plan_status_reason``, beside the top-level
    ``criterion_status`` the digest's criterion leg reported), which
    ``plan_status_transition`` commits on its own. A
    script with no marker returns ``{"committed": False, "nothing_to_commit":
    True}`` without error. ``commit_v2``'s own ``nothing_to_commit: True``
    (a peer already landed the bytes) passes through unmodified, as a
    non-error. Every reply also carries ``stranded`` -- ``{chunk id: [sorted declared
    paths]}`` for each ``incomplete_chunks`` id the request marker names, the
    work the commit left uncommitted; ``{}`` when none, or when no marker was read. The key is
    omitted on a ``missing-run-outcome`` or ``unreviewed`` refusal: the incomplete set was not
    known, so an empty map would falsely read as "nothing stranded".
    ``refused: "no-credited-chunk-landed"`` (``committed`` false) when something is stranded and no
    non-bookkeeping chunk lands, contributes or was already landed (``landed_chunks``, a clean done
    chunk); ``refused: "credited-chunk-uncommitted"`` with ``uncommitted`` ({chunk: [paths]}) when a
    credited chunk's changed declared write is outside the commit. Neither writes anything.
    ``stranded_residue`` (present only when non-empty) maps each stranded chunk to its declared paths
    that a side route (review residue, run-owned dirt, prefix expansion, run artifacts) would have
    carried and the commit left in the tree.
    ``withheld_chunks`` (present only when non-empty) maps each done chunk whose every declared
    path is withheld to ``[{path, reason}]``; it is in neither ``chunks_committed`` nor
    ``no_product_hunk``, is not flipped ``coded``, gets no receipt, and its ``incomplete_reasons``
    entry reads ``withheld: <reason>``. ``withhold_unmatched`` lists withheld paths the commit did
    not carry. A refire with no bound sizing reads only its own record dir's withholds, not a
    predecessor's.
    ``entangled`` (present only when non-empty) maps each chunk held back from the commit to
    the stranded chunk ids whose uncommitted ``.py`` files it imports (transitively): landing it
    would leave an unresolvable import at that sha. It is stranded with them, its reason
    ``entangled: ...``; land the pair by hand once the definer is finished.
    ``blockers`` (present only when non-empty) maps each executor-PARTIAL row, whose
    files stay uncommitted, to the undone work its report names.
    ``regraded`` (present only when non-empty) maps each executor-PARTIAL row, re-graded
    complete, to why. Three sources: ``outside-footprint...`` (the report's ``gate-blocker:
    outside-footprint`` line, every file of the row in the passed review's ``prep.slice_files``);
    ``ledger-met: <item ids>`` (a ledger-bearing run whose ledger passed, a non-BLOCKED readable
    report, every file in a review slice with no unresolved finding or confinement violation; the
    delivery verdict is not read); ``credited: <evidence>`` (``workflow.revise credit_chunks``).
    A held chunk is never re-graded. It commits with the run,
    leaves ``incomplete_chunks``, and is coded like a DONE row.
    Once the run reaches its commit (every refusal and no-op before that omits it),
    the reply also carries ``undeclared_dirty`` -- the
    dirty files (modified or untracked) in the directories of the run's declared
    writes, or under its declared prefixes, that no chunk declares: work the
    commit could never see. Reported, never committed, except a path the committing session
    itself claimed and no live peer holds: the follow-on commit lands it (reply
    ``own_left_behind_commit``), though it stays in this list. ``None`` when git could
    not answer; ``undeclared_dirty_total`` appears when the list is capped.
    A file the run's review stage wrote (per-agent touch records since the run began) that is still
    dirty, uncontested and declared by no sibling run rides the one commit as ``review_residue``
    (a ``Review-Residue:`` trailer each). One that cannot land (``peer-contested``, ``deleted``,
    ``out-of-worktree``) refuses before anything lands: ``refused: "review-edit-unlanded"`` with
    ``review_edits_unlanded`` ({path: reason}). An unanswerable source lands the commit with
    ``ok: false`` and ``review_edits: null``; a landed residue path still dirty sets ``ok: false``
    and ``review_residue_dirty``.

    No-script close: a call carrying ``plan_path`` and ``run_id`` and no ``script_path``
    (the caller's own params; a ``task_output_path`` digest's script does not count) closes
    a run whose terminal commit already landed, from the plan and the run's close-job
    record. The judge verdict is the first of: the close-job record's ``criterion``, the
    run's review record (when it is the plan's), the supplied ``task_output_path`` digest's
    ``inline_review.criterion``. ``task_output_path`` must be this run's own output. Its
    reply is ``close_without_script.close_landed_run``'s own (``criterion_status``,
    ``verdict_source``, ``review_stamp``, ``plan_status``, ``close``) with no ``stranded``,
    ``undeclared_dirty``, ``incomplete_reasons`` or ``next`` key. ``refused`` is one of
    ``no-close-job``, ``close-job-plan-mismatch``, ``terminal-commit-not-landed``,
    ``no-judge-verdict``, ``task-output-run-mismatch``.
    """
    from coordinator_core.git.call_io_scope import call_io_scope

    with call_io_scope():
        reply = _handler_body(params, repo_root)
    if isinstance(reply, dict) and reply.get("committed") and reply.get("sha"):
        _fire_index_refresh(repo_root)
    return reply


def _handler_body(params: dict, repo_root: Optional[Path] = None) -> dict:
    stranded: dict = {}
    scope: dict = {}
    withheld: dict = {}
    blockers: dict = {}
    regraded: dict = {}
    close_ctx: dict = {}
    reply = _terminal_commit(
        params, repo_root, stranded, scope, withheld, blockers, regraded, close_ctx=close_ctx
    )
    if (
        _is_no_script_call(params) or _is_halted_record_call(params) or _is_grind_call(params)
        or _is_fanout_record_call(params)
    ):
        return reply
    if close_ctx:
        reply["close"] = _close_block(close_ctx, reply)
        if "siblings" in reply["close"]:
            reply["siblings"] = reply["close"]["siblings"]
    if reply.get("refused") not in _STRANDED_UNKNOWN_REFUSALS:
        reply["stranded"] = {c: sorted(ps) for c, ps in stranded.items()}
    if blockers:
        reply["blockers"] = blockers
    if regraded:
        reply["regraded"] = regraded
    if scope.get("peer_held_left"):
        from coordinator_core.ops.dispatch_emit import peer_claim_contract

        reply["peer_held_left"] = peer_claim_contract.left_out(scope["peer_held_left"])
    if scope.get("case_canonicalized"):
        reply["case_canonicalized"] = scope["case_canonicalized"]
    if scope:
        if scope.get("run_owned") is not None:
            reply["run_owned"] = scope["run_owned"]
        if scope.get("stranded_residue"):
            reply["stranded_residue"] = {c: sorted(ps) for c, ps in scope["stranded_residue"].items()}
        in_scope_dirty: set = set()
        reply.update(
            _undeclared_dirty(
                scope["root"], scope["request"], scope.get("snapshot"), frozenset(scope.get("landed", ())),
                run_id=params.get("run_id") if isinstance(params, dict) else None,
                full_out=in_scope_dirty,
                session_id=scope.get("session_id"),
            )
        )
        arts = reply.get("run_artifacts")
        named = in_scope_dirty | set(scope.get("landed", ())) | set(reply.get("archived") or ())
        if isinstance(arts, dict):
            named |= set(arts.get("landed") or ()) | set(arts.get("withheld") or ())
        named |= set(reply.get("run_artifact_residue") or ())
        reply.update(residue_classes.dirty_engine_residue(scope["root"], exclude=named))
    incomplete = params.get("incomplete_chunks") if isinstance(params, dict) else None
    landed = (params.get("landed_chunks") if isinstance(params, dict) else None) or []
    held = params.get("held") if isinstance(params, dict) else None
    held = {
        k: v for k, v in _held_entries(params if isinstance(params, dict) else {}).items()
        if k not in _released_ids(params if isinstance(params, dict) else {})
    }
    if (landed or regraded) and isinstance(incomplete, list) and isinstance(landed, list):
        reply["incomplete_chunks"] = sorted(set(incomplete) - set(landed) - set(regraded))
    if isinstance(incomplete, list) and isinstance(landed, list):
        entangled = reply.get("entangled") or {}
        open_ids = (
            (set(incomplete) - set(landed) - set(regraded)) | set(entangled) | set(held)
            | set(reply.get("withheld_chunks") or ())
        )
        reasons = {}
        for i in sorted(open_ids):
            if i in held:
                reasons[i] = f"held: {held[i]}"
            elif i in withheld:
                reasons[i] = withheld[i]
            elif i in entangled:
                reasons[i] = "entangled: imports stranded " + ", ".join(entangled[i])
            elif i in blockers:
                reasons[i] = f"executor_partial: {blockers[i]}" if blockers[i] else "executor_partial"
            else:
                reasons[i] = "incomplete_unreported"
        if reasons:
            reply["incomplete_reasons"] = reasons
    if stranded or reply.get("incomplete_reasons"):
        reply["next"] = _resume_hint(stranded, scope, params)
    return reply


def _resume_hint(stranded: dict, scope: dict, params: dict) -> str:
    request = scope.get("request")
    plan = getattr(request, "plan_path", None) or (
        params.get("plan_path") if isinstance(params, dict) else None
    ) or "<plan>"
    run_text = (params.get("task_output_path") if isinstance(params, dict) else None) or "<run-output-file>"
    command = f"emit-dispatch-workflow --plan {plan} --only-incomplete {run_text}"
    if stranded:
        return (
            f"stranded files for {', '.join(sorted(stranded))}: keep them in tree, "
            f"re-emit with {command}"
        )
    return f"re-emit the incomplete rows with {command}"


def _drift_paths(blob_drift: object) -> list:
    """Paths named by a commit_v2 ``blob_drift`` list."""
    return sorted({d["path"] for d in blob_drift if isinstance(d, dict) and isinstance(d.get("path"), str)}) \
        if isinstance(blob_drift, list) else []


def _released_ids(params: dict) -> set:
    """Chunk ids named in ``params.release_held``: held entries dropped from the withholding."""
    rel = params.get("release_held") if isinstance(params, dict) else None
    return {r for r in (rel if isinstance(rel, list) else []) if isinstance(r, str)}


def _held_entries(params: dict) -> dict:
    """``{chunk id: reason}`` of ``params.held``, in object form (``{chunk, reason}``) or bare string form."""
    held = params.get("held") if isinstance(params, dict) else None
    out: dict = {}
    for h in held if isinstance(held, list) else []:
        if isinstance(h, str):
            out[h] = ""
        elif isinstance(h, dict) and isinstance(h.get("chunk"), str):
            out[h["chunk"]] = h.get("reason") or ""
    return out


def _held_ids(params: dict) -> set:
    return set(_held_entries(params)) - _released_ids(params)


_STOP_RULE_REPLY_RE = re.compile(r"^[ \t>\"]*[*_`]{0,2}STOP-RULE-FIRED[*_`]{0,2}:[ \t]*\S", re.MULTILINE)


def _stop_rule_fired_ids(worktree_root: Path, request: CommitRequest) -> set:
    """Chunk ids whose report carries a STOP-RULE-FIRED line: the row halted the run and is never coded."""
    out: set = set()
    for chunk in request.chunks:
        text = _read_rel(worktree_root, chunk.report) if chunk.report else None
        if text is not None and _STOP_RULE_REPLY_RE.search(text):
            out.add(chunk.id)
    return out


def _held_write_paths(worktree_root: Path, request: CommitRequest, held_ids: set) -> dict:
    """``{path: [held chunk ids]}`` of every ``writes:`` path a held chunk declares: the marker's
    chunk paths plus the plan spine row of the same id (a held chunk may be absent from the marker)."""
    out: dict = {}
    for chunk in request.chunks:
        if chunk.id in held_ids:
            for p in chunk.paths:
                out.setdefault(p, []).append(chunk.id)
    text = _read_rel(worktree_root, request.plan_path) if request.plan_path else None
    if text is not None:
        import yaml

        from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

        located = locate_fenced_block(text.replace("\r\n", "\n"))
        try:
            rows = _yaml_load(located.body) or [] if located.status == LocateStatus.LOCATED else []
        except yaml.YAMLError:
            rows = []
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and str(row.get("id")) in held_ids:
                for p in row.get("writes") or ():
                    if isinstance(p, str):
                        ids = out.setdefault(PurePosixPath(p.replace("\\", "/")).as_posix(), [])
                        if str(row["id"]) not in ids:
                            ids.append(str(row["id"]))
    return out


#: The mode predicates below restate `halted_record_commit.is_halted_record_call`,
#: `roadmap_fanout_commit.is_fanout_record_call` and `grind_commit_contract.is_grind_call` so a plan
#: run need not load those modules to learn it is none of them; a test pins them to the originals.
def _is_halted_record_call(params: object) -> bool:
    halted = params.get("halted_by") if isinstance(params, dict) else None
    return isinstance(halted, dict) and isinstance(halted.get("halted"), str) and bool(halted["halted"])


def _is_fanout_record_call(params: object) -> bool:
    spec = params.get("fanout") if isinstance(params, dict) else None
    plans = spec.get("plans") if isinstance(spec, dict) else None
    return isinstance(plans, list) and bool(plans) and all(isinstance(p, str) and p for p in plans)


def _is_grind_call(params: object) -> bool:
    grind = params.get("grind") if isinstance(params, dict) else None
    return isinstance(grind, dict) and isinstance(grind.get("rows"), list)


def _is_no_script_call(params: object) -> bool:
    """True for a call that names a plan and a run and carries no ``script_path`` key of its own."""
    return (
        isinstance(params, dict)
        and "script_path" not in params
        and isinstance(params.get("plan_path"), str)
        and isinstance(params.get("run_id"), str)
    )


# Another live call owns the claim and runs its own stamp leg; a stamp here would race it.
_REFUSAL_STAMP_SKIPPED_CODES = frozenset({"run-token-in-flight", "run-token"})


def _refusal_names_plan(reply: dict, plan_path: str) -> bool:
    """True when a peer-claim/blob-drift refusal lists ``plan_path``; committing it would override the refusal (DR-462)."""
    if reply.get("refused") not in ("peer-claim", "blob-drift"):
        return False
    claims = reply.get("peer_claims")
    named = {c.get("path") for c in claims if isinstance(c, dict)} if isinstance(claims, list) else set()
    return plan_path in named or plan_path in _drift_paths(reply.get("blob_drift"))


def _stamp_on_refusal(params: dict, repo_root: Optional[Path], reply: dict) -> dict:
    """Refusal stamp fields for a refused reply, ``{}`` when skipped or the stamp itself fails."""
    plan_path = params.get("plan_path") if isinstance(params, dict) else None
    if repo_root is None or not isinstance(plan_path, str) or not plan_path:
        return {}
    if reply.get("refused") in _REFUSAL_STAMP_SKIPPED_CODES or _refusal_names_plan(reply, plan_path):
        return {}
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.lifecycle import main_worktree_root
    from coordinator_core.ops.dispatch_emit.refusal_coded_stamp import stamp_landed_rows_on_refusal

    session_id = params.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not _UUID_RE.fullmatch(session_id)):
        session_id = None
    try:
        return stamp_landed_rows_on_refusal(main_worktree_root(repo_root), repo_root, params, reply, session_id)
    except Exception as exc:
        return {"refusal_coded_stamp_error": f"{type(exc).__name__}: {exc}"}


def _terminal_commit(
    params: dict, repo_root: Optional[Path], stranded: dict, scope: dict,
    withheld: Optional[dict] = None, blockers: Optional[dict] = None,
    regraded: Optional[dict] = None,
    close_ctx: Optional[dict] = None,
) -> dict:
    """Run the commit and settle the run token it redeemed: a landed commit consumes the claim;
    every other return, and an exception raised before commit_v2 landed one, releases it."""
    from coordinator_core.ops.dispatch_emit import halted_hold_release

    token_state: dict = {}
    try:
        with halted_hold_release.release_scope():
            reply = _terminal_commit_inner(
                params, repo_root, stranded, scope, withheld, blockers, regraded, close_ctx, token_state
            )
    except BaseException:
        _settle_token(token_state, landed=bool(token_state.get("landed")))
        raise
    _settle_token(token_state, landed=bool(token_state.get("landed")))
    if reply.get("refused") and not reply.get("committed"):
        reply.update(_stamp_on_refusal(params, repo_root, reply))
    close = token_state.pop("since_halt_close", None)
    if close is None:
        return reply
    if reply.get("committed"):
        reply = {**reply, "since_halt": close["since_halt"]}
        return reply if reply.get("paths") else {**reply, "already_landed": True}
    if reply.get("nothing_to_commit") and not reply.get("refused") and not reply.get("error"):
        return {**reply, **close, "nothing_to_commit": False}
    return reply


_CLAIM_POLL_SECS = 0.05
#: Ceiling on the claim wait, inside the 500ms process budget; never the dispatcher deadline.
_CLAIM_WAIT_MAX_SECS = 0.3
CLAIM_WAIT_LEG = "run-token-claim-wait"


def _redeem_or_answer(worktree_root: Path, run_id: object, token: str, script_path: object) -> Optional[dict]:
    """Redeem the run token; None when this call holds the claim, else the reply that ends it.

    A failed redeem answers ``already_landed`` from the spent receipt when its sha is on HEAD.
    A claim a first call still holds is polled (read-only, no spawn) until it resolves or the
    dispatcher's deadline is spent; a claim released meanwhile is redeemed here. An unresolved
    claim is refused ``run-token-in-flight``; anything else is refused ``run-token``."""
    import time

    from coordinator_core import dispatch_abandon
    from coordinator_core.ops.dispatch_emit import run_token, terminal_commit_reconcile

    if run_token.redeem(worktree_root, run_id, token, script_path):
        return None
    landed = terminal_commit_reconcile.already_landed_reply(worktree_root, run_id, token, script_path)
    if landed is not None:
        return landed
    if run_token.claim_pending(worktree_root, run_id, token, script_path):
        left = dispatch_abandon.remaining()
        if left is not None:
            end = time.monotonic() + min(_CLAIM_WAIT_MAX_SECS, max(0.0, left - dispatch_abandon.REPLY_MARGIN_SECS))
            while run_token.claim_pending(worktree_root, run_id, token, script_path) and time.monotonic() < end:
                landed = terminal_commit_reconcile.already_landed_reply(worktree_root, run_id, token, script_path)
                if landed is not None:
                    return landed
                time.sleep(_CLAIM_POLL_SECS)
            if run_token.redeem(worktree_root, run_id, token, script_path):
                return None
            landed = terminal_commit_reconcile.already_landed_reply(worktree_root, run_id, token, script_path)
            if landed is not None:
                return landed
        if run_token.claim_pending(worktree_root, run_id, token, script_path):
            return _error(
                "a first call of this run's terminal commit still holds the run token claim and has not "
                "finished; this call committed nothing",
                refused="run-token-in-flight",
                blocked_leg=CLAIM_WAIT_LEG,
            )
    return _error(
        "params.run_token does not match a live, unspent token minted for params.run_id and "
        "this script (a token admits one landed commit). A run that ended with its commit unlanded "
        "recovers without run_token: coordinator-invoke dispatch.terminal_commit --from-task-output <file>",
        refused="run-token",
    )


def _settle_token(token_state: dict, *, landed: bool, receipt: Optional[dict] = None) -> None:
    claim = token_state.get("claim")
    if claim is None:
        return
    from coordinator_core.ops.dispatch_emit import run_token

    root, run_id, token, script_path = claim
    if landed:
        run_token.consume(root, run_id, landed=receipt)
    else:
        run_token.release(root, run_id, token, script_path)


def _terminal_commit_inner(
    params: dict, repo_root: Optional[Path], stranded: dict, scope: dict,
    withheld: Optional[dict], blockers: Optional[dict],
    regraded: Optional[dict],
    close_ctx: Optional[dict],
    token_state: dict,
) -> dict:
    if repo_root is None:
        return _error(
            "dispatch.terminal_commit requires a common_dir-keyed dispatch; "
            "repo_root (git common dir) was not supplied"
        )

    if _is_fanout_record_call(params):
        from coordinator_core.ops.dispatch_emit.roadmap_fanout_commit import commit_fanout_record
        from coordinator_core.lifecycle import main_worktree_root

        if _is_halted_record_call(params):
            return _error("params carry both halted_by and fanout", refused="mode-ambiguous")
        return commit_fanout_record(main_worktree_root(repo_root), repo_root, params)

    if _is_halted_record_call(params):
        from coordinator_core.ops.dispatch_emit.halted_record_commit import commit_halted_record
        from coordinator_core.lifecycle import main_worktree_root

        if _is_grind_call(params):
            return _error("params carry both grind and halted_by", refused="mode-ambiguous")
        return commit_halted_record(main_worktree_root(repo_root), repo_root, params)

    if _is_grind_call(params):
        from coordinator_core.ops.dispatch_emit.land_run_grind import commit_grind_rows
        from coordinator_core.lifecycle import main_worktree_root

        return commit_grind_rows(main_worktree_root(repo_root), repo_root, params)

    if _is_no_script_call(params):
        from coordinator_core.ops.dispatch_emit import close_without_script
        from coordinator_core.lifecycle import main_worktree_root

        return close_without_script.close_landed_run(main_worktree_root(repo_root), repo_root, params)

    task_output_raw = params.get("task_output_path") if isinstance(params, dict) else None
    if isinstance(params, dict) and isinstance(params.get("task_output_path"), str):
        try:
            params = {**_params_from_task_output(params["task_output_path"], params.get("run_id")), **{
                k: v for k, v in params.items() if k != "task_output_path"}}
        except ValueError as exc:
            return _error(str(exc))

    missing = _missing_run_outcome(params)
    if missing is not None:
        return missing

    if isinstance(params, dict) and not params.get("script_path"):
        from coordinator_core.lifecycle import main_worktree_root
        from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM
        from coordinator_core.ops.dispatch_emit.halted_record_commit import commit_halted_record
        from coordinator_core.ops.dispatch_emit.halted_run_route import land_only_commit_route

        root = main_worktree_root(repo_root)
        payload = land_only_commit_route(root, params.get("run_id"))
        if payload is not None:
            return commit_halted_record(root, repo_root, {**params, HALTED_RECORD_PARAM: payload})
        halted = halted_refusal(root, params.get("run_id"), "terminal commit")
        if halted is not None:
            return _error(halted, refused="halted-run")

    refusal = validate_params("dispatch.terminal_commit", params, _PARAM_FIELDS)
    if refusal is not None:
        return _error(refusal["error"])

    from coordinator_core.ops.dispatch_emit import landing_remedy as _remedy

    try:
        param_withheld = _remedy.param_withholds(params)
    except ValueError as exc:
        return _error(f"params.withhold_paths: {exc}")

    script_path_raw = params["script_path"]
    incomplete_chunks = set(params["incomplete_chunks"]) - set(params.get("landed_chunks") or [])
    inline_review = params.get("inline_review")

    from coordinator_core.git.commit_trailers import _UUID_RE

    session_id = params.get("session_id")
    if session_id is not None and (
        not isinstance(session_id, str) or not _UUID_RE.fullmatch(session_id)
    ):
        session_id = None

    from coordinator_core.lifecycle import main_worktree_root

    worktree_root = main_worktree_root(repo_root)

    if params.get("run_token") is not None:
        from coordinator_core.ops.dispatch_emit import run_token

        answered = _redeem_or_answer(worktree_root, params.get("run_id"), params["run_token"], script_path_raw)
        if answered is not None:
            return answered
        token_state["claim"] = (worktree_root, params.get("run_id"), params["run_token"], script_path_raw)

    guarded_script = contained_path(worktree_root / script_path_raw, [worktree_root])
    if guarded_script is None:
        return _error(
            f"params.script_path {script_path_raw!r} does not resolve under the "
            "caller's own worktree"
        )

    script_source = "script"
    try:
        script_text = guarded_script.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        from coordinator_core.ops.dispatch_emit.emission_receipt import receipt_landing_text

        script_text = receipt_landing_text(guarded_script)
        if not script_text:
            return _error(
                f"script-missing: {script_path_raw!r} is gone and its receipt carries no landing "
                f"markers ({exc}); run: python -m coordinator_core.ops.dispatch_emit."
                f"stranded_part_rebuild --script {script_path_raw} --task-output <task output "
                "path> --inventory <tranche record>",
                refused="script-missing",
            )
        script_source = "receipt"
    except OSError as exc:
        return _error(f"cannot read params.script_path {script_path_raw!r}: {exc}")

    manifest_rel: Optional[str] = None
    ask_run_dir: Optional[Path] = None
    manifest_declared: tuple = ()
    request: Optional[CommitRequest] = None
    try:
        manifest_rel = parse_manifest_marker(script_text)
        if manifest_rel is not None:
            if any(ln.startswith(MARKER_PREFIX) for ln in script_text.splitlines()):
                return _error(
                    "script carries both an inline terminal-commit-request marker "
                    "and an ask-run-manifest marker; emit one",
                    refused="both-markers",
                )
            run_root = worktree_root / RUN_DIR_ROOT
            manifest_abs = contained_path(
                worktree_root / manifest_rel, [worktree_root / RECORD_DIR_ROOT, run_root]
            )
            if manifest_abs is None:
                return _error(
                    f"ask-run-manifest path {manifest_rel!r} does not resolve under "
                    f"{RECORD_DIR_ROOT} or {RUN_DIR_ROOT}",
                    refused="manifest-escapes-run-root",
                )
            try:
                manifest = StageManifest.from_json(
                    json.loads(manifest_abs.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return _error(f"cannot read ask-run manifest {manifest_rel!r}: {exc!r}")
            if withheld is not None:
                withheld.update({g.id: g.reason for g in manifest.gated})
                withheld.update({o.id: f"owed: {o.detail}" for o in manifest.owed})
            ask_run_dir = manifest_abs.parent
            manifest_declared = tuple(manifest.review_declared_paths)
            from coordinator_core.ops.dispatch_emit.in_run_widen import record_widens

            record_widens(ask_run_dir, params.get("widened"))
            request_abs = contained_path(worktree_root / manifest.marker_path, [run_root])
            if request_abs is None:
                return _error(
                    f"manifest marker_path {manifest.marker_path!r} does not resolve "
                    f"under {RUN_DIR_ROOT}",
                    refused="manifest-escapes-run-root",
                )
            try:
                request = parse_marker(request_abs.read_text(encoding="utf-8"))
            except OSError as exc:
                return _error(f"cannot read manifest marker_path {manifest.marker_path!r}: {exc}")
        else:
            request = parse_marker(script_text)
    except MalformedCommitRequestError as exc:
        return _error(f"malformed commit request: {exc}", refused="malformed-request")
    if request is None:
        request = _anchor_request(worktree_root, params, inline_review)
    if request is None:
        return {"committed": False, "nothing_to_commit": True}
    # A declared directory (`docs/plans`) is a scope, not a file: git's pathspec staging refuses it.
    if any((worktree_root / p).is_dir() for c in request.chunks for p in c.paths):
        request = dataclasses.replace(request, chunks=tuple(
            dataclasses.replace(c, paths=tuple(p for p in c.paths if not (worktree_root / p).is_dir()))
            for c in request.chunks
        ))
    anchor_only = not request.chunks
    reviewed_bytes, reviewed_at, prep_from_disk = _reconcile_prep(
        worktree_root, session_id, _run_id_of(params, ask_run_dir, script_path_raw), request, inline_review
    )
    # An ask run's product commit carries product, its delivery note and its record directory;
    # a stageable plan gets the second, stamp commit.
    product_only = ask_run_dir is not None
    record_dir_rel: Optional[str] = None
    if ask_run_dir is not None:
        try:
            rel = ask_run_dir.relative_to(worktree_root.resolve()).as_posix()
        except ValueError:
            rel = None
        if rel is not None and (rel == RECORD_DIR_ROOT or rel.startswith(RECORD_DIR_ROOT + "/")):
            record_dir_rel = rel
    # A review-only run closes no row: a row reopened since its emit must stay open.
    rows_untouched = anchor_only or request.review_only
    if close_ctx is not None:
        applies = bool(request.plan_path) and not rows_untouched
        kind, plan_governs = (
            _close_kind(_read_rel(worktree_root, request.plan_path)) if applies else ("plan-run", True)
        )
        from coordinator_core.ops.dispatch_emit import run_close
        from coordinator_core.ops.dispatch_emit.run_close import kind_presets

        presets = kind_presets(kind, plan_governs=plan_governs)
        close_ctx.update(
            root=worktree_root,
            # run_id is the close-job record key every reader uses (the script stem, re-keyed
            # to this run when the stem names a different run's .workflow script);
            # sibling_run_id is the ask run's own id, which names its sibling-status file.
            run_id=run_close.own_close_key(
                _run_id_of(params, ask_run_dir, script_path_raw),
                PurePosixPath(script_path_raw.replace("\\", "/")).stem,
            ),
            sibling_run_id=_run_id_of(params, ask_run_dir, script_path_raw),
            applies=applies,
            plan_rel=request.plan_path,
            inline_review=inline_review if isinstance(inline_review, dict) else {},
            legs=dict(presets),
            completion_entry=None,
            kind=kind,
            presets=presets,
        )

    no_change = _no_change_refusal(
        worktree_root, manifest if ask_run_dir is not None else None, request, inline_review,
        ask_run_dir,
    )
    if no_change is not None:
        return no_change
    # Review at close is deterministic (PM ruling 2026-10-01): a run whose
    # result carries no review-stage output cannot land code, however the
    # script was composed or stripped.
    if inline_review is None:
        return _error(
            "params.inline_review is absent -- the run carries no review-stage "
            "output. Run the digest's `terminal_commit_cli` line verbatim (or pass "
            "task_output_path = the Workflow run's output file, the path named in its completion "
            "notification); else re-emit with a review stage and re-fire"
            + route_suffix(worktree_root, params.get("run_id")),
            refused="unreviewed",
        )
    if not inline_review.get("integration_stem") or inline_review.get("slices") is None:
        return _error(
            "params.inline_review is missing integration_stem and/or slices -- "
            f"no review-stage output to land against (got: {inline_review!r}). "
            "Use the digest's `terminal_commit_cli` line verbatim",
            refused="unreviewed",
        )
    floor_code = review_floor.floor_code_paths(request, inline_review, prep_from_disk)
    not_run = _tests_not_run_refusal(worktree_root, params, request, inline_review)
    if not_run is not None:
        return not_run
    # DR-475: the review floor is a trailer note, never a refusal. An empty-diff landing it flags
    # still short-circuits to the no-op landing when its ledger is met.
    floor_note = _review_floor_note(request, inline_review, prep_from_disk, worktree_root)
    no_op = None
    if floor_note is not None and ask_run_dir is not None:
        no_op = _empty_diff_landing(worktree_root, request)
    if no_op is not None:
        unmet, no_op_ledger, _ = _ledger_gate(
            worktree_root, ask_run_dir, params.get("ledger_verdict"), manifest_abs,
            [g.id for g in manifest.gated],
            {o.id: o.detail for o in manifest.owed if o.reason == "operator"},
            _read_rel(worktree_root, request.plan_path) if request.plan_path else None,
            inline_review, params.get(TERMINAL_RESUME_LEDGER_PARAM), request.plan_path,
            sidecar_root=worktree_root,
        )
        if unmet is None and no_op_ledger is not None:
            return no_op
    over_budget = _xs_over_budget_refusal(
        worktree_root, params, _run_id_of(params, ask_run_dir, script_path_raw), request
    )
    if over_budget is not None:
        if "resize_arm" not in over_budget:
            return over_budget
        _apply_xs_resize(worktree_root, request, over_budget)
    ledger = None
    open_rows: list = []
    if ask_run_dir is not None:
        recovery_withheld = _recovery_withheld_param(params)
        unmet, ledger, open_rows = _ledger_gate(
            worktree_root, ask_run_dir, params.get("ledger_verdict"), manifest_abs,
            [g.id for g in manifest.gated],
            {o.id: o.detail for o in manifest.owed if o.reason == "operator"},
            _read_rel(worktree_root, request.plan_path) if request.plan_path else None,
            inline_review,
            params.get(TERMINAL_RESUME_LEDGER_PARAM), request.plan_path,
            recovery_withheld=recovery_withheld,
            code_paths=floor_code, sidecar_root=worktree_root,
        )
        if unmet is not None:
            if not (unmet.get("already_landed") and unmet.get("committed")):
                return unmet
            token_state["since_halt_close"] = unmet
        # Trap: names only; land_run also withholds their import-closure dependents (`_recovery_dependents`).
        incomplete_chunks = incomplete_chunks | set(recovery_withheld)
    ledger_met = (
        _ledger_met_observation(ledger, params.get("ledger_verdict"), open_rows)
        if ask_run_dir is not None else None
    )
    if close_ctx is not None:
        close_ctx["ledger_met"] = ledger_met
    withheld_product: list = []

    observed_branch = head_branch(worktree_root)
    if observed_branch == "HEAD":
        return _error(
            "HEAD is detached; check out a branch and re-fire, or commit by hand",
            refused="detached-head",
            observed_branch="HEAD",
        )
    if observed_branch is None:
        return _error(
            "HEAD is unreadable; repair the worktree and re-fire, or commit by hand",
            refused="branch-unreadable",
        )
    expected_branch = request.expected_branch
    if expected_branch is not None and expected_branch != observed_branch:
        return _error(
            f"run expected branch {expected_branch!r} but HEAD is on "
            f"{observed_branch!r}; check out {expected_branch} and re-fire, "
            "or commit by hand",
            refused="branch-mismatch",
            expected_branch=expected_branch,
            observed_branch=observed_branch,
        )
    branch_check = "ok" if expected_branch is not None else "not-recorded"

    marker_id = request.deliverable_id
    if marker_id is not None:
        plan_id: Optional[str] = None
        plan_readable = True
        if request.plan_path:
            plan_text = _read_rel(worktree_root, request.plan_path)
            plan_readable = plan_text is not None
            plan_id = plan_deliverable_id(plan_text) if plan_text is not None else None
        refusal: Optional[str] = None
        if valid_deliverable_id(marker_id) is None:
            refusal = "is not a valid dlv- id"
        elif request.plan_path and not plan_readable:
            refusal = f"cannot be verified: plan_path {request.plan_path!r} is unreadable"
        elif request.plan_path and marker_id != plan_id:
            refusal = "differs from the plan's deliverable_id"
        if refusal is not None:
            return _error(
                f"marker deliverable_id {marker_id!r} {refusal}",
                deliverable_id_marker=marker_id,
                deliverable_id_plan=plan_id,
            )
    elif request.plan_path:
        plan_text = _read_rel(worktree_root, request.plan_path)
        plan_id = plan_deliverable_id(plan_text) if plan_text is not None else None
        if plan_id is not None:
            return _error(
                "marker carries no deliverable_id but the plan's is set",
                deliverable_id_marker=None,
                deliverable_id_plan=plan_id,
            )

    # Zero-integration-stage path (2026-09-28 PM order, step b'): a
    # `bookkeeping` inline_review carries `wave_sidecar_paths`/`prep_sidecar`/
    # `plan_id` (the one-stage `inline_review` never does -- its
    # `integration_stem` names a real integration sidecar an agent already
    # wrote). Run the mechanical bookkeeping step FIRST, before the commit is
    # built, so the record it writes lands in the SAME commit as the code it
    # reviews -- the only seam that can call it, since the emitted script has
    # no JS-callable "invoke a Python op" primitive
    # (review_mint/wave_bookkeeping.py's own module docstring).
    bookkeeping_record_path: Optional[str] = None
    review_record_error: Optional[str] = None
    own_evidence_write: Optional[tuple] = None
    if not anchor_only:
        _derive_judge_status(inline_review)
    register_failure = (
        None if anchor_only else _register_gate(
            worktree_root, request.plan_path, inline_review,
            [i.id for i in ledger.items] if ledger is not None else (),
            run_id=_run_id_of(params, ask_run_dir, script_path_raw),
        )
    )
    record_abs: Optional[Path] = None
    record: Optional[dict] = None
    stage_returns = _stage_returns(inline_review) if inline_review is not None else None
    if (
        inline_review is not None
        and inline_review.get("plan_id")
        and isinstance(inline_review.get("wave_sidecar_paths"), list)
        and (inline_review.get("prep_sidecar") or stage_returns)
    ):
        if not session_id:
            return _error(
                "params.inline_review names a bookkeeping stem (wave_sidecar_paths/"
                "plan_id present) but params.session_id is missing or "
                "not canonical-UUID-shaped -- bookkeep_wave needs it to place the record"
            )
        from coordinator_core.ops.review_mint.wave_bookkeeping import bookkeep_wave

        wave_sidecar_paths = [
            worktree_root / p for p in inline_review["wave_sidecar_paths"] if isinstance(p, str)
        ]
        try:
            record = bookkeep_wave(
                wave_sidecar_paths,
                repo_root=worktree_root,
                session_id=session_id,
                plan_id=inline_review["plan_id"],
                prep_sidecar=inline_review.get("prep_sidecar"),
                record_stem=inline_review["integration_stem"],
                stage_returns=stage_returns,
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silently dropped
            return _error(f"review_mint.bookkeep_wave failed: {exc}")
        record_path_abs = record_abs = Path(record["sidecar_path"])
        try:
            bookkeeping_record_path = str(
                record_path_abs.relative_to(worktree_root)
            ).replace("\\", "/")
        except ValueError:
            bookkeeping_record_path = None
        # Written here, not in bookkeep_wave: supersede and review_stamp --repair re-record an
        # earlier firing and would duplicate rows. The evidence-companion check below stages it.
        if request.plan_path and record.get("review_firings"):
            from coordinator_core.ops.criterion_evidence import _criterion_evidence_record
            from coordinator_core.lifecycle import main_worktree_root as _mwr

            try:
                # Trap: a token-released retry re-appends these rows; the writer has no dedupe key.
                if _mwr(worktree_root).resolve() != worktree_root.resolve():
                    review_record_error = (
                        f"review rows not written: main worktree {_mwr(worktree_root)} "
                        f"differs from commit root {worktree_root}"
                    )
                else:
                    from coordinator_core.session import touch_record as _tr

                    plan_pp = PurePosixPath(request.plan_path)
                    ev_rel = str(plan_pp.with_name(plan_pp.stem + _EVIDENCE_SIDECAR_SUFFIX))
                    ev_before = _tr.compute_content_hash(worktree_root / ev_rel)
                    written = _criterion_evidence_record(
                        {"plan_path": request.plan_path, "reviews": record["review_firings"]},
                        repo_root=worktree_root,
                    )
                    if isinstance(written, dict) and written.get("error"):
                        review_record_error = str(written["error"])
                    else:
                        ev_after = _tr.compute_content_hash(worktree_root / ev_rel)
                        if ev_after and ev_after != ev_before:
                            own_evidence_write = (ev_rel, ev_before, ev_after)
            except (OSError, ValueError) as exc:
                review_record_error = f"review rows not written: {exc}"

    companions: list = []
    cited_sizing: Optional[str] = None
    if not anchor_only:
        cited_sizing = _cited_sizing(worktree_root, request.plan_path)
        if cited_sizing is not None:
            companions.append(cited_sizing)
        if request.plan_path:
            evidence_rel = str(
                PurePosixPath(request.plan_path).with_name(
                    PurePosixPath(request.plan_path).stem + _EVIDENCE_SIDECAR_SUFFIX
                )
            )
            if (worktree_root / evidence_rel).is_file():
                companions.append(evidence_rel)
    plan_seam = (
        str(PurePosixPath(request.plan_path).with_name(PurePosixPath(request.plan_path).stem + _SEAM_SIDECAR_SUFFIX))
        if request.plan_path else None
    )
    ask_note_rel: Optional[str] = None
    ask_baton: Optional[str] = None
    if ask_run_dir is not None:
        ask_baton = _cited_baton(worktree_root, cited_sizing if not anchor_only else None)
        if ask_baton:
            companions.append(ask_baton)
        candidate = f"{DELIVERY_NOTE_DIR}/{ask_run_dir.name}.md"
        if (worktree_root / candidate).is_file():
            ask_note_rel = candidate
            _write_note_verdicts(worktree_root / candidate, params.get("ledger_verdict"), inline_review, worktree_root)
    usage_reply: dict = (
        _write_usage(worktree_root, session_id, ask_run_dir.name)
        if ask_run_dir is not None and not anchor_only else {}
    )
    from coordinator_core.ops.dispatch_emit import run_artifacts

    art_run_id = ask_run_dir.name if ask_run_dir is not None else params.get("run_id")
    artifacts: Optional[run_artifacts.RunArtifacts] = None
    if not anchor_only and isinstance(art_run_id, str) and art_run_id:
        from coordinator_core.ops.dispatch_emit.halted_record_request import within_handoffs_home

        artifacts = within_handoffs_home(worktree_root, run_artifacts.candidate_paths(
            worktree_root, art_run_id, sizing_rel=cited_sizing, plan_rel=request.plan_path,
            doc_paths=params.get(run_artifacts.DOC_PATHS_PARAM) or (),
        ))
    plan_artifacts = artifacts is not None
    if (not anchor_only and request.plan_path
            and _close_kind(_read_rel(worktree_root, request.plan_path))[0] == "mise-run"):
        spine_stem = PurePosixPath(request.plan_path).name.removesuffix(".md").removesuffix(".spine")
        minted = run_artifacts.mise_candidate_paths(
            worktree_root, spine_stem, inventory_rel=(_inventory_record_rel(worktree_root, request.plan_path) or (None,))[0]
        )
        merged = {k: set(v) for k, v in (artifacts.by_class.items() if artifacts is not None else ())}
        for cls, paths in minted.by_class.items():
            merged.setdefault(cls, set()).update(paths)
        artifacts = run_artifacts.RunArtifacts(
            run_id=artifacts.run_id if artifacts is not None else spine_stem,
            by_class={k: tuple(sorted(v)) for k, v in merged.items()},
            record_dirs=artifacts.record_dirs if artifacts is not None else (),
            root=worktree_root,
        )
    foreign_repo = not process_artifact_policy.hosts_coordinator_state(worktree_root)
    proc_wh: dict = {}
    proc_record_dirs: tuple = (*((artifacts.record_dirs) if artifacts is not None else ()), *((record_dir_rel,) if record_dir_rel else ()))
    if artifacts is not None:
        artifacts, proc_wh = process_artifact_policy.withhold_process_artifacts(worktree_root, artifacts)

    def _with_process_withheld(reply: dict) -> dict:
        if proc_wh:
            ra = reply.setdefault("run_artifacts", {"landed": [], "withheld": {}})
            ra["withheld"] = dict(sorted({**ra.get("withheld", {}), **proc_wh}.items()))
            process_artifact_policy.exclude_locally(worktree_root, proc_wh, proc_record_dirs if foreign_repo else ())
        return reply

    review_run_id = _run_id_of(params, ask_run_dir, script_path_raw)
    review_checked = not anchor_only and bool(session_id)
    review_writes: Optional[dict] = None
    if review_checked:
        from coordinator_core.ops.dispatch_emit import review_edits

        read_run_id, read_session = _resumed_review_source(
            worktree_root, inline_review, review_run_id, session_id
        )
        try:
            review_writes = review_edits.review_writes(
                worktree_root, read_session, _review_since(worktree_root, read_run_id, inline_review)
            )
        except Exception:  # noqa: BLE001 -- an unreadable source is unanswerable, never "no edits"
            review_writes = None
        if review_writes is not None:
            review_writes = {review_edits.norm(k): v for k, v in review_writes.items()}
            for recorded in sorted(_review_recorded_edits(inline_review)):
                review_writes.setdefault(recorded, "review-record")
    from coordinator_core.ops.dispatch_emit import run_touched as _run_touched

    recorded_pc = _recorded_post_checkpoint(worktree_root, ask_run_dir, review_run_id, inline_review)
    commit_obliged: tuple = ()
    if isinstance(params.get("ledger_verdict"), dict):
        from coordinator_core.ops.dispatch_emit.post_commit_paths import commit_act_paths

        commit_obliged = commit_act_paths(worktree_root, params["ledger_verdict"])
    obliged_dirs = [p for p in commit_obliged if (worktree_root / p).is_dir()]
    obliged_files = [p for p in commit_obliged if p not in obliged_dirs]

    snapshot = _worktree_snapshot(
        worktree_root,
        [p for c in request.chunks for p in c.paths]
        + sorted(recorded_pc)
        + obliged_files
        + [p for p in (request.plan_path, plan_seam, bookkeeping_record_path) if p]
        + companions
        + ([ask_note_rel] if ask_note_rel else [])
        + (list(artifacts.paths()) if artifacts is not None else [])
        + [p for p in (review_writes or ()) if not review_edits.out_of_worktree(p)]
        + list(manifest_declared)
        + sorted(_run_touched.touched_paths(
            request, inline_review, artifacts.paths() if artifacts is not None else ()
        )),
        [prefix for c in request.chunks for prefix in c.prefixes]
        + obliged_dirs
        + ([record_dir_rel] if record_dir_rel else [])
        + (list(artifacts.record_dirs) + [RECORD_DIR_ROOT] if artifacts is not None else []),
        _scope_pathspecs(request),
    )
    art_landable: Optional[run_artifacts.Landable] = None
    if artifacts is not None and snapshot is not None:
        from coordinator_core.ops.dispatch_emit import abandoned_runs

        live_ids = {
            str(rec["run_id"]) for rec in abandoned_runs._token_records(worktree_root)
            if isinstance(rec.get("run_id"), str)
        }
        if plan_artifacts:
            artifacts = artifacts.with_superseded(
                run_artifacts.superseded_runs(
                    worktree_root, art_run_id, cited_sizing or next(iter(artifacts.by_class.get("sizing", ())), None),
                    snapshot.changed, live_run_ids=live_ids,
                ),
                RECORD_DIR_ROOT,
            )
        art_landable = run_artifacts.landable(
            worktree_root, artifacts, snapshot.changed, snapshot.ignored_among(list(artifacts.paths())),
            session_id=session_id,
        )
        art_classes = _PRODUCT_ARTIFACT_CLASSES | ({"plan"} if product_only else frozenset())
        art_landable = run_artifacts.Landable(
            present=tuple(p for p in art_landable.present if artifacts.class_of(p) in art_classes),
            deleted=tuple(p for p in art_landable.deleted if artifacts.class_of(p) in art_classes),
            withheld=art_landable.withheld,
            contest_unknown=art_landable.contest_unknown,
        )

    known_ids = {chunk.id for chunk in request.chunks}
    unsent_memos = _memo_rows_without_receipt(worktree_root, request)
    report_cache: dict = {}
    regraded_here = _regradable_chunks(
        worktree_root, request, incomplete_chunks, inline_review, report_cache
    )
    if regraded_here:
        incomplete_chunks = incomplete_chunks - set(regraded_here)
        if regraded is not None:
            regraded.update(regraded_here)
    held_now = _held_ids(params)
    more_regraded: dict = {}
    if ledger is not None:
        more_regraded.update(_ledger_met_regrades(
            worktree_root, request, incomplete_chunks, inline_review, report_cache,
            params.get("ledger_verdict"), held_now,
        ))
    credited_regraded = _credit_regrades(
        worktree_root, artifacts, record_dir_rel, request, incomplete_chunks, held_now, ask_run_dir,
        cited_sizing or (next(iter(artifacts.by_class.get("sizing", ())), None) if artifacts is not None else None),
    )
    more_regraded.update({k: v for k, v in credited_regraded.items() if k not in more_regraded})
    if isinstance(close_ctx, dict) and close_ctx.get("kind") == "xs-s-run":
        more_regraded.update(_xs_closeout_regrades(
            worktree_root, request, incomplete_chunks - set(more_regraded) - _held_ids(params),
            snapshot, report_cache,
        ))
    if more_regraded:
        incomplete_chunks = incomplete_chunks - set(more_regraded)
        if regraded is not None:
            regraded.update(more_regraded)
    # The wake digest lists every unfinished row, including rows that never
    # started and so never reached the marker; those carry no paths to hold
    # back, so they are reported, not refused.
    # A held chunk's work is not the run's to land, whatever its report or the review's slice say.
    held_ids = _held_ids(params) | _stop_rule_fired_ids(worktree_root, request)
    incomplete_chunks = incomplete_chunks | (held_ids & known_ids)
    # Trap: a carrier's own digest lists only the carrier's own unfinished rows; only merge_digests
    # unions the other siblings', so unmerged sibling_scope rows are never graded done here.
    unmerged = unmerged_sibling_scope(request, params) & known_ids
    incomplete_chunks = incomplete_chunks | unmerged
    if unmerged and regraded is not None:
        for cid in unmerged:
            regraded.pop(cid, None)
    unmarked_incomplete = sorted(incomplete_chunks - known_ids)
    stranded.update(
        {c.id: list(c.paths) for c in request.chunks if c.id in incomplete_chunks}
    )

    withheld_partial: dict = {}
    partial_chunks = _partial_chunks(worktree_root, request, incomplete_chunks, withheld_partial)
    partial_ids = {c.id for c in partial_chunks}
    entangled = _entangled_chunks(
        worktree_root, request, incomplete_chunks - partial_ids, report_cache, snapshot
    )
    if entangled:
        incomplete_chunks = incomplete_chunks | set(entangled)
        partial_chunks = [c for c in partial_chunks if c.id not in entangled]
        stranded.update({c.id: list(c.paths) for c in request.chunks if c.id in entangled})
    partial_chunks = [c for c in partial_chunks if c.id not in held_ids and c.id not in unmerged]
    done_chunks = [c for c in request.chunks if c.id not in incomplete_chunks]

    # A path a stranded chunk declares never rides the commit by a side route (review residue,
    # run-owned dirt, prefix expansion, run artifacts); a partial chunk that lands is not stranded.
    landing_partial_ids = {c.id for c in partial_chunks}
    barred = {cid: paths for cid, paths in stranded.items() if cid not in landing_partial_ids}
    residue: dict = {}

    def _bar_stranded(paths) -> list:
        hit = credit_gate.stranded_residue(barred, paths)
        if not hit:
            return list(paths)
        for cid, hit_paths in hit.items():
            residue.setdefault(cid, set()).update(hit_paths)
        dropped = {p for hit_paths in hit.values() for p in hit_paths}
        return [p for p in paths if p not in dropped]

    held_paths = _held_write_paths(worktree_root, request, held_ids) if held_ids else {}
    for held_path, held_by in held_paths.items():
        for held_cid in held_by:
            barred[held_cid] = [*barred.get(held_cid, ()), held_path]
    if held_paths:
        overlap: dict = {}
        for chunk in done_chunks + partial_chunks:
            for p in list(chunk.paths) + (_own_prefix_files(worktree_root, chunk, report_cache) or []):
                if p in held_paths:
                    entry = overlap.setdefault(p, {"held": held_paths[p], "landed": []})
                    if chunk.id not in entry["landed"]:
                        entry["landed"].append(chunk.id)
        if overlap:
            shown = "; ".join(
                f"{p} (held {', '.join(e['held'])}; landed {', '.join(e['landed'])})"
                for p, e in sorted(overlap.items())
            )
            return _error(
                f"path(s) declared by both a held chunk and a landed chunk cannot be committed "
                f"consistently: {shown}. Land the landed chunks and release the held chunk(s): "
                f"{_remedy.release_held_call('dispatch.terminal_commit', task_output_raw, sorted({h for e in overlap.values() for h in e['held']}))}"
                "; or list the landed chunk in params.held",
                refused="held-landed-overlap",
                overlap=overlap,
            )

    all_paths: list = []
    prefix_files: list = []

    from coordinator_core.ops.dispatch_emit import run_touched

    touched = run_touched.touched_paths(
        request, inline_review, artifacts.paths() if artifacts is not None else ()
    ) | frozenset(p.replace("\\", "/") for p in manifest_declared if p)

    left_out_map: dict = {}
    for chunk in done_chunks:
        own_prefix_files = _with_expansion(
            _own_prefix_files(worktree_root, chunk, report_cache),
            _expanded_prefix_files(worktree_root, chunk, snapshot, session_id, touched, left_out=left_out_map),
        )
        if own_prefix_files is None:
            return _error(
                f"chunk {chunk.id!r} declares prefixes {list(chunk.prefixes)} but its "
                f"report {chunk.report!r} carries no `{PREFIX_CLAIM_LABEL}` list -- "
                "list it in params.incomplete_chunks to commit the rest"
            )
        own_prefix_files = _bar_stranded(own_prefix_files)
        chunk_paths = list(chunk.paths) + own_prefix_files
        all_paths.extend(chunk_paths)
        prefix_files.extend(own_prefix_files)

    if withheld_partial and blockers is not None:
        blockers.update(withheld_partial)
    for chunk in partial_chunks:
        own_prefix_files = _with_expansion(
            _own_prefix_files(worktree_root, chunk, report_cache),
            _expanded_prefix_files(worktree_root, chunk, snapshot, session_id, touched, left_out=left_out_map),
        ) or []
        own_prefix_files = _bar_stranded(own_prefix_files)
        all_paths.extend(list(chunk.paths) + own_prefix_files)
        prefix_files.extend(own_prefix_files)

    if (
        bookkeeping_record_path is not None and not product_only and bookkeeping_record_path not in all_paths
        and not machine_local_paths([bookkeeping_record_path])
    ):
        all_paths.append(bookkeeping_record_path)

    record_dir_present: set = set()
    if record_dir_rel is not None and snapshot is not None:
        from coordinator_core.ops.dispatch_emit.halted_record_commit import record_dir_changes

        present, _gone = record_dir_changes(worktree_root, record_dir_rel, snapshot.changed)
        record_dir_present = set(present)
        all_paths.extend(p for p in present if p not in all_paths)

    # A sibling-checkout path never reaches the home commit (it would drop as
    # absent-at-HEAD); each sibling repo lands its own commit after the home one.
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import split_sibling_paths

    all_paths, sibling_paths = split_sibling_paths(all_paths, worktree_root)

    # A case-variant of a tracked path would pass the existence check on a case-insensitive
    # filesystem and reach the landing allowlist unclassified; land git's own spelling instead.
    from coordinator_core.git.tracked_case import tracked_spelling

    spelled = tracked_spelling(worktree_root, all_paths) if all_paths else {}
    case_map = {p: s for p, s in spelled.items() if s is not None and s != p}
    if case_map:
        all_paths = list(dict.fromkeys(case_map.get(p, p) for p in all_paths))
        held_paths = {case_map.get(p, p): ids for p, ids in held_paths.items()}
        scope["case_canonicalized"] = dict(case_map)

        def _respelled(chunks):
            return type(chunks)(
                dataclasses.replace(c, paths=tuple(dict.fromkeys(case_map.get(p, p) for p in c.paths)))
                for c in chunks
            )

        done_chunks = _respelled(done_chunks)
        partial_chunks = _respelled(partial_chunks)

    def _with_siblings(reply: dict) -> dict:
        if sibling_paths:
            reply["sibling_commits"] = _commit_siblings(
                sibling_paths, _subject(done_chunks, "review trail"), reply.get("sha"), session_id
            )
        return reply

    art_has_landing = art_landable is not None and bool(art_landable.present or art_landable.deleted)
    if (not all_paths and not art_has_landing and (anchor_only or request.review_only) and record_abs is not None
            and request.plan_path and (worktree_root / request.plan_path).is_file()):
        all_paths.append(request.plan_path)
    if not all_paths and not art_has_landing and recorded_pc:
        if snapshot is None:
            return _snapshot_unavailable_refusal(recorded_pc)
        early_named = _post_checkpoint_named(
            _withhold_reasons(worktree_root, artifacts, record_dir_rel), param_withheld, proc_wh,
            art_landable=art_landable, left_out=left_out_map, held=held_paths,
        )
        early_unclaimed = _unclaimed_post_checkpoint(snapshot, recorded_pc, landing=set(), named=early_named)
        if early_unclaimed:
            return _unclaimed_refusal(recorded_pc, early_unclaimed)
    if not all_paths and not art_has_landing:
        if close_ctx is not None:
            close_ctx.clear()
        return _with_process_withheld(_with_siblings({"committed": False, "nothing_to_commit": not sibling_paths}))

    if not anchor_only and not plan_artifacts:
        for companion in companions:
            if companion not in all_paths:
                all_paths.append(companion)
    seam_own: list = []
    if not (anchor_only or product_only):
        for seam_rel in _seam_sidecars(
            worktree_root, request.plan_path, [c.id for c in done_chunks + partial_chunks]
        ):
            seam_own.append(seam_rel)
            if seam_rel not in all_paths:
                all_paths.append(seam_rel)

    note_rel = ask_note_rel
    if (
        ask_note_rel is not None
        and ask_note_rel not in all_paths
        and not (snapshot is not None and snapshot.ignored_among([ask_note_rel]))
    ):
        all_paths.append(ask_note_rel)
    else:
        ask_note_rel = None

    if held_paths:
        all_paths = [p for p in all_paths if p not in held_paths]
    note_committed = note_rel if note_rel is not None and note_rel in all_paths else None

    # Done and partial chunks' paths only (an undone chunk's are not own-declared), respelled to the tracked case: a refire's withheld scope must survive a spelling variant.
    declared_writes = {case_map.get(p, p) for c in done_chunks + partial_chunks for p in c.paths}
    chunk_declared = set(declared_writes)
    inherited_paths: dict = {}
    if ask_run_dir is not None:
        inherited_paths = {
            p: origin
            for p, origin in inherited_scope(worktree_root, params.get(TERMINAL_RESUME_LEDGER_PARAM)).items()
            if (worktree_root / p).is_file()
        }
        from coordinator_core.ops.dispatch_emit.plan_residue import own_plan_residue

        inherited_paths.update(
            (p, "own-plan-residue")
            for p in own_plan_residue(worktree_root, ask_run_dir, session_id or "")
            if p not in inherited_paths and p not in held_paths and (worktree_root / p).is_file()
        )
        declared_writes |= set(inherited_paths)
        all_paths.extend(p for p in inherited_paths if p not in all_paths and p not in held_paths)
    absent = [p for p in all_paths if not (worktree_root / p).exists()]
    dropped_absent: list = []
    deleted_paths: list = []
    reverted_absent: list = []
    reverted = _reverted_paths(params, inline_review)
    if absent:
        from coordinator_core.git.commit import partition_declared_deletions

        partition = partition_declared_deletions(worktree_root, absent)
        if partition is None:
            return _error(
                "cannot resolve HEAD tree spine to classify absent path(s) "
                f"{absent}"
            )
        tracked_at_head, absent_from_head = partition
        undeclared = [p for p in tracked_at_head if p not in declared_writes]
        if undeclared:
            return _error(
                "path(s) gone from the worktree but still tracked at HEAD are not "
                f"in any DONE chunk's declared writes, an undeclared deletion: {undeclared}"
            )
        reverted_absent = sorted(p for p in tracked_at_head if p in reverted)
        deleted_paths = sorted(p for p in set(tracked_at_head) if p not in reverted)
        dropped_absent = list(absent_from_head)
        removed = set(dropped_absent) | set(deleted_paths) | set(reverted_absent)
        all_paths = [p for p in all_paths if p not in removed]

    if art_landable is not None:
        all_paths.extend(
            p for p in _bar_stranded([q for q in art_landable.present if q not in all_paths])
        )
        deleted_paths = sorted(
            set(deleted_paths)
            | {p for p in _bar_stranded(list(art_landable.deleted)) if p not in reverted}
        )
        all_paths = [p for p in all_paths if p not in deleted_paths]

    if not all_paths and not deleted_paths:
        if close_ctx is not None:
            close_ctx.clear()
        return _with_process_withheld(_with_siblings({"committed": False, "nothing_to_commit": not sibling_paths}))

    run_owned, owned_deletions = _run_owned_dirt(
        worktree_root, request, snapshot, session_id, all_paths, deleted_paths, touched,
        left_out=left_out_map,
    )
    if left_out_map:
        scope["peer_held_left"] = left_out_map
    run_owned = _bar_stranded(run_owned)
    # Trap: a slice file is any file changed since the run base, a concurrent run's included; an
    # own-dir file whose blob drops lines a foreign commit added since the base is that run's dirt.
    run_owned_foreign: list = []
    if run_owned:
        from coordinator_core.ops.dispatch_emit import landing_integrity as _li

        owned_run_id = _run_id_of(params, ask_run_dir, script_path_raw)
        owned_base = _li.run_base_for(worktree_root, owned_run_id, params)
        if owned_base is not None:
            overlapped = {
                f.path for f in _li.since_base_findings(
                    worktree_root, list(run_owned), owned_base,
                    run_id=owned_run_id, plan=request.plan_path,
                )
            }
            run_owned_foreign = sorted(overlapped)
            run_owned = [p for p in run_owned if p not in overlapped]
    owned_deletions = [p for p in _bar_stranded(list(owned_deletions)) if p not in reverted]
    all_paths.extend(run_owned)
    obliged_changed: list = []
    if commit_obliged:
        answered = _changed(snapshot, worktree_root, list(commit_obliged))
        if answered is None:
            return _error(
                f"cannot read the worktree state of post_commit `commit` path(s) {list(commit_obliged)}",
                refused=POST_COMMIT_PATH_UNLANDED,
            )
        obliged_changed = sorted(p for p in answered if (worktree_root / p).is_file())
        all_paths.extend(p for p in obliged_changed if p not in all_paths)
    deleted_paths = sorted(set(deleted_paths) | set(owned_deletions))
    scope["run_owned"] = list(run_owned)
    # A session's own `.git/` scratch (review-targets.txt) reaches the touched set but never a
    # tree: commit_paths refuses the whole commit on it.
    all_paths = [p for p in all_paths if canonical_repo_relative_path_refusal(p) is None]
    deleted_paths = [p for p in deleted_paths if canonical_repo_relative_path_refusal(p) is None]

    if foreign_repo:
        for p in (bookkeeping_record_path, note_rel, request.plan_path, *record_dir_present, *companions, *seam_own):
            if p and p not in declared_writes:
                proc_wh.setdefault(p, process_artifact_policy.WITHHELD_REASON)
    if proc_wh:
        all_paths = [p for p in all_paths if p not in proc_wh]
        deleted_paths = [p for p in deleted_paths if p not in proc_wh]
        run_owned = [p for p in run_owned if p not in proc_wh]
        scope["run_owned"] = list(run_owned)

    from coordinator_core.ops.dispatch_emit.shipped_claims import SELF_APPLIED_WITHHOLDS

    wh_reasons = {
        p: r for p, r in {
            **_withhold_reasons(worktree_root, artifacts, record_dir_rel),
            **param_withheld,
            **_remedy.review_carried_withholds(request),
        }.items() if r not in SELF_APPLIED_WITHHOLDS
    }
    wh_map: dict = {}
    wh_unmatched: list = []
    wh_chunks: dict = {}
    if wh_reasons:
        carried_now = set(all_paths) | set(deleted_paths)
        wh_map = {p: f"withhold_paths: {r}" for p, r in sorted(wh_reasons.items()) if p in carried_now}
        wh_unmatched = sorted(p for p in wh_reasons if p not in carried_now)
        for c in done_chunks:
            own = set(c.paths) | set(_own_prefix_files(worktree_root, c, report_cache) or [])
            if own and own <= set(wh_reasons):
                wh_chunks[c.id] = [{"path": p, "reason": wh_reasons[p]} for p in sorted(own)]
        if wh_chunks:
            incomplete_chunks = incomplete_chunks | set(wh_chunks)
            done_chunks = [c for c in done_chunks if c.id not in wh_chunks]
            stranded.update({c.id: list(c.paths) for c in request.chunks if c.id in wh_chunks})
            if regraded is not None:
                kept = credit_gate.without_withheld(regraded, wh_chunks)
                regraded.clear()
                regraded.update(kept)
            if withheld is not None:
                withheld.update({
                    cid: "withheld: " + "; ".join(e["reason"] for e in entries)
                    for cid, entries in wh_chunks.items()
                })
        if wh_map:
            all_paths = [p for p in all_paths if p not in wh_map]
            deleted_paths = [p for p in deleted_paths if p not in wh_map]
            run_owned = [p for p in run_owned if p not in wh_map]
            scope["run_owned"] = list(run_owned)
            if art_landable is not None:
                art_landable = run_artifacts.Landable(
                    present=tuple(p for p in art_landable.present if p not in wh_map),
                    deleted=tuple(p for p in art_landable.deleted if p not in wh_map),
                    withheld=art_landable.withheld,
                    contest_unknown=art_landable.contest_unknown,
                )

    def _with_withheld(reply: dict) -> dict:
        _with_process_withheld(reply)
        if wh_chunks:
            reply["withheld_chunks"] = wh_chunks
        if wh_unmatched:
            reply["withhold_unmatched"] = wh_unmatched
        return reply

    if (wh_map or proc_wh) and not all_paths and not deleted_paths:
        scope.update(root=worktree_root, request=request, snapshot=snapshot, session_id=session_id)
        if close_ctx is not None:
            close_ctx.clear()
        return _with_withheld(_with_siblings({
            "committed": False, "nothing_to_commit": not sibling_paths,
            "run_artifacts": {"landed": [], "withheld": wh_map},
        }))

    scope_request = request
    if product_only and ask_note_rel is not None:
        scope_request = CommitRequest(
            chunks=request.chunks + (ChunkCommit(id="run-note", title="run note", paths=(ask_note_rel,)),),
            deliverable_id=request.deliverable_id, plan_path=request.plan_path,
            session_id=request.session_id,
        )
    scope.update(root=worktree_root, request=scope_request, snapshot=snapshot, session_id=session_id)
    scope["stranded_residue"] = residue
    review_land: tuple = ()
    review_unanswerable: Optional[str] = None
    from coordinator_core.ops.dispatch_emit.review_edits import norm as review_edits_norm

    sibling_claim_run_id = _run_id_of(params, ask_run_dir, script_path_raw)
    sibling_claims_seen: set = set()
    sibling_claims: dict = {}

    def claims_over(paths: Sequence[str]) -> dict:
        """Unlanded-sibling claims over `paths`, read once per path (an earlier answer is kept)."""
        from coordinator_core.ops.dispatch_emit import sibling_claims as sc

        fresh = {p for p in paths if p and p not in sibling_claims_seen}
        if fresh:
            sibling_claims_seen.update(fresh)
            sibling_claims.update(sc.sibling_run_claims(
                worktree_root, sibling_claim_run_id, fresh, own_plan=request.plan_path,
            ))
        return sibling_claims

    claims_over([
        *all_paths, *deleted_paths,
        *(p for p in (review_writes or ()) if not p.startswith(("/", "../")) and p != ".." and p[1:2] != ":"),
        *(p for ps in residue.values() for p in ps),
    ])
    # A barred path another unlanded run claims or a live peer holds is that run's dirt, not this
    # run's stranded residue: it stays barred but is not reported under this run's chunk.
    # Trap: no test pins this filter or the matching `foreign=` on run_artifacts.residue.
    for cid in list(residue):
        residue[cid] = {p for p in residue[cid] if p not in sibling_claims and p not in left_out_map}
        if not residue[cid]:
            del residue[cid]
    if review_checked:
        checkpointed = _plan_checkpoint_touched(
            worktree_root, request.plan_path,
            [p for p in (review_writes or ()) if not review_edits.out_of_worktree(p)],
        )
        verdict = _review_edit_verdict(
            worktree_root, review_writes, snapshot, session_id, review_run_id,
            set(all_paths) | set(deleted_paths), sibling_claims,
            own_files=frozenset(
                {review_edits_norm(p) for c in request.chunks for p in c.paths}
                | {review_edits_norm(p) for p in prefix_files}
                | {review_edits_norm(p) for p in checkpointed}
                | {review_edits_norm(p) for p in _review_recorded_edits(inline_review)}
                | {
                    review_edits_norm(p)
                    for p in ((inline_review.get("prep") or {}).get("slice_files") or ())
                    if isinstance(p, str)
                }
                | {review_edits_norm(p) for p in (artifacts.paths() if artifacts is not None else ())}
                | {
                    review_edits_norm(p)
                    for p in (_leg_footprint_writes(worktree_root, ask_run_dir) or () if ask_run_dir is not None else ())
                }
                | _sole_run_review_writes(worktree_root, read_session, read_run_id, inline_review)
            ),
        )
        if verdict.halt:
            return _error(
                "review-stage edit(s) outside this run's commit cannot land: "
                + ", ".join(f"{p} ({why})" for p, why in verdict.halt.items())
                + ". Commit or release them, then re-fire",
                refused="review-edit-unlanded",
                review_edits_unlanded=dict(verdict.halt),
            )
        review_land = tuple(_bar_stranded(list(verdict.land)))
        review_unanswerable = verdict.unanswerable
        all_paths.extend(p for p in review_land if p not in all_paths)
    from coordinator_core.ops.ceremony.commit_gates import is_never_commit

    never_commit_dropped = sorted(
        {p for p in [*all_paths, *deleted_paths] if is_never_commit(p)}
    )
    if never_commit_dropped:
        dropped_never = set(never_commit_dropped)
        all_paths = [p for p in all_paths if p not in dropped_never]
        deleted_paths = [p for p in deleted_paths if p not in dropped_never]
        review_land = tuple(p for p in review_land if p not in dropped_never)
        if not all_paths and not deleted_paths:
            if close_ctx is not None:
                close_ctx.clear()
            return _with_withheld(_with_siblings({
                "committed": False, "nothing_to_commit": not sibling_paths,
                "never_commit_dropped": never_commit_dropped,
            }))
    # Unlike a hand commit, a run's product commit never absorbs a live peer's in-flight edit.
    from coordinator_core.ops.ceremony import commit_v2 as commit_v2_mod

    changed = _changed(snapshot, worktree_root, all_paths)
    if token_state.get("since_halt_close") is not None and changed is not None and not deleted_paths:
        run_artifact_paths = {
            *record_dir_present, *(artifacts.paths() if artifacts is not None else ()),
            *([bookkeeping_record_path] if bookkeeping_record_path else ()),
        }
        if not any(p in changed and p not in run_artifact_paths for p in all_paths):
            if close_ctx is not None:
                close_ctx.clear()
            return _with_process_withheld(_with_siblings({"committed": False, "nothing_to_commit": not sibling_paths}))
    # Only a path this commit actually carries can absorb a peer's edit: a clean declared path a
    # peer holds is not contention, and refusing on it wedges every review-only close behind it.
    carried = [p for p in all_paths if changed is None or p in changed] + list(deleted_paths)
    own_seniority = _own_run_seniority(worktree_root, params.get("run_id"), script_path_raw)
    peer_holds: list = []
    commit_v2_mod._peer_claim_warnings(worktree_root, carried, peer_holds, own_sid=session_id)
    from coordinator_core.ops import warp_run_deliverable

    try:
        run_claims = warp_run_deliverable.live_run_write_claims(
            worktree_root, carried, exclude_session=session_id or "",
            cwd=str(worktree_root),
            minted_before=own_seniority,
            exclude_run=params.get("run_id") if isinstance(params.get("run_id"), str) else None,
        )
    except Exception:
        run_claims = None
    seen = set(peer_holds)
    for path, sids in sorted((run_claims or {}).items()):
        for sid in sids:
            if (path, sid) not in seen:
                peer_holds.append((path, sid))
                seen.add((path, sid))
    if peer_holds:
        from coordinator_core.ops.dispatch_emit import peer_claim_contract

        message, fields = peer_claim_contract.refusal(
            peer_holds, _run_id_of(params, ask_run_dir, script_path_raw)
        )
        return _error(message, refused="peer-claim", **fields)
    final_paths_set = set(all_paths)

    def contributes(c) -> bool:
        def in_commit(p: str) -> bool:
            return p in final_paths_set and (changed is None or p in changed)

        return any(in_commit(p) or p in deleted_paths for p in list(c.paths)) or any(
            in_commit(p) for p in _own_prefix_files(worktree_root, c, report_cache) or []
        )

    contributing_chunks = [c for c in done_chunks if contributes(c)]
    contributing_partial = [c for c in partial_chunks if contributes(c)]
    for c in contributing_partial:
        stranded.pop(c.id, None)

    absent_dropped = set(dropped_absent)
    landed_ids = set(params.get("landed_chunks") or []) | {
        c.id for c in done_chunks
        if c not in contributing_chunks and not (c.paths and set(c.paths) <= absent_dropped)
    }
    landed_ids |= _own_evidence_append_chunks(
        worktree_root, request, set(stranded) - set(wh_chunks), final_paths_set, changed
    )
    if credit_gate.no_credited_landing(
        set(stranded) - set(wh_chunks), {c.id for c in contributing_chunks + contributing_partial}, landed_ids
    ):
        if close_ctx is not None:
            close_ctx.clear()
        shown = "; ".join(f"{cid}: {', '.join(paths)}" for cid, paths in sorted(stranded.items()))
        why = "".join(f" {cid} blocker: {text}." for cid, text in sorted(withheld_partial.items()) if cid in stranded)
        return _error(
            f"no product chunk lands in this commit; stranded work stays uncommitted: {shown}.{why} "
            "Credit one: workflow.revise "
            f"run_id={_run_id_of(params, ask_run_dir, script_path_raw)} "
            "credit_chunks=[{chunk, evidence}], then re-fire; or finish or land the stranded chunks, "
            "then re-fire",
            refused=credit_gate.REFUSED_NO_CREDITED_LANDING,
        )

    credited: dict = {}
    for c in done_chunks + contributing_partial:
        declared = set(c.paths) | set(_own_prefix_files(worktree_root, c, report_cache) or [])
        credited[c.id] = sorted(
            declared - absent_dropped - set(wh_reasons) - set(never_commit_dropped) - set(reverted_absent)
        )
    sibling_set = {p for ps in sibling_paths.values() for p in ps}
    landing_now = set(all_paths) | set(deleted_paths) | sibling_set
    unprobed = sorted({p for ps in credited.values() for p in ps} - landing_now)
    credit_changed = changed
    if changed is not None and unprobed:
        probed = _changed(snapshot, worktree_root, unprobed)
        credit_changed = None if probed is None else set(changed) | set(probed)
    uncommitted = credit_gate.uncommitted_credited(
        credited, set(all_paths), set(deleted_paths), sibling_set,
        None if credit_changed is None else set(credit_changed),
    )
    if uncommitted:
        if close_ctx is not None:
            close_ctx.clear()
        shown = "; ".join(f"{cid}: {', '.join(paths)}" for cid, paths in sorted(uncommitted.items()))
        return _error(
            f"credited chunk(s) have declared writes outside this commit: {shown}. "
            "Land them or list the chunk in params.incomplete_chunks, then re-fire",
            refused=credit_gate.REFUSED_CREDITED_UNCOMMITTED,
            uncommitted=uncommitted,
        )

    from datetime import datetime, timezone

    from coordinator_core.git.git_state import head_sha
    from coordinator_core.ops.dispatch_emit.completion_receipt import (
        _remove,
        build_run_receipts,
        write_run_receipts,
    )

    receipt_paths: list = []
    receipts_built: list = []
    try:
        receipts_built = [] if anchor_only else build_run_receipts(
            worktree_root,
            request,
            done_ids=[c.id for c in done_chunks],
            incomplete_ids=sorted(incomplete_chunks),
            record=record,
            script_path=script_path_raw,
            session_id=session_id,
            base_sha=head_sha(worktree_root),
            branch=observed_branch,
            now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            local=product_only,
        )
        receipt_paths = write_run_receipts(worktree_root, receipts_built) if receipts_built else []
    except Exception as exc:  # noqa: BLE001 -- write_run_receipts already removed its partial writes
        return _error(f"completion receipt write failed: {exc}", refused="receipt-write-failed")
    if not product_only:
        all_paths.extend(receipt_paths)

    subject = (
        f"review anchor: {request.plan_path} -- delivery PASS, no product files"
        if anchor_only
        else _subject(
            contributing_chunks or contributing_partial,
            f"review trail: {inline_review['integration_stem']}",
        )
    )
    if not subject.strip(" :"):
        _remove(worktree_root, receipt_paths)
        return _error(
            f"terminal commit subject {subject!r} is empty; chunk ids and the "
            "integration stem are both blank",
            refused="empty-subject",
        )
    paragraphs = [subject]
    caller_message = params.get("message")
    if isinstance(caller_message, str) and caller_message.strip():
        paragraphs.append(caller_message.strip())
    if deleted_paths:
        # The undeclared-staged-deletion guard reads the message for a removal verb.
        # Its own paragraph: a non-trailer line inside the trailer block makes git
        # stop parsing the block, and review_stamp keys on the Inline-Review trailer.
        paragraphs.append("Removes declared write(s): " + ", ".join(deleted_paths))
    trailers = []
    if request.deliverable_id:
        trailers.append(f"Deliverable-Id: {request.deliverable_id}")
    extra_plan_text = _read_rel(worktree_root, request.plan_path) if request.plan_path else None
    if extra_plan_text is not None:
        trailers.extend(
            f"Deliverable-Id: {did}"
            for did in plan_trailer_deliverable_ids(extra_plan_text)
            if did != request.deliverable_id
        )
    resolves = None if anchor_only or incomplete_chunks else _resolving_sizing(worktree_root, request.plan_path)
    if resolves is not None:
        trailers.append(f"Resolves-Sizing: {resolves['id']} {resolves['tshirt']}")
    trailers.extend(f"Review-Residue: {p}" for p in review_land)
    trailers.extend(
        "Requirement-Open: " + " ".join((str(row["id"]) + " " + str(row["reason"])).split())
        for row in open_rows
    )
    trailers.extend(
        f"Verified-Only: {rid} -- {' '.join(ev['command'].split())} -> {' '.join(ev['observed'].split())}"
        for rid, ev in sorted(verified_only_rows(params.get(TERMINAL_RESUME_LEDGER_PARAM)).items())
    )
    trailers.append(
        f"Inline-Review: applies {inline_review['integration_stem']} -- execute-review: "
        f"{inline_review['slices']} slices, {inline_review.get('fixes')} fixes"
    )
    if floor_note is not None:
        trailers.append(review_floor.floor_trailer(floor_note))
    if ledger is not None:
        trailers.append(f"Ledger-Receipt: {review_floor.receipt(review_floor.verdict_count(inline_review, worktree_root))}")
    from coordinator_core.ops.dispatch_emit import landing_integrity as _li_close

    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_trailer_lines, script_run_base

    trailers.extend(run_closed_trailer_lines((
        *_li_close.closing_bases(
            worktree_root, _run_id_of(params, ask_run_dir, script_path_raw),
            {**params, "review_run_base": request.review_run_base},
        ),
        script_run_base(script_text),
    )))
    if product_only:
        trailers.extend(_ask_trailers(worktree_root, request.plan_path, record_abs, receipts_built))
    elif record_abs is not None:
        import hashlib

        try:
            trailers.append(f"Review: {hashlib.sha256(record_abs.read_bytes()).hexdigest()}")
        except OSError:
            pass
    review_trailed = any(t.startswith("Review: ") for t in trailers)
    paragraphs.append("\n".join(trailers))
    message = "\n\n".join(paragraphs)

    sibling_run_id = _run_id_of(params, ask_run_dir, script_path_raw)

    halted_held: dict = {}
    claims_memo: dict = {}

    def _claimed_or_held(candidates):
        """Sibling-claimed candidates plus any a held chunk declares: commit_v2's left-behind carry never takes them."""
        memo = claims_memo.pop("scan", None)
        wanted = set(candidates)
        if memo is not None and wanted <= memo[0]:
            claimed = memo[1] & wanted
        else:
            claimed = _live_run_claims(worktree_root, candidates, sibling_run_id, halted_held)
        if claimed is None or not held_paths:
            return claimed
        return {*claimed, *(p for p in candidates if p in held_paths)}

    def commit_v2(params: dict, root: Path):
        from coordinator_core.ops.ceremony import commit_companion

        with commit_companion.excluding_sibling_claims(_claimed_or_held):
            return reentrant_dispatch("ceremony.commit_v2", params, repo_root=root)

    allowlist_path = _regenerate_landing_allowlist(worktree_root, all_paths)
    if allowlist_path is not None and allowlist_path not in all_paths:
        all_paths.append(allowlist_path)

    # Machine-local share paths (the bookkeeping record, review sidecars) are review references,
    # never part of the commit's pathspec: the commit guard refuses them.
    from coordinator_core.git.machine_local_paths import machine_local_paths as _machine_local

    _local = set(_machine_local([*all_paths, *deleted_paths]))
    record_dropped = bookkeeping_record_path in _local
    if _local:
        all_paths = [p for p in all_paths if p not in _local]
        deleted_paths = [p for p in deleted_paths if p not in _local]
    # A record-only commit still anchors the review stamp: it lands as a marker on the plan path.
    marker_anchor = record_dropped and (bool(request.review_only) or not request.chunks)
    if record_dropped and not all_paths and not deleted_paths and request.plan_path:
        all_paths = [request.plan_path]
        marker_anchor = True
    # With the record dropped, a review that applied fixes still lands its trailer when the
    # product paths match HEAD.
    if record_dropped and request.plan_path and inline_review.get("fixes"):
        marker_anchor = True

    # One choke point for every admission arm: a path an unlanded sibling run claims and this run
    # does not declare never reaches the integrity gate or the commit.
    from coordinator_core.ops.dispatch_emit import sibling_claims as _sc

    own_declared = {
        *(declared_writes - _live_claimed_inherited(
            worktree_root, inherited_paths, claims_over(list(inherited_paths)),
            {p for c in request.chunks for p in c.paths},
        )),
        *run_owned, *receipt_paths,
        *(p for p in (ask_note_rel, bookkeeping_record_path, cited_sizing, allowlist_path) if p),
    }
    if record_dir_rel:
        own_declared.update(
            p for p in (*all_paths, *deleted_paths) if p == record_dir_rel or p.startswith(record_dir_rel.rstrip("/") + "/")
        )
    from coordinator_core.ops.dispatch_emit.in_run_widen import widened_paths

    widened = list(widened_paths(ask_run_dir)) if ask_run_dir is not None else []
    own_declared.update(widened)
    all_paths, sibling_withheld = _sc.filter_own(
        all_paths, claims_over([*all_paths, *deleted_paths]), own_declared,
    )
    deleted_paths, withheld_deleted = _sc.filter_own(deleted_paths, sibling_claims, own_declared)
    sibling_withheld.update(withheld_deleted)
    foreign_held = foreign_halted_withheld(
        worktree_root, sibling_run_id, request.plan_path,
        [p for p in (*all_paths, *deleted_paths) if p not in chunk_declared],
    )
    if foreign_held:
        all_paths = [p for p in all_paths if p not in foreign_held]
        deleted_paths = [p for p in deleted_paths if p not in foreign_held]
        sibling_withheld.update(foreign_held)
    if sibling_withheld:
        review_land = tuple(p for p in review_land if p not in sibling_withheld)
        if not all_paths and not deleted_paths:
            _remove(worktree_root, receipt_paths)
            if close_ctx is not None:
                close_ctx.clear()
            return _with_withheld(_with_siblings({
                "committed": False, "nothing_to_commit": not sibling_paths,
                "sibling_withheld": dict(sibling_withheld),
            }))

    # Allow-list over every admission arm: only the run's declared writes, the files its reports
    # claim under declared prefixes, and the artifacts the op itself adds reach the integrity
    # gate. Anything else stays dirty and is reported, never refused.
    from coordinator_core.ops.dispatch_emit import landing_scope as _ls

    own_artifacts = {
        *(artifacts.paths() if artifacts is not None else ()),
        *(art_landable.present if art_landable is not None else ()),
        *(art_landable.deleted if art_landable is not None else ()),
        *receipt_paths, *companions, *seam_own, *review_land, *run_owned,
        *(p for p in (note_rel, ask_note_rel, bookkeeping_record_path, cited_sizing,
                      allowlist_path, request.plan_path) if p),
    }
    own_prefixes = [
        *(prefix for c in done_chunks + partial_chunks for prefix in c.prefixes),
        *((record_dir_rel,) if record_dir_rel else ()),
        *(artifacts.record_dirs if artifacts is not None else ()),
    ]
    all_paths, foreign_now = _ls.own_landing_set(
        all_paths, declared=declared_writes, declared_prefixes=own_prefixes,
        own_artifacts=own_artifacts,
    )
    deleted_paths, foreign_deleted = _ls.own_landing_set(
        deleted_paths, declared=declared_writes, declared_prefixes=own_prefixes,
        own_artifacts=own_artifacts,
    )
    foreign_excluded = sorted({*foreign_now, *foreign_deleted, *run_owned_foreign})
    if foreign_excluded:
        review_land = tuple(p for p in review_land if p not in foreign_excluded)
        if not all_paths and not deleted_paths:
            _remove(worktree_root, receipt_paths)
            if close_ctx is not None:
                close_ctx.clear()
            return _with_withheld(_with_siblings({
                "committed": False, "nothing_to_commit": not sibling_paths,
                _ls.FOREIGN_EXCLUDED_KEY: foreign_excluded,
            }))

    merge_plan = _merge_landing_plan(
        worktree_root, all_paths, deleted_paths,
        product={*declared_writes, *run_owned},
    )
    merge_artifacts: dict = {}
    if merge_plan is not None:
        if merge_plan.get("refusal"):
            _remove(worktree_root, receipt_paths)
            return _error(merge_plan["refusal"], **merge_plan["extra"])
        all_paths = merge_plan["paths"]
        deleted_paths = merge_plan["deleted"]
        merge_artifacts = merge_plan["artifacts"]

    # The product commit never carries the session's claimed leftovers: commit_v2 would widen the
    # pathspec past the declared set. They land in the follow-on commit after this one.
    commit_params: dict = {
        "paths": all_paths, "message": message,
        "carry_own_left_behind": False,
        "carry_stopped_runs": False,
        "full_reply": True,
    }
    if merge_plan is not None:
        commit_params["finish_merge"] = True
    if deleted_paths:
        commit_params["deleted_paths"] = deleted_paths
    # A DONE chunk's own declared write that is now absent is a planned deletion; when it removes a
    # file added since the rollback window opened, commit_v2's staged-rollback check reads it as
    # restoring the older absence. Caller-named restores count only inside the run's own staged set.
    asked_reverts = params.get("declared_reverts") or []
    own_staged = set(all_paths) | set(deleted_paths)
    declared_reverts_ignored = sorted({p for p in asked_reverts if p not in own_staged})
    own_reverts = sorted({p for p in asked_reverts if p in own_staged} | set(deleted_paths))
    if own_reverts:
        commit_params["declared_reverts"] = own_reverts
    if params.get("allow_empty") is True or marker_anchor or ((anchor_only or request.review_only) and review_trailed):
        commit_params["allow_empty"] = True  # marker_anchor: the review anchor must land tree-equal
    stage_before: Optional[str] = None
    stage_stamped = False
    if not anchor_only and isinstance(art_run_id, str) and art_run_id:
        from coordinator_core.ops.dispatch_emit import ask_contract as _ac_stage

        # Written before the candidate set is hashed and staged so run-stage.json rides this commit.
        try:
            stage_dir = _ac_stage.record_dir(worktree_root, art_run_id)
            stage_dir_rel = stage_dir.relative_to(worktree_root).as_posix() + "/"
            # No record dir: a stamp would fabricate a one-file record. No path outside it lands:
            # the run landed nothing, so "landed" would be false (and a stamp alone is no change).
            should_stamp = stage_dir.is_dir() and any(
                not p.startswith(stage_dir_rel)
                and (snapshot is None or not snapshot.covers(p) or p in snapshot.changed)
                for p in (*all_paths, *deleted_paths)
            )
            if should_stamp:
                stage_before = (_ac_stage.read_run_stage(worktree_root, art_run_id) or {}).get("stage")
                _ac_stage.write_run_stage(worktree_root, art_run_id, "landed")
                stage_stamped = True
                stage_rel = (
                    _ac_stage.record_dir(worktree_root, art_run_id) / _ac_stage.RUN_STAGE_FILE
                ).relative_to(worktree_root).as_posix()
                if stage_rel not in all_paths and not stage_rel.startswith(RUN_DIR_ROOT.rstrip("/") + "/"):
                    all_paths.append(stage_rel)
        except (ValueError, OSError):
            stage_before = None

    def _unstamp_landed() -> None:
        """Undo the pre-commit `landed` stamp on a path that does not land: restore the prior stage,
        or drop the file when the run had none."""
        if not stage_stamped:
            return
        from coordinator_core.ops.dispatch_emit import ask_contract as _ac_unstamp

        try:
            if stage_before in _ac_unstamp.RUN_STAGES:
                _ac_unstamp.write_run_stage(worktree_root, art_run_id, stage_before)
            else:
                (_ac_unstamp.record_dir(worktree_root, art_run_id) / _ac_unstamp.RUN_STAGE_FILE).unlink(
                    missing_ok=True
                )
        except (ValueError, OSError):
            pass

    reread_artifacts: list[str] = []
    if session_id is not None:
        commit_params["session_id"] = session_id
        fixes_at, wave_paths = _fixes_integrated_at(worktree_root, inline_review)
        if fixes_at is None and request.review_only:
            fixes_at, wave_paths = _remedy.review_only_anchor(worktree_root, request, inline_review)
        expected = _recorded_fingerprints(
            worktree_root, session_id,
            [p for c in done_chunks for p in c.paths] + list(run_owned),
            all_paths, deleted_paths,
            excluded={*receipt_paths, allowlist_path, bookkeeping_record_path, note_committed},
            reviewed=reviewed_bytes, reviewed_at=reviewed_at,
            fixes_integrated_at=fixes_at, wave_paths=wave_paths,
            review_events=_review_write_events(worktree_root, review_writes),
            run_started_at=_run_started_at(worktree_root, art_run_id),
        )
        if note_committed and note_committed not in deleted_paths:
            from coordinator_core.session import touch_record

            note_hash = touch_record.compute_content_hash(worktree_root / note_committed)
            if note_hash:
                expected[note_committed] = note_hash
        if own_evidence_write is not None:
            # (path, bytes before this call's append, bytes after): the expectation moves past
            # the call's own append only when it sat at the pre-append bytes; any other value
            # is a peer's and keeps refusing.
            ev_path, ev_before, ev_after = own_evidence_write
            if ev_path in all_paths and ev_path not in deleted_paths and expected.get(ev_path) == ev_before:
                expected[ev_path] = ev_after
        if expected:
            from coordinator_core.ops.dispatch_emit import run_artifact_ownership

            owned = run_artifact_ownership.own_artifact_paths(worktree_root, art_run_id)
            reread_artifacts = owned.drop_expectations(expected, keep={note_committed})
        if expected:
            commit_params["expected_content_sha256"] = expected
    if snapshot is not None:
        # Receipts and the regenerated allowlist are new tracked-by-design files the
        # snapshot predates; every other path must be covered or commit_v2 asks git itself.
        fresh = {*receipt_paths, allowlist_path}
        unread = [p for p in all_paths if p not in fresh and not snapshot.covers(p)]
        if not unread:
            commit_params["ignored_untracked"] = snapshot.ignored_among(
                [p for p in all_paths if p not in fresh]
            )

    _carried = set(all_paths) | set(deleted_paths)
    residue_py = sorted(
        p for p in snapshot.changed
        if p.endswith(".py") and p not in _carried and (worktree_root / p).is_file()
    ) if snapshot is not None else []
    kept_supplied = _kept_blob_supply(
        worktree_root, art_run_id, _run_id_of(params, ask_run_dir, script_path_raw),
        [p for p in all_paths if p in carried], sorted(declared_writes), request.plan_path, own_seniority,
    )
    if kept_supplied:
        # The run's own blob lands, not the worktree bytes a peer also edited: the gate's co-declared
        # arm skips these paths. The run's own-write sha256 is checked against the blob's bytes here
        # (the worktree carries the peer's hunk, and commit_paths refuses a supplied path's worktree
        # sha as unverifiable); a stale blob refuses.
        stale = _stale_kept_blobs(repo_root, kept_supplied, commit_params.get("expected_content_sha256") or {})
        if stale:
            _unstamp_landed()
            _remove(worktree_root, receipt_paths)
            return _error(
                f"landing refused kept-blob-stale: {stale[0][0]} (kept blob {stale[0][1][:12]} is not the "
                "run's latest own write); re-run the path so kept.json records the current blob",
                refused=KEPT_BLOB_STALE, stale_paths=[p for p, _ in stale],
            )
        commit_params["supplied_blobs"] = kept_supplied
        if "expected_content_sha256" in commit_params:
            commit_params["expected_content_sha256"] = {
                p: h for p, h in commit_params["expected_content_sha256"].items() if p not in kept_supplied
            }
    integrity_extra = _landing_integrity_gate(
        worktree_root, all_paths, deleted_paths, record_abs, params,
        _run_id_of(params, ask_run_dir, script_path_raw), request.plan_path,
        residue_py=residue_py, residue_known=snapshot is not None,
        merge_open=merge_plan is not None,
        own_declared=sorted(declared_writes), session_id=session_id,
        carried_paths=[p for p in carried if p not in kept_supplied], minted_before=own_seniority,
    )
    if integrity_extra.get("refused"):
        _unstamp_landed()
        _remove(worktree_root, receipt_paths)
        findings = integrity_extra["integrity"]
        first = findings[0]
        remedy = ""
        if first["reason"] == "since-base-overlap":
            from coordinator_core.ops import warp_run_deliverable

            remedy = ". " + _sc.reland_remedy(
                warp_run_deliverable.record_sizing_rel(
                    worktree_root, _run_id_of(params, ask_run_dir, script_path_raw)
                )
            )
        integrity_extra.pop("supplied_blobs", None)
        if sibling_withheld:
            integrity_extra["sibling_withheld"] = dict(sibling_withheld)
        return _error(
            f"landing refused {first['reason']}: {first['path']} ({first['detail']}){remedy}",
            **integrity_extra,
        )

    if integrity_extra.get("supplied_blobs"):
        commit_params["supplied_blobs"] = integrity_extra.pop("supplied_blobs")

    landed_now = set(all_paths)
    unlanded = [p for p in obliged_changed if p not in landed_now]
    if unlanded:
        _unstamp_landed()
        _remove(worktree_root, receipt_paths)
        named = sorted({o for o in commit_obliged if any(u == o or u.startswith(o + "/") for u in unlanded)})
        return _error(
            f"a post_commit `commit` row names {named} but {len(unlanded)} file(s) under it did not "
            f"land (withheld, peer-held or ignored): {unlanded[:10]}. Land them or revise the row",
            refused=POST_COMMIT_PATH_UNLANDED,
            unlanded=unlanded,
        )

    from coordinator_core.ops.dispatch_emit import landing_secrets as _secrets

    secret_findings = _secrets.scan(worktree_root, all_paths)
    if secret_findings:
        _unstamp_landed()
        _remove(worktree_root, receipt_paths)
        return _error(
            _secrets.refusal(secret_findings),
            refused=_secrets.SECRET_IN_DIFF,
            secret_findings=[f._asdict() for f in secret_findings[:10]],
        )

    reach_reply_field: dict = {}
    shipped_reply_field: dict = {}
    ask_note_text: Optional[str] = None
    if ask_note_rel is not None:
        reach_withheld = {
            **(dict(art_landable.withheld) if art_landable is not None else {}),
            **{p: str(why) for p, why in sibling_withheld.items()},
            **{p: "peer-held" for p in held_paths},
            **{p: str(why) for p, why in proc_wh.items()},
            **{p: str(wh_reasons[p]) for p in wh_map},
        }
        ask_note_text = _read_rel(worktree_root, ask_note_rel)
        shipped_withheld = _drop_inherited_withheld(
            worktree_root, reach_withheld, inherited_paths, sibling_run_id, request.plan_path,
        )
        reach_refused, reach_reply_field = reach_gate(
            worktree_root, ask_note_text, landing_paths=all_paths,
            deleted_paths=deleted_paths, withheld_paths=shipped_withheld,
        )
        if reach_refused is not None:
            _unstamp_landed()
            _remove(worktree_root, receipt_paths)
            return _error(reach_refused, refused=REACH_CLAIM_REFUSED, reach_claims=reach_reply_field)
        pre_held: dict = {}
        pre_claims = _live_run_claims(worktree_root, all_paths, sibling_run_id, pre_held)
        if pre_claims is not None:
            claims_memo["scan"] = (set(all_paths), pre_claims)
            halted_held.update(pre_held)
            declared_held = _dirty_held_by_halted_run(worktree_root, pre_held)
            cohold_refused = cohold_refusal.refusal(declared_held, sibling_run_id, worktree_root)
            if cohold_refused is not None:
                claims_memo.clear()
                _unstamp_landed()
                _remove(worktree_root, receipt_paths)
                return _error(cohold_refused, refused=cohold_refusal.REFUSED_COHOLD)
        stranded_rows = {
            p: (c.id, f"executor PARTIAL: {withheld_partial[c.id]}" if c.id in withheld_partial else "incomplete")
            for c in request.chunks if c.id in incomplete_chunks for p in c.paths
        }

        def _claim_owners(paths: Sequence[str]) -> dict:
            from coordinator_core.ops.dispatch_emit import halted_withheld_index

            halted: dict = {}
            live = _live_run_claims(worktree_root, paths, sibling_run_id, halted)
            if live is None:
                return {}
            found = {p: ", ".join(halted[p]) if p in halted else "a live peer run" for p in live}
            wanted = set(paths) - set(found)
            for p, rid_ in halted_withheld_index.withheld_by_path(worktree_root).items():
                if p in wanted:
                    found[p] = rid_
            return {p: o for p, o in found.items() if o != sibling_run_id}

        shipped_refused, shipped_reply_field = shipped_gate(
            worktree_root, ask_note_text, landing_paths=all_paths,
            deleted_paths=deleted_paths, withheld_paths=shipped_withheld,
            own_paths=declared_writes, row_of=stranded_rows, owners=_claim_owners, run_id=sibling_run_id,
            widened=widened,
        )
        if shipped_refused is not None:
            _unstamp_landed()
            _remove(worktree_root, receipt_paths)
            return _error(
                shipped_refused, refused=refused_code(shipped_reply_field), shipped_claims=shipped_reply_field,
            )

    from coordinator_core.git import ref_cas_retry

    # Receipts are run-unique so no peer can touch them; the regenerated allowlist stays in the
    # compared paths because a peer's change to it must refuse rather than be overwritten.
    _own_receipts = set(receipt_paths)
    _overlap_paths = [p for p in (*all_paths, *deleted_paths) if p not in _own_receipts]

    claims_before = (
        _claims_before_landing(worktree_root, [*all_paths, *deleted_paths])
        if session_id is not None and not anchor_only else {}
    )

    if recorded_pc and snapshot is None:
        claims_memo.clear()
        _unstamp_landed()
        _remove(worktree_root, receipt_paths)
        return _snapshot_unavailable_refusal(recorded_pc)
    if recorded_pc:
        landing = set(all_paths) | set(deleted_paths)
        named = _post_checkpoint_named(
            wh_reasons, proc_wh, art_landable=art_landable, sibling_withheld=sibling_withheld,
            left_out=left_out_map, held=held_paths,
        )
        unclaimed = _unclaimed_post_checkpoint(snapshot, recorded_pc, landing=landing, named=named)
        if unclaimed and session_id is not None:
            carried_after = set(_session_left_behind(
                worktree_root, session_id, [*all_paths, *deleted_paths], claims_before, _claimed_or_held,
            ))
            unclaimed = [p for p in unclaimed if p not in carried_after]
        if unclaimed:
            claims_memo.clear()
            _unstamp_landed()
            _remove(worktree_root, receipt_paths)
            return _unclaimed_refusal(recorded_pc, unclaimed)

    def _attempt():
        return commit_v2(commit_params, repo_root)

    def _cas_lost(r) -> bool:
        return (
            isinstance(r, dict)
            and not r.get("committed")
            and ref_cas_retry.is_ref_moved_text(r.get("error"))
        )

    try:
        reply, overlap = ref_cas_retry.retry_ref_moved(
            worktree_root, _overlap_paths, _attempt, _cas_lost
        )
    except OpUnavailableError:
        _unstamp_landed()
        _remove(worktree_root, receipt_paths)
        return _error("ceremony.commit_v2 is not registered")
    except BaseException:
        _unstamp_landed()
        _remove(worktree_root, receipt_paths)
        raise
    if not isinstance(reply, dict):
        reply = {"committed": False, "sha": None, "error": f"unexpected commit_v2 reply: {reply!r}"}

    tolerated: list = []
    real_drift: Optional[list] = None
    if reply.get("refused") == "blob-drift" and not overlap and isinstance(reply.get("blob_drift"), list):
        from coordinator_core.ops.dispatch_emit.comment_only_drift import tolerated_drift

        tolerated, real_drift = tolerated_drift(
            worktree_root, inline_review.get("prep_sidecar") if isinstance(inline_review, dict) else None,
            reply["blob_drift"], untrusted_paths=all_paths,
        )
        if tolerated and not real_drift:
            moved = dict(commit_params["expected_content_sha256"])
            trailers = []
            for t in tolerated:
                moved[t["path"]] = t["actual"]
                trailers.append(f"Comment-Only-Drift: {t['path']} {t['expected'][:12]}->{t['actual'][:12]}")
            commit_params["expected_content_sha256"] = moved
            commit_params["message"] = commit_params["message"].rstrip("\n") + "\n" + "\n".join(trailers) + "\n"
            try:
                reply, overlap = ref_cas_retry.retry_ref_moved(
                    worktree_root, _overlap_paths, _attempt, _cas_lost
                )
            except OpUnavailableError:
                _unstamp_landed()
                _remove(worktree_root, receipt_paths)
                return _error("ceremony.commit_v2 is not registered")
            except BaseException:
                _unstamp_landed()
                _remove(worktree_root, receipt_paths)
                raise
            if not isinstance(reply, dict):
                reply = {"committed": False, "sha": None, "error": f"unexpected commit_v2 reply: {reply!r}"}

    if not (reply.get("committed") and reply.get("sha")):
        _unstamp_landed()
    reply = dict(reply)
    reply["script_source"] = script_source
    if tolerated:
        reply["comment_only_drift"] = tolerated
    if overlap and isinstance(reply.get("error"), str):
        reply["refused"] = overlap
        reply["error"] += (
            ". A peer commit changed a path this landing writes; refire after the peer's commit"
        )
    left_behind: list = []
    if reply.get("committed") and reply.get("sha") and session_id is not None and not anchor_only:
        left_behind = _session_left_behind(
            worktree_root, session_id, [*all_paths, *deleted_paths], claims_before, _claimed_or_held,
        )
    held_report = _dirty_held_by_halted_run(worktree_root, halted_held)
    if held_report:
        reply["held_by_halted_run"] = held_report
    if sibling_withheld:
        reply["sibling_withheld"] = dict(sibling_withheld)
    if foreign_excluded:
        reply[_ls.FOREIGN_EXCLUDED_KEY] = foreign_excluded
    if reply.get("refused") == "blob-drift" and isinstance(reply.get("error"), str):
        reply["error"] = _remedy.blob_drift_remedy(
            reply["error"], worktree_root, _run_id_of(params, ask_run_dir, script_path_raw),
            "dispatch.terminal_commit", task_output_raw,
            real_drift if real_drift is not None else _drift_paths(reply.get("blob_drift")),
        )
    if reply.get("committed") and reply.get("sha"):
        token_state["landed"] = True
        # Spend at landing, before any post-commit step, so a repeat call finds the receipt.
        _settle_token(token_state, landed=True, receipt={
            "sha": str(reply["sha"]), "rows": [c.id for c in contributing_chunks], "paths": list(all_paths),
        })
        _release_siblings_at_landing(close_ctx, str(reply["sha"]))
        from coordinator_core.ops.dispatch_emit.halted_run import mark_predecessors_landed

        try:
            mark_predecessors_landed(worktree_root, _run_id_of(params, ask_run_dir, script_path_raw), str(reply["sha"]))
        except Exception:  # noqa: BLE001 -- the landed commit stands; the predecessor can be recorded landed by hand
            pass
        from coordinator_core.ops.dispatch_emit.emission_receipt import stamp_landed

        stamp_landed(guarded_script, str(reply["sha"]))
        if session_id is not None and not anchor_only:
            from coordinator_core.ops.dispatch_emit import sibling_claims as _sc_follow

            _land_own_left_behind(
                reply, str(reply["sha"]), session_id, worktree_root, repo_root, left_behind,
                withheld=[
                    *foreign_excluded,
                    *wh_reasons,
                    *_sc_follow.shared_dir_holds(
                        worktree_root, sibling_run_id, [*all_paths, *declared_writes], left_behind,
                        own_plan=request.plan_path,
                    ),
                ],
                snapshot=snapshot,
            )
    if not reply.get("committed"):
        _remove(worktree_root, receipt_paths)
        receipt_paths = []
        reply = _cap_commit_reply(reply)
    if reply.get("committed") and reply.get("sha"):
        # Advance plan status so a re-fire (which selects live `open` rows)
        # cannot redo landed work. incomplete_chunks never reach here.
        scope.setdefault("landed", set()).update(all_paths)
        if merge_artifacts:
            reply["merge_artifacts"] = _land_merge_artifacts(
                commit_v2, repo_root, merge_artifacts, message, str(reply["sha"]), session_id,
            )
            if reply["merge_artifacts"].get("committed") or reply["merge_artifacts"].get("nothing_to_commit"):
                scope["landed"].update(merge_artifacts["paths"])
                scope["landed"].update(merge_artifacts["deleted"])
        withheld_product = (
            _withheld_product_files(worktree_root, snapshot, declared_writes, {*all_paths, *deleted_paths})
            if ledger is not None else []
        )
        if withheld_product:
            ledger_met = None
            if close_ctx is not None:
                close_ctx["ledger_met"] = None
            reply["ledger_withheld_product"] = withheld_product
        reply = _cap_commit_reply(reply)
        no_delta = set(reply.get("no_delta") or [])
        coded_chunks = [
            c for c in contributing_chunks
            if any(
                p in deleted_paths or p not in no_delta
                for p in list(c.paths) + (_own_prefix_files(worktree_root, c, report_cache) or [])
                if p in final_paths_set or p in deleted_paths
            )
        ]
        if len(coded_chunks) != len(contributing_chunks):
            kept = {c.id for c in coded_chunks}
            reply["no_product_hunk"] = [c.id for c in contributing_chunks if c.id not in kept]
        noop_chunks = [c for c in done_chunks if c not in coded_chunks]
        # A no-hunk row whose every declared write is absent delivered nothing to
        # close against: coding it drops it from every later emit.
        absent = set(dropped_absent)
        unbuilt = [c for c in noop_chunks if c.paths and set(c.paths) <= absent]
        if unbuilt:
            reply["uncoded_absent"] = [c.id for c in unbuilt]
            noop_chunks = [c for c in noop_chunks if c not in unbuilt]
        checkpoint_of = (
            {} if rows_untouched or not noop_chunks
            else _checkpoint_attribution(worktree_root, str(reply["sha"]), request.plan_path, noop_chunks)
        )
        outside_of = (
            {} if rows_untouched or not noop_chunks
            else _outside_landings(
                worktree_root, str(reply["sha"]), request.plan_path,
                [c for c in noop_chunks if c.id not in checkpoint_of],
            )
        )
        outside_rows: dict = {}
        for chunk_id, shas in outside_of.items():
            for plan_rel, ids in _source_rows_by_plan(worktree_root, request.plan_path, [chunk_id]).items():
                for rid in ids:
                    outside_rows.setdefault(plan_rel, {})[rid] = shas
        checkpoint_rows: dict = {}
        for chunk_id, csha in checkpoint_of.items():
            for plan_rel, ids in _source_rows_by_plan(worktree_root, request.plan_path, [chunk_id]).items():
                checkpoint_rows.setdefault(csha, {}).setdefault(plan_rel, set()).update(ids)
        if request.review_only and not anchor_only:
            review_coded, review_no_delta = _review_only_coding(
                worktree_root, str(reply["sha"]), request, inline_review
            )
            for csha, by_plan in review_coded.items():
                for plan_rel, ids in by_plan.items():
                    ids = ids - unmerged
                    if ids:
                        checkpoint_rows.setdefault(csha, {}).setdefault(plan_rel, set()).update(ids)
            if review_no_delta:
                reply["review_uncoded_no_delta"] = review_no_delta
        predecessor = {} if rows_untouched else _predecessor_rows(
            worktree_root, request.plan_path, params.get(TERMINAL_RESUME_LEDGER_PARAM)
        )
        if unmerged:
            predecessor = {
                csha: {plan_rel: ids - unmerged for plan_rel, ids in by_plan.items()}
                for csha, by_plan in predecessor.items()
            }
        resume_ledger_param = params.get(TERMINAL_RESUME_LEDGER_PARAM)
        own_coded = (
            frozenset(c.id for c in coded_chunks) | frozenset(checkpoint_of) | frozenset(outside_of) | unmerged
        )
        if rows_untouched:
            halted_checkpoints = {}
        elif resume_ledger_param is not None:
            halted_checkpoints = _halted_run_checkpoint_rows(
                worktree_root, request.plan_path, resume_ledger_param, own_coded
            )
        else:
            halted_checkpoints = _earlier_fire_checkpoint_rows(
                worktree_root, request.plan_path, str(reply["sha"]), own_coded
            )
        for csha, by_plan in halted_checkpoints.items():
            for plan_rel, ids in by_plan.items():
                fresh = ids.difference(*(other.get(plan_rel, set()) for other in predecessor.values()))
                if fresh:
                    checkpoint_rows.setdefault(csha, {}).setdefault(plan_rel, set()).update(fresh)
        landed_before = {
            (plan_rel, rid)
            for by_plan in (*predecessor.values(), *halted_checkpoints.values())
            for plan_rel, ids in by_plan.items() for rid in ids
        }
        for csha, by_plan in predecessor.items():
            for plan_rel, ids in by_plan.items():
                checkpoint_rows.setdefault(csha, {}).setdefault(plan_rel, set()).update(ids)
        noop_rows = (
            {} if rows_untouched
            else _source_rows_by_plan(
                worktree_root, request.plan_path,
                [c.id for c in noop_chunks if c.id not in checkpoint_of and c.id not in outside_of],
            )
        )
        noop_rows = {
            plan_rel: {rid for rid in ids if (plan_rel, rid) not in landed_before}
            for plan_rel, ids in noop_rows.items()
        }
        source_rows = (
            {request.plan_path: set()}
            if rows_untouched
            else _source_rows_by_plan(
                worktree_root, request.plan_path, [c.id for c in coded_chunks]
            )
        )
        partial_rows = _source_rows_by_plan(
            worktree_root, request.plan_path, [c.id for c in contributing_partial]
        )
        # Before the coded stamp: that commit carries the plan, so a stamp
        # minted here lands in it and costs no commit of its own.
        if record_abs is not None and record is not None and (
            request.plan_path in source_rows or request.plan_path in noop_rows
            or request.plan_path in outside_rows or request.plan_path in partial_rows
            or any(request.plan_path in by_plan for by_plan in checkpoint_rows.values())
        ):
            reply.update(
                _mint_review_stamp(
                    worktree_root, request.plan_path, str(reply["sha"]), record_abs, record,
                    tree=reply.get("tree"),
                )
            )
        writeback = {} if anchor_only else _register_writeback(
            worktree_root, request.plan_path, inline_review, str(reply["sha"])
        )
        register_sizing = writeback.pop("register_sizing", None)
        reply.update(writeback)
        pending_operator = _awaiting_operator(open_rows)
        if pending_operator:
            reply["awaiting_operator"] = pending_operator
        not_stamped = _ledger_not_stamped_reason(ledger, params.get("ledger_verdict"), open_rows)
        if withheld_product:
            reply["exit_criterion_not_stamped"] = (
                f"ledger rows not reported met: commit omits changed file(s) {withheld_product}"
            )[:_UNDONE_CAP]
        elif not_stamped:
            reply["exit_criterion_not_stamped"] = not_stamped
        if (
            record_abs is not None and record is not None and not anchor_only
            and source_rows and request.plan_path not in source_rows
            and request.plan_path.endswith(_SPINE_SUFFIX)
        ):
            reply.update(
                _mint_mise_receipt(
                    worktree_root, request.plan_path, str(reply["sha"]), record_abs, record,
                    tree=reply.get("tree"),
                )
            )
        if ledger is not None and not withheld_product:
            reply.update(_ledger_writeback(
                worktree_root, ledger, params.get("ledger_verdict"),
                str(params.get("run_id") or ask_run_dir.name), str(reply["sha"]),
            ))
        shipped_sizing = None
        dropped_sizing = None
        dropped_mint_failed = False
        if ask_note_text and not anchor_only and not foreign_repo:
            dropped_sizing = cited_sizing or (ledger.sizing_rel if ledger is not None else None)
            if dropped_sizing:
                dropped = _dropped_part_rows(worktree_root, dropped_sizing, ask_note_text)
                dropped_mint_failed = "dropped_parts_error" in dropped
                reply.update(dropped)
                if not dropped:
                    dropped_sizing = None
        if dropped_mint_failed:
            reply["sizing_ship_skipped"] = "a dropped part could not be written to the register"
        elif not anchor_only and not incomplete_chunks and not foreign_repo:
            shipped = _ship_cited_sizing(worktree_root, repo_root, request.plan_path)
            shipped_sizing = shipped.pop("sizing_shipped", None)
            reply.update(shipped)
            if shipped_sizing:
                run_id = _run_id_of(params, ask_run_dir, script_path_raw)
                reply.update(_write_resolution(worktree_root, shipped_sizing, str(reply["sha"]), run_id))
        also_commit = tuple(
            p for p in (
                request.plan_path if reply.get("review_stamp") == "minted" else None,
                reply.get("review_receipt"),
                register_sizing,
                dropped_sizing,
                shipped_sizing,
            ) if p
        )
        late_rewrites: list = []
        if artifacts is not None:
            sizings = [*artifacts.by_class.get("sizing", ()), *([ledger.sizing_rel] if ledger is not None and ledger.sizing_rel else [])]
            plans = [request.plan_path] if product_only and request.plan_path else []
            sha_now = str(reply["sha"])
            late_rewrites = [
                p for p in dict.fromkeys([*sizings, *plans])
                if p not in also_commit and p not in scope["landed"]
                and not (snapshot is not None and snapshot.ignored_among([p]))
                and _differs_from_commit(worktree_root, sha_now, p)
            ]
            also_commit += tuple(late_rewrites)
        bug_rels: list = []
        bug_saved: dict = {}
        if not foreign_repo:
            bug_fields, bug_rels, bug_saved = _close_marked_bugs(
                worktree_root, [*done_chunks, *partial_chunks], str(reply["sha"]), snapshot, report_cache, ledger,
            )
            reply.update(bug_fields)
            also_commit += tuple(p for p in bug_rels if p not in also_commit)
        paying_close = close_ctx is not None and close_ctx["applies"]
        if close_ctx is not None:
            prep = inline_review.get("prep")
            base = prep.get("run_base_sha") if isinstance(prep, dict) else None
            close_ctx.update(
                review_receipt=reply.get("review_receipt")
                or (request.plan_path if reply.get("review_stamp") == "minted" else None),
                run_base_sha=base if isinstance(base, str) else None,
                terminal_sha=str(reply["sha"]),
            )
        if paying_close:
            also_commit += tuple(
                _pay_inline_legs(worktree_root, close_ctx, str(reply["sha"]), session_id, snapshot)
            )
        broken_tells = _falsifier_broken_tells(params.get("falsifier_broken"), request.plan_path)
        if close_ctx is not None:
            close_ctx["falsifier_broken"] = broken_tells
        flips = bool(
            request.plan_path
            and reply.get("review_stamp") == "minted"
            and broken_tells is None
            and not register_failure
            and not incomplete_chunks
            and not unsent_memos
        )
        archive_targets = ()
        consumed_baton = None
        if shipped_sizing and re.search(
            r"^requirement_register:", _read_rel(worktree_root, shipped_sizing) or "", re.MULTILINE
        ):
            consumed_baton = _cited_baton(worktree_root, shipped_sizing)
            archive_targets = (shipped_sizing, consumed_baton) if consumed_baton else (shipped_sizing,)
        plan_commits = not product_only or (
            bool(request.plan_path)
            and snapshot is not None
            and not snapshot.ignored_among([request.plan_path])
        )
        stamp_commit = bool(shipped_sizing) or plan_commits or bool(late_rewrites) or bool(bug_rels)
        if not foreign_repo:
            reply.update(
                _stamp_coded_commit(
                    commit_v2, worktree_root, repo_root, source_rows,
                    str(reply["sha"]), session_id,
                    also_commit=also_commit,
                    noop_rows=noop_rows, checkpoint_rows=checkpoint_rows, outside_rows=outside_rows,
                    snapshot=snapshot,
                    flip_plan=request.plan_path if flips else None,
                    flip_reason=_criterion_reason(inline_review), ledger_met=ledger_met,
                    receipt=request.plan_path if plan_commits and reply.get("review_stamp") == "minted" else None,
                    trailers=(f"Sizing-Shipped: {shipped_sizing} {reply['sha']}",) if shipped_sizing else (),
                    commit=stamp_commit,
                    archive_after_flip=archive_targets,
                    memo_closes=_memo_rows_to_close(worktree_root, request),
                )
            )
        flipped = reply.pop("_flip", None)
        stamp_committed = tuple(reply.pop("_committed", ()))
        if bug_rels and ("coded_stamp_error" in reply or not set(bug_rels) <= set(stamp_committed)):
            _restore_texts(worktree_root, bug_saved)
            reply["bug_close_refused"] = [
                *reply.get("bug_close_refused", ()),
                *({"id": i, "reason": "stamp commit did not land; row restored"} for i, r in reply["bugs_closed"].items() if r in bug_rels),
            ]
            reply["bugs_closed"] = {i: r for i, r in reply["bugs_closed"].items() if r not in bug_rels}
            if not reply["bugs_closed"]:
                del reply["bugs_closed"]
        scope["landed"].update(stamp_committed)
        if shipped_sizing:
            archived_to = reply.get("archived") or {}
            if shipped_sizing not in stamp_committed and shipped_sizing not in archived_to:
                reply["ok"] = False
                reply["error"] = (
                    f"shipped sizing {shipped_sizing} did not land: "
                    + str(reply.get("coded_stamp_error") or "stamp commit carried no path for it")
                    + ". The product commit stands; ship it through ceremony.commit_v2"
                )
        if paying_close:
            _settle_close(worktree_root, close_ctx, "coded_stamp_error" not in reply)
        if shipped_sizing:
            reply["sizing_shipped"] = {"sizing": shipped_sizing, "sha": str(reply["sha"])}
        if resolves is not None:
            reply.update(_record_resolution(worktree_root, resolves, str(reply["sha"])))
        coded_stale = reply.pop("coded_index_stale", [])
        if coded_stale:
            reply["index_stale"] = sorted(set(reply.get("index_stale") or []) | set(coded_stale))
        if request.plan_path and reply.get("review_stamp") == "minted" and broken_tells is not None:
            reply["plan_status"] = "not-stamped"
            reply["plan_status_reason"] = (
                "falsifier integrity BROKEN at pre-dispatch ("
                + (", ".join(broken_tells) or "no tell named")
                + "): a met judge on this falsifier is not self-stamped; close it through the EM "
                "re-judge route (terminal-judge.md § Closing a plan whose run judged not_met)"
            )
        elif register_failure and reply.get("review_stamp") == "minted":
            reply["plan_status"] = "not-stamped"
            reply["plan_status_reason"] = register_failure
        elif flips and reply.get("coded_sha") and flipped is not None:
            reply.update(flipped)
        elif flips and product_only and "coded_stamp_error" not in reply:
            reply.update(
                _stamp_plan_implemented(
                    worktree_root, request.plan_path, str(reply["sha"]),
                    reason=_criterion_reason(inline_review), ledger_met=ledger_met,
                )
            )
        elif request.plan_path and reply.get("review_stamp") == "minted":
            reply["plan_status"] = "not-stamped"
            reply["plan_status_reason"] = (
                f"incomplete chunks remain: {sorted(incomplete_chunks)}"
                if incomplete_chunks
                else "memo not sent (no sender-side receipt): "
                + ", ".join(f"{rid} ({slug})" for rid, slug in sorted(unsent_memos.items()))
                if unsent_memos
                else "coded-stamp commit did not land"
            )
        criterion = inline_review.get("criterion")
        if isinstance(criterion, dict) and criterion.get("status"):
            reply["criterion_status"] = criterion["status"]
        if ledger is not None and isinstance(params.get("ledger_verdict"), dict) and "sha" in reply:
            reply.pop("post_commit_owed", None)
            owed = _lifecycle_owed(
                params["ledger_verdict"], str(reply["sha"]), reply, consumed_baton, close_ctx,
            )
            if owed:
                reply["post_commit_owed"] = owed
    if reply.get("committed") and reply.get("sha"):
        residue = _commit_residue(
            worktree_root, request, snapshot, report_cache, session_id,
            excluded=set(all_paths) | set(deleted_paths) | set(scope.get("landed", ())) | set(dropped_absent) | set(wh_map) | set(never_commit_dropped) | {p for ps in sibling_paths.values() for p in ps},
            unfinished=incomplete_chunks | held_ids,
            commit_v2_mod=commit_v2_mod,
        )
        if residue:
            reply["residue"] = residue
            bad = sorted(p for p, why in residue.items() if why == "unexplained")
            if bad:
                reply["ok"] = False
                reply["error"] = (
                    "commit landed but requested path(s) are still dirty or untracked: "
                    + ", ".join(bad)
                    + ". The run token is spent: commit them through ceremony.commit_v2"
                )
        if artifacts is not None:
            landed_now = (
                set(all_paths) | set(deleted_paths) | set(scope.get("landed", ()))
                | set(reply.get("archived") or ())
            )
            withheld_art = {**(dict(art_landable.withheld) if art_landable is not None else {}), **wh_map}
            if not foreign_repo:
                fresh_art = run_artifacts.candidate_paths(
                    worktree_root, art_run_id, sizing_rel=cited_sizing, plan_rel=request.plan_path,
                    doc_paths=params.get(run_artifacts.DOC_PATHS_PARAM) or (),
                )
                late_foreign = set(claims_over([*fresh_art.paths(), *artifacts.paths()])) | set(left_out_map)
                landed_now |= set(_reland_late_artifacts(
                    reply, worktree_root, repo_root, artifacts, fresh_art,
                    landed_now | set(withheld_art) | late_foreign | set(never_commit_dropped),
                    session_id, str(reply["sha"]),
                ))
            art_residue = run_artifacts.residue(
                artifacts, snapshot.changed if snapshot is not None else (), landed_now, withheld_art,
                foreign=set(claims_over(artifacts.paths())) | set(left_out_map),
            )
            reply["run_artifacts"] = {
                "landed": sorted(
                    {p for p in artifacts.paths() if p in landed_now}
                    | set((reply.get("late_reland") or {}).get("paths") or ())
                ),
                "withheld": dict(sorted(withheld_art.items())),
            }
            if art_residue:
                reply["run_artifact_residue"] = art_residue
                reply["ok"] = False
                reply["error"] = (
                    "run artifact(s) still dirty after the commit: "
                    + ", ".join(f"{p} ({cls})" for p, cls in art_residue.items())
                    + ". The run token is spent: commit them through ceremony.commit_v2"
                )
        elif wh_map:
            reply["run_artifacts"] = {"landed": [], "withheld": dict(sorted(wh_map.items()))}
        if reread_artifacts:
            reply.setdefault("run_artifacts", {"landed": [], "withheld": {}})["reread"] = reread_artifacts
        if review_land:
            reply["review_residue"] = sorted(review_land)
            still = _changed_paths(worktree_root, list(review_land))
            if still is None or still:
                reply["review_residue_dirty"] = sorted(still) if still else sorted(review_land)
                reply["ok"] = False
                reply["error"] = (
                    "review residue still dirty after the commit: "
                    + ", ".join(reply["review_residue_dirty"])
                    + ". The run token is spent: commit them through ceremony.commit_v2"
                )
    if review_unanswerable is not None:
        reply["review_edits"] = None
        reply["ok"] = False
        reply["error"] = (
            (str(reply["error"]) + "; " if reply.get("error") else "")
            + f"review-stage edits unverified: {review_unanswerable}; check the tree for review-written files"
        )
    if integrity_extra.get("since_base_check"):
        reply["since_base_check"] = integrity_extra["since_base_check"]
    if reach_reply_field.get("checked"):
        reply["reach_claims"] = reach_reply_field
    if shipped_reply_field.get("checked"):
        reply["shipped_claims"] = shipped_reply_field
    reply["targeted_tests_check"] = integrity_extra["targeted_tests_check"]
    reply["reverify_check"] = integrity_extra["reverify_check"]
    if "retested_tests" in integrity_extra:
        reply["retested_tests"] = integrity_extra["retested_tests"]
    if "targeted_tests_note" in integrity_extra:
        reply["targeted_tests_note"] = integrity_extra["targeted_tests_note"]
    for key in ("advisory", "provenance", "targeted_tests_foreign"):
        if integrity_extra.get(key):
            reply[key] = integrity_extra[key]
    reply["receipts"] = receipt_paths
    reply["receipt_coverage"] = "written" if receipt_paths else "unidentified"
    if (
        receipt_paths
        and reply.get("plan_status", "implemented") != "implemented"
        and any(fm.get("verdict") == "agent-delivered" for fm, _prose in receipts_built)
    ):
        reply["receipt_divergence"] = (
            f"receipt verdict agent-delivered beside plan_status {reply['plan_status']!r}"
        )
    if review_record_error:
        reply["review_record_error"] = review_record_error
    reply["branch_check"] = branch_check
    reply["chunks_committed"] = [c.id for c in contributing_chunks]
    no_hunk = [c for c in done_chunks if c not in contributing_chunks and c.id not in reply.get("no_product_hunk", [])]
    if no_hunk:
        reply["no_product_hunk"] = sorted(set(reply.get("no_product_hunk", [])) | {c.id for c in no_hunk})
    if entangled:
        reply["entangled"] = entangled
    reply["partial_committed"] = [c.id for c in contributing_partial]
    _with_withheld(reply)
    reply["dropped_absent"] = dropped_absent
    reply["never_commit_dropped"] = never_commit_dropped
    reply["deleted_paths"] = deleted_paths
    if reverted_absent:
        reply["reverted_absent"] = reverted_absent
    if declared_reverts_ignored:
        reply["declared_reverts_ignored"] = declared_reverts_ignored
    reply["unmarked_incomplete"] = unmarked_incomplete
    if unmerged:
        reply["sibling_rows_unmerged"] = sorted(unmerged)
    if unsent_memos:
        reply["memo_unsent"] = dict(sorted(unsent_memos.items()))
    reply["prefix_files"] = prefix_files
    reply.update(_declared_reconcile(ask_run_dir, all_paths))
    reply.update(usage_reply)
    if reply.get("committed"):
        from coordinator_core.ops.dispatch_emit import run_push

        reply["push"] = run_push.nudge_push(worktree_root, worktree_root)
    if reply.get("committed") or reply.get("nothing_to_commit"):
        _with_siblings(reply)
    elif sibling_paths:
        reply["sibling_commits"] = [
            {"repo": root.name, "paths": paths, "committed": False,
             "error": "home commit did not land; sibling commit withheld"}
            for root, paths in sibling_paths.items()
        ]
    return reply


