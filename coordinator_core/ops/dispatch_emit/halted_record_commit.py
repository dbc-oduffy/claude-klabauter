"""The halted-run commit: ``dispatch.terminal_commit`` given ``halted_by``.

A halted engine-ask run leaves its ``state/warp/<run_id>/`` record directory behind; this lands it
as one ``ceremony.commit_v2`` call carrying that directory's top-level files, the run's
``run_artifacts`` candidate set (sizing, baton, plan, evidence sidecar, slice sizings, superseded
record dirs, claimed docs-stage paths), the baton the run recorded (left open, ``status_reason``
stamped with the halt), and
the halt reason in the message. Candidates are named paths, never the dirty tree. A finished chunk's tracked
deletion is declared, a deletion git cannot see is dropped, and a path a live peer's claim holds
is withheld and named. Every halt kind, ``ledger-check`` included, lands the record and run artifacts
only: a chunk's product and test files are withheld as ``halt-record-only``, because a shared dirty
tree puts other runs' hunks in those files; the resumed run re-runs the chunks and its terminal
commit lands them. The reply's ``targeted_tests.check`` still reports a carried verdict.
The commit carries no ``Lands-Chunks`` trailer and stamps no row coded. No receipt, sizing ship
or close job runs. The run
token is only checked (``run_token.admits``), never claimed: no branch, raise or killed op can strand
it, so a halt never spends the token the resumed run's product commit needs.

The request/manifest reads, ledger and kept records, and tree eviction live in
``halted_record_request``, ``halted_record_ledger`` and ``halted_record_eviction``; this module
re-exports them. Trap: a patch on a re-exported name here reaches only this module's own callers;
a function that moved calls its siblings through its new module, so patch it there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core import dispatch_abandon
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit import halt_product_gates, process_artifact_policy, review_edits, run_artifacts
from coordinator_core.ops.warp_run_deliverable import record_sizing_rel
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.worktree_snapshot import record_dir_changes
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.row_returns import CONSUMED_RETURNS_PARAM
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_DEPENDENTS_UNSTARTED,
    HALT_PLAN_FIELD,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    halted_record_message,
)
from coordinator_core.ops.dispatch_emit.land_run_halt import (
    _settle_ledger_pointer, held_rows_for, marker_kind,
)
from coordinator_core.ops.dispatch_emit.halted_record_request import (  # noqa: F401 -- re-exported
    _coded_rows,
    _held_reason,
    _manifest_baseline,
    _manifest_doc,
    _manifest_kept_from,
    _run_request,
    done_chunks,
    is_halted_record_call,
    within_handoffs_home,
)
from coordinator_core.ops.dispatch_emit.halted_record_ledger import (  # noqa: F401 -- re-exported
    _EVIDENCE_CAP_BYTES,
    _OUTCOME_HALT_KINDS,
    _blocking_items,
    _capped,
    _carried_returns,
    _ledger_halt_plan_and_blocking,
    _persist_blobs,
    _persist_run_outcome,
    _post_checkpoint_candidates,
    _record_kept,
    _record_withheld_prints,
    _xs_spine_halt,
)
from coordinator_core.ops.dispatch_emit.halted_record_eviction import (  # noqa: F401 -- re-exported
    EVICTED_NOT_IMPORTABLE,
    RUN_DIRT_UNCOMMITTED,
    TreeEviction,
    _NOT_RUN_OWNED,
    _live_run_ids,
    _review_written,
    _run_written,
    _withhold_run_dirt,
    plan_tree_eviction,
)


def _reap_staged_twins(worktree_root: Path, run_id: str, *, own: bool) -> None:
    """Sweep expired staged twins always; drop the halted run's own twins only when ``own`` (its run
    token verified). Zero spawns; never fails the commit. Trap: an unverified caller must never pass
    ``own=True``, since a valid-shaped run_id alone would delete a live run's twin."""
    from coordinator_core.ops.dispatch_emit import launcher_staged_twin

    try:
        if own:
            launcher_staged_twin.remove_twins(worktree_root, run_id)
        launcher_staged_twin.sweep_expired_twins(worktree_root)
    except Exception:  # noqa: BLE001 -- cleanup must not fail the commit
        pass


