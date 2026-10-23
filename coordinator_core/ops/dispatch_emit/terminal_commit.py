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
    not gitignored and not held by a live peer's claim; a withheld path is named in
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
from coordinator_core.ops.dispatch_emit import credit_gate, gate_owed, halt_baseline, pre_halt_credit  # noqa: E402 -- sibling-package group
from coordinator_core.ops.dispatch_emit.land_run import _em_discharged_rows  # noqa: E402
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
    Field(TERMINAL_RESUME_LEDGER_PARAM, "dict"),
    Field("doc_paths", "str_list"),
    Field("allow_empty", "bool"),
    Field("declared_reverts", "str_list"),
)

_PYTEST_NODE = re.compile(r"[\w./-]+\.py::[\w.\[\]-]+(?:::[\w.\[\]-]+)*")


def _params_from_task_output(path_raw: str) -> dict:
    """`next_action.params` out of a Workflow task-output file, bare or `{summary, logs, result}`-wrapped.

    Same envelope unwrap as `land-wave.py :: _wave_result`: a dict `result` is the digest.
    Raises ValueError naming the file when no `next_action.params` dict is found.
    """
    path = Path(path_raw)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read params.task_output_path {path_raw!r}: {exc}") from exc
    if isinstance(data, dict) and "next_action" not in data and isinstance(data.get("result"), dict):
        data = data["result"]
    action = data.get("next_action") if isinstance(data, dict) else None
    found = action.get("params") if isinstance(action, dict) else None
    if not isinstance(found, dict):
        raise ValueError(
            f"{path_raw!r} carries no next_action.params: neither a digest nor a "
            "task-output envelope wrapping one"
        )
    return found


def _delivery_not_rostered(inline_review: dict) -> bool:
    """True when the review carries no delivery verdict because the roster names no
    delivery-verifier: no objection, unlike any other absent verdict."""
    from coordinator_core.ops.review_mint.roster import DELIVERY_NOT_ROSTERED

    return (
        not isinstance(inline_review.get("delivery"), dict)
        and inline_review.get("delivery_not_run") == DELIVERY_NOT_ROSTERED
    )


def _anchor_request(worktree_root: Path, params: dict, inline_review: Optional[dict]) -> Optional[CommitRequest]:
    """The zero-chunk request for a marker-less run whose review delivered PASS
    with no product file: it still owes the plan a fresh `Inline-Review`
    commit for `review_stamp` to anchor on. ``None`` for any other shape."""
    plan_rel = params.get("plan_path")
    if not isinstance(plan_rel, str) or not isinstance(inline_review, dict):
        return None
    delivery = inline_review.get("delivery")
    if _delivery_not_rostered(inline_review):
        delivery = inline_review.get("prep")
    elif not isinstance(delivery, dict) or delivery.get("verdict") != "PASS":
        return None
    if not isinstance(delivery, dict):
        return None
    files = delivery.get("product_files")
    if isinstance(files, bool) or not isinstance(files, int) or files != 0:
        return None
    plan_text = _read_rel(worktree_root, plan_rel)
    if plan_text is None:
        return None
    return CommitRequest(
        chunks=(), plan_path=plan_rel, deliverable_id=plan_deliverable_id(plan_text)
    )


def _error(message: str, **extra: object) -> dict:
    result: dict = {"committed": False, "sha": None, "error": message}
    result.update(extra)
    return result


def _landing_integrity_gate(
    worktree_root: Path, all_paths: list, deleted_paths: list, record_abs: Optional[Path],
    params: dict, run_id: Optional[str], plan: Optional[str] = None,
) -> dict:
    """Pre-commit integrity findings over the product landing set (unparseable ``.py``, and a
    blob dropping lines a foreign commit added since the run base, and the test verdict
    (a valid fresh retest verdict, else the carried one) naming a run-caused failure:
    ``targeted-tests-failed``, with ``targeted_tests_check`` always set and ``retested_tests`` on
    ``pass-on-retest``). Returns ``{}``-like extras:
    ``refused`` + ``integrity`` on any finding, ``since_base_check: "no-base"`` when the run has
    no recorded base so the overlap arm was skipped."""
    from coordinator_core.ops.dispatch_emit import landing_integrity as li

    skip = set(deleted_paths)
    if record_abs is not None:
        try:
            skip.add(Path(record_abs).resolve().relative_to(Path(worktree_root).resolve()).as_posix())
        except ValueError:
            pass
    paths = [p for p in all_paths if p not in skip]
    findings = li.unparseable_py(worktree_root, paths)
    extra: dict = {}
    base = li.run_base_for(worktree_root, run_id, params)
    if base is None:
        extra["since_base_check"] = "no-base"
    else:
        findings += li.since_base_findings(worktree_root, paths, base, run_id=run_id, plan=plan)
    # Trap: ``inline_review.tests.status`` is already baseline-aware (a baseline-cleared run folds
    # to ``pass``), so a carried ``fail`` here is run-caused or a failed row verification.
    tested, tcheck = li.landing_test_gate(worktree_root, params, run_id)
    findings += tested
    extra["targeted_tests_check"] = tcheck
    if tcheck == "pass-on-retest":
        fresh = li.fresh_targeted_verdict(worktree_root, run_id) if run_id else None
        if fresh is not None:
            extra["retested_tests"] = fresh.get("tests")
    if findings:
        extra["refused"] = findings[0].reason
        extra["integrity"] = [
            {"path": f.path, "reason": f.reason, "detail": f.detail} for f in findings
        ]
    return extra


#: Refusals raised before the run's incomplete set is known: ``stranded`` would
#: read ``{}`` ("nothing left behind") when it is really unknown, so the key is omitted.
_STRANDED_UNKNOWN_REFUSALS = frozenset({"missing-run-outcome", "unreviewed"})


def _missing_run_outcome(params: object) -> Optional[dict]:
    """Refusal naming every run-outcome param the caller omitted, else ``None``.

    The emitted script's marker records what the run promised (chunks, paths),
    never how it ended: ``incomplete_chunks`` and the review-stage
    ``inline_review`` exist only in the run's result digest
    (``next_action.params``), so a bare ``script_path`` cannot supply them.
    Deriving them from chunk reports would land code the review wave never saw,
    which the deterministic-review ruling forbids. Checked once, ahead of the
    per-field validation, so the caller learns every gap in one reply.
    """
    if not isinstance(params, dict) or not isinstance(params.get("script_path"), str):
        return None
    absent = [
        name for name in ("incomplete_chunks", "inline_review") if params.get(name) is None
    ]
    if "incomplete_chunks" not in absent:
        return None
    return _error(
        f"params {absent} not passed. The run outcome is not derivable from "
        f"script_path {params['script_path']!r} (it carries only the commit-request marker: "
        "chunks and declared paths) and no params.task_output_path was given. Pass "
        "task_output_path=<the run's task-output file> (its next_action.params supplies "
        "incomplete_chunks and inline_review), or pass incomplete_chunks and inline_review "
        "from the digest's terminal_commit_cli line. A run that died before its review "
        "stage has no inline_review to pass: re-emit it review-only (emit-dispatch-workflow "
        "--inventory <inv> --review-only --rows <DONE ids> --run-base <sha>) and land from "
        "that run's terminal_commit_cli line",
        refused="missing-run-outcome",
        missing=absent,
    )


_XS_SPINE_ORIGIN = "sizing object"
_KIND_FIELD = re.compile(r"^(derived_from|scope_mode)[ \t]*:[ \t]*(.*?)[ \t]*$", re.M)


def _close_kind(plan_text: Optional[str]) -> tuple[str, bool]:
    """``(close kind, plan_governs)`` read off the run's plan path frontmatter.

    A minted inventory spine is ``mise-run``; a minted XS spine (``derived_from: sizing object``)
    is ``xs-s-run`` with no governing plan; an S ``scope_mode: spec-dispatch`` plan is ``xs-s-run``
    with one. Anything else, or no readable plan, is ``plan-run``. Values are matched after
    dropping a trailing `` # comment`` and quotes; the text is LF-normalised first.
    """
    if plan_text is None:
        return "plan-run", True
    from coordinator_core.frontmatter.primitives import split_frontmatter

    split = split_frontmatter(plan_text.replace("\r\n", "\n"))
    if split is None:
        return "plan-run", True
    fields: dict = {}
    for name, raw in _KIND_FIELD.findall(split.fm_text):
        fields.setdefault(name, re.sub(r"[ \t]+#.*$", "", raw).strip().strip("\"'"))
    origin = fields.get("derived_from")
    if origin == _MINTED_SPINE_ORIGIN:
        return "mise-run", False
    if origin == _XS_SPINE_ORIGIN:
        return "xs-s-run", False
    if fields.get("scope_mode") == "spec-dispatch":
        return "xs-s-run", True
    return "plan-run", True


def _cited_sizing(worktree_root: Path, plan_path: Optional[str]) -> Optional[str]:
    """Worktree-relative path of the sizing the run's plan or minted spine cites
    as ``sizing_object:``, when it is an existing file under ``state/sizings/``.

    The warp ask arm rewrites that sizing during the run (``status: routed``, the
    ``plan:`` link) and no chunk declares it, so the commit carries it by this
    citation. In-process reads only: no spawn.
    """
    if not plan_path:
        return None
    text = _read_rel(worktree_root, plan_path)
    if text is None:
        return None
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

    split = split_frontmatter(text)
    cited = read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None
    if not cited:
        return None
    return _sizing_file_rel(worktree_root, cited)


def _cited_baton(worktree_root: Path, sizing_rel: Optional[str]) -> Optional[str]:
    """Worktree-relative path of the handoff the cited sizing names as its top-level ``baton:``,
    when it is an existing file under the handoffs home. In-process read: no spawn."""
    text = _read_rel(worktree_root, sizing_rel) if sizing_rel else None
    if text is None:
        return None
    import yaml

    try:
        doc = _yaml_load(text)
    except yaml.YAMLError:
        return None
    cited = doc.get("baton") if isinstance(doc, dict) else None
    if not isinstance(cited, str) or not cited:
        return None
    guarded = contained_path(worktree_root / cited, [Path(home_dir(str(worktree_root), "handoffs"))])
    if guarded is None or not guarded.is_file():
        return None
    return guarded.relative_to(worktree_root.resolve()).as_posix()


def _derive_judge_status(inline_review: object) -> Optional[str]:
    """Re-derive ``inline_review.criterion.status`` from its structured ``clauses``, in place.

    Any ``not_met`` clause gives ``not_met``; else any non-``met`` clause gives ``indeterminate``;
    a ``met`` reply whose ``clauses`` or ``observed`` list is present and empty is
    ``indeterminate`` (a reply that omits the key is a digest shape that never carried it, left
    alone). A derived status
    that differs from the reply's is written over it and the disagreement appended to
    ``criterion["deviations"]``; a derived status never upgrades a non-``met`` reply. Returns the
    deviation text, or ``None`` when the reply agrees or carries no judged criterion.
    """
    criterion = inline_review.get("criterion") if isinstance(inline_review, dict) else None
    if not isinstance(criterion, dict) or criterion.get("status") != "met":
        return None
    clauses = criterion.get("clauses")
    if not isinstance(clauses, list):
        return None
    statuses = [c.get("status") if isinstance(c, dict) else None for c in clauses]
    if "not_met" in statuses:
        derived, why = "not_met", "a clause is not_met"
    elif any(s != "met" for s in statuses):
        derived, why = "indeterminate", "a clause is not met or indeterminate"
    elif not statuses:
        derived, why = "indeterminate", "met reply carries no clauses"
    elif "observed" in criterion and not criterion["observed"]:
        derived, why = "indeterminate", "met reply observed nothing"
    else:
        return None
    note = f"judge reply status 'met' disagrees with its clauses: derived {derived} ({why})"
    criterion["status"] = derived
    criterion["reason"] = note
    criterion["deviations"] = [*(criterion.get("deviations") or []), note]
    return note