def commit_halted_record(worktree_root: Path, repo_root: Path, params: dict) -> dict:
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.terminal_commit_refusals import _error

    halted = params[HALTED_RECORD_PARAM]
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not safe_id(run_id):
        return _error(f"params.run_id {run_id!r} is not a single safe path segment", refused="run-id-unsafe")
    if contained_path(worktree_root / RECORD_DIR_ROOT / run_id, [worktree_root / RECORD_DIR_ROOT]) is None:
        return _error(f"record dir for {run_id!r} escapes {RECORD_DIR_ROOT}", refused="record-dir-escapes")
    record_dir_rel = f"{RECORD_DIR_ROOT}/{run_id}"

    from coordinator_core.ops.dispatch_emit.run_close import sibling_release

    siblings = sibling_release(worktree_root, run_id, None)
    token = params.get("run_token")
    script_path = params.get("script_path")
    verified = token is not None and run_token.admits(worktree_root, run_id, token, script_path)
    _reap_staged_twins(worktree_root, run_id, own=verified)
    if token is not None and not verified:
        refusal = _error(
            "params.run_token does not match a live, unspent token minted for params.run_id and "
            "this script (a token admits one landed commit)",
            refused="run-token",
        )
        if siblings is not None:
            refusal["siblings"] = siblings
        return refusal
    reply = _land(worktree_root, repo_root, params, run_id, record_dir_rel, halted)
    if token is not None:
        reply = _reconcile_landing(worktree_root, run_id, token, script_path, halted, reply)
    if siblings is not None:
        reply["siblings"] = siblings
    if not reply.get("error"):
        from coordinator_core.ops.dispatch_emit import run_push

        reply["push"] = run_push.nudge_push(worktree_root, repo_root)
    return reply


def _reconcile_landing(
    worktree_root: Path, run_id: str, token: object, script_path: object, halted: dict, reply: dict
) -> dict:
    """Record a landed record commit beside the live token; answer a re-run that finds nothing left
    to commit as ``already_landed`` with the recorded sha when it is on HEAD and the halt kind is the
    same. The token is never consumed or released here, and a re-run lands no second commit."""
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY
    from coordinator_core.ops.dispatch_emit.terminal_commit_reconcile import landed_on_head

    kind = halted.get("halted")
    if reply.get("committed") and isinstance(reply.get("sha"), str):
        run_token.note_halt_landing(
            worktree_root, run_id, token, script_path,
            {"sha": reply["sha"], "paths": list(reply.get("paths") or []), "halted": kind},
        )
        return reply
    if reply.get("committed") is False and reply.get("nothing_to_commit") and not reply.get("error"):
        landed = run_token.halt_landing(worktree_root, run_id, token, script_path)
        sha = landed.get("sha") if landed else None
        same_halt = bool(landed) and landed.get("halted") == kind
        if same_halt and isinstance(sha, str) and landed_on_head(worktree_root, sha):
            answer = {k: v for k, v in reply.items() if k != "nothing_to_commit"}
            return {**answer, "committed": True, "sha": sha, ALREADY_LANDED_KEY: True,
                    "paths": list(landed.get("paths") or [])}
    return reply


HALT_RUN_REASON = "halted-run"


def _stamp_run_baton(
    worktree_root: Path, run_id: str, kind: str, reason: Optional[str]
) -> tuple[Optional[str], Optional[dict]]:
    """Stamp the baton the run recorded with ``status_reason`` naming the halt; ``status`` and
    ``deployment_state`` stay as they are, so a refire still picks it up. Returns
    ``(baton_rel, error)``: ``baton_rel`` is the stamped path (None when the run recorded no
    readable baton inside the handoffs home), ``error`` is ``{path, error}`` for a refused write."""
    from coordinator_core.frontmatter.primitives import rebuild, split_frontmatter
    from coordinator_core.frontmatter.primitives import insert_fm_field, read_fm_field, replace_fm_field
    from coordinator_core.locked_write import LockTimeout, locked_rmw
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton
    from coordinator_core.session.record_homes import home_dir

    rel = read_run_baton(worktree_root, run_id)
    if rel is None:
        return None, None
    abs_path = contained_path(worktree_root / rel, [Path(home_dir(str(worktree_root), "handoffs"))])
    if abs_path is None or not abs_path.is_file():
        return None, None
    value = f"run {run_id} halted: {kind}" + (f": {reason}" if reason else "")

    def compose(old: str) -> str:
        split = split_frontmatter(old)
        if split is None:
            raise ValueError("baton has no frontmatter")
        fm = split.fm_text
        if read_fm_field(fm, "status_reason") is None:
            fm = insert_fm_field(fm, "status_reason", value)
        else:
            fm = replace_fm_field(fm, "status_reason", value)
        return old if fm == split.fm_text else rebuild(split, fm)

    try:
        locked_rmw(abs_path, compose, repo_root=worktree_root)
    except (LockTimeout, OSError, ValueError) as exc:
        return rel, {"path": rel, "error": str(exc)}
    return rel, None


def _baton_cascade(worktree_root: Path, repo_root: Path, run_id: str) -> dict:
    """``{"run_baton_cascade": ...}`` for the run's recorded baton, ``{}`` when it recorded none.
    The op fires only for a baton already terminal; no headroom before the dispatch deadline
    records ``op-unavailable`` and calls nothing."""
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton

    rel = read_run_baton(worktree_root, run_id)
    if rel is None:
        return {}
    left = dispatch_abandon.remaining()
    if left is not None and left - dispatch_abandon.REPLY_MARGIN_SECS <= 0:
        return {"run_baton_cascade": {"fired": [], "skipped": [{"baton": rel, "reason": "op-unavailable"}]}}
    from coordinator_core.ops.dispatch_emit.run_baton_cascade import fire_for_batons

    return {"run_baton_cascade": fire_for_batons(worktree_root, repo_root, [rel], None)}


def _reland_late_writes(
    worktree_root: Path, repo_root: Path, params: dict, run_id: str, record_dir_rel: str, halted: dict,
    sizing_rel: Optional[str], session_id: Optional[str], withheld: dict, process_withheld: dict,
) -> Optional[tuple]:
    """After the halt commit, land run-artifact candidates written during the op (file pathspecs only).

    Returns (artifacts, still_changed, relanded) from a fresh snapshot, or None when it cannot be
    taken. Peer-held and not-claimed paths stay withheld (``run_artifacts.landable``); a failed
    second commit leaves the paths changed, so ``residue`` reports them.
    """
    from coordinator_core.ops.dispatch_emit.terminal_commit import _worktree_snapshot
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    raw_docs = params.get(run_artifacts.DOC_PATHS_PARAM)
    fresh = within_handoffs_home(worktree_root, run_artifacts.candidate_paths(
        worktree_root, run_id,
        sizing_rel=sizing_rel,
        plan_rel=halted.get(HALT_PLAN_FIELD) if isinstance(halted.get(HALT_PLAN_FIELD), str) else None,
        doc_paths=[d for d in raw_docs if isinstance(d, str)] if isinstance(raw_docs, list) else (),
    ))
    snap = _worktree_snapshot(worktree_root, list(fresh.paths()), [record_dir_rel, RECORD_DIR_ROOT], [])
    if snap is None:
        return None
    fresh = fresh.with_superseded(
        run_artifacts.superseded_runs(
            worktree_root, run_id, sizing_rel, snap.changed, live_run_ids=_live_run_ids(repo_root)
        ),
        RECORD_DIR_ROOT,
    )
    fresh, more = process_artifact_policy.withhold_process_artifacts(worktree_root, fresh)
    blocked = {**withheld, **process_withheld, **more}
    land = run_artifacts.landable(worktree_root, fresh, snap.changed, snap.ignored, session_id=session_id)
    withheld.update({p: r for p, r in land.withheld.items() if p not in withheld})
    present = [p for p in land.present if p not in blocked and not halt_product_gates.is_scratch(p)]
    gone = [p for p in land.deleted if p not in blocked and not halt_product_gates.is_scratch(p)]
    relanded: list = []
    if present or gone:
        left = dispatch_abandon.remaining()
        if left is None or left - dispatch_abandon.REPLY_MARGIN_SECS > 0:
            commit_params: dict = {
                "paths": present + gone,
                "message": f"halted: re-land late run-artifact writes of {run_id}",
                "carry_stopped_runs": False,
                "ignored_untracked": snap.ignored_among(present + gone),
            }
            if gone:
                commit_params["deleted_paths"] = gone
                commit_params["declared_reverts"] = gone
            if session_id is not None:
                commit_params["session_id"] = session_id
            try:
                second = reentrant_dispatch("ceremony.commit_v2", commit_params, repo_root=repo_root)
            except OpUnavailableError:
                second = None
            if isinstance(second, dict) and not second.get("error") and not second.get("refused"):
                relanded = present + gone
    again = _worktree_snapshot(worktree_root, list(fresh.paths()), [record_dir_rel, RECORD_DIR_ROOT], [])
    still = set(again.changed) if again is not None else set(snap.changed) - set(relanded)
    return fresh, still, relanded