def _mint_mise_receipt(
    worktree_root: Path, spine_rel: str, sha: str, record_abs: Path, record: dict,
    tree: Optional[str] = None,
) -> dict:
    """Mint the receipt onto the mise inventory record behind ``spine_rel``. Returns
    ``{"review_receipt": rel}`` on success, ``{"review_stamp": "refused", ...}`` on a refusal,
    ``{}`` when no record resolves. The product commit stands either way."""
    found = _inventory_record_rel(worktree_root, spine_rel)
    if found is None:
        return {}
    inventory_rel = found[0]
    guarded = contained_path(worktree_root / inventory_rel, [worktree_root])
    if guarded is None:
        return {}
    from coordinator_core.ops.review_stamp import MintRefusal, mint_receipt

    try:
        mint_receipt(guarded, worktree_root, resolved=(sha, record_abs, record), terminal_tree_sha=tree)
    except MintRefusal as exc:
        return {"review_stamp": "refused", "review_stamp_refusal": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        return {"review_stamp": "refused", "review_stamp_refusal": repr(exc)}
    return {"review_receipt": inventory_rel}


#: Per-plan seam-check sidecar suffix; `plan.seam_record` writes `<plan-stem>` + this beside the plan
#: (prep phase) or `<plan-stem>.wave-<N>` + this (wave-boundary phase).
_SEAM_SIDECAR_SUFFIX = ".seam.yaml"

#: Mirrors `plan_tasks_mutate.EVIDENCE_SIDECAR_SUFFIX` (pinned by test): importing that module costs ~25ms.
_EVIDENCE_SIDECAR_SUFFIX = ".evidence.yaml"


def _seam_sidecars(worktree_root: Path, plan_path: Optional[str], chunk_ids: list) -> list:
    """Worktree-relative `.seam.yaml` sidecars on disk beside the run's plan and each
    source plan its committed rows came from, in sorted order."""
    if not plan_path:
        return []
    plans = {plan_path, *_source_rows_by_plan(worktree_root, plan_path, chunk_ids)}
    found = []
    for plan in sorted(plans):
        pure = PurePosixPath(plan)
        prep = str(pure.with_name(pure.stem + _SEAM_SIDECAR_SUFFIX))
        if (worktree_root / prep).is_file():
            found.append(prep)
        fire = str(pure.with_name(pure.stem + ".fire" + _SEAM_SIDECAR_SUFFIX))
        if (worktree_root / fire).is_file():
            found.append(fire)
        try:
            waves = sorted(
                p.name for p in (worktree_root / pure.parent).glob(f"{_glob.escape(pure.stem)}.wave-*{_SEAM_SIDECAR_SUFFIX}")
                if p.is_file()
            )
        except OSError:
            waves = []
        found.extend(str(pure.with_name(name)) for name in waves)
    return found


_MEMO_NO_SLUG = "no slug: declare receipt:"


def _memo_rows_without_receipt(worktree_root: Path, request: CommitRequest) -> dict:
    """``{row id: slug}`` of the plan's open memo-send rows whose sender-side receipt is absent.

    A memo row may be dispatched to an executor; it closes on
    ``memo-outbox/sent/<slug>.md`` existing, when ``_stamp_coded_commit`` resolves the row
    ``delivered_elsewhere`` with ``disposition_ref`` naming that receipt (a non-open row is not reported).
    An open row naming no slug maps to ``"no slug: declare receipt:"``, never dropped.
    Unreadable plan or spine: empty.
    """
    if not request.plan_path:
        return {}
    text = _read_rel(worktree_root, request.plan_path)
    if text is None:
        return {}
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.ops.dispatch_emit.memo_rows import (
        _is_memo_send_row,
        memo_receipt_exists,
        memo_row_slug,
    )

    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return {}
    try:
        rows = _yaml_load(located.body) or []
    except yaml.YAMLError:
        return {}
    out: dict = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get("performer") == "em":
            continue
        if (row.get("disposition") or "open") != "open" or not _is_memo_send_row(row):
            continue
        slug = memo_row_slug(row)
        if slug is None:
            out[str(row["id"])] = _MEMO_NO_SLUG
        elif not memo_receipt_exists(worktree_root, slug):
            out[str(row["id"])] = slug
    return out


def _memo_rows_to_close(worktree_root: Path, request: CommitRequest) -> dict:
    """``{plan: {row id: (receipt ref, receiver)}}`` of the run's chunk rows that are open
    memo-send rows with a sender-side receipt on disk. One stat per memo row, no spawn."""
    if not request.plan_path:
        return {}
    text = _read_rel(worktree_root, request.plan_path)
    if text is None:
        return {}
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.ops.dispatch_emit.memo_rows import (
        _is_memo_send_row,
        memo_receipt_exists,
        memo_row_slug,
    )
    from coordinator_core.session.machinery_paths import MEMO_OUTBOX_RELDIR

    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return {}
    try:
        rows = _yaml_load(located.body) or []
    except yaml.YAMLError:
        return {}
    ids = {c.id for c in request.chunks}
    out: dict = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or str(row.get("id")) not in ids:
            continue
        if (row.get("disposition") or "open") != "open" or not _is_memo_send_row(row):
            continue
        slug = memo_row_slug(row)
        if slug is not None and memo_receipt_exists(worktree_root, slug):
            to = row.get("memo_to")
            out.setdefault(request.plan_path, {})[str(row["id"])] = (
                f"{MEMO_OUTBOX_RELDIR}/sent/{slug}.md",
                to.strip() if isinstance(to, str) and to.strip() else "its receiver",
            )
    return out


_LANDS_CHUNKS_LINE = re.compile(r"^Lands-Chunks:[ \t]*(?P<v>.+?)\s*$", re.M)


def _predecessor_rows(worktree_root: Path, plan_path: Optional[str], resume: object) -> dict:
    """``{halt sha: {plan: {row id}}}`` of the rows a ledger resume's halt commit landed.

    Reads the one commit ``resume_ledger.halt_sha`` names, in-process (zero spawns), and maps
    its ``Lands-Chunks:`` ids to plan rows. Empty for a non-resume close, a bad sha or an
    unreadable commit.
    """
    halt_sha = resume.get("halt_sha") if isinstance(resume, dict) else None
    if not plan_path or not isinstance(halt_sha, str) or not _FULL_SHA_RE.fullmatch(halt_sha):
        return {}
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir

    meta = commit_meta(resolve_git_common_dir(worktree_root), halt_sha)
    message = (meta or {}).get("message") or ""
    ids = sorted({
        i for m in _LANDS_CHUNKS_LINE.finditer(message)
        for i in re.split(r"[,\s]+", m.group("v")) if i
    })
    by_plan = _source_rows_by_plan(worktree_root, plan_path, ids)
    return {halt_sha: by_plan} if by_plan else {}


_HALTED_WALK_CAP = 500


def _checkpoint_rows_from(
    worktree_root: Path, plan_path: Optional[str], start_sha: object, exclude: frozenset = frozenset(),
) -> dict:
    """``{checkpoint sha: {plan: {row id}}}`` of the still-open rows same-plan checkpoints landed.

    One in-process walk (``checkpoint_stamp.checkpoint_rows_landed``, zero spawns) from
    ``start_sha`` for the plan's rows open on disk minus ``exclude``; each row maps to the
    newest checkpoint that named it. ``Checkpoint-Base:`` is not a stop: the wanted set, the
    plan's ``created:`` floor and ``_HALTED_WALK_CAP`` bound it. Empty for a bad sha or a plan
    with no open row.
    """
    if not plan_path or not isinstance(start_sha, str) or not _FULL_SHA_RE.fullmatch(start_sha):
        return {}
    wanted = frozenset(_open_row_ids(worktree_root, plan_path) - set(exclude))
    if not wanted:
        return {}
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import checkpoint_rows_landed, plan_created_floor

    walk = checkpoint_rows_landed(
        worktree_root, plan_path, start_sha.lower(), wanted,
        floor_epoch=plan_created_floor(_read_rel(worktree_root, plan_path) or ""),
        cap=_HALTED_WALK_CAP,
    )
    landed: dict = {}
    for sha, ids in walk.landed.items():
        by_plan = _source_rows_by_plan(worktree_root, plan_path, sorted(ids))
        if by_plan:
            landed[sha] = by_plan
    return landed


def _halted_run_checkpoint_rows(
    worktree_root: Path, plan_path: Optional[str], resume: object, exclude: frozenset = frozenset(),
) -> dict:
    """``_checkpoint_rows_from`` the ledger resume's ``halt_sha``; empty for a non-resume close."""
    halt_sha = resume.get("halt_sha") if isinstance(resume, dict) else None
    return _checkpoint_rows_from(worktree_root, plan_path, halt_sha, exclude)


def _earlier_fire_checkpoint_rows(
    worktree_root: Path, plan_path: Optional[str], terminal_sha: str, exclude: frozenset = frozenset(),
) -> dict:
    """``_checkpoint_rows_from`` the terminal commit's first parent: a non-resume close's earlier fires."""
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir

    meta = commit_meta(resolve_git_common_dir(worktree_root), terminal_sha)
    parents = (meta or {}).get("parents") or []
    return _checkpoint_rows_from(worktree_root, plan_path, parents[0] if parents else None, exclude)


def _checkpoint_attribution(worktree_root: Path, terminal_sha: str, plan_path: Optional[str], chunks: list) -> dict:
    """Chunk id -> sha of the run's own ``checkpoint(wave N)`` commit that landed its writes.

    Walks first-parent history in-process (zero spawns) from the terminal commit's
    parent while the subject is a checkpoint of this plan (``Checkpoint-Plan:``
    trailer), so the walk is bounded by the run. A commit lands a chunk's file when
    its blob differs from its parent's. Among the commits touching a chunk, one whose
    subject names the chunk id wins; otherwise the earliest. A chunk no checkpoint
    touched is absent: it stays a no-op row.
    """
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import _blob_at_commit

    common_dir = resolve_git_common_dir(worktree_root)
    meta = commit_meta(common_dir, terminal_sha)
    parents = (meta or {}).get("parents") or []
    sha = parents[0] if parents else None
    checkpoints: list = []  # newest first: (sha, parent, named ids)
    for _ in range(500):
        meta = commit_meta(common_dir, sha) if sha else None
        if meta is None:
            break
        message = meta.get("message") or ""
        if is_stamp_commit(message) and meta.get("parents"):
            sha = meta["parents"][0]
            continue
        subject = CHECKPOINT_SUBJECT.match((message.splitlines() or [""])[0].strip())
        plan = CHECKPOINT_PLAN_LINE.search(message)
        if not subject or not meta.get("parents") or (plan_path and (not plan or plan.group("v") != plan_path)):
            break
        named = {i.strip() for i in subject.group("ids").split(",") if i.strip()}
        checkpoints.append((sha, meta["parents"][0], named))
        sha = meta["parents"][0]
    out: dict = {}
    for chunk in chunks:
        hits = [
            (csha, named) for csha, parent, named in reversed(checkpoints)
            if any(_blob_at_commit(common_dir, csha, p) != _blob_at_commit(common_dir, parent, p) for p in chunk.paths)
        ]
        if hits:
            out[chunk.id] = next((h[0] for h in hits if chunk.id in h[1]), hits[0][0])
    return out


def _open_row_ids(worktree_root: Path, plan_rel: str) -> set:
    """Ids of the plan spine rows whose disposition is ``open`` on disk now."""
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.execute_plan_assemble.row_spans import _row_disposition

    text = _read_rel(worktree_root, plan_rel)
    if text is None:
        return set()
    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return set()
    try:
        rows = _yaml_load(located.body) or []
    except yaml.YAMLError:
        return set()
    if not isinstance(rows, list):
        return set()
    return {
        str(r.get("id")) for r in rows
        if isinstance(r, dict) and _row_disposition(r) == "open"
    }


def _review_only_coding(
    worktree_root: Path, terminal_sha: str, request: CommitRequest, inline_review: dict,
) -> tuple:
    """``({sha: {plan: {row id}}}, [row ids with no delta])`` for a review-only run.

    A review row is coded only when the marker recorded it ``open`` at emit, it is still
    ``open`` on disk, the inline review and judge passed, and some commit in
    ``run_base..terminal_sha`` changes a blob in its writes; it closes at the newest such
    commit. An open row no commit touched is reported, never closed. The walk is
    in-process (zero spawns) along first parents; a run base it never reaches (past the
    walk cap, on a merged-in branch, rewritten away) attributes nothing, and every
    still-open reviewed row then lands in the no-delta list.
    """
    if (
        not request.review_open_rows
        or not request.review_run_base
        or _reviewed_files_if_passed(inline_review) is None
    ):
        return {}, []
    criterion = inline_review.get("criterion")
    if isinstance(criterion, dict) and criterion.get("status") == "not_met":
        return {}, []
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import _blob_at_commit

    common_dir = resolve_git_common_dir(worktree_root)
    base = request.review_run_base
    delta: list = []  # newest first: (sha, parent)
    sha = terminal_sha
    reached = False
    for _ in range(_REVIEW_WALK_CAP):
        if sha.startswith(base):
            reached = True
            break
        meta = commit_meta(common_dir, sha)
        if meta is None or not meta.get("parents"):
            break
        delta.append((sha, meta["parents"][0]))
        sha = meta["parents"][0]
    if not reached:
        delta = []

    open_now: dict = {}
    coded: dict = {}
    no_delta: list = []
    wanted = set(request.review_open_rows)
    for chunk in request.chunks:
        if chunk.id not in wanted:
            continue
        by_plan = _source_rows_by_plan(worktree_root, request.plan_path, [chunk.id])
        live = {
            plan_rel: ids for plan_rel, ids in by_plan.items()
            if ids & open_now.setdefault(plan_rel, _open_row_ids(worktree_root, plan_rel))
        }
        if not live:
            continue
        hit = next(
            (
                csha for csha, parent in delta
                if any(
                    _blob_at_commit(common_dir, csha, p) != _blob_at_commit(common_dir, parent, p)
                    for p in chunk.paths
                )
            ),
            None,
        )
        if hit is None:
            no_delta.append(chunk.id)
            continue
        for plan_rel, ids in live.items():
            coded.setdefault(hit, {}).setdefault(plan_rel, set()).update(ids)
    return coded, no_delta


#: The engine's resolution ledger: one row per sizing a run landed whole, never committed.
SIZING_LEDGER_REL = ".coordinator-local/sizings.jsonl"


def _resolving_sizing(worktree_root: Path, plan_path: Optional[str]) -> Optional[dict]:
    """``{rel, id, tshirt, ask_sha256}`` for the run's cited sizing, else None. In-process reads."""
    rel = _cited_sizing(worktree_root, plan_path)
    text = _read_rel(worktree_root, rel) if rel else None
    if text is None:
        return None
    import hashlib

    import yaml

    try:
        doc = _yaml_load(text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    estimate = doc.get("estimate") if isinstance(doc.get("estimate"), dict) else {}
    words = "\n\n".join(
        str(v.get("text")) for v in doc.get("pm_verbatims") or [] if isinstance(v, dict) and v.get("text")
    ) or str(doc.get("intent") or "")
    return {
        "rel": rel,
        "id": doc.get("deliverable_id") or PurePosixPath(rel).stem,
        "tshirt": estimate.get("tshirt") or "?",
        "route": doc.get("route"),
        "ask_sha256": hashlib.sha256(words.encode("utf-8")).hexdigest(),
    }


def _record_resolution(worktree_root: Path, resolves: dict, sha: str) -> dict:
    """Append the sizing's resolution row (``{id, tshirt, route, ask_sha256, resolved_sha, ts,
    sizing}``) to ``SIZING_LEDGER_REL``. Never raises: the product commit stands."""
    from datetime import datetime, timezone

    row = {
        "id": resolves["id"],
        "tshirt": resolves["tshirt"],
        "route": resolves["route"],
        "ask_sha256": resolves["ask_sha256"],
        "resolved_sha": sha,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sizing": resolves["rel"],
    }
    try:
        from coordinator_core.atomic_append import append_line

        sink = worktree_root / SIZING_LEDGER_REL
        sink.parent.mkdir(parents=True, exist_ok=True)
        append_line(sink, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        return {"sizing_ledger_error": repr(exc)}
    return {"sizing_resolved": row}


def _ship_cited_sizing(worktree_root: Path, repo_root: Path, plan_path: Optional[str]) -> dict:
    """Flip the run's cited sizing to ``status: shipped`` through ``sizing.ship`` once its work
    has landed whole. ``{"sizing_shipped": rel}`` on a write, ``{}`` when nothing applies or it
    was already shipped, ``{"sizing_ship_skipped": reason}`` when ``sizing.ship`` refuses (a register
    not fully met, or a cascade-only sizing: it stays live and the deliverable cascade is the backstop);
    never raises -- the product commit stands. A register-bearing sizing ships here too: ``sizing.ship``
    runs ``requirement_register.ship_refusal`` itself over the rows ``_register_writeback`` just wrote.
    The sizing schema has no SHA field: the SHA rides the carrying commit's ``Sizing-Shipped`` trailer."""
    sizing_rel = _cited_sizing(worktree_root, plan_path)
    if sizing_rel is None:
        return {}
    try:
        reply = reentrant_dispatch("sizing.ship", {"sizing_path": sizing_rel}, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        return {"sizing_ship_skipped": repr(exc)}
    if not isinstance(reply, dict) or reply.get("exit_code") != 0:
        return {"sizing_ship_skipped": str(reply.get("error") if isinstance(reply, dict) else reply)}
    return {"sizing_shipped": sizing_rel} if reply.get("applied") else {}


def _recorded_fingerprints(
    worktree_root: Path, session_id: str, candidates: Sequence[str], all_paths: Sequence[str],
    deleted_paths: Sequence[str], *, excluded: set,
    reviewed: Optional[dict] = None, reviewed_at: Optional[float] = None,
    fixes_integrated_at: Optional[float] = None, wave_paths: frozenset = frozenset(),
    review_events: Optional[dict] = None,
) -> dict:
    """``{path: sha256}`` the run's expectation for the commit's candidate paths; one batched sink
    read. Per path the newer of the session's last recorded write and the run's newest review
    (``reviewed`` -> sha256 of the bytes it read, ``reviewed_at`` epoch seconds) supplies the hash;
    an exact tie goes to the session write. When the review wave applied fixes
    (``fixes_integrated_at``: epoch the wave's integration was recorded), a reviewed path whose
    bytes were last written no later than that moment, after the newest session write, is declared
    at those bytes; a later change keeps the older expectation and refuses. A path the wave's own
    record lists (``wave_paths``) counts as reviewed when no stored hash names it. A path whose
    newest session event postdates the review (``fixes_integrated_at`` when set, else
    ``reviewed_at``) and whose file mtime is no earlier than that event is declared at its current
    bytes: the run left them after review; a peer rewrite after that write is not detected. Candidates are DONE-chunk and run-owned paths; a live
    peer's claim on either already refused the call upstream, so no contest check runs here.
    ``review_events`` (``{path: (sha256, epoch)}``, ``_review_write_events``) is a review fix-pass
    agent's own newest write; it supplies the hash for a path whose session event is older or absent,
    so the run's own fix-pass write is the expectation and a later foreign edit still refuses.
    A hashless newest session event, or an unrecorded path outside the review, carries no entry."""
    from coordinator_core.session import touch_record

    committed = set(all_paths) - set(deleted_paths)
    wanted = [p for p in dict.fromkeys(candidates) if p in committed and p not in excluded]
    if not wanted:
        return {}
    seen = touch_record.last_seen_events(session_id, wanted, worktree_root)
    reviewed = reviewed if reviewed_at is not None and isinstance(reviewed, dict) else {}
    out: dict = {}
    for p in wanted:
        event = seen.get(p)
        review_hash = reviewed.get(p)
        if (review_hash or p in wave_paths) and fixes_integrated_at is not None and (event is None or fixes_integrated_at > event[1]):
            target = worktree_root / p
            try:
                settled = target.stat().st_mtime <= fixes_integrated_at
            except OSError:
                settled = False
            post_fix = touch_record.compute_content_hash(target) if settled else None
            if post_fix:
                out[p] = post_fix
                continue
        anchor = fixes_integrated_at if fixes_integrated_at is not None else reviewed_at
        if event is not None and anchor is not None and event[1] > anchor:
            target = worktree_root / p
            try:
                written_after = target.stat().st_mtime >= event[1]
            except OSError:
                written_after = False
            current = touch_record.compute_content_hash(target) if written_after else None
            if current:
                out[p] = current
                continue
        if review_hash and (event is None or reviewed_at > event[1]):
            out[p] = review_hash
        elif event is not None and event[0]:
            out[p] = event[0]
        agent_event = (review_events or {}).get(p)
        if agent_event is not None and agent_event[0] and (event is None or agent_event[1] > event[1]):
            out[p] = agent_event[0]
    return out


def _review_write_events(worktree_root: Path, review_writes: Optional[dict]) -> dict:
    """``{path: (sha256, epoch)}`` of the newest hashed write event, in the sink of the agent
    ``review_writes`` names for that path, per path of ``review_writes``; one sink walk per
    distinct agent. A path whose agent's newest write is hashless, or whose sink is unreadable,
    has no entry."""
    if not review_writes:
        return {}
    from coordinator_core.session import core, touch_record

    hub = core.sessions_dir(str(worktree_root))
    if not hub:
        return {}
    by_agent: dict = {}
    for path, agent_id in review_writes.items():
        by_agent.setdefault(agent_id, set()).add(path)
    out: dict = {}
    for agent_id, paths in by_agent.items():
        newest: dict = {}
        try:
            for member in touch_record.discover_family(touch_record.sink_path(Path(hub) / ".agents" / agent_id)):
                for line in touch_record.iter_complete_lines(member.read_bytes()):
                    try:
                        ev = touch_record.decode_line(line)
                    except touch_record.MalformedRecordLine:
                        continue
                    if (
                        ev.verb == touch_record.VERB_TOUCH and ev.kind != touch_record.KIND_READ
                        and ev.path in paths and ev.timestamp >= newest.get(ev.path, (None, float("-inf")))[1]
                    ):
                        newest[ev.path] = (ev.content_hash, ev.timestamp)
        except OSError:
            continue
        out.update({p: v for p, v in newest.items() if v[0]})
    return out


def _fixes_integrated_at(worktree_root: Path, inline_review: object) -> tuple:
    """``(epoch, reviewed_paths)``: when the review wave's integration was recorded (its
    sidecar's mtime) and that sidecar's ``prep.slice_files``; ``(None, frozenset())`` when the
    wave applied no fixes or its sidecar does not resolve. The sidecar's mtime is the cutoff, so a
    later rewrite of that sidecar moves it forward; ``integration.sidecar`` is tried before the
    share-dir glob, which scans every session's directory."""
    if not isinstance(inline_review, dict):
        return None, frozenset()
    fixes = inline_review.get("fixes")
    if not (isinstance(fixes, int) and not isinstance(fixes, bool) and fixes > 0):
        return None, frozenset()
    integration = inline_review.get("integration")
    sidecar = integration.get("sidecar") if isinstance(integration, dict) else None
    candidates: list = []
    if isinstance(sidecar, str) and sidecar:
        candidates.append(worktree_root / sidecar)
    stem = inline_review.get("integration_stem")
    if isinstance(stem, str) and stem and "/" not in stem and "\\" not in stem:
        share = worktree_root / ".coordinator-local" / "subagent-share"
        try:
            candidates.extend(share.glob(f"*/{stem}.md"))
        except OSError:
            pass
    # A bookkeeping stem's record may carry no integration file: the reviewer's own sidecar then
    # records which paths its applied fixes touched (its ledger rows) and when it finished
    # (its recorded ``verified_at``, not the file's mtime, which any later touch moves).
    stamps: list = []
    for c in candidates:
        try:
            stamps.append((c.stat().st_mtime, c))
        except OSError:
            continue
    paths: set = set()
    wave_stamps: list = []
    listed = inline_review.get("wave_sidecar_paths")
    for w in listed if isinstance(listed, list) else []:
        if isinstance(w, str) and w:
            done_at, fixed = _reviewer_applied_fixes(worktree_root / w)
            if done_at is not None:
                wave_stamps.append(done_at)
                paths.update(fixed)
    if stamps:
        at, newest = max(stamps, key=lambda s: s[0])
        paths.update(_wave_slice_files(newest))
    elif wave_stamps:
        at = max(wave_stamps)
    else:
        return None, frozenset()
    return at, frozenset(paths)


_VERIFIED_AT_RE = re.compile(r"^findings_ledger:.*?\bverified_at:\s*['\"]?([0-9T:.\-]+Z?)", re.MULTILINE)
_LEDGER_BLOCK_RE = re.compile(r"^## Findings Ledger\b.*?^```json\s*\n(.*?)\n```", re.MULTILINE | re.DOTALL)


def _reviewer_applied_fixes(sidecar: Path) -> tuple:
    """``(completed_epoch, fixed_paths)`` from a review-wave reviewer's sidecar: the epoch of its
    frontmatter ``findings_ledger.verified_at`` and the ``file`` of each row of its
    ``## Findings Ledger`` block that is verified, not suspended or rejected, and changed text
    (a non-empty ``before`` or ``after``). A path merely named elsewhere in the sidecar is not
    fixed. ``(None, frozenset())`` when unreadable or unstamped."""
    try:
        text = sidecar.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, frozenset()
    stamp = _VERIFIED_AT_RE.search(text.split("\n---", 2)[0] if text.startswith("---") else "")
    done_at = _epoch_of(stamp.group(1) if stamp else None)
    if done_at is None:
        return None, frozenset()
    block = _LEDGER_BLOCK_RE.search(text)
    try:
        rows = json.loads(block.group(1)) if block else []
    except ValueError:
        rows = []
    fixed = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("verified") is not True:
            continue
        if row.get("status") in ("suspended", "rejected") or not (row.get("before") or row.get("after")):
            continue
        f = row.get("file")
        if isinstance(f, str) and f:
            fixed.add(f.replace("\\", "/"))
    return done_at, frozenset(fixed)


def _wave_slice_files(sidecar: Path) -> frozenset:
    """``prep.slice_files`` of the wave's on-disk integration record: the paths the wave reviewed.
    A run whose prep was a test-runner sidecar stores no ``reviewed_sha256``; this is then the
    only record of which paths the wave's fixes may have touched. Empty when unreadable."""
    import yaml

    from coordinator_core.frontmatter.primitives import split_frontmatter

    try:
        split = split_frontmatter(sidecar.read_text(encoding="utf-8"))
        fm = _yaml_load(split.fm_text) if split else None
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return frozenset()
    prep = fm.get("prep") if isinstance(fm, dict) else None
    files = prep.get("slice_files") if isinstance(prep, dict) else None
    return frozenset(f for f in files if isinstance(f, str)) if isinstance(files, list) else frozenset()


def _epoch_of(stamp: object) -> Optional[float]:
    """Epoch seconds of an ISO-8601 ``Z`` stamp, ``None`` when absent or unparseable."""
    if not isinstance(stamp, str):
        return None
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def _withhold_reasons(worktree_root: Path, artifacts, record_dir_rel: Optional[str]) -> dict:
    """``{path: reason}`` the EM withheld via ``workflow.revise withhold_paths``, read from this
    run's record dir and the superseded predecessor dirs ``artifacts`` already carries (a
    predecessor's entries are read first, so this run's own win). Empty with no bound sizing:
    ``superseded_runs`` then yields no predecessor, so only the run's own dir is read."""
    from coordinator_core.ops.dispatch_emit import run_withhold

    dirs = list(artifacts.record_dirs) if artifacts is not None else []
    if record_dir_rel and record_dir_rel not in dirs:
        dirs.insert(0, record_dir_rel)
    return run_withhold.read(worktree_root / d for d in reversed(dirs))


USAGE_FILE = "usage.json"


def _write_usage(worktree_root: Path, session_id: Optional[str], run_id: str) -> dict:
    """Write ``state/warp/<run_id>/usage.json`` (``scan_run`` shape, ``as_of: terminal-commit``,
    ``unsettled`` agent ids) and return ``{total_tokens, usd}``, else ``{usage_total: None, usage_reason}``.

    Never raises: a run dir that does not resolve uniquely, or any I/O failure, leaves no file."""
    if not session_id:
        return {"usage_total": None, "usage_reason": "no session_id"}
    try:
        from coordinator_core.usage import agent_usage, run_locate

        session_dir = run_locate.session_dir_for(session_id)
        wf = run_locate.locate_run_dir(session_dir, run_id) if session_dir is not None else None
        if wf is None:
            return {"usage_total": None, "usage_reason": "run dir not located"}
        scan = agent_usage.scan_run(str(wf))
        body = {
            **scan,
            "as_of": "terminal-commit",
            "unsettled": sorted(run_locate._unsettled_agent_ids(wf / "journal.jsonl")),
        }
        target = worktree_root / RECORD_DIR_ROOT / run_id / USAGE_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        replace_text(target, json.dumps(body, indent=2, sort_keys=True) + "\n")
        total = scan["run_total"]
        return {"usage_total": {"total_tokens": total["total_tokens"], "usd": total["usd"]}}
    except Exception as exc:  # noqa: BLE001 -- usage is a report, never a commit gate
        return {"usage_total": None, "usage_reason": f"usage scan failed: {exc!r}"}


_RESOLUTION_SHA = re.compile(r"^[0-9a-f]{7,64}$")


def _run_id_of(params: dict, ask_run_dir: Optional[Path], script_path_raw: str) -> str:
    """Which run this commit is for: ``params.run_id``, else the ask run dir's name, else the
    script stem (a non-ask script's stem is its run id; an ask script's is ``<run_id>.workflow``)."""
    rid = params.get("run_id")
    if isinstance(rid, str) and rid:
        return rid
    if ask_run_dir is not None:
        return ask_run_dir.name
    return PurePosixPath(script_path_raw.replace("\\", "/")).stem


def _reconcile_prep(
    worktree_root: Path, session_id: Optional[str], run_id: Optional[str],
    request: CommitRequest, inline_review: object,
) -> tuple:
    """Replace the relayed ``inline_review["prep"]`` with the reply ``review.prep`` stored on disk
    for this run, in place, and point ``prep_sidecar`` at it.

    Disk truth the engine wrote wins over a review agent's copy of it, which may be trimmed
    (``product_files`` null, ``slice_files`` empty) and then reads as a zero-file diff. Does nothing
    when no base resolves or no single stored reply matches; the relay then stands.

    Returns ``(reviewed_sha256, recorded_at_epoch)`` of the stored reply -- the bytes the run's
    newest review read -- or ``({}, None)`` when there is no usable reply or timestamp.
    """
    none: tuple = ({}, None)
    if not isinstance(inline_review, dict) or not session_id or not run_id:
        return none
    prep = inline_review.get("prep")
    base = prep.get("run_base_sha") if isinstance(prep, dict) else None
    if not (isinstance(base, str) and _RESOLUTION_SHA.match(base)):
        from coordinator_core.ops.dispatch_emit.ask_fanout_contract import recorded_run_base

        base = recorded_run_base(worktree_root, run_id)
    if not base:
        return none
    from coordinator_core.ops import review_prep

    reply = review_prep.stored_reply_for_run(
        worktree_root, session_id, base, run_key=run_id,
        within=[p for c in request.chunks for p in c.paths], covering=True,
    )
    if reply is None:
        return none
    inline_review["prep"] = {
        "run_base_sha": reply["run_base_sha"],
        "product_files": reply["product_files"],
        "foreign_claims": reply["foreign_claims"],
        "slice_files": [f for s in reply["slices"] or [] if isinstance(s, dict) for f in s.get("files") or []],
    }
    inline_review["prep_sidecar"] = reply["sidecar_path"]
    reviewed = reply.get("reviewed_sha256")
    return (reviewed if isinstance(reviewed, dict) else {}), _epoch_of(reply.get("recorded_at"))


def _write_resolution(worktree_root: Path, sizing_rel: str, sha: str, run_id: str) -> dict:
    """Write ``resolution: {sha, run_id}`` beside the sizing's ``status: shipped``, in place and
    schema-checked. ``{}`` on a write, ``{"resolution_error": reason}`` otherwise; never raises --
    the product commit stands."""
    if not _RESOLUTION_SHA.match(sha or "") or not _RESOLUTION_RUN_ID.match(run_id or ""):
        return {"resolution_error": f"unrecordable resolution sha={sha!r} run_id={run_id!r}"}
    import yaml

    from coordinator_core.frontmatter.primitives import write_fm_nested_field
    from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter
    from coordinator_core.locked_write import MutateAbort, locked_rmw

    schema_path = Path(__file__).parent.parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"

    def mutate(old: str) -> str:
        new = write_fm_nested_field(old, "resolution", f'  sha: "{sha}"\n  run_id: "{run_id}"\n')
        try:
            errors = validate_frontmatter(_yaml_load(new) or {}, schema_path)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal
            raise MutateAbort(f"resolution write refused: {exc}")
        if errors:
            raise MutateAbort(f"resolution write refused: {format_validation_errors(errors)}")
        return new

    try:
        locked_rmw(worktree_root / sizing_rel, mutate, repo_root=worktree_root)
    except MutateAbort as exc:
        return {"resolution_error": str(exc.args[0]) if exc.args else "resolution write refused"}
    except Exception as exc:  # noqa: BLE001 -- lock timeout, I/O: refuse, keep the commit
        return {"resolution_error": f"resolution write refused: {exc!r}"}
    return {}


def _archive_in_run(worktree_root: Path, repo_root: Path, rels: tuple) -> tuple:
    """Rename the run's shipped sizing (``rels[0]``) and consumed baton (``rels[1:]``) under
    ``archive/`` in-process, through the sweep families' own classification. Returns
    ``([(old_rel, new_rel, Move)], [{"id", "reason"}])``: the renames done, and the targets a
    rail left live for the boot sweep. Spawns nothing and commits nothing."""
    from coordinator_core.ops.fleet import archive_sizings, archive_terminal_handoffs

    root = worktree_root.resolve()
    planned = []
    skipped = []
    for index, rel in enumerate(rels):
        if index == 0:
            move, reason = archive_sizings.plan_in_run_move(worktree_root, rel)
        else:
            move, reason = archive_terminal_handoffs.plan_in_run_move(worktree_root, repo_root, rel)
        if move is None:
            skipped.append({"id": rel, "reason": reason})
        else:
            planned.append((rel, move))
    done = []
    for rel, move in planned:
        _acted, failed = archive_terminal_handoffs.apply_sweep([move])
        if failed:
            skipped.extend(failed)
            continue
        done.append((rel, move.dst.relative_to(root).as_posix(), move))
    return done, skipped


def _undo_archive(archived: list) -> None:
    """Put back what ``_archive_in_run`` renamed when the carrying commit did not land. A
    byte-identical duplicate (``force``) already lived at the destination, so it is copied back."""
    import shutil

    for _old, _new, move in archived:
        try:
            if move.force:
                shutil.copy2(move.dst, move.src)
            else:
                os.replace(move.dst, move.src)
        except OSError:
            continue


#: Artifact classes the product commit carries; ``sizing`` rides the stamp commit (it is rewritten after
#: the product commit), ``plan`` only for an ask run (a plan-route DAG run stamps its plan itself).
_PRODUCT_ARTIFACT_CLASSES = frozenset({
    "record-dir", "evidence-sidecar", "seam-sidecar", "baton", "slice-sizing",
    "superseded-record", "docs-stage", "delivery-note",
})


def _differs_from_commit(worktree_root: Path, sha: str, rel: str) -> bool:
    """True when ``rel`` exists on disk and its content is not the blob ``sha`` holds for it.

    In-process (no spawn); a CRLF working copy of an LF blob counts as equal. Hashes sha1 only: a
    sha256-format repo always reads as differing.
    """
    import hashlib

    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import _blob_at_commit

    try:
        data = (worktree_root / rel).read_bytes()
    except OSError:
        return False
    head = _blob_at_commit(resolve_git_common_dir(worktree_root), sha, rel)
    if not isinstance(head, str):
        return True
    for body in (data, data.replace(b"\r\n", b"\n")):
        if hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest() == head:
            return False
    return True


def _stamp_coded_commit(
    commit_v2, worktree_root: Path, repo_root: Path, source_rows: dict,
    sha: str, session_id: Optional[str], also_commit: tuple = (),
    noop_rows: Optional[dict] = None, checkpoint_rows: Optional[dict] = None,
    snapshot: Optional[_Snapshot] = None, flip_plan: Optional[str] = None,
    receipt: Optional[str] = None,
    trailers: tuple = (), commit: bool = True, archive_after_flip: tuple = (),
    flip_reason: Optional[str] = None,
    outside_rows: Optional[dict] = None,
    ledger_met: Optional[str] = None,
    memo_closes: Optional[dict] = None,
) -> dict:
    """Second, plan-only ``commit_v2`` call stamping the product commit's
    rows ``coded`` with ``disposition_ref: <sha>`` -- a SHA only exists once
    the product commit has landed, so this cannot ride in it. One call per
    run, never per row. ``noop_rows`` (same shape as ``source_rows``) are done rows
    with no hunk in the commit: they close ``coded`` against the run's sha with a
    no-change ``disposition_detail``, so none stays ``open`` beneath a met run.
    ``checkpoint_rows`` (``{checkpoint sha: {plan: {row id}}}``) are done rows the run's
    own checkpoint commits landed: they close ``coded`` at that checkpoint's sha.
    ``outside_rows`` (``{plan: {row id: [landing sha, newest first]}}``) are done rows whose
    writes commits outside the run landed: they close ``coded`` at the newest landing sha
    with a no-op detail naming every landing sha. An edit that would make a schema-valid plan invalid
    is refused before any write; on any failure every plan edit is restored.
    ``memo_closes`` (``{plan: {row id: (receipt ref, receiver)}}``) are receipted memo-send rows:
    they close ``delivered_elsewhere`` via ``memo``, never ``coded``, in the same commit.
    ``flip_plan`` names the plan whose ``implemented`` status is written to disk without
    committing (``_stamp_plan_implemented`` reads the rows just written ``coded``), so
    the one commit below carries rows, minted stamp and status together; its reply
    returns under ``_flip``. A commit that does not land restores the flip with the rows.
    ``commit=False`` (an ask run whose plan is gitignored or unknown to the snapshot) leaves the
    stamps on disk and lands nothing. ``_committed`` names the paths the commit carried.
    ``receipt`` names the file holding the run's minted ``review_stamp``. When it is gitignored
    (a minted XS spine) the commit cannot carry it and the receipt dies with the scratch dir, so
    the reply adds ``coded_stamp_error`` beside the rest of the commit's result.
    ``archive_after_flip`` (the run's shipped sizing, then its consumed baton) is renamed under
    ``archive/`` only once the flip above reads ``implemented``; both ends of each rename ride this
    commit (``archived`` in the reply, a refusal in ``archive_skipped``). A plan that did not flip
    leaves them live for the boot sweep.
    Returns ``{"rows_coded", "coded_sha"}`` or ``{"coded_stamp_error"}``.
    """
    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source

    originals: dict = {}
    rows_coded: dict = {}
    archived: list = []

    def restore() -> None:
        for rel, text in originals.items():
            replace_text(worktree_root / rel, text)
        _undo_archive(archived)

    noop_rows = noop_rows or {}
    checkpoint_rows = checkpoint_rows or {}
    outside_rows = outside_rows or {}
    memo_closes = memo_closes or {}
    plans = set(source_rows) | set(noop_rows) | set(outside_rows) | set(memo_closes)
    for by_plan in checkpoint_rows.values():
        plans |= set(by_plan)
    for plan_rel in sorted(plans):
        original = _read_rel(worktree_root, plan_rel)
        if original is None:
            continue
        noop_ids = set(noop_rows.get(plan_rel, ())) - set(source_rows.get(plan_rel, ()))
        updated, flipped = _flip_rows_coded(
            original, set(source_rows.get(plan_rel, ())) | noop_ids, sha,
            {rid: _NOOP_DETAIL for rid in noop_ids},
        )
        for csha, by_plan in sorted(checkpoint_rows.items()):
            if plan_rel in by_plan:
                updated, more = _flip_rows_coded(updated, set(by_plan[plan_rel]), csha)
                flipped = sorted(set(flipped) | set(more))
        for rid, shas in sorted(outside_rows.get(plan_rel, {}).items()):
            updated, more = _flip_rows_coded(updated, {rid}, shas[0], {rid: _outside_landing_detail(shas)})
            flipped = sorted(set(flipped) | set(more))
        if plan_rel in memo_closes:
            updated, more = _close_memo_rows_delivered(updated, memo_closes[plan_rel])
            flipped = sorted(set(flipped) | set(more))
        if not flipped:
            continue
        invalid = check_plan_tasks_source(updated)
        if invalid is not None and check_plan_tasks_source(original) is None:
            restore()
            return {"coded_stamp_error": f"{plan_rel}: stamped spine fails schema: {invalid}"}
        replace_text(worktree_root / plan_rel, updated)
        originals[plan_rel] = original
        rows_coded[plan_rel] = flipped

    if not commit:
        return {"rows_coded": rows_coded, "coded_sha": None}
    # A plan `review_stamp.mint` just wrote rides this commit even when none of
    # its rows flipped (a re-fire over already-coded rows).
    paths = sorted(set(rows_coded) | set(also_commit))
    flip_result: Optional[dict] = None
    if flip_plan is not None and paths:
        before = _read_rel(worktree_root, flip_plan)
        flip_result = _stamp_plan_implemented(
            worktree_root, flip_plan, sha, reason=flip_reason, ledger_met=ledger_met,
        )
        if flip_result.get("plan_status") == "implemented":
            if before is not None:
                originals.setdefault(flip_plan, before)
            paths = sorted(set(paths) | {flip_plan})
    archive_skipped: list = []
    archive_deleted: list = []
    if archive_after_flip and flip_result is not None and flip_result.get("plan_status") == "implemented":
        archived, archive_skipped = _archive_in_run(worktree_root, repo_root, archive_after_flip)
        old = {o for o, _n, _m in archived}
        paths = sorted((set(paths) - old) | {n for _o, n, _m in archived})
        if snapshot is not None:
            snapshot.note_written([n for _o, n, _m in archived])
        archive_deleted = _tracked_at_head(worktree_root, sorted(old))
    # A path is stageable unless it is an untracked gitignored plan (a warp run
    # spine), which is never staged; the snapshot answers that without a spawn.
    ignored = set(snapshot.ignored_among(paths)) if snapshot is not None else set()
    paths = [p for p in paths if p not in ignored]
    uncovered = [p for p in paths if snapshot is None or not snapshot.covers(p)]
    if uncovered:
        stageable = _changed_paths(worktree_root, uncovered)
        if stageable is not None:
            paths = [p for p in paths if p not in uncovered or p in stageable]
    lost_receipt = (
        {"coded_stamp_error": f"{receipt}: review receipt is untracked and gitignored, so no commit "
         "can carry it; the run is unreceipted"}
        if receipt is not None and receipt not in paths else {}
    )
    if not paths:
        return {"rows_coded": {}, "coded_sha": None, **lost_receipt}
    n = sum(len(v) for v in rows_coded.values())
    message = f"mark {n} rows coded ({sha[:7]})" if n else f"review stamp ({sha[:7]})"
    if archive_deleted:
        message += "\n\narchived (moved under archive/): " + ", ".join(archive_deleted)
    if trailers:
        message += "\n\n" + "\n".join(trailers)
    params: dict = {"paths": paths, "message": message}
    if archive_deleted:
        params["deleted_paths"] = archive_deleted
    if snapshot is not None and not uncovered:
        params["ignored_untracked"] = []
    if session_id is not None:
        params["session_id"] = session_id
    try:
        reply = commit_v2(params, repo_root)
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        reply = {"committed": False, "error": repr(exc)}
    if not isinstance(reply, dict) or not reply.get("committed"):
        restore()
        detail = reply.get("error") if isinstance(reply, dict) else reply
        return {"coded_stamp_error": f"coded-stamp commit did not land: {detail!r}"}
    out = {"rows_coded": rows_coded, "coded_sha": reply.get("sha"), "_committed": paths, **lost_receipt}
    if flip_result is not None:
        out["_flip"] = flip_result
    if archived:
        out["archived"] = {o: n for o, n, _m in archived}
    if archive_skipped:
        out["archive_skipped"] = archive_skipped
    if reply.get("index_stale"):
        out["coded_index_stale"] = list(reply["index_stale"])
    return out


def _parse_prefix_claims(report_text: str) -> Optional[list]:
    """Parse a DONE-report's ``created-under-prefix:`` claim list.

    Mirrors the executor contract emit.py's ``_prefix_claim_field`` states:
    one claim per line, each line ``created-under-prefix: <path>`` (or
    ``created-under-prefix: none`` for an explicit empty claim). Returns the
    (possibly empty) list of claimed paths, or ``None`` when the report
    carries NO ``created-under-prefix:`` line at all -- the "report has no
    list" refusal case D3 names.
    """
    claims: list = []
    saw_label = False
    for raw_line in report_text.splitlines():
        line = raw_line.strip().lstrip("-*").strip()
        if not line.startswith(PREFIX_CLAIM_LABEL):
            continue
        saw_label = True
        rest = line[len(PREFIX_CLAIM_LABEL) :].strip().strip("`'\"")
        if not rest or rest.lower() == "none":
            continue
        claims.append(rest)
    if not saw_label:
        return None
    return claims


def _own_prefix_files(worktree_root: Path, chunk, report_cache: dict) -> Optional[list]:
    """Files ``chunk``'s own report claims under ITS OWN declared prefixes.

    Returns ``None`` when the chunk declares prefixes but its report carries
    no ``created-under-prefix:`` list at all (the refusal case), or when the
    report file itself cannot be read. Returns ``[]`` when the report
    explicitly claims nothing (``created-under-prefix: none`` / no claim
    lines beyond that). A claimed file sitting OUTSIDE every one of the
    chunk's own prefixes is silently excluded -- only files under the
    chunk's OWN prefix are trusted, per D3.
    """
    if not chunk.prefixes:
        return []
    if chunk.report in report_cache:
        report_text = report_cache[chunk.report]
    else:
        try:
            report_text = (worktree_root / chunk.report).read_text(encoding="utf-8")
        except OSError:
            report_text = None
        report_cache[chunk.report] = report_text
    if report_text is None:
        return None
    claims = _parse_prefix_claims(report_text)
    if claims is None:
        return None
    # Match on whole path segments: a bare prefix ``src/foo`` must not admit
    # the leading-string sibling ``src/foobar/x``.
    bounded = [
        (prefix, prefix if prefix.endswith("/") else prefix + "/")
        for prefix in chunk.prefixes
    ]
    return [
        path
        for path in claims
        if any(path == prefix or path.startswith(under) for prefix, under in bounded)
    ]


def _expanded_prefix_files(
    worktree_root: Path, chunk, snapshot: Optional["_Snapshot"], session_id: Optional[str],
    touched: frozenset, *, left_out: Optional[dict] = None,
) -> list:
    """Existing changed files under ``chunk``'s own ``writes_under`` prefixes that ``touched``
    (this run's own touched set) names and no live peer contests, read from the pre-commit
    ``snapshot`` (no git spawn). Empty with no snapshot, no session id or no prefixes. Admission
    is the run's touched set, never a session claim: a sibling run's file under a shared prefix
    is not staged and surfaces in ``undeclared_dirty``. ``session_id`` only keys the live-peer
    contest. ``left_out``, when given, receives the ``{path: [sid]}`` it dropped as contested."""
    if snapshot is None or not session_id or not chunk.prefixes:
        return []
    found = sorted(
        p
        for p in snapshot.changed_among(list(chunk.prefixes))
        if p in touched and (worktree_root / p).is_file()
    )
    if not found:
        return []
    from coordinator_core.session import scope as session_scope

    root_str = str(worktree_root)
    contested = session_scope.contested_by_live_peers(found, session_id, cwd=root_str)
    if contested is None:
        return []
    if left_out is not None:
        left_out.update({p: list(contested[p]) for p in found if p in contested})
    return [p for p in found if p not in contested]


def _with_expansion(claimed: Optional[list], expanded: list) -> Optional[list]:
    """The report's claimed prefix files joined with the snapshot expansion; ``None`` (the
    no-claim-list refusal) only when the report has no claim list and the expansion is empty."""
    if claimed is None:
        return list(expanded) or None
    return claimed + [p for p in expanded if p not in claimed]


def _stage_returns(inline_review: dict) -> Optional[dict]:
    """The run record's stage-return fields out of the digest's
    ``inline_review``, or ``None`` when the digest predates them (no ``prep``
    block). Maps the digest's names onto the record keys ``review_stamp.mint``
    reads."""
    if not isinstance(inline_review.get("prep"), dict):
        return None
    out: dict = {k: inline_review.get(k) for k in ("prep", "delivery", "tests", "criterion")}
    not_run = inline_review.get("delivery_not_run")
    if isinstance(not_run, str) and not_run:
        out["delivery_not_run"] = not_run
    wave_not_run = inline_review.get("wave_not_run")
    if isinstance(wave_not_run, list) and wave_not_run:
        out["wave_not_run"] = wave_not_run
    fixes = inline_review.get("fixes")
    if isinstance(fixes, int) and not isinstance(fixes, bool):
        out["fixes_applied"] = fixes
    integration = inline_review.get("integration")
    if isinstance(integration, dict):
        out["integration_sidecar"] = integration.get("sidecar")
        out["unresolved"] = integration.get("unresolved") or []
        out["confinement_violations"] = integration.get("confinement_violations") or 0
    return out


def _routed_from_manifest(manifest_path: Optional[Path]) -> dict:
    """``{id: engine root}`` the stage manifest routed; ``{}`` when unreadable (fail-open to the
    cwd-only gate)."""
    if manifest_path is None:
        return {}
    from coordinator_core.ops.dispatch_emit.engine_owned_rows import routed_roots

    try:
        return routed_roots(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# Closed list: a corpus the run machinery writes must be named here, or a declared write into it counts as product.
_PROCESS_STATE_CORPORA = frozenset({"sizings", "warp", "handoffs", "apm-rulings", "queue-grind"})


def _is_process_state(rel: str) -> bool:
    """True for a ``state/`` path the run machinery itself writes: a file directly under
    ``state/`` or anything under a process corpus. Any other ``state/<corpus>/...`` record is
    product the chunk produced."""
    if not rel.startswith("state/"):
        return False
    parts = rel.split("/")
    return len(parts) < 3 or parts[1] in _PROCESS_STATE_CORPORA


def _manifest_declared(manifest_path: Optional[Path]) -> tuple:
    """``(declared product paths, manifest head_sha)`` from one read of the manifest: the `writes`
    of its rows minus run-record paths and the run's own process state (``_is_process_state``), and
    the staging HEAD (None when absent or not a full sha). A write into a state corpus such as
    ``state/bug-backlog/`` is product. ``([], None)`` when the manifest is unreadable."""
    if manifest_path is None:
        return [], None
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        rows = data.get("rows")
    except (OSError, ValueError, AttributeError):
        return [], None
    skip = (RECORD_DIR_ROOT.rstrip("/") + "/", RUN_DIR_ROOT.rstrip("/") + "/")
    paths: list = []
    for row in rows if isinstance(rows, list) else ():
        writes = row.get("writes") if isinstance(row, dict) else None
        for w in writes if isinstance(writes, list) else ():
            if isinstance(w, str) and w and not w.startswith(skip) and not _is_process_state(w) and w not in paths:
                paths.append(w)
    head = data.get("head_sha")
    return paths, head if isinstance(head, str) and _FULL_SHA_RE.fullmatch(head) else None


def verified_only_rows(resume: object) -> dict:
    """``{row_id: {command, observed}}`` from a ledger resume's ``verified_only``: rows whose only
    remainder was a check, discharged by one that ran. An entry missing either string is no
    exemption -- a bare claim is what the unchanged-since-halt guard exists to refuse."""
    raw = resume.get("verified_only") if isinstance(resume, dict) else None
    out: dict = {}
    for row_id, ev in (raw.items() if isinstance(raw, dict) else ()):
        if not isinstance(ev, dict):
            continue
        command, observed = ev.get("command"), ev.get("observed")
        if isinstance(command, str) and command.strip() and isinstance(observed, str) and observed.strip():
            out[str(row_id)] = {"command": command.strip(), "observed": observed.strip()}
    return out


def _every_staged_row_landed(resume: object) -> bool:
    """True when a ledger resume carries ``unlanded`` as an empty list: every open plan row was
    landed before the halt (checkpoint or halt commit, landed-writes gate) or credited, so no
    declared path owes a diff. An absent or non-list ``unlanded`` is no evidence."""
    raw = resume.get(RESUME_LEDGER_UNLANDED_FIELD) if isinstance(resume, dict) else None
    return isinstance(raw, list) and not raw


def _withheld_work_pending(worktree_root: Path, resume: object, declared: Sequence[str]) -> bool:
    """True when a declared path the halt withheld (``run_artifacts.withheld`` of the resume, else
    of the halted run's ``halted.json``) is dirty or untracked: the halt left that product in the
    tree with the halt state's own bytes, so ``git diff <halt_sha>`` reads it as unchanged while
    it is exactly the work this refire has to land."""
    if not isinstance(resume, dict):
        return False
    arts = resume.get("run_artifacts")
    withheld = arts.get("withheld") if isinstance(arts, dict) else None
    if not isinstance(withheld, dict):
        run_id = resume.get("run_id")
        from coordinator_core.ops._path_guard import safe_id

        if not (isinstance(run_id, str) and safe_id(run_id)):
            return False
        from coordinator_core.ops.dispatch_emit.halted_run import halted_withheld

        withheld = halted_withheld(worktree_root / RECORD_DIR_ROOT / run_id)
    pending = [p for p in declared if p in withheld]
    if not pending:
        return False
    status = run_git(
        ["--literal-pathspecs", "status", "--porcelain", "--untracked-files=all", "--", *pending],
        cwd=str(worktree_root),
    )
    return status.returncode == 0 and bool(status.stdout.strip())


def _unchanged_since_halt_failures(
    worktree_root: Path, resume: object, verdict: object, manifest_path: Optional[Path],
    plan_rel: Optional[str] = None, detail: Optional[dict] = None,
) -> list:
    """``[{id, reason}]`` for each blocking row of a ledger resume that ``verdict`` judges met or
    post_commit while no declared product path differs from the diff base. One ``git diff
    --quiet`` against the worktree, run only when such a row exists. The base is the first that
    resolves per ``halt_baseline.since_halt_base``: ``since_sha`` (``param``); the run's first
    ledger-check halt (``resolved``); the 40-hex ``halt_sha`` (``param``); the manifest's
    ``head_sha``, the HEAD the refire's staging stamped (``stage-head``). No base, an empty
    declared set or any git exit other than 0 or 1 refuses the same rows. A resume whose
    ``unlanded`` is an empty list owes no diff and refuses nothing (``_every_staged_row_landed``).
    When ``detail`` is given and the rows are refused it receives ``{base, base_source, outcome}``,
    ``outcome`` one of ``unchanged``, ``no-base``, ``no-declared-paths``, ``git-exit-<n>``.
    On the unchanged refusal, rows whose met_by names a commit at or before the base are recorded
    in ``detail["cited"]`` and, when they cover every refused row, stand as the landing."""
    blocking = resume.get("blocking") if isinstance(resume, dict) else None
    items = verdict.get("items") if isinstance(verdict, dict) else None
    if not isinstance(blocking, list) or not isinstance(items, list):
        return []
    judged = [
        str(i["id"]) for i in items
        if isinstance(i, dict) and i.get("id") in blocking and i.get("status") in ("met", "post_commit")
    ]
    exempt = verified_only_rows(resume)
    credited = pre_halt_credit.credited_rows(resume, verdict)
    discharged = _em_discharged_rows(resume, items) if judged else frozenset()
    post_commit = {
        str(i["id"]) for i in items if isinstance(i, dict) and i.get("status") == "post_commit"
    }
    unverified = credited & post_commit
    judged = [
        i for i in judged
        if i not in exempt and (i not in credited or i in unverified) and i not in discharged
    ]
    if not judged or _every_staged_row_landed(resume):
        return []
    paths, stage_head = _manifest_declared(manifest_path)
    base, source = halt_baseline.since_halt_base(worktree_root, resume, plan_rel, stage_head)
    if unverified and base:
        verified = verified_post_commit_credit(worktree_root, base, items, unverified)
        judged = [i for i in judged if i not in verified]
        if not judged:
            return []
    outcome = "no-base" if base is None else "no-declared-paths" if not paths else "unchanged"
    if outcome == "unchanged":
        result = run_git(
            ["--literal-pathspecs", "diff", "--quiet", base, "--", *paths], cwd=str(worktree_root)
        )
        if result.returncode == 1:
            return []
        if result.returncode == 0:
            # `git diff` cannot see a file the remaining-work leg created and left unstaged.
            created = run_git(
                ["--literal-pathspecs", "ls-files", "--others", "--exclude-standard", "--", *paths],
                cwd=str(worktree_root),
            )
            if created.returncode == 0 and created.stdout.strip():
                return []
            if created.returncode == 0 and _withheld_work_pending(worktree_root, resume, paths):
                return []
            if created.returncode != 0:
                outcome = f"git-exit-{created.returncode}"
            elif detail is not None:
                owners = [
                    x for x in (plan_rel, resume.get("plan"), resume.get("run_id")) if isinstance(x, str)
                ]
                cited = cited_at_or_before(worktree_root, base, items, judged)
                if cited and all(i in cited for i in judged):
                    shas = set(cited.values())
                    detail[CITED_DETAIL_KEY] = cited
                    detail["landed_sha"] = next(iter(shas)) if len(shas) == 1 else base
                    detail["landed_paths"] = list(paths)
                else:
                    landed = _landed_before_halt(worktree_root, base, paths, owners)
                    if landed:
                        detail["landed_sha"] = landed
                        detail["landed_paths"] = list(paths)
        else:
            outcome = f"git-exit-{result.returncode}"
    if detail is not None:
        detail.update(base=base, base_source=source, outcome=outcome)
    return [{"id": i, "reason": UNCHANGED_SINCE_HALT_REASON} for i in judged]


_VERDICTS_HEADING = "## Verdicts"


def _verdicts_section(ledger_verdict: object, inline_review: dict) -> str:
    """The delivery note's ``## Verdicts`` section: the ledger checker's per-item status and the
    review, test and criterion results, rendered from the params the commit already holds."""
    def cell(value: object) -> str:
        return str(value if value not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")

    lines = [_VERDICTS_HEADING, "", "Written by dispatch.terminal_commit from the run's verdict params.", ""]
    items = ledger_verdict.get("items") if isinstance(ledger_verdict, dict) else None
    if isinstance(items, list) and items:
        lines += ["### Requirements ledger", "", "| item | status | essential | wired | remaining |", "|---|---|---|---|---|"]
        for item in items:
            if not isinstance(item, dict):
                continue
            remaining = item.get("remaining")
            lines.append(
                f"| {cell(item.get('id'))} | {cell(item.get('status'))} | {cell(item.get('essential'))} "
                f"| {cell(item.get('wired'))} | {cell('; '.join(map(str, remaining)) if isinstance(remaining, list) else None)} |"
            )
        lines.append("")
    prep = inline_review.get("prep") if isinstance(inline_review.get("prep"), dict) else {}
    delivery = inline_review.get("delivery") if isinstance(inline_review.get("delivery"), dict) else {}
    lines += [
        "### Review", "",
        f"- delivery verdict: {cell(delivery.get('verdict'))}; unbacked claims: {cell(delivery.get('claims_unbacked'))}",
        f"- product files: {cell(prep.get('product_files'))}; slices reviewed: {cell(inline_review.get('slices'))}; "
        f"fixes applied: {cell(inline_review.get('fixes'))}",
        "",
    ]
    tests = inline_review.get("tests") if isinstance(inline_review.get("tests"), dict) else {}
    lines += [
        "### Tests", "",
        f"- status: {cell(tests.get('status'))}; run: {cell(tests.get('run'))}; failed: {cell(tests.get('failed'))}",
        "",
    ]
    criterion = inline_review.get("criterion") if isinstance(inline_review.get("criterion"), dict) else {}
    lines += [
        "### Criterion", "",
        f"- status: {cell(criterion.get('status'))}",
        f"- observation: {cell(criterion.get('observation'))}",
        *([f"- reason: {cell(criterion.get('reason'))}"] if criterion.get("reason") else []),
        "",
    ]
    return "\n".join(lines)


def _write_note_verdicts(note_abs: Path, ledger_verdict: object, inline_review: object) -> None:
    """Append ``## Verdicts`` to the docs stage's delivery note, once. Never raises: the note is
    a record, and a failed append must not refuse the product commit."""
    if not isinstance(inline_review, dict):
        return
    try:
        text = note_abs.read_text(encoding="utf-8")
        if _VERDICTS_HEADING in text:
            return
        note_abs.write_text(
            text.rstrip("\n") + "\n\n" + _verdicts_section(ledger_verdict, inline_review),
            encoding="utf-8", newline="\n",
        )
    except (OSError, ValueError):
        return


def _awaiting_operator(open_rows: Sequence[dict]) -> list:
    """``[{id, row, command}]`` for each open ledger row classified ``awaiting_operator``."""
    return [{"id": r["id"], "row": r["row"], "command": r["command"]} for r in open_rows if "command" in r]


def _ledger_gate(
    worktree_root: Path, run_dir: Path, verdict: object, manifest_path: Optional[Path] = None,
    gated: Optional[Sequence[str]] = None, operator: Optional[dict] = None,
    plan_text: Optional[str] = None, inline_review: Optional[dict] = None,
    resume_ledger: object = None, plan_rel: Optional[str] = None,
) -> tuple[Optional[dict], object, list]:
    """(refusal or None, ledger, open) for a ledger-bearing ask run: the refusal when ``verdict``
    does not pass ``requirements_ledger.judge`` against the live register rows, an engine-routed
    item (``manifest_path``'s ``engine_rows``) included when it names another root than the one
    owed. ``gated`` is the manifest's gated chunk ids; a ``not_met`` row whose ``withheld_by`` names
    one passes and comes back in ``open`` (``[{id, reason}]``), as does a non-essential one.
    A run emitted without a ledger is not gated (``(None, None, [])``). The pass is one gate among
    the review stage's; it discharges none of them. ``plan_rel`` is the request's plan, the
    since-halt check's halt-commit lookup when ``resume_ledger`` names none; a since-halt refusal
    carries ``since_halt: {base, base_source, outcome}``. A since-halt refusal closes as already
    landed when every refused row is in ``since_halt["cited"]`` (its met_by names a pre-halt commit)."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    try:
        ledger = requirements_ledger.read_ledger(worktree_root, run_dir)
    except requirements_ledger.LedgerRefused as exc:
        return _error(
            f"the run's bound sizing is unreadable: {exc}. Restore the sizing and re-fire",
            refused="ledger-unreadable",
        ), None, []
    if ledger is None:
        return None, None, []
    if not isinstance(verdict, dict):
        return _error(
            "the run is ledger-bearing but params.ledger_verdict is absent: the final ledger "
            "checker's verdict gates this commit. Re-run the run's ledger check and pass its "
            "verdict, or revise with workflow.revise and re-run the check",
            refused="ledger-unjudged",
        ), ledger, []
    result = requirements_ledger.judge(
        ledger, verdict, _routed_from_manifest(manifest_path), gated=gated, operator=operator,
        repo_root=worktree_root, plan_text=plan_text,
        wave_reviews=[w for w in (inline_review or {}).get("wave_reviews") or [] if isinstance(w, dict)],
        noted_paths=requirements_ledger.noted_paths(worktree_root, run_dir),
    )
    since_halt: dict = {}
    if resume_ledger is not None:
        resume_ledger = pre_halt_credit.with_notes(resume_ledger, requirements_ledger.read_notes(run_dir))
        stale = _unchanged_since_halt_failures(
            worktree_root, resume_ledger, verdict, manifest_path, plan_rel, since_halt
        )
        if stale:
            result = {**result, "pass": False, "failures": [*result["failures"], *stale]}
    if result["pass"]:
        return None, ledger, result.get("open") or []
    foreign = [
        str(f["id"]) for f in result["failures"] if f["reason"] == requirements_ledger.NOT_A_ROW_REASON
    ]
    message = "the requirements are not met: " + "; ".join(
        (f"{f['id']}: {f['reason']}" if f["id"] is not None else f["reason"]) for f in result["failures"][:10]
    )
    if foreign:
        message += f". Verdict ids that are not register rows: {', '.join(foreign)}"
    landed = _since_halt_close(since_halt, result["failures"], verdict)
    if landed:
        return {
            "committed": True, "sha": landed, "already_landed": True,
            "paths": since_halt["landed_paths"], "since_halt": since_halt,
        }, ledger, []
    return _error(
        message,
        refused="ledger-unmet",
        ledger_failures=result["failures"],
        ledger_items=len(ledger.items),
        all_of_pm_ask=ledger.all_of_pm_ask,
        awaiting_operator=_awaiting_operator(result["open"]),
        **({"since_halt": since_halt} if since_halt else {}),
    ), ledger, []


def _ledger_writeback(worktree_root: Path, ledger, verdict: object, run_id: str, sha: str) -> dict:
    """Write the passed verdict onto the bound sizing's register rows; never raises -- the
    product commit stands. ``{}`` or ``{"requirements_writeback_error": reason}``, plus
    ``post_commit_owed`` (``[{id, acts, sha}]``) when a ``post_commit`` item still owes a sha memo."""
    if ledger is None or not ledger.sizing_rel or not isinstance(verdict, dict):
        return {}
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    out: dict = {}
    try:
        requirements_ledger.apply_verdict(worktree_root, ledger.sizing_rel, verdict, run_id=run_id, sha=sha)
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        out["requirements_writeback_error"] = str(exc)
    owed = requirements_ledger.post_commit_owed(verdict, sha)
    if owed:
        out["post_commit_owed"] = owed
    return out


def _ask_trailers(
    worktree_root: Path, plan_path: Optional[str], record_abs: Optional[Path], receipts: list
) -> list:
    """The trailers an ask run's product commit carries in place of the process records it
    leaves out: ``Falsifier: <pytest node>`` (the plan's falsifier), ``Review: <sha256>`` (the
    review record under ``.coordinator-local/``) and one ``Completion-Receipt: <id>`` each."""
    import hashlib

    from coordinator_core.completion_receipts.model import RECEIPT_TRAILER

    out = []
    plan_text = _read_rel(worktree_root, plan_path) if plan_path else None
    if plan_text is not None:
        from coordinator_core.ops.dispatch_emit.emit import _prime_exit_criterion_falsifier

        falsifier = _prime_exit_criterion_falsifier(plan_text) or {}
        node = _PYTEST_NODE.search(str(falsifier.get("how") or ""))
        if node:
            out.append(f"Falsifier: {node.group(0)}")
    if record_abs is not None:
        try:
            out.append(f"Review: {hashlib.sha256(record_abs.read_bytes()).hexdigest()}")
        except OSError:
            pass
    out.extend(f"{RECEIPT_TRAILER}: {fm['receipt_id']}" for fm, _prose in receipts)
    return out


def _mint_review_stamp(
    worktree_root: Path, plan_rel: Optional[str], sha: str, record_abs: Path, record: dict,
    tree: Optional[str] = None,
) -> dict:
    """Mint the plan's ``review_stamp`` against the commit this op just
    landed. A refusal is reported, never raised: the product commit stands."""
    if not plan_rel:
        return {}
    guarded = contained_path(worktree_root / plan_rel, [worktree_root])
    if guarded is None:
        return {}
    from coordinator_core.ops.review_stamp import MintRefusal, mint

    try:
        mint(
            guarded, worktree_root, build_test_path=None, resolved=(sha, record_abs, record),
            criterion_open_ok=True, terminal_tree_sha=tree,
        )
    except MintRefusal as exc:
        return {"review_stamp": "refused", "review_stamp_refusal": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        return {"review_stamp": "refused", "review_stamp_refusal": repr(exc)}
    return {"review_stamp": "minted"}


def _falsifier_broken_tells(marks: object, plan_path: Optional[str]) -> Optional[list]:
    """The fired tells when the run's pre-dispatch review called ``plan_path``'s
    falsifier BROKEN, else None. ``marks`` is the digest's ``[{plan, tells}]``;
    plans are matched as repo-relative POSIX paths, so a ``./`` or backslash
    spelling on either side still matches."""
    if not plan_path or not isinstance(marks, list):
        return None

    def norm(p: str) -> str:
        p = p.replace("\\", "/")
        return p[2:] if p.startswith("./") else p

    want = norm(plan_path)
    for mark in marks:
        key = mark.get("plan") if isinstance(mark, dict) else None
        tells = mark.get("tells") if isinstance(mark, dict) else None
        if isinstance(key, str) and norm(key) == want:
            return [str(t) for t in tells] if isinstance(tells, list) else []
    return None


def _criterion_reason(inline_review: object) -> Optional[str]:
    """The judge's ``reason`` off the run record's criterion, or None when absent."""
    criterion = inline_review.get("criterion") if isinstance(inline_review, dict) else None
    reason = criterion.get("reason") if isinstance(criterion, dict) else None
    return reason.strip() or None if isinstance(reason, str) else None


def _stamp_plan_implemented(
    worktree_root: Path, plan_rel: str, sha: str, *, reason: Optional[str] = None,
    ledger_met: Optional[str] = None,
) -> dict:
    """Write the plan's ``implemented`` status to disk, uncommitted, when the
    review stamp it now carries records a ``met`` criterion; the caller's commit
    carries it. The verdict is read off the plan on disk, never a param, so only
    a minted stamp can discharge it. A refusal (open spine rows, the goal gate)
    is reported and writes nothing. The refusal text is kept from its head: the
    cause leads it and the override boilerplate trails. ``reason`` is the judge's own reason,
    appended to the stamp prose only; the met check never reads it. ``ledger_met`` (the
    ``_ledger_met_observation`` line) discharges an undecided criterion (absent or not ``met``/
    ``not_met``) with source "requirements ledger"; a ``not_met`` criterion still refuses. An
    ``indeterminate`` stamp is likewise discharged at the transition's ``criterion_refusal`` by
    the same ``ledger_met``, forwarded to it."""
    import contextlib
    import io

    import yaml

    from coordinator_core.archive_stamp import cs_stamp_plan_implemented
    from coordinator_core.ops.plan_status_transition import _FALSIFIER_OUTPUT_MAX

    plan_abs = worktree_root / plan_rel
    text = _read_rel(worktree_root, plan_rel)
    from coordinator_core.frontmatter.primitives import split_frontmatter

    split = split_frontmatter(text.replace("\r\n", "\n")) if text is not None else None
    try:
        fm = (_yaml_load(split.fm_text) or {}) if split is not None else {}
    except yaml.YAMLError:
        fm = {}
    stamp = fm.get("review_stamp") if isinstance(fm, dict) else None
    criterion = stamp.get("criterion") if isinstance(stamp, dict) else None
    status = criterion.get("status") if isinstance(criterion, dict) else None
    if status == "met":
        observation = str(criterion.get("observation") or "").strip()
        source = criterion.get("sidecar") or "the run's criterion leg"
    elif ledger_met and ledger_met.strip() and status != "not_met":
        observation, source = ledger_met.strip(), "requirements ledger"
    else:
        return {"plan_status": "not-stamped", "plan_status_reason": "review_stamp criterion is not met"}
    if not observation:
        return {"plan_status": "not-stamped", "plan_status_reason": "criterion observation is empty"}
    err = io.StringIO()
    recorded = isinstance(fm, dict) and fm.get("exit_criterion_met") is not None
    record = (
        {}
        if recorded
        else {
            "falsifier_verdict": "pass",
            "falsifier_output": observation[:_FALSIFIER_OUTPUT_MAX],
            "prose": f"criterion met at {sha} per {source}" + (f"; {reason}" if reason else ""),
        }
    )
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = cs_stamp_plan_implemented(
            str(plan_abs), **record, refuse_open_spine_rows=True, commit=False, ship_sha=sha,
            ledger_met=ledger_met if status != "met" else None,
        )
    if rc != 0:
        reason = err.getvalue().strip()[:600]
        return {"plan_status": "refused", "plan_status_refusal": reason, "plan_status_reason": reason}
    return {"plan_status": "implemented"}


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


def _peer_held(
    worktree_root: Path, session_id: Optional[str], paths: list, *, run_id: Optional[str] = None
) -> set:
    """The paths another session's touch claim, or a live peer run's declared writes, holds: a
    peer's in-flight edit sitting in one of this run's directories, not this run's residue.
    ``run_id`` keeps the caller's own run from holding its own declared paths. No git spawn."""
    if not paths:
        return set()
    held: set = set()
    if session_id:
        from coordinator_core.session import claim_index

        try:
            answer = claim_index.classify_paths(session_id, paths, cwd=str(worktree_root))
        except Exception:
            return set()
        held = {p for p, o in answer.by_path.items() if o.verdict == claim_index.OWNERSHIP_PEER}
    from coordinator_core.ops import warp_run_deliverable

    try:
        run_claims = warp_run_deliverable.live_run_write_claims(
            worktree_root, paths, exclude_session=session_id or "", cwd=str(worktree_root),
            exclude_run=run_id,
        )
    except Exception:
        run_claims = None
    return held | set(run_claims or ())


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


def _own_run_seniority(worktree_root: Path, run_id: object, script_path_raw: object = None) -> Optional[tuple]:
    """`(minted_at, run_id)` of the committing run's claimed token. A plan run mints no token: its
    emitted script's mtime is then the moment it was minted, keyed by the script stem. None when
    neither resolves. Seniority only drops a junior peer's declaration; blob-drift still refuses
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
    try:
        return (script.stat().st_mtime, key)
    except OSError:
        pass
    from datetime import datetime

    from coordinator_core.ops.dispatch_emit.emission_receipt import _read_receipt

    receipt = _read_receipt(script)
    try:
        return (datetime.fromisoformat(receipt["emitted_at"]).timestamp(), key)
    except (TypeError, KeyError, ValueError):
        return None


def _land_own_left_behind(
    reply: dict, product_sha: str, session_id: str, worktree_root: Path, repo_root: Optional[Path],
    withheld: Sequence[str] = (),
) -> None:
    """Land the session's uncontested left-behind claims (``reply["own_left_behind"]["left"]``) in
    a follow-on commit and record it under ``own_carry.REPLY_KEY``. A refusal is recorded as
    ``{"error": ...}`` and never un-lands the product commit; nothing left makes no call.
    ``withheld`` paths (foreign-excluded, or beside a live sibling run's declared path) are never
    carried."""
    from coordinator_core.ops.dispatch_emit import own_carry
    from coordinator_core.session import scope as session_scope

    info = reply.get("own_left_behind")
    left = info.get("left") if isinstance(info, dict) else None
    if not left:
        return
    contested = session_scope.contested_by_live_peers(list(left), session_id, cwd=str(worktree_root))
    if contested is None:
        return
    try:
        carry = own_carry.own_carry_params(list(left), {*contested, *withheld}, product_sha, session_id)
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


def _in_scope(path: str, request: CommitRequest) -> bool:
    """Whether ``path`` matches one of ``_scope_pathspecs(request)``."""
    parent = path.rpartition("/")[0]
    for chunk in request.chunks:
        if any(p.rpartition("/")[0] == parent for p in chunk.paths):
            return True
        if any(path.startswith(prefix.rstrip("/") + "/") for prefix in chunk.prefixes):
            return True
    return False


def _stopped_run_carried(reply: dict) -> set:
    """Paths in commit_v2's ``stopped_run_residue.carried`` reply, normalised to ``/``; empty when
    the key is absent or malformed."""
    block = reply.get("stopped_run_residue")
    carried = block.get("carried") if isinstance(block, dict) else None
    if not isinstance(carried, dict):
        return set()
    return {
        p.replace("\\", "/")
        for ps in carried.values() if isinstance(ps, (list, tuple))
        for p in ps if isinstance(p, str)
    }


def _undeclared_dirty(
    worktree_root: Path, request: CommitRequest,
    snapshot: Optional[_Snapshot] = None, landed: frozenset = frozenset(),
    *, run_id: Optional[str] = None, full_out: Optional[set] = None,
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
    """``(files, deletions)`` beside the run's declared writes (``_in_scope``) that join the one
    commit_v2 call, read from the pre-commit snapshot; both empty with no snapshot or session id.
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
    in_scope = sorted(p for p in snapshot.changed if p not in taken and _in_scope(p, request))
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
    if contested is None:
        return [], []
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


def _review_since(
    worktree_root: Path, run_id: Optional[str], inline_review: object
) -> Optional[float]:
    """Epoch seconds the run began: its ``submission.json`` ``submitted_at``, else the committer
    time of its base commit (the review prep's ``run_base_sha``, else the recorded run base), read
    in-process. ``None`` when neither resolves."""
    started = _run_started_at(worktree_root, run_id)
    if started is not None:
        return started
    prep = inline_review.get("prep") if isinstance(inline_review, dict) else None
    base = prep.get("run_base_sha") if isinstance(prep, dict) else None
    if not (isinstance(base, str) and _RESOLUTION_SHA.match(base)) and run_id and _RESOLUTION_RUN_ID.match(run_id):
        from coordinator_core.ops.dispatch_emit.ask_fanout_contract import recorded_run_base

        base = recorded_run_base(worktree_root, run_id)
    if not (isinstance(base, str) and _RESOLUTION_SHA.match(base)):
        return None
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir

    meta = commit_meta(resolve_git_common_dir(worktree_root), base)
    epoch = (meta or {}).get("committer_epoch")
    return float(epoch) if isinstance(epoch, int) and not isinstance(epoch, bool) else None


def _resumed_review_source(
    worktree_root: Path, inline_review: object, run_id: Optional[str], session_id: Optional[str]
) -> tuple:
    """``(run_id, session_id)`` whose review writes this landing reads: the halted run named by
    ``inline_review[RESUMED_RUN_ID_KEY]`` with its recorded emitter, when that id is safe and its
    record dir exists; else the caller's own pair."""
    from coordinator_core.ops._path_guard import safe_id

    resumed = inline_review.get(RESUMED_RUN_ID_KEY) if isinstance(inline_review, dict) else None
    if not (isinstance(resumed, str) and resumed and safe_id(resumed)):
        return run_id, session_id
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        if not record_dir(worktree_root, resumed).is_dir():
            return run_id, session_id
    except ValueError:
        return run_id, session_id
    from coordinator_core.ops.dispatch_emit.halted_run import recorded_emitter

    return resumed, recorded_emitter(worktree_root, resumed) or session_id


def _review_edit_verdict(
    worktree_root: Path, writes: Optional[dict], snapshot: Optional[_Snapshot],
    session_id: Optional[str], run_id: Optional[str], committed: set,
    sibling_claims: Optional[dict] = None,
    own_files: Optional[frozenset] = None,
):
    """``review_edits.partition`` over the review stage's writes against the pre-commit snapshot.
    Without a snapshot the dirt cannot be read, so the verdict is unanswerable. ``sibling_claims``
    ({path: run id} an unlanded sibling run claims) joins the live siblings' declared paths.
    ``own_files`` bounds residue: a write outside it belongs to another run of the session and is
    neither landed nor halted."""
    from coordinator_core.ops.dispatch_emit import review_edits

    if writes is not None and own_files is not None:
        writes = {k: v for k, v in writes.items() if review_edits._norm(k) in own_files}

    if writes is None or snapshot is None:
        return review_edits.partition(
            None, changed=set(), ignored=(), committed=set(), sibling_declared={},
            contested=set(), present=lambda _p: False,
        )
    paths = [review_edits._norm(k) for k in writes]
    inside = [p for p in paths if not review_edits._out_of_worktree(p)]
    candidates = [p for p in inside if p in snapshot.changed and p not in committed]
    contested: set = set()
    if candidates and session_id:
        from coordinator_core.session import scope as session_scope

        found = session_scope.contested_by_live_peers(candidates, session_id, cwd=str(worktree_root))
        contested = set(found) if found else set()
    return review_edits.partition(
        writes,
        changed=snapshot.changed,
        ignored=snapshot.ignored_among(inside),
        committed=committed,
        sibling_declared={**(sibling_claims or {}), **review_edits.live_sibling_declared(worktree_root, run_id)},
        contested=contested,
        present=lambda p: (worktree_root / p).is_file(),
    )


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


_STATUS_FIELD = r'^[ \t>#-]*"?[*_`]{0,2}status[*_`]{0,2}[ \t]*:[ \t]*[*_`]{0,2}[ \t]*'
_DELIVERED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}DONE(?:_WITH_CONCERNS)?[*_`]{0,2}:|<exit-status>DONE</exit-status>'
    rf'|(?i:{_STATUS_FIELD}DONE(?:_WITH_CONCERNS)?\b)',
    re.MULTILINE,
)
_UNDELIVERED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}(?:PARTIAL|BLOCKED)[*_`]{0,2}:|<exit-status>(?:PARTIAL|BLOCKED)</exit-status>'
    rf'|(?i:{_STATUS_FIELD}(?:PARTIAL|BLOCKED)\b)',
    re.MULTILINE,
)
_PARTIAL_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}PARTIAL[*_`]{0,2}:|<exit-status>PARTIAL</exit-status>'
    rf'|(?i:{_STATUS_FIELD}PARTIAL\b)',
    re.MULTILINE,
)
_UNDONE_LINE_RE = re.compile(r"^\s*[-*]?\s*[*_`]{0,2}(?:not done|undone|remaining)", re.IGNORECASE)
_CAUSE_HEADING_RE = re.compile(
    r"^#{1,6}\s+.*\b(?:why|cause|blocker|blocked|reason)\b", re.IGNORECASE
)
_UNDONE_CAP = 400


def _cause_section(lines: list) -> list:
    """The body of the first heading naming a cause/blocker/reason, up to the next heading."""
    for i, ln in enumerate(lines):
        if _CAUSE_HEADING_RE.match(ln.strip()):
            body = []
            for nxt in lines[i + 1:]:
                if nxt.lstrip().startswith("#"):
                    break
                if nxt.strip():
                    body.append(nxt.strip())
            return [ln.strip().lstrip("#").strip() + ":"] + body if body else []
    return []


def _undone_summary(text: str) -> str:
    """The report's named undone work: its ``Not done``/``Remaining`` lines, else its
    cause/blocker section, else its PARTIAL line."""
    lines = text.splitlines()
    picked = [ln.strip() for ln in lines if _UNDONE_LINE_RE.match(ln)]
    if not picked:
        picked = _cause_section(lines)
    if not picked:
        picked = [
            ln.strip() for ln in lines
            if re.match(r'\s*"?[*_`]{0,2}PARTIAL', ln) or re.match(_STATUS_FIELD + "PARTIAL", ln, re.I)
        ]
    return " | ".join(picked)[:_UNDONE_CAP]


def _partial_chunks(
    worktree_root: Path, request: CommitRequest, incomplete: set, withheld: Optional[dict] = None
) -> list:
    """Incomplete chunks whose executor itself reported DONE/DONE_WITH_CONCERNS: the row
    is incomplete only because the delivery verdict failed (an AC needing a later runtime
    step), and its files were reviewed with the wave. An executor PARTIAL or BLOCKED, an
    unreadable report, or one carrying no DONE status stays stranded; a PARTIAL row is
    recorded in ``withheld`` as ``{id: undone AC text}``."""
    out: list = []
    for chunk in request.chunks:
        if chunk.id not in incomplete or not chunk.report:
            continue
        text = _read_rel(worktree_root, chunk.report)
        if text is None:
            continue
        if _DELIVERED_REPORT_RE.search(text) and not _UNDELIVERED_REPORT_RE.search(text):
            out.append(chunk)
        elif withheld is not None and _PARTIAL_REPORT_RE.search(text):
            withheld[chunk.id] = _undone_summary(text)
    return out


def _reverted_paths(params: dict, inline_review: object) -> frozenset:
    """Repo-relative paths the run's test-runner or review recorded as reverted: the string
    entries of ``reverted`` under ``params.targeted_tests``, ``inline_review.tests`` and
    ``inline_review.integration``. Malformed input contributes nothing and never raises.
    A reverted path absent from the worktree is a revert, never a declared deletion."""
    holders: list = [params.get("targeted_tests")]
    if isinstance(inline_review, dict):
        holders += [inline_review.get("tests"), inline_review.get("integration")]
    found: set = set()
    for holder in holders:
        listed = holder.get("reverted") if isinstance(holder, dict) else None
        if isinstance(listed, list):
            found.update(p.replace("\\", "/") for p in listed if isinstance(p, str) and p)
    return frozenset(found)


def _reviewed_files_if_passed(inline_review: dict) -> Optional[set]:
    """The files the review wave covered (``prep.slice_files``), or ``None`` when the
    review did not pass: delivery verdict PASS, no unresolved finding, no confinement
    violation. Absent evidence is not a pass."""
    prep = inline_review.get("prep")
    delivery = inline_review.get("delivery")
    integration = inline_review.get("integration")
    unrostered = _delivery_not_rostered(inline_review)
    if not (isinstance(prep, dict) and (unrostered or isinstance(delivery, dict)) and isinstance(integration, dict)):
        return None
    files = prep.get("slice_files")
    if (
        (not unrostered and delivery.get("verdict") != "PASS")
        or integration.get("unresolved")
        or integration.get("confinement_violations")
        or not isinstance(files, list)
    ):
        return None
    return {f for f in files if isinstance(f, str)}


def _regradable_chunks(
    worktree_root: Path, request: CommitRequest, incomplete: set, inline_review: dict,
    report_cache: dict,
) -> dict:
    """``{chunk id: gate-blocker text}`` for executor-PARTIAL chunks the run's own review
    clears: the report carries a ``gate-blocker: outside-footprint`` line (the row's build
    gate went red only from errors outside its footprint), every declared and own-prefix
    file sits in ``prep.slice_files``, and the review passed. Prose that merely resembles
    the cause does not qualify; a chunk with no files never does."""
    reviewed = _reviewed_files_if_passed(inline_review)
    if reviewed is None:
        return {}
    out: dict = {}
    for chunk in request.chunks:
        if chunk.id not in incomplete or not chunk.report:
            continue
        text = _read_rel(worktree_root, chunk.report)
        if text is None or not _PARTIAL_REPORT_RE.search(text):
            continue
        blocker = gate_owed.match(text, gate_owed.KIND_OUTSIDE_FOOTPRINT)
        if blocker is None:
            continue
        prefix_files = _own_prefix_files(worktree_root, chunk, report_cache)
        if prefix_files is None:
            continue
        files = set(chunk.paths) | set(prefix_files)
        if files and files <= reviewed:
            out[chunk.id] = f"outside-footprint{blocker[1].rstrip()}"[:_UNDONE_CAP]
    return out


_BLOCKED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}BLOCKED[*_`]{0,2}:|<exit-status>BLOCKED</exit-status>'
    rf'|(?i:{_STATUS_FIELD}BLOCKED\b)',
    re.MULTILINE,
)


def _is_delivered_report(text: str) -> bool:
    """True for a DONE/DONE_WITH_CONCERNS report: ``_partial_chunks`` lands that row, not a regrade."""
    return bool(_DELIVERED_REPORT_RE.search(text)) and not _UNDELIVERED_REPORT_RE.search(text)


def _clean_review_files(inline_review: dict) -> Optional[set]:
    """``prep.slice_files`` when the review has no unresolved finding and no confinement
    violation, else ``None``. Unlike ``_reviewed_files_if_passed`` the delivery verdict is not read."""
    prep = inline_review.get("prep")
    integration = inline_review.get("integration")
    if not (isinstance(prep, dict) and isinstance(integration, dict)):
        return None
    files = prep.get("slice_files")
    if integration.get("unresolved") or integration.get("confinement_violations") or not isinstance(files, list):
        return None
    return {f for f in files if isinstance(f, str)}


def _ledger_met_ids(ledger_verdict: object) -> list:
    """Ids of the verdict items whose status is ``met`` or whose only owed acts the landing itself
    performs (``requirements_ledger.close_dischargeable``), in verdict order."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    items = ledger_verdict.get("items") if isinstance(ledger_verdict, dict) else None
    return [
        str(i["id"]) for i in (items if isinstance(items, list) else [])
        if isinstance(i, dict) and i.get("id") is not None
        and (i.get("status") == "met" or requirements_ledger.close_dischargeable(i))
    ]


def _lifecycle_owed(
    verdict: dict, sha: str, reply: dict, consumed_baton: Optional[str], close_ctx: Optional[dict],
) -> list:
    """``post_commit_owed`` entries once the close stamp ran: a lifecycle act counts as performed
    when the plan reads implemented, or the consumed baton was archived or shipped inline."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    done: set = set()
    if reply.get("plan_status") == "implemented":
        done.add("plan_implemented")
    legs = close_ctx.get("legs") if isinstance(close_ctx, dict) else None
    shipped = legs.get("baton-ship") if isinstance(legs, dict) else None
    if (consumed_baton and consumed_baton in (reply.get("archived") or {})) or (
        getattr(shipped, "status", None) in ("ran", "discharged")
    ):
        done.add("baton_archive")
    return requirements_ledger.post_commit_owed(verdict, sha, discharged=done)


def _blocking_open_rows(ledger: object, verdict: object, open_rows: object) -> list:
    """The open rows that keep ``_ledger_met_observation`` from stamping: every open row except an
    awaiting-operator row whose verdict item is explicitly ``essential: false``, under a ledger that
    is not ``all_of_pm_ask`` and whose row is not a design-conformance row (always essential)."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    items = verdict.get("items") if isinstance(verdict, dict) else None
    design = {
        str(i.id) for i in (getattr(ledger, "items", None) or ())
        if requirements_ledger._is_design(getattr(i, "anchor", None))
    }
    optional = {
        str(i.get("id")) for i in (items if isinstance(items, list) else [])
        if isinstance(i, dict) and i.get("essential") is False and str(i.get("id")) not in design
    } if not getattr(ledger, "all_of_pm_ask", False) else set()
    return [r for r in (open_rows or []) if not (isinstance(r, dict) and "command" in r and str(r.get("id")) in optional)]


def _ledger_met_observation(ledger: object, verdict: object, open_rows: object) -> Optional[str]:
    """``"ledger-met: <ids>"`` when a ledger-bearing run's passed gate left no blocking open row
    (an awaiting-operator row the verdict marks non-essential does not block) and the verdict names
    a ``met`` item; else None. Call only after ``_ledger_gate`` passed."""
    if ledger is None or _blocking_open_rows(ledger, verdict, open_rows):
        return None
    met = _ledger_met_ids(verdict)
    return ("ledger-met: " + ", ".join(met))[:_UNDONE_CAP] if met else None


def _ledger_not_stamped_reason(ledger: object, verdict: object, open_rows: object) -> Optional[str]:
    """Why a passed ledger gate yielded no ``ledger_met`` observation, for the close reply; None when
    the run is not ledger-bearing or an observation exists."""
    if ledger is None or _ledger_met_observation(ledger, verdict, open_rows) is not None:
        return None
    blocking = _blocking_open_rows(ledger, verdict, open_rows)
    if blocking:
        why = "; ".join(f"{r.get('id')}: {r.get('reason')}" for r in blocking)
        return f"exit_criterion_met not stamped: open ledger row(s) not met ({why})"[:_UNDONE_CAP]
    return "exit_criterion_met not stamped: the ledger verdict names no met row"


def _ledger_met_regrades(
    worktree_root: Path, request: CommitRequest, incomplete: set, inline_review: dict,
    report_cache: dict, ledger_verdict: object, held: set,
) -> dict:
    """``{chunk id: "ledger-met: <met item ids>"}`` for incomplete chunks a passed requirements
    ledger plus a clean review cover: the report is readable and not BLOCKED, the chunk is not held,
    and every declared and own-prefix file sits in ``prep.slice_files``. Call only once the ledger
    gate passed. The ledger is run-wide, not mapped to chunks: a PARTIAL report's undone text is not
    weighed."""
    met = _ledger_met_ids(ledger_verdict)
    blocked = set(held)
    files: dict = {}
    candidates = []
    for chunk in request.chunks:
        if chunk.id not in incomplete or not chunk.report:
            continue
        text = _read_rel(worktree_root, chunk.report)
        if text is not None and _is_delivered_report(text):
            continue
        candidates.append(chunk)
        prefix_files = _own_prefix_files(worktree_root, chunk, report_cache)
        if text is None or _BLOCKED_REPORT_RE.search(text) or prefix_files is None:
            blocked.add(chunk.id)
            continue
        files[chunk.id] = set(chunk.paths) | set(prefix_files)
    ids = credit_gate.ledger_regradable(
        [c.id for c in candidates], blocked, files, _clean_review_files(inline_review)
    )
    reason = ("ledger-met: " + ", ".join(met))[:_UNDONE_CAP]
    return {cid: reason for cid in ids}


def _credit_regrades(
    worktree_root: Path, artifacts, record_dir_rel: Optional[str], request: CommitRequest,
    incomplete: set, held: set, run_dir: Optional[Path] = None, sizing_rel: Optional[str] = None,
) -> dict:
    """``{chunk id: "credited: <evidence>"}`` for incomplete chunks the EM credited through
    ``workflow.revise credit_chunks`` (this run's record dir, its superseded predecessors', and any
    earlier run of ``sizing_rel``, committed or not; this run's own entries win). A credit overrides a
    BLOCKED report; a hold still bars it."""
    from coordinator_core.ops.dispatch_emit import run_credit

    dirs = list(artifacts.record_dirs) if artifacts is not None else []
    if record_dir_rel and record_dir_rel not in dirs:
        dirs.insert(0, record_dir_rel)
    paths = [worktree_root / d for d in reversed(dirs)]
    if run_dir is not None:
        paths.append(run_dir)
    credits = run_credit.read(paths)
    if sizing_rel:
        credits = {**run_credit.credits_for_sizing(worktree_root, sizing_rel), **credits}
    if not credits:
        return {}
    known = []
    for chunk in request.chunks:
        if chunk.id not in incomplete or chunk.id not in credits:
            continue
        text = _read_rel(worktree_root, chunk.report) if chunk.report else None
        if text is None or not _is_delivered_report(text):
            known.append(chunk.id)
    got = credit_gate.credit_regradable(known, credits, held)
    return {cid: f"credited: {evidence}"[:_UNDONE_CAP] for cid, evidence in got.items()}


def _is_closeout_path(rel: str, evidence_rel: Optional[str]) -> bool:
    """True for a plan evidence sidecar, an evidence index or a run process-state path
    (baton, sizing, run record): what a closeout row writes in place of product."""
    return rel == evidence_rel or rel.endswith(".evidence.yaml") or _is_process_state(rel)


def _xs_closeout_regrades(
    worktree_root: Path, request: CommitRequest, incomplete: set, snapshot: Optional[_Snapshot],
    report_cache: dict,
) -> dict:
    """``{chunk id: evidence}`` for incomplete chunks of an xs-arm run whose report is DONE and whose
    changed declared paths are all evidence/state paths: the closeout's own writes are its credited
    chunk. A chunk with no changed declared path, or any product path, never qualifies."""
    candidates = [c for c in request.chunks if c.id in incomplete and c.report and c.paths]
    if not candidates:
        return {}
    plan_pp = PurePosixPath(request.plan_path) if request.plan_path else None
    evidence_rel = str(plan_pp.with_name(plan_pp.stem + _EVIDENCE_SIDECAR_SUFFIX)) if plan_pp else None
    out: dict = {}
    for chunk in candidates:
        text = _read_rel(worktree_root, chunk.report)
        if text is None or not _is_delivered_report(text):
            continue
        prefix = _own_prefix_files(worktree_root, chunk, report_cache)
        if prefix is None:
            continue
        declared = sorted(set(chunk.paths) | set(prefix))
        changed = _changed(snapshot, worktree_root, declared)
        if changed and all(_is_closeout_path(p, evidence_rel) for p in changed):
            out[chunk.id] = "credited: xs closeout evidence/state writes"[:_UNDONE_CAP]
    return out


def _own_evidence_append_chunks(
    worktree_root: Path, request: CommitRequest, stranded_ids: set, carried: set,
    changed: Optional[set],
) -> set:
    """Stranded chunk ids whose own deliverable is the plan evidence append this commit carries.

    A chunk qualifies when its report is DONE, none of its declared paths differs from HEAD (the
    work its footprint forbids is the append itself) and the plan or its evidence sidecar is changed
    and in the commit. Unanswerable change state (``changed`` None) credits nothing."""
    if not request.plan_path or changed is None:
        return set()
    plan_pp = PurePosixPath(request.plan_path)
    evidence_rel = str(plan_pp.with_name(plan_pp.stem + _EVIDENCE_SIDECAR_SUFFIX))
    if not ({request.plan_path, evidence_rel} & carried & changed):
        return set()
    out: set = set()
    for chunk in request.chunks:
        if chunk.id not in stranded_ids or not chunk.report or set(chunk.paths) & changed:
            continue
        text = _read_rel(worktree_root, chunk.report)
        if text is not None and _is_delivered_report(text):
            out.add(chunk.id)
    return out


def _changed_paths(worktree_root: Path, paths: list) -> Optional[set]:
    """The subset of ``paths`` whose worktree bytes differ from HEAD (modified,
    added, untracked or deleted): the files a commit over them can carry. One
    scoped ``git status`` spawn; ``None`` when git could not answer."""
    if not paths:
        return set()
    result = run_git(
        [
            "-C", str(worktree_root), "--no-optional-locks", "status",
            "--porcelain", "-z", "--no-renames", "--untracked-files=all", "--",
            *[f":(literal){p}" for p in paths],
        ],
        binary=True,
    )
    if not result.ok:
        return None
    return {
        entry[3:].decode("utf-8", "surrogateescape")
        for entry in result.stdout_bytes.split(b"\0")
        if len(entry) > 3
    }


def _changed(snapshot: Optional[_Snapshot], worktree_root: Path, paths: list) -> Optional[set]:
    """``_changed_paths`` answered from ``snapshot`` where it covers a path, by a spawn for the rest."""
    if snapshot is None:
        return _changed_paths(worktree_root, paths)
    known = [p for p in paths if snapshot.covers(p)]
    rest = [p for p in paths if not snapshot.covers(p)]
    out = snapshot.changed_among(known)
    if rest:
        extra = _changed_paths(worktree_root, rest)
        if extra is None:
            return None
        out |= extra
    return out


def _commit_residue(
    worktree_root: Path, request: CommitRequest, snapshot: Optional[_Snapshot],
    report_cache: dict, session_id: Optional[str], *, excluded: set,
    unfinished: set, commit_v2_mod,
) -> dict:
    """``{path: reason}`` for every path a chunk declares (or claims by prefix) that the
    pre-commit read saw changed and the commit did not carry; ``excluded`` holds the paths
    the commit carried or legitimately dropped. A path the snapshot does not cover costs
    one scoped status spawn. The reason is the unfinished
    chunk ids declaring it, ``peer-held``, or ``unexplained``."""
    owners: dict = {}
    for chunk in request.chunks:
        for p in list(chunk.paths) + (_own_prefix_files(worktree_root, chunk, report_cache) or []):
            owners.setdefault(p, []).append(chunk.id)
    candidates = [p for p in owners if p not in excluded]
    if not candidates:
        return {}
    dirty = _changed(snapshot, worktree_root, candidates)
    if not dirty:
        return {}
    peer: list = []
    commit_v2_mod._peer_claim_warnings(worktree_root, sorted(dirty), peer, own_sid=session_id)
    peer_paths = {p for p, _sid in peer}
    out: dict = {}
    for p in sorted(dirty):
        ids = sorted(set(owners[p]) & unfinished)
        out[p] = (", ".join(ids) if ids else "peer-held" if p in peer_paths else "unexplained")
    return out


def _entangled_chunks(
    worktree_root: Path, request: CommitRequest, stranded_ids: set, report_cache: dict,
    snapshot: Optional[_Snapshot] = None,
) -> dict:
    """Committable chunks whose ``.py`` files import a file of a stranded chunk, transitively.

    Committing such a chunk lands an importer whose target is absent from the
    tree at that sha (``leak_gate`` / ``import_closure`` would refuse the whole
    commit). Only stranded files that differ from HEAD count: an unchanged one
    already resolves. Returns ``{chunk id: sorted ids of the stranded chunks it
    needs}``; a chunk needing only another entangled chunk names that chain's root.
    A stranded path a committable chunk also declares lands with that chunk, so it blocks no importer.
    """
    by_id = {c.id: c for c in request.chunks}
    declared: dict = {}
    prefix_roots: list = []
    for cid in stranded_ids:
        chunk = by_id.get(cid)
        if chunk is None:
            continue
        for path in chunk.paths:
            declared[path] = cid
        for prefix in chunk.prefixes:
            prefix_roots.append((prefix.rstrip("/"), cid))
    done_declared = {p for c in request.chunks if c.id not in stranded_ids for p in c.paths}
    specs = [p for p in declared if p.endswith(".py")] + [root for root, _ in prefix_roots]
    changed = _changed(snapshot, worktree_root, specs)
    blocked_files: dict = {}
    if changed is None:
        blocked_files = {p: c for p, c in declared.items() if p.endswith(".py")}
    else:
        for path in changed:
            if not path.endswith(".py"):
                continue
            if path in done_declared:
                continue
            if path in declared:
                blocked_files[path] = declared[path]
                continue
            for root, cid in prefix_roots:
                if path == root or path.startswith(root + "/"):
                    blocked_files[path] = cid
                    break
    if not blocked_files:
        return {}

    from coordinator_core.authoring_leaks.import_closure import _absolute_base, _needed_paths
    from coordinator_core.pyimports import collect_imports

    files_of: dict = {}
    imports_of: dict = {}
    for chunk in request.chunks:
        if chunk.id in stranded_ids:
            continue
        prefix_files = _own_prefix_files(worktree_root, chunk, report_cache) or []
        needed: set = set()
        for rel in list(chunk.paths) + list(prefix_files):
            text = _read_rel(worktree_root, rel) if rel.endswith(".py") else None
            if text is None:
                continue
            try:
                records, _ = collect_imports(ast.parse(text))
            except (SyntaxError, ValueError):
                continue
            for rec in records:
                rec_needed = _needed_paths([(rel, rec)])
                base = _absolute_base(rec, rel)
                if base and "." in base:
                    # `top/__init__.py` is only the gate's first-party probe here; the import binds it
                    # only when it names the top package itself.
                    rec_needed.discard(f"{base.split('.')[0]}/__init__.py")
                needed |= rec_needed
        imports_of[chunk.id] = needed
        files_of[chunk.id] = set(chunk.paths) | set(prefix_files)

    entangled: dict = {}
    grew = True
    while grew:
        grew = False
        for cid, needed in imports_of.items():
            if cid in entangled:
                continue
            roots = {blocked_files[p] for p in needed if p in blocked_files}
            for other, other_roots in entangled.items():
                if files_of[other] & needed:
                    roots |= set(other_roots)
            if roots:
                entangled[cid] = sorted(roots)
                grew = True
    return entangled


def _subject(contributing: list, fallback: str) -> str:
    ids = ", ".join(c.id for c in contributing)
    titles = "; ".join(c.title for c in contributing if c.title.strip())
    if not ids:
        return fallback
    return f"{ids}: {titles}" if titles else ids


def _regenerate_landing_allowlist(worktree_root: Path, paths: list) -> Optional[str]:
    """Repo-relative ``setup/publish-targets.portable`` when the commit's paths
    add a published-tree top-level name the landing allowlist does not
    classify and this call regenerated it (written to the worktree, to ride the
    commit); else ``None``.

    The pre-filter is `commit_paths`' own: a commit touching neither
    generator-owned dir, or a tree without the declarations yaml, pays no read
    and no spawn. A generator failure leaves the worktree untouched, so
    ``commit_paths`` refuses with its own message.
    """
    from coordinator_core.git import published_tree_classification as ptc

    touched = ptc.touched_published_names(paths)
    if not touched or not (worktree_root / ptc.DECLARATIONS_PATH).is_file():
        return None

    def read_worktree(rel: str) -> Optional[str]:
        try:
            return (worktree_root / rel).read_text(encoding="utf-8")
        except OSError:
            return None

    if not ptc.unclassified(touched, read_worktree):
        return None

    import importlib.util

    generator = worktree_root / "coordinator" / "bin" / "publish-allowlist-generate.py"
    try:
        from coordinator_core.bin_lib_binding import ensure_bin_lib_bound

        ensure_bin_lib_bound(str(generator.parent))
        spec = importlib.util.spec_from_file_location("publish_allowlist_generate", generator)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pending = tuple(p for p in paths if (worktree_root / p).is_file())
        regenerated = module.regenerate_portable(worktree_root, pending)
        portable = worktree_root / ptc.PORTABLE_PATH
        if regenerated == portable.read_text(encoding="utf-8"):
            return None
        portable.write_text(regenerated, encoding="utf-8", newline="\n")
    except Exception:  # noqa: BLE001 -- commit_paths refuses with its own message
        return None
    return ptc.PORTABLE_PATH


#: Script the close job runs as; ``None`` is ``ops/ceremony/close_background.py``. A budget
#: driver points it at a no-op so the gate measures the launch, not the job's own work.
CLOSE_JOB_SCRIPT: Optional[str] = None
_CLOSE_JOB_WINDOW_MINUTES = 5
_BACKGROUND_LEGS = ("review", "baton-drain", "orientation-refresh", "memo-lifecycle", "post-close")
_NOT_LANDED = "coded-stamp commit did not land"


def _leg(status: str, detail: str = "", root=None):
    """A ``LegResult`` whose detail is path-normalised: a path under *root* reads repo-relative,
    any other path-shaped token reads ``<path>``, and the rest of the text survives."""
    from coordinator_core.ops.dispatch_emit.run_close import LegResult, scrub_detail

    return LegResult(status, scrub_detail(detail, root))


def _plan_source_memo(worktree_root: Path, plan_rel: Optional[str]) -> Optional[str]:
    from coordinator_core.ops.dispatch_emit.source_memo_ref import resolve_source_memo

    return resolve_source_memo(worktree_root, plan_rel)


def _deferred_rows(worktree_root: Path, plan_rel: str) -> list:
    """Ids of the plan's spine rows marked ``deferred: true`` (harvest is not built here)."""
    text = _read_rel(worktree_root, plan_rel)
    if text is None:
        return []
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return []
    try:
        rows = _yaml_load(located.body) or []
    except yaml.YAMLError:
        return []
    return sorted(
        str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("deferred") is True
    ) if isinstance(rows, list) else []


def _run_own_baton(worktree_root: Path, run_id: str) -> Optional[str]:
    """The baton the run's gate minted and recorded, when it is a file inside the handoffs home."""
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton

    rel = read_run_baton(worktree_root, run_id)
    if rel is None:
        return None
    home = [Path(home_dir(str(worktree_root), "handoffs"))]
    target = contained_path(worktree_root / rel, home)
    return rel if target is not None and target.is_file() else None


def _pay_inline_legs(
    worktree_root: Path, ctx: dict, work_sha: str, session_id: Optional[str],
    snapshot: Optional[_Snapshot],
) -> list:
    """Run the ``completion-entry`` and ``baton-ship`` legs; returns the repo files they wrote,
    which the coded-stamp commit carries. ``ctx["rollback"]`` keeps what undoing them needs."""
    from coordinator_core.ops.dispatch_emit.completion_receipt import _handoff_claims
    from coordinator_core.ops.dispatch_emit.run_close_baton_ship import baton_ship_leg
    from coordinator_core.ops.dispatch_emit.run_close_completion import completion_entry_leg

    run_id, legs = ctx["run_id"], ctx["legs"]
    ctx["session_id"] = session_id
    criterion = ctx["inline_review"].get("criterion")
    criterion = criterion if isinstance(criterion, dict) else None
    try:
        result, entry_paths = completion_entry_leg(worktree_root, run_id, ctx["plan_rel"], criterion, ledger_met=ctx.get("ledger_met"))
    except Exception as exc:  # noqa: BLE001 -- a leg failure is reported, never raised
        result, entry_paths = _leg("failed", f"completion-entry raised {type(exc).__name__}"), []
    legs["completion-entry"] = result
    if result.status in ("ran", "discharged"):
        ctx["completion_entry"] = (entry_paths or [result.detail.removeprefix("entry ")])[0]

    batons = _handoff_claims(worktree_root, session_id)
    status = criterion.get("status") if criterion else None
    saved: dict = {}
    written = list(entry_paths)
    discharged = status == "met" or (bool(ctx.get("ledger_met")) and status != "not_met")
    if discharged:
        own = _run_own_baton(worktree_root, run_id)
        if own and own not in batons:
            batons = [*batons, own]
    ctx["batons"] = batons
    if batons and not discharged:
        legs["baton-ship"] = _leg("failed", f"criterion {status or 'absent'}: batons not shipped")
    else:
        saved = {rel: _read_rel(worktree_root, rel) for rel in batons}
        try:
            shipped = baton_ship_leg(worktree_root, run_id, batons, work_sha)
        except Exception as exc:  # noqa: BLE001 -- a leg failure is reported, never raised
            shipped = _leg("failed", f"baton-ship raised {type(exc).__name__}")
        legs["baton-ship"] = shipped
        if shipped.status == "ran":
            written += batons
        else:
            _restore_texts(worktree_root, saved)
            saved = {}
    ctx["rollback"] = {"entries": list(entry_paths), "saved": saved}
    if snapshot is not None:
        snapshot.note_written(written)
    return written


def _restore_texts(worktree_root: Path, saved: dict) -> None:
    for rel, text in saved.items():
        if text is not None:
            replace_text(worktree_root / rel, text)


def _launch_close_job(worktree_root: Path, run_id: str, batons: list, memo: Optional[str]) -> bool:
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

    engine = Path(__file__).resolve().parents[3]
    script = CLOSE_JOB_SCRIPT or str(engine / "coordinator_core" / "ops" / "ceremony" / "close_background.py")
    params = json.dumps({"claimed_batons": list(batons), "source_memo": memo})
    return spawn_detached(
        str(engine), script, [run_id, "--root", str(worktree_root), "--params", params],
        site_free=True,
    )


def _background_legs(worktree_root: Path, ctx: dict) -> dict:
    """Write the close-job record, launch the job, and report each background leg."""
    from datetime import datetime, timedelta

    from coordinator_core.ops.dispatch_emit.run_close import read_close_job, write_close_job

    run_id, batons = ctx["run_id"], ctx.get("batons") or []
    memo = _plan_source_memo(worktree_root, ctx["plan_rel"])
    legs: dict = {}
    queue: list = []
    for leg in _BACKGROUND_LEGS:
        if leg == "baton-drain" and not batons:
            legs[leg] = _leg("not-applicable", "run claims no baton")
        elif leg == "review" and not ctx.get("review_receipt"):
            continue
        elif leg in ctx["presets"]:
            legs[leg] = ctx["presets"][leg]
        elif leg == "memo-lifecycle" and not memo:
            legs[leg] = _leg("not-applicable", "plan names no source memo")
        else:
            queue.append(leg)
    deadline = datetime.now().astimezone() + timedelta(minutes=_CLOSE_JOB_WINDOW_MINUTES)
    try:
        write_close_job(
            worktree_root, run_id, queue, deadline,
            session_id=ctx.get("session_id"),
            plan_rel=ctx["plan_rel"],
            source_memo=memo,
            claimed_batons=batons,
            completion_entry=ctx.get("completion_entry"),
            settled={leg: ctx["legs"][leg] for leg in ("completion-entry", "baton-ship") if leg in ctx["legs"]},
            review_receipt=ctx.get("review_receipt"),
            run_base_sha=ctx.get("run_base_sha"),
            terminal_sha=ctx.get("terminal_sha"),
            criterion=_judged if isinstance(_judged := ctx["inline_review"].get("criterion"), dict) else None,
            falsifier_broken=ctx.get("falsifier_broken"),
        )
    except (OSError, ValueError) as exc:
        legs.update({leg: _leg("failed", f"close job record not written: {type(exc).__name__}") for leg in queue})
        return legs
    if not _launch_close_job(worktree_root, run_id, batons, memo):
        legs.update({leg: _leg("failed", "close job did not start") for leg in queue})
        return legs
    legs.update(read_close_job(worktree_root, run_id, datetime.now().astimezone(), queue))
    return legs


def _settle_close(worktree_root: Path, ctx: dict, landed: bool) -> None:
    """After the coded-stamp commit: forward lessons and launch the job when it landed, else
    undo the inline legs' files and report every leg that depended on it ``failed``."""
    from coordinator_core.ops.dispatch_emit.completion_receipt import _remove

    legs = ctx["legs"]
    if not landed:
        rollback = ctx.get("rollback") or {}
        _remove(worktree_root, rollback.get("entries", []))
        _restore_texts(worktree_root, rollback.get("saved", {}))
        for leg, result in list(legs.items()):
            if result.status == "ran":
                legs[leg] = _leg("failed", _NOT_LANDED)
        for leg in ("lessons", *_BACKGROUND_LEGS):
            legs[leg] = _leg("failed", _NOT_LANDED)
        ctx["completion_entry"] = None
        return
    from coordinator_core.ops.dispatch_emit.run_close_lessons import lessons_leg

    criterion = ctx["inline_review"].get("criterion")
    try:
        legs["lessons"] = lessons_leg(
            worktree_root, ctx["run_id"], criterion if isinstance(criterion, dict) else None
        )
    except Exception as exc:  # noqa: BLE001 -- a leg failure is reported, never raised
        legs["lessons"] = _leg("failed", f"lessons raised {type(exc).__name__}")
    legs.update(_background_legs(worktree_root, ctx))


def _inline_paid_legs(reply: dict, root) -> dict:
    """The land-run-only legs a landed terminal commit paid inline, read off its reply:
    ``ran`` for a minted review stamp, a coded-stamp commit and a shipped sizing; ``failed``
    for a skipped sizing ship. A leg the reply is silent on is absent."""
    paid: dict = {}
    if reply.get("review_stamp") == "minted":
        paid["review-stamp"] = _leg("ran", "review stamp minted")
    if reply.get("coded_sha"):
        paid["coded-stamp"] = _leg("ran", "coded-stamp commit landed")
    if reply.get("sizing_shipped"):
        paid["sizing-ship"] = _leg("ran", "sizing shipped")
    elif reply.get("sizing_ship_skipped"):
        paid["sizing-ship"] = _leg("failed", f"sizing ship skipped: {reply['sizing_ship_skipped']}", root)
    return paid


def _close_block(ctx: dict, reply: dict) -> dict:
    """The reply's ``close`` block: the settled inline and background legs, with ``review`` and
    ``plan-reconciliation`` read off the reply, and every leg nobody settled ``failed`` with why."""
    from coordinator_core.ops.dispatch_emit.run_close import LAND_RUN_ONLY_LEGS, RUN_BOUND_LEGS, build_close_block

    legs = dict(ctx["legs"])
    if ctx["applies"]:
        already_landed = (
            reply.get("committed") is not True
            and bool(reply.get("nothing_to_commit"))
            and not reply.get("refused")
        )
        if already_landed:
            na = _leg("not-applicable", "already landed: no fresh diff to commit")
            if "review" not in legs and reply.get("review_stamp") != "refused":
                legs["review"] = na
            if (
                "plan-reconciliation" not in ctx["presets"]
                and "plan-reconciliation" not in legs
                and not reply.get("plan_status")
            ):
                legs["plan-reconciliation"] = na
            for leg in RUN_BOUND_LEGS:
                legs.setdefault(leg, na)
        if reply.get("committed") is not True:
            why = f"terminal commit did not land ({reply['refused']})" if reply.get("refused") else "terminal commit did not land"
        elif "coded_stamp_error" in reply:
            why = _NOT_LANDED
        else:
            why = "leg did not run"
        stamp = reply.get("review_stamp")
        if "review" not in legs:
            if stamp == "refused":
                legs["review"] = _leg(
                    "failed", str(reply.get("review_stamp_refusal") or "review stamp refused"), ctx["root"]
                )
            else:
                legs["review"] = _leg("failed", f"no review stamp minted: {why}" if why != "leg did not run" else "no review stamp minted")
        status = reply.get("plan_status")
        if "plan-reconciliation" not in ctx["presets"] and status == "implemented":
            deferred = (
                [] if ctx["kind"] == "xs-s-run" else _deferred_rows(ctx["root"], ctx["plan_rel"])
            )
            legs["plan-reconciliation"] = (
                _leg("failed", "deferred rows unharvested: " + ", ".join(deferred))
                if deferred
                else _leg("ran", "plan implemented")
            )
        elif "plan-reconciliation" in ctx["presets"] or (
            already_landed and "plan-reconciliation" in legs
        ):
            pass  # the preset is applied below / the already-landed leg stands
        elif status:
            legs["plan-reconciliation"] = _leg(
                "failed", str(reply.get("plan_status_reason") or status), ctx["root"]
            )
        else:
            legs["plan-reconciliation"] = _leg("failed", f"plan not reconciled: {why}")
        landed_whole = reply.get("committed") is True and "coded_stamp_error" not in reply
        if landed_whole:
            legs.update({leg: result for leg, result in _inline_paid_legs(reply, ctx["root"]).items() if leg not in legs})
        for leg in RUN_BOUND_LEGS:
            if landed_whole and leg in LAND_RUN_ONLY_LEGS:
                continue  # unpaid on a landed run: build_close_block reads it not-applicable
            legs.setdefault(leg, _leg("failed", why))
        legs.update(ctx["presets"])
    block = build_close_block(ctx["run_id"], ctx["applies"], legs, ctx["completion_entry"], kind=ctx["kind"])
    sha = reply.get("sha")
    if ctx["applies"]:
        from coordinator_core.ops.dispatch_emit.run_close import sibling_release

        landed = reply.get("committed") is True and isinstance(sha, str) and bool(sha)
        siblings = sibling_release(
            Path(ctx["root"]), ctx.get("sibling_run_id") or ctx["run_id"], sha if landed else None
        )
        if siblings is not None:
            block["siblings"] = siblings
    return block


def _release_siblings_at_landing(close_ctx: Optional[dict], sha: str) -> None:
    """Mark the run landed in its sibling-status file the moment the product commit lands.

    Runs before every post-commit leg so a worker death after the commit cannot strand the run's
    ``after`` siblings; ``_close_block``'s later call rewrites landed as landed. Only when the close
    applies (the predicate ``_close_block`` uses). A failure here never fails the landed commit.
    """
    if not close_ctx or not close_ctx.get("applies"):
        return
    from coordinator_core.ops.dispatch_emit.run_close import sibling_release

    try:
        sibling_release(Path(close_ctx["root"]), close_ctx.get("sibling_run_id") or close_ctx["run_id"], sha)
    except Exception:  # noqa: BLE001 -- _close_block repeats the release; the commit stands
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
        return _handler_body(params, repo_root)


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
        h["chunk"]: h.get("reason") or ""
        for h in (held if isinstance(held, list) else [])
        if isinstance(h, dict) and isinstance(h.get("chunk"), str)
        and h["chunk"] not in _released_ids(params if isinstance(params, dict) else {})
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


def _held_ids(params: dict) -> set:
    held = params.get("held")
    return {
        h["chunk"] for h in (held if isinstance(held, list) else [])
        if isinstance(h, dict) and isinstance(h.get("chunk"), str)
    } - _released_ids(params)


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


def _terminal_commit(
    params: dict, repo_root: Optional[Path], stranded: dict, scope: dict,
    withheld: Optional[dict] = None, blockers: Optional[dict] = None,
    regraded: Optional[dict] = None,
    close_ctx: Optional[dict] = None,
) -> dict:
    """Run the commit and settle the run token it redeemed: a landed commit consumes the claim;
    every other return, and an exception raised before commit_v2 landed one, releases it."""
    token_state: dict = {}
    try:
        reply = _terminal_commit_inner(
            params, repo_root, stranded, scope, withheld, blockers, regraded, close_ctx, token_state
        )
    except BaseException:
        _settle_token(token_state, landed=bool(token_state.get("landed")))
        raise
    _settle_token(token_state, landed=bool(token_state.get("landed")))
    close = token_state.get("since_halt_close")
    if close is None:
        return reply
    if reply.get("committed"):
        return {**reply, "since_halt": close["since_halt"]}
    if reply.get("nothing_to_commit") and not reply.get("refused") and not reply.get("error"):
        return {**reply, **close, "nothing_to_commit": False}
    return reply


_CLAIM_POLL_SECS = 0.05


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
            end = time.monotonic() + max(0.0, left - dispatch_abandon.REPLY_MARGIN_SECS)
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
            params = {**_params_from_task_output(params["task_output_path"]), **{
                k: v for k, v in params.items() if k != "task_output_path"}}
        except ValueError as exc:
            return _error(str(exc))

    missing = _missing_run_outcome(params)
    if missing is not None:
        return missing

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
    anchor_only = not request.chunks
    reviewed_bytes, reviewed_at = _reconcile_prep(
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
        from coordinator_core.ops.dispatch_emit.run_close import kind_presets

        presets = kind_presets(kind, plan_governs=plan_governs)
        close_ctx.update(
            root=worktree_root,
            # run_id is the close-job record key every reader uses (the script stem);
            # sibling_run_id is the ask run's own id, which names its sibling-status file.
            run_id=PurePosixPath(script_path_raw.replace("\\", "/")).stem,
            sibling_run_id=_run_id_of(params, ask_run_dir, script_path_raw),
            applies=applies,
            plan_rel=request.plan_path,
            inline_review=inline_review if isinstance(inline_review, dict) else {},
            legs=dict(presets),
            completion_entry=None,
            kind=kind,
            presets=presets,
        )

    # Review at close is deterministic (PM ruling 2026-10-01): a run whose
    # result carries no review-stage output cannot land code, however the
    # script was composed or stripped.
    if inline_review is None:
        return _error(
            "params.inline_review is absent -- the run carries no review-stage "
            "output. Run the digest's `terminal_commit_cli` line verbatim (or pass "
            "task_output_path = the Workflow run's output file, the path named in its completion "
            "notification); else re-emit with a review stage and re-fire",
            refused="unreviewed",
        )
    if not inline_review.get("integration_stem") or inline_review.get("slices") is None:
        return _error(
            "params.inline_review is missing integration_stem and/or slices -- "
            f"no review-stage output to land against (got: {inline_review!r}). "
            "Use the digest's `terminal_commit_cli` line verbatim",
            refused="unreviewed",
        )
    ledger = None
    open_rows: list = []
    if ask_run_dir is not None:
        unmet, ledger, open_rows = _ledger_gate(
            worktree_root, ask_run_dir, params.get("ledger_verdict"), manifest_abs,
            [g.id for g in manifest.gated],
            {o.id: o.detail for o in manifest.owed if o.reason == "operator"},
            _read_rel(worktree_root, request.plan_path) if request.plan_path else None,
            inline_review,
            params.get(TERMINAL_RESUME_LEDGER_PARAM), request.plan_path,
        )
        if unmet is not None:
            if not (unmet.get("already_landed") and unmet.get("committed")):
                return unmet
            token_state["since_halt_close"] = unmet
    ledger_met = (
        _ledger_met_observation(ledger, params.get("ledger_verdict"), open_rows)
        if ask_run_dir is not None else None
    )
    if close_ctx is not None:
        close_ctx["ledger_met"] = ledger_met

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
            _write_note_verdicts(worktree_root / candidate, params.get("ledger_verdict"), inline_review)
    usage_reply: dict = (
        _write_usage(worktree_root, session_id, ask_run_dir.name)
        if ask_run_dir is not None and not anchor_only else {}
    )
    from coordinator_core.ops.dispatch_emit import run_artifacts

    art_run_id = ask_run_dir.name if ask_run_dir is not None else params.get("run_id")
    artifacts: Optional[run_artifacts.RunArtifacts] = None
    if not anchor_only and isinstance(art_run_id, str) and art_run_id:
        from coordinator_core.ops.dispatch_emit.halted_record_commit import within_handoffs_home

        artifacts = within_handoffs_home(worktree_root, run_artifacts.candidate_paths(
            worktree_root, art_run_id, sizing_rel=cited_sizing, plan_rel=request.plan_path,
            doc_paths=params.get(run_artifacts.DOC_PATHS_PARAM) or (),
        ))
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
            review_writes = {review_edits._norm(k): v for k, v in review_writes.items()}
    snapshot = _worktree_snapshot(
        worktree_root,
        [p for c in request.chunks for p in c.paths]
        + [p for p in (request.plan_path, plan_seam, bookkeeping_record_path) if p]
        + companions
        + ([ask_note_rel] if ask_note_rel else [])
        + (list(artifacts.paths()) if artifacts is not None else [])
        + [p for p in (review_writes or ()) if not review_edits._out_of_worktree(p)],
        [prefix for c in request.chunks for prefix in c.prefixes]
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
    held_ids = _held_ids(params)
    incomplete_chunks = incomplete_chunks | (held_ids & known_ids)
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
    partial_chunks = [c for c in partial_chunks if c.id not in held_ids]
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
    )

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

    if record_dir_rel is not None and snapshot is not None:
        from coordinator_core.ops.dispatch_emit.halted_record_commit import record_dir_changes

        present, _gone = record_dir_changes(worktree_root, record_dir_rel, snapshot.changed)
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
    if not all_paths and not art_has_landing:
        if close_ctx is not None:
            close_ctx.clear()
        return _with_siblings({"committed": False, "nothing_to_commit": not sibling_paths})

    if not anchor_only and artifacts is None:
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

    declared_writes = {p for c in done_chunks + partial_chunks for p in c.paths}
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
        return _with_siblings({"committed": False, "nothing_to_commit": not sibling_paths})

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
    deleted_paths = sorted(set(deleted_paths) | set(owned_deletions))
    scope["run_owned"] = list(run_owned)
    # A session's own `.git/` scratch (review-targets.txt) reaches the touched set but never a
    # tree: commit_paths refuses the whole commit on it.
    all_paths = [p for p in all_paths if canonical_repo_relative_path_refusal(p) is None]
    deleted_paths = [p for p in deleted_paths if canonical_repo_relative_path_refusal(p) is None]

    wh_reasons = {
        **_withhold_reasons(worktree_root, artifacts, record_dir_rel),
        **param_withheld,
        **_remedy.review_carried_withholds(request),
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
        if wh_chunks:
            reply["withheld_chunks"] = wh_chunks
        if wh_unmatched:
            reply["withhold_unmatched"] = wh_unmatched
        return reply

    if wh_map and not all_paths and not deleted_paths:
        scope.update(root=worktree_root, request=request, snapshot=snapshot)
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
    scope.update(root=worktree_root, request=scope_request, snapshot=snapshot)
    scope["stranded_residue"] = residue
    review_land: tuple = ()
    review_unanswerable: Optional[str] = None
    from coordinator_core.ops.dispatch_emit.review_edits import _norm as review_edits_norm

    sibling_claim_run_id = _run_id_of(params, ask_run_dir, script_path_raw)
    sibling_claims_seen: set = set()
    sibling_claims: dict = {}

    def claims_over(paths: Sequence[str]) -> dict:
        """Unlanded-sibling claims over `paths`, read once per path (an earlier answer is kept)."""
        from coordinator_core.ops.dispatch_emit import sibling_claims as sc

        fresh = {p for p in paths if p and p not in sibling_claims_seen}
        if fresh:
            sibling_claims_seen.update(fresh)
            sibling_claims.update(sc.sibling_run_claims(worktree_root, sibling_claim_run_id, fresh))
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
        verdict = _review_edit_verdict(
            worktree_root, review_writes, snapshot, session_id, review_run_id,
            set(all_paths) | set(deleted_paths), sibling_claims,
            own_files=frozenset(
                {review_edits_norm(p) for c in request.chunks for p in c.paths}
                | {review_edits_norm(p) for p in prefix_files}
                | {
                    review_edits_norm(p)
                    for p in ((inline_review.get("prep") or {}).get("slice_files") or ())
                    if isinstance(p, str)
                }
                | {review_edits_norm(p) for p in (artifacts.paths() if artifacts is not None else ())}
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
    # Only a path this commit actually carries can absorb a peer's edit: a clean declared path a
    # peer holds is not contention, and refusing on it wedges every review-only close behind it.
    carried = [p for p in all_paths if changed is None or p in changed] + list(deleted_paths)
    peer_holds: list = []
    commit_v2_mod._peer_claim_warnings(worktree_root, carried, peer_holds, own_sid=session_id)
    from coordinator_core.ops import warp_run_deliverable

    try:
        run_claims = warp_run_deliverable.live_run_write_claims(
            worktree_root, carried, exclude_session=session_id or "",
            cwd=str(worktree_root),
            minted_before=_own_run_seniority(worktree_root, params.get("run_id"), script_path_raw),
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
            contributing_chunks + contributing_partial,
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
    from coordinator_core.ops.dispatch_emit import landing_integrity as _li_close

    close_base = _li_close.run_base_for(worktree_root, _run_id_of(params, ask_run_dir, script_path_raw), params)
    if close_base is not None:
        trailers.append(f"Checkpoint-Run-Closed: {close_base}")
    review_base = request.review_run_base
    if isinstance(review_base, str) and review_base and review_base != close_base:
        trailers.append(f"Checkpoint-Run-Closed: {review_base}")
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

    def _claimed_or_held(candidates):
        """Sibling-claimed candidates plus any a held chunk declares: commit_v2's left-behind carry never takes them."""
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
        *declared_writes, *run_owned, *receipt_paths,
        *(p for p in (ask_note_rel, bookkeeping_record_path, cited_sizing, allowlist_path) if p),
    }
    if record_dir_rel:
        own_declared.update(
            p for p in (*all_paths, *deleted_paths) if p == record_dir_rel or p.startswith(record_dir_rel.rstrip("/") + "/")
        )
    all_paths, sibling_withheld = _sc.filter_own(
        all_paths, claims_over([*all_paths, *deleted_paths]), own_declared,
    )
    deleted_paths, withheld_deleted = _sc.filter_own(deleted_paths, sibling_claims, own_declared)
    sibling_withheld.update(withheld_deleted)
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

    # The product commit never carries the session's claimed leftovers: commit_v2 would widen the
    # pathspec past the declared set. They land in the follow-on commit after this one.
    commit_params: dict = {
        "paths": all_paths, "message": message,
        "carry_own_left_behind": False,
        "carry_stopped_runs": False,
    }
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

    integrity_extra = _landing_integrity_gate(
        worktree_root, all_paths, deleted_paths, record_abs, params,
        _run_id_of(params, ask_run_dir, script_path_raw), request.plan_path,
    )
    if integrity_extra.get("refused"):
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
        if sibling_withheld:
            integrity_extra["sibling_withheld"] = dict(sibling_withheld)
        return _error(
            f"landing refused {first['reason']}: {first['path']} ({first['detail']}){remedy}",
            **integrity_extra,
        )

    from coordinator_core.git import ref_cas_retry

    # Receipts are run-unique so no peer can touch them; the regenerated allowlist stays in the
    # compared paths because a peer's change to it must refuse rather than be overwritten.
    _own_receipts = set(receipt_paths)
    _overlap_paths = [p for p in (*all_paths, *deleted_paths) if p not in _own_receipts]

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
        _remove(worktree_root, receipt_paths)
        return _error("ceremony.commit_v2 is not registered")
    except BaseException:
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
                _remove(worktree_root, receipt_paths)
                return _error("ceremony.commit_v2 is not registered")
            except BaseException:
                _remove(worktree_root, receipt_paths)
                raise
            if not isinstance(reply, dict):
                reply = {"committed": False, "sha": None, "error": f"unexpected commit_v2 reply: {reply!r}"}

    reply = dict(reply)
    reply["script_source"] = script_source
    if tolerated:
        reply["comment_only_drift"] = tolerated
    if overlap and isinstance(reply.get("error"), str):
        reply["refused"] = overlap
        reply["error"] += (
            ". A peer commit changed a path this landing writes; refire after the peer's commit"
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
        from coordinator_core.ops.dispatch_emit.emission_receipt import stamp_landed

        stamp_landed(guarded_script, str(reply["sha"]))
        if session_id is not None and not anchor_only:
            from coordinator_core.ops.dispatch_emit import sibling_claims as _sc_follow

            left_behind = (reply.get("own_left_behind") or {}).get("left") or ()
            _land_own_left_behind(
                reply, str(reply["sha"]), session_id, worktree_root, repo_root,
                withheld=[
                    *foreign_excluded,
                    *_sc_follow.shared_dir_holds(
                        worktree_root, sibling_run_id, [*all_paths, *declared_writes], left_behind,
                    ),
                ],
            )
    if not reply.get("committed"):
        _remove(worktree_root, receipt_paths)
        receipt_paths = []
    if reply.get("committed") and reply.get("sha"):
        # Advance plan status so a re-fire (which selects live `open` rows)
        # cannot redo landed work. incomplete_chunks never reach here.
        scope.setdefault("landed", set()).update(all_paths)
        scope["landed"].update(_stopped_run_carried(reply))
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
                    checkpoint_rows.setdefault(csha, {}).setdefault(plan_rel, set()).update(ids)
            if review_no_delta:
                reply["review_uncoded_no_delta"] = review_no_delta
        predecessor = {} if rows_untouched else _predecessor_rows(
            worktree_root, request.plan_path, params.get(TERMINAL_RESUME_LEDGER_PARAM)
        )
        resume_ledger_param = params.get(TERMINAL_RESUME_LEDGER_PARAM)
        own_coded = frozenset(c.id for c in coded_chunks) | frozenset(checkpoint_of) | frozenset(outside_of)
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
        if not_stamped:
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
        if ledger is not None:
            reply.update(_ledger_writeback(
                worktree_root, ledger, params.get("ledger_verdict"),
                str(params.get("run_id") or ask_run_dir.name), str(reply["sha"]),
            ))
        shipped_sizing = None
        if not anchor_only and not incomplete_chunks:
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
        stamp_commit = bool(shipped_sizing) or plan_commits or bool(late_rewrites)
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
            art_residue = run_artifacts.residue(
                artifacts, snapshot.changed if snapshot is not None else (), landed_now, withheld_art,
                foreign=set(claims_over(artifacts.paths())) | set(left_out_map),
            )
            reply["run_artifacts"] = {
                "landed": sorted(p for p in artifacts.paths() if p in landed_now),
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
    reply["targeted_tests_check"] = integrity_extra["targeted_tests_check"]
    if "retested_tests" in integrity_extra:
        reply["retested_tests"] = integrity_extra["retested_tests"]
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
    if unsent_memos:
        reply["memo_unsent"] = dict(sorted(unsent_memos.items()))
    reply["prefix_files"] = prefix_files
    reply.update(usage_reply)
    if reply.get("committed") or reply.get("nothing_to_commit"):
        _with_siblings(reply)
    elif sibling_paths:
        reply["sibling_commits"] = [
            {"repo": root.name, "paths": paths, "committed": False,
             "error": "home commit did not land; sibling commit withheld"}
            for root, paths in sibling_paths.items()
        ]
    return reply