def _land(
    worktree_root: Path, repo_root: Path, params: dict, run_id: str, record_dir_rel: str, halted: dict
) -> dict:
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit.credit_gate import KEPT_REVIEW_CHUNK_ID
    from coordinator_core.ops.dispatch_emit.terminal_commit import _worktree_snapshot
    from coordinator_core.ops.dispatch_emit.terminal_commit_refusals import _error
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import split_sibling_paths
    from coordinator_core.ops.ceremony import commit_v2
    from coordinator_core.ops.dispatch_emit.terminal_commit import _tracked_at_head
    from coordinator_core.ops.dispatch_emit.terminal_commit_ledger import _own_prefix_files
    from coordinator_core.ops.dispatch_emit import landing_integrity
    from coordinator_core.ops.dispatch_emit import landing_targeted_tests

    base = {"record_dir": record_dir_rel, "halted": halted["halted"]}
    halt_reason = _held_reason(halted) if halted["halted"] == HALT_DEPENDENTS_UNSTARTED else None
    run_base = landing_integrity.run_base_for(worktree_root, run_id, params)
    session_id: Optional[str] = params.get("session_id")
    if not (isinstance(session_id, str) and _UUID_RE.fullmatch(session_id)):
        session_id = None

    finished = done_chunks(worktree_root, params)
    declared: dict = {}  # path -> owning chunk id, first declarer wins
    report_cache: dict = {}
    for chunk in finished:
        if chunk.id == KEPT_REVIEW_CHUNK_ID:
            continue
        for path in list(chunk.paths) + (_own_prefix_files(worktree_root, chunk, report_cache) or []):
            declared.setdefault(path, chunk.id)
    owner = declared
    home, _siblings = split_sibling_paths(list(owner), worktree_root)
    declared = [p for p in home if not p.startswith(record_dir_rel + "/")]
    if not params.get("script_path"):
        # Land-only ask: no script names the product, so the marker's withheld paths are it. Not put
        # in ``owner``: no chunk owns them, so the record-only/closure legs never re-withhold them.
        # Every recorded reason is declared; peer-held, sibling and foreign-halted paths are re-withheld
        # by the gates below. Paths come from the marker uncontained: ``snapshot.changed`` bounds them.
        try:
            withheld_recorded = sorted(halted_run.halted_withheld_strict(worktree_root / record_dir_rel))
        except halted_run.UnreadableHaltRecord as exc:
            return _error(f"halt marker unreadable, its paths are held: {exc.reason}", refused=exc.reason, **base)
        recorded, _ = split_sibling_paths(withheld_recorded, worktree_root)
        declared += [p for p in recorded if p not in declared and not p.startswith(record_dir_rel + "/")]
    sizing_rel = record_sizing_rel(worktree_root, run_id)
    if (
        sizing_rel
        and not Path(sizing_rel).is_absolute()
        and contained_path(worktree_root / sizing_rel, [worktree_root]) is not None
        and sizing_rel not in owner
        and not sizing_rel.startswith(record_dir_rel + "/")
    ):
        declared.append(sizing_rel)
    scratch_declared = [p for p in declared if halt_product_gates.is_scratch(p)]
    declared = [p for p in declared if not halt_product_gates.is_scratch(p)]

    outcome_rel = _persist_run_outcome(worktree_root, run_id, halted, params)
    plan_rel = None
    if (worktree_root / record_dir_rel).is_dir():
        plan_rel, blocking, blocking_items = (
            _ledger_halt_plan_and_blocking(repo_root, halted)
            if marker_kind(halted) == "ledger-check" else (None, (), [])
        )
        if halted["halted"] == "ledger-check" and plan_rel is None:
            plan_rel, blocking, blocking_items = _xs_spine_halt(
                worktree_root, record_dir_rel, halted
            )
        write_halt_marker(
            worktree_root / record_dir_rel,
            halted=marker_kind(halted),
            sizing_rel=sizing_rel,
            plan_rel=plan_rel,
            blocking=blocking if plan_rel else None,
            blocking_items=blocking_items if plan_rel else None,
            run_base_sha=run_base,
            reason=halt_reason,
            next_action=halted.get("next_action") if isinstance(halted.get("next_action"), dict) else None,
            held_rows=held_rows_for(worktree_root, run_id, plan_rel),
        )
    raw_docs = params.get(run_artifacts.DOC_PATHS_PARAM)
    artifacts = within_handoffs_home(worktree_root, run_artifacts.candidate_paths(
        worktree_root, run_id,
        sizing_rel=sizing_rel,
        plan_rel=halted.get(HALT_PLAN_FIELD) if isinstance(halted.get(HALT_PLAN_FIELD), str) else None,
        doc_paths=[d for d in raw_docs if isinstance(d, str)] if isinstance(raw_docs, list) else (),
    ))
    artifacts, process_withheld = process_artifact_policy.withhold_process_artifacts(worktree_root, artifacts)
    if process_withheld:
        declared = [p for p in declared if p != sizing_rel]
        if sizing_rel:
            process_withheld[sizing_rel] = process_artifact_policy.WITHHELD_REASON
    baton_rel, baton_error = _stamp_run_baton(worktree_root, run_id, halted["halted"], halt_reason)
    if process_withheld:
        if baton_rel is not None:
            process_withheld[baton_rel] = process_artifact_policy.WITHHELD_REASON
    elif baton_rel is not None and baton_rel not in artifacts.by_class.get("baton", ()) and baton_rel not in declared:
        declared.append(baton_rel)
    baton_reply: dict = {"run_baton": baton_error} if baton_error is not None else {}
    review_written = _review_written(worktree_root, session_id, run_id)
    review_literals = [] if review_written is None else [
        p for p in review_written if not review_edits.out_of_worktree(review_edits.norm(p))
    ]
    run_written = _run_written(worktree_root, session_id, run_id)
    request, _ = _run_request(worktree_root, params.get("script_path"))
    request_owned = {p: c.id for c in (request.chunks if request is not None else ()) for p in c.paths}
    coded, coded_error = _coded_rows(worktree_root, halted)
    coded_reply: dict = {"coded_rows_unreadable": coded_error} if coded_error else {}
    uncoded_dirt = [
        review_edits.norm(p) for p, cid in request_owned.items() if p not in owner and cid not in coded
    ]
    manifest_doc = _manifest_doc(worktree_root / record_dir_rel)
    post_candidates = _post_checkpoint_candidates(
        worktree_root, record_dir_rel, manifest_doc,
        {**{p: c for p, c in request_owned.items() if c not in coded}, **owner},
        review_literals, run_written,
    )
    listed = declared + list(artifacts.paths()) + review_literals + run_written
    snapshot = _worktree_snapshot(
        worktree_root, listed + [p for p in [*post_candidates, *uncoded_dirt] if p not in listed],
        [record_dir_rel, RECORD_DIR_ROOT], [],
    )
    if snapshot is None:
        return _error(f"git status over {record_dir_rel} failed", **base)
    artifacts = artifacts.with_superseded(
        run_artifacts.superseded_runs(
            worktree_root, run_id, sizing_rel, snapshot.changed, live_run_ids=_live_run_ids(repo_root)
        ),
        RECORD_DIR_ROOT,
    )
    artifacts, more_withheld = process_artifact_policy.withhold_process_artifacts(worktree_root, artifacts)
    process_withheld.update(more_withheld)
    paths, deleted = record_dir_changes(worktree_root, record_dir_rel, snapshot.changed)
    if process_withheld:
        process_withheld.update({p: process_artifact_policy.WITHHELD_REASON for p in (*paths, *deleted)})
        paths, deleted = [], []

    product: list = []
    gone: list = []
    for path in declared:
        if path not in snapshot.changed:
            continue
        (product if (worktree_root / path).is_file() else gone).append(path)
    tracked_gone = set(_tracked_at_head(worktree_root, gone))
    dropped = [{"path": p, "reason": "deletion-git-cannot-see"} for p in gone if p not in tracked_gone]
    gone = [p for p in gone if p in tracked_gone]
    withheld: dict = dict(process_withheld)
    withheld.update({p: halt_product_gates.SCRATCH_EXPORT for p in scratch_declared})

    handled = set(paths) | set(deleted) | set(declared)
    own_record = {p for p in artifacts.paths() if artifacts.class_of(p) == "record-dir"}
    extra_artifacts = run_artifacts.landable(
        worktree_root,
        run_artifacts.RunArtifacts(
            run_id=run_id,
            by_class={
                cls: tuple(p for p in ps if p not in handled and p not in own_record)
                for cls, ps in artifacts.by_class.items()
            },
            record_dirs=artifacts.record_dirs,
            root=artifacts.root,
        ),
        snapshot.changed, (), session_id=session_id,
    )
    withheld.update(extra_artifacts.withheld)
    art_paths = [p for p in extra_artifacts.present if not halt_product_gates.is_scratch(p)]
    art_deleted = [p for p in extra_artifacts.deleted if not halt_product_gates.is_scratch(p)]
    withheld.update({
        p: halt_product_gates.SCRATCH_EXPORT
        for p in (*extra_artifacts.present, *extra_artifacts.deleted) if halt_product_gates.is_scratch(p)
    })

    from coordinator_core.ops.dispatch_emit import sibling_claims
    from coordinator_core.ops.dispatch_emit.terminal_commit_review import _withhold_reasons

    em_withheld = _withhold_reasons(worktree_root, artifacts, record_dir_rel)
    withheld.update({
        p: em_withheld[p]
        for p in (*product, *gone, *art_paths, *art_deleted, *(n for n in map(review_edits.norm, review_literals) if n in snapshot.changed))
        if p in em_withheld
    })

    sibling = sibling_claims.sibling_run_claims(
        worktree_root, run_id, product + gone + art_paths + art_deleted + review_literals
    )
    if sibling:
        own_declared = set(owner) | set(paths) | set(deleted) | own_record
        if sizing_rel:
            own_declared.add(sizing_rel)
        product, w1 = sibling_claims.filter_own(product, sibling, own_declared)
        gone, w2 = sibling_claims.filter_own(gone, sibling, own_declared)
        art_paths, w3 = sibling_claims.filter_own(art_paths, sibling, own_declared)
        art_deleted, w4 = sibling_claims.filter_own(art_deleted, sibling, own_declared)
        withheld.update({p: sibling_claims.SIBLING_WITHHELD_REASON for p in (*w1, *w2, *w3, *w4)})

    from coordinator_core.ops.dispatch_emit.land_run import foreign_halted_withheld

    halt_plan = halted.get(HALT_PLAN_FIELD)
    foreign = foreign_halted_withheld(
        worktree_root, run_id, halt_plan if isinstance(halt_plan, str) else None,
        product + gone + art_paths + art_deleted + review_literals,
    )
    if foreign:
        withheld.update({p: "foreign-halted-withheld" for p in foreign})
        product = [p for p in product if p not in withheld]
        gone = [p for p in gone if p not in withheld]
        art_paths = [p for p in art_paths if p not in withheld]
        art_deleted = [p for p in art_deleted if p not in withheld]

    holds: list = []
    warnings = commit_v2._peer_claim_warnings(
        worktree_root, product + gone + art_paths + art_deleted, holds, own_sid=session_id
    )
    held = {p for p, _sid in holds}
    withheld.update({p: "peer-held" for p in sorted(held)})
    peer_held_by: dict = {}
    if holds:
        from coordinator_core.ops.dispatch_emit import peer_claim_contract

        peer_held_by = {"peer_held_by": peer_claim_contract.group(holds)}
    # Warning text is commit_v2._peer_claim_warnings' contract: "claim state indeterminate (<exc>)" or "... for '<path>'".
    indeterminate = [w for w in warnings if w.startswith("claim state indeterminate")]
    if indeterminate:
        # Only declared product paths are withheld on an indeterminate claim; run artifacts (record, sizing) still land.
        everywhere = any(w.startswith("claim state indeterminate (") for w in indeterminate)
        for p in product + gone:
            if p not in held and (everywhere or f"claim state indeterminate for {p!r}" in indeterminate):
                withheld[p] = "peer-claim-indeterminate"
    gated = halt_product_gates.gate_halt_product(
        worktree_root, run_id,
        [p for p in product if p not in withheld and p not in (sizing_rel, baton_rel)],
        [p for p in gone if p not in withheld and p not in (sizing_rel, baton_rel)],
        owner, list(owner),
        plan=halt_plan if isinstance(halt_plan, str) else None,
    )
    withheld.update({
        p: r for p, r in gated.items()
        if r != halt_product_gates.UNOWNED_PRODUCT or halted["halted"] == "ledger-check"
    })
    product = [p for p in product if p not in withheld]
    gone = [p for p in gone if p not in withheld]
    art_paths = [p for p in art_paths if p not in withheld]
    art_deleted = [p for p in art_deleted if p not in withheld]
    targeted: Optional[dict] = None
    tfind: list = []
    tcheck = ""
    if halted["halted"] == "ledger-check":
        tfind, tcheck = landing_integrity.landing_test_gate(worktree_root, params, run_id)
        targeted = {
            "check": tcheck,
            "findings": [{"path": f.path, "reason": f.reason, "detail": f.detail} for f in tfind],
            **({"note": tfind.deleted_note} if tfind.deleted_note else {}),
        }
    withheld.update({p: "halt-record-only" for p in product + gone if p in owner})
    product = [p for p in product if p not in withheld]
    gone = [p for p in gone if p not in withheld]
    paths = paths + product + art_paths
    deleted = deleted + gone + art_deleted
    landed_classes: dict = {}
    for p in art_paths + art_deleted:
        cls = artifacts.class_of(p)
        if cls:
            landed_classes[cls] = landed_classes.get(cls, 0) + 1
    review_reply: dict = {}
    unlanded: dict = {}
    if review_written is None:
        review_reply["review_edits"] = None
    else:
        verdict = review_edits.partition(
            review_written, changed=snapshot.changed, ignored=snapshot.ignored,
            committed=set(paths) | set(deleted), sibling_declared=sibling, contested=set(),
            present=lambda p: (worktree_root / p).is_file(),
        )
        # A halted run lands its halt record and declared writes only; review edits stay in the tree.
        unlanded = {p: HALT_RUN_REASON for p in sorted({*verdict.halt, *verdict.land})}
        probe = [p for p in verdict.land if p != "scratch" and not p.startswith("scratch/")]
        review_holds: list = []
        if probe:
            commit_v2._peer_claim_warnings(worktree_root, probe, review_holds, own_sid=session_id)
        for p, _sid in review_holds:
            unlanded[p] = "peer-held"
            withheld[p] = "peer-held"
    review_reply["review_edits_unlanded"] = unlanded

    # Trap: taken before _record_kept appends kept.json/returns.json to ``paths``; the record files are
    # never post-checkpoint candidates, so they must not count as landed here.
    landed_now = set(paths) | set(deleted)
    _withhold_run_dirt(withheld, [*run_written, *post_candidates, *uncoded_dirt], snapshot.changed, landed_now)
    eviction = plan_tree_eviction(
        worktree_root, record_dir_rel, run_written, snapshot.changed,
        [*run_written, *owner, *declared], withheld, paths, run_id,
    )

    if process_withheld:
        process_artifact_policy.exclude_locally(worktree_root, process_withheld, artifacts.record_dirs)

    if not paths and not deleted:
        _settle_ledger_pointer(repo_root, run_id, halted, sizing_rel, outcome_rel)
        return {
            "committed": False, "sha": None, "nothing_to_commit": True,
            **({"withheld": withheld} if withheld else {}), **peer_held_by,
            **({"targeted_tests": targeted} if targeted is not None else {}), **review_reply,
            **baton_reply, **coded_reply, **_baton_cascade(worktree_root, repo_root, run_id), **base,
        }

    if (worktree_root / record_dir_rel).is_dir():
        halted_run.record_withheld(worktree_root / record_dir_rel, withheld)
        _record_withheld_prints(worktree_root, record_dir_rel, withheld, paths)
    kept_reply = _record_kept(
        worktree_root, record_dir_rel, owner, eviction.kept_view(withheld) if eviction else withheld,
        halted, paths, params.get(CONSUMED_RETURNS_PARAM),
        post_candidates=[p for p in post_candidates if p in snapshot.changed], landed=landed_now,
        common_dir=resolve_git_common_dir(worktree_root), manifest_doc=manifest_doc,
    )
    message = halted_record_message(run_id, halted)
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_halted_trailer_lines

    for line in run_halted_trailer_lines(landing_integrity.closing_bases(worktree_root, run_id, params)):
        message += "\n" + line
    for path in unlanded:
        message += "\nReview-Edit-Unlanded: " + path
    for path in (kept_reply.get("kept") or {}).get(kept_work.POST_CHECKPOINT_KEY, ()):
        message += "\nKept-Post-Checkpoint: " + path
    head_message = message

    def compose_message(removed: list) -> str:
        out = head_message
        if landed_classes:
            out += "\nLands-Artifacts: " + ", ".join(f"{c}={n}" for c, n in sorted(landed_classes.items()))
        if removed:
            out += "\nRemoves: " + ", ".join(removed)
        return out

    message = compose_message(deleted)
    if eviction is not None:
        try:
            eviction.apply()
        except OSError as exc:
            return _error(f"evicting unimportable run bytes failed: {exc}", **base)
        extra_evicted = {"evicted": eviction.evicted, **({"unimportable_unowned": eviction.unowned} if eviction.unowned else {})}
    else:
        extra_evicted = {}
    commit_params: dict = {"paths": paths, "message": message, "carry_stopped_runs": False}
    if deleted:
        commit_params["deleted_paths"] = deleted
        commit_params["declared_reverts"] = deleted
    if product or art_paths:
        commit_params["ignored_untracked"] = snapshot.ignored_among(paths)
    if session_id is not None:
        commit_params["session_id"] = session_id
        if product:
            from coordinator_core.ops.dispatch_emit.terminal_commit_review import _recorded_fingerprints

            expected = _recorded_fingerprints(worktree_root, session_id, product, paths, deleted, excluded=set())
            if expected:
                commit_params["expected_content_sha256"] = expected
    left = dispatch_abandon.remaining()
    if left is not None and left - dispatch_abandon.REPLY_MARGIN_SECS <= 0:
        return _error(
            f"ceremony.commit_v2 step not started: {left:.1f}s left before the dispatch deadline; "
            "nothing committed",
            **base,
        )
    try:
        reply = reentrant_dispatch("ceremony.commit_v2", commit_params, repo_root=repo_root)
    except OpUnavailableError:
        return _error("ceremony.commit_v2 is not registered", **base)
    if not isinstance(reply, dict):
        return _error(f"unexpected commit_v2 reply: {reply!r}", **base)
    if reply.get("refused") == "blob-drift":
        from coordinator_core.ops.dispatch_emit.terminal_commit import _drift_paths

        drifted = {p for p in _drift_paths(reply.get("blob_drift")) if p in product or p in gone}
        if drifted:
            withheld.update({p: "blob-drift" for p in sorted(drifted)})
            product = [p for p in product if p not in drifted]
            gone = [p for p in gone if p not in drifted]
            paths = [p for p in paths if p not in drifted]
            deleted = [p for p in deleted if p not in drifted]
            if (worktree_root / record_dir_rel).is_dir():
                halted_run.record_withheld(worktree_root / record_dir_rel, withheld)
            commit_params["paths"] = paths
            commit_params["message"] = compose_message(deleted)
            for key in ("deleted_paths", "declared_reverts"):
                if deleted:
                    commit_params[key] = deleted
                else:
                    commit_params.pop(key, None)
            commit_params["ignored_untracked"] = snapshot.ignored_among(paths)
            if "expected_content_sha256" in commit_params:
                kept_hashes = {p: h for p, h in commit_params["expected_content_sha256"].items() if p in paths}
                if kept_hashes:
                    commit_params["expected_content_sha256"] = kept_hashes
                else:
                    commit_params.pop("expected_content_sha256")
            try:
                reply = reentrant_dispatch("ceremony.commit_v2", commit_params, repo_root=repo_root)
            except OpUnavailableError:
                return _error("ceremony.commit_v2 is not registered", **base)
            if not isinstance(reply, dict):
                return _error(f"unexpected commit_v2 reply: {reply!r}", **base)
    extra: dict = {**review_reply, **peer_held_by, **baton_reply, **coded_reply, **kept_reply, **extra_evicted}
    if not reply.get("error"):
        _settle_ledger_pointer(repo_root, run_id, halted, sizing_rel, outcome_rel)
        extra.update(_baton_cascade(worktree_root, repo_root, run_id))
        late = _reland_late_writes(
            worktree_root, repo_root, params, run_id, record_dir_rel, halted, sizing_rel,
            session_id, withheld, process_withheld,
        )
        if late is not None:
            artifacts, still_changed, relanded = late
            reply["relanded"] = relanded
            paths = paths + relanded
        else:
            still_changed = set(snapshot.changed) - set(paths) - set(deleted)
        left = run_artifacts.residue(artifacts, still_changed, paths + deleted, withheld)
        if left:
            extra["run_artifact_residue"] = left
    # A halt record stamps no chunk coded; the key stays so callers reading it never KeyError.
    extra["lands_chunks_stamp"] = None
    extra["run_artifacts"] = {"landed": sorted(paths + deleted), "withheld": withheld}
    if withheld:
        extra["withheld"] = withheld
    if dropped:
        extra["dropped"] = dropped
    if targeted is not None:
        extra["targeted_tests"] = targeted
    if tfind:
        extra["advisory"] = landing_targeted_tests.advisory_entries(tfind)
    return {**reply, "paths": paths + deleted, **extra, **base}  # paths: everything the commit names
