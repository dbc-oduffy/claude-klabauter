"""The halted-run commit: ``dispatch.terminal_commit`` given ``halted_by``.

A halted engine-ask run leaves its ``state/warp/<run_id>/`` record directory behind; this lands it
as one ``ceremony.commit_v2`` call carrying that directory's top-level files, the run's
``run_artifacts`` candidate set (sizing, baton, plan, evidence sidecar, slice sizings, superseded
record dirs, claimed docs-stage paths), the declared writes of every chunk the run finished, and
the halt reason in the message. Candidates are named paths, never the dirty tree. A finished chunk's tracked
deletion is declared, a deletion git cannot see is dropped, and a path a live peer's claim holds
is withheld and named. Only a ``ledger-check`` halt lands chunk product, because a resumed run counts
only that halt's ``Lands-Chunks`` (``emit.resume_landed_rows``) and re-runs every other halt's
chunks; any other halt lands the record and run artifacts only (``halt-record-only``). A
``ledger-check`` halt lands a chunk only with its import closure: a chunk whose Python or TS/JS
imports would be missing at the commit, transitively, is withheld whole (``import-closure-open``).
A chunk owning a ``.py`` that fails to parse (``py-unparseable``) or whose landing blob drops lines a
foreign commit added since the run's base (``since-base-overlap``) is withheld whole the same way,
and so is a chunk any of whose changed declared writes is withheld for another reason
(``chunk-write-set-partial``): a chunk lands whole or not at all.
With no finished chunk the commit is record-only. No receipt, sizing ship,
plan stamp or close job runs, and the run token is redeemed then always released: a halt never
spends the token the resumed run's product commit needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit import review_edits, run_artifacts
from coordinator_core.ops.warp_run_deliverable import record_sizing_rel
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_PLAN_FIELD,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RUN_OUTCOME_FILE,
    LedgerHalt,
    halted_record_message,
    ledger_halt_path,
    write_ledger_halt,
    write_run_outcome,
)


def is_halted_record_call(params: object) -> bool:
    """True when ``params[HALTED_RECORD_PARAM]`` is a dict with a non-empty str ``halted``."""
    halted = params.get(HALTED_RECORD_PARAM) if isinstance(params, dict) else None
    return isinstance(halted, dict) and isinstance(halted.get("halted"), str) and bool(halted["halted"])


def within_handoffs_home(worktree_root: Path, artifacts: run_artifacts.RunArtifacts) -> run_artifacts.RunArtifacts:
    """``artifacts`` without any ``baton`` candidate outside the handoffs home: a sizing's
    ``baton:`` naming another directory is never landed."""
    from coordinator_core.session.record_homes import home_dir

    home = [Path(home_dir(str(worktree_root), "handoffs"))]
    kept = tuple(
        p for p in artifacts.by_class.get("baton", ()) if contained_path(worktree_root / p, home) is not None
    )
    if kept == artifacts.by_class.get("baton", ()):
        return artifacts
    return run_artifacts.RunArtifacts(
        run_id=artifacts.run_id,
        by_class={**artifacts.by_class, "baton": kept},
        record_dirs=artifacts.record_dirs,
        root=artifacts.root,
    )


def record_dir_changes(worktree_root: Path, record_dir_rel: str, changed: set) -> tuple[list, list]:
    """The record dir's changed top-level files from one status read: ``(present, deleted)``.
    ``present`` are files on disk; ``deleted`` are changed entries gone from disk."""
    prefix = record_dir_rel.rstrip("/") + "/"
    present: list = []
    deleted: list = []
    for rel in sorted(changed):
        if not rel.startswith(prefix) or "/" in rel[len(prefix):]:
            continue
        (present if (worktree_root / rel).is_file() else deleted).append(rel)
    return present, deleted


def _run_request(worktree_root: Path, script_path: object):
    """The commit request an emitted script (or its ask-run manifest) promises, plus the ids the
    manifest withheld: ``(request, withheld_ids)``. ``(None, set())`` when nothing is readable."""
    import json

    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, StageManifest
    from coordinator_core.ops.dispatch_emit.commit_request import (
        MalformedCommitRequestError,
        parse_manifest_marker,
        parse_marker,
    )

    if not isinstance(script_path, str) or not script_path:
        return None, set()
    script = contained_path(worktree_root / script_path, [worktree_root])
    if script is None:
        return None, set()
    try:
        text = script.read_text(encoding="utf-8")
        manifest_rel = parse_manifest_marker(text)
        if manifest_rel is None:
            return parse_marker(text), set()
        manifest_abs = contained_path(
            worktree_root / manifest_rel, [worktree_root / RECORD_DIR_ROOT, worktree_root / RUN_DIR_ROOT]
        )
        if manifest_abs is None:
            return None, set()
        manifest = StageManifest.from_json(json.loads(manifest_abs.read_text(encoding="utf-8")))
        marker = contained_path(worktree_root / manifest.marker_path, [worktree_root / RUN_DIR_ROOT])
        if marker is None:
            return None, set()
        withheld = {g.id for g in manifest.gated} | {o.id for o in manifest.owed}
        return parse_marker(marker.read_text(encoding="utf-8")), withheld
    except (OSError, ValueError, KeyError, TypeError, MalformedCommitRequestError):
        return None, set()


def done_chunks(worktree_root: Path, params: dict) -> list:
    """The chunks a halted run finished. With ``params.incomplete_chunks`` the rest are done, as in
    the DAG mode; without it a chunk is done only when its own report reads DONE and not
    PARTIAL/BLOCKED. A manifest-withheld chunk and an unreadable request are never done."""
    from coordinator_core.ops.dispatch_emit.terminal_commit import (
        _DELIVERED_REPORT_RE,
        _UNDELIVERED_REPORT_RE,
        _read_rel,
    )

    request, withheld = _run_request(worktree_root, params.get("script_path"))
    if request is None:
        return []
    listed = params.get("incomplete_chunks")
    incomplete = set(listed) if isinstance(listed, list) else None
    out: list = []
    for chunk in request.chunks:
        if chunk.id in withheld or (incomplete is not None and chunk.id in incomplete):
            continue
        if incomplete is None:
            text = _read_rel(worktree_root, chunk.report) if chunk.report else None
            if text is None or not _DELIVERED_REPORT_RE.search(text) or _UNDELIVERED_REPORT_RE.search(text):
                continue
        out.append(chunk)
    return out


def commit_halted_record(worktree_root: Path, repo_root: Path, params: dict) -> dict:
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error

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
    if token is not None and not run_token.redeem(worktree_root, run_id, token, script_path):
        refusal = _error(
            "params.run_token does not match a live, unspent token minted for params.run_id and "
            "this script (a token admits one landed commit)",
            refused="run-token",
        )
        if siblings is not None:
            refusal["siblings"] = siblings
        return refusal
    try:
        reply = _land(worktree_root, repo_root, params, run_id, record_dir_rel, halted)
        if siblings is not None:
            reply["siblings"] = siblings
        return reply
    finally:
        if token is not None:
            run_token.release(worktree_root, run_id, token, script_path)


#: Evidence kept per blocking item, in UTF-8 bytes, so the marker stays small.
_EVIDENCE_CAP_BYTES = 2000


def _capped(text: str) -> str:
    return text.encode("utf-8")[:_EVIDENCE_CAP_BYTES].decode("utf-8", "ignore")


def _blocking_items(items: list) -> list:
    out = []
    for i in items:
        if not isinstance(i, dict) or not isinstance(i.get("id"), str):
            continue
        rem = i.get("remaining")
        evidence = i.get("evidence")
        out.append({
            "id": i["id"],
            "remaining": [r for r in rem if isinstance(r, str)] if isinstance(rem, list) else [],
            "evidence": _capped(evidence) if isinstance(evidence, str) else "",
        })
    return out


def _ledger_halt_plan_and_blocking(repo_root: Path, halted: dict) -> tuple:
    """(plan_rel, blocking ids, blocking items) of a halt naming a relative, contained plan;
    (None, (), []) otherwise. Each item is `{id, remaining, evidence}` with strings only."""
    plan_rel = halted.get(HALT_PLAN_FIELD)
    if not isinstance(plan_rel, str) or not plan_rel or Path(plan_rel).is_absolute():
        return None, (), []
    if contained_path(repo_root / plan_rel, [repo_root]) is None:
        return None, (), []
    raw = halted.get("items")
    items = _blocking_items(raw if isinstance(raw, list) else [])
    return plan_rel, tuple(i["id"] for i in items), items


def _persist_run_outcome(repo_root: Path, run_id: str, halted: dict, params: dict) -> Optional[str]:
    """Write the run's review outcome into its record dir for an all-landed commit halt (a dict
    ``params.run_outcome`` with a dict ``inline_review`` and no incomplete chunk); returns its
    repo-relative posix path, else None."""
    outcome = params.get("run_outcome")
    if halted.get("halted") != "commit" or not isinstance(outcome, dict):
        return None
    if not isinstance(outcome.get("inline_review"), dict) or outcome.get("incomplete_chunks"):
        return None
    write_run_outcome(repo_root, run_id, outcome)
    return f"{RECORD_DIR_ROOT}/{run_id}/{RUN_OUTCOME_FILE}"


def _settle_ledger_pointer(
    repo_root: Path, run_id: str, halted: dict, sizing_rel: Optional[str], outcome_rel: Optional[str] = None
) -> None:
    """A landed ledger-check halt writes its plan's ledger-halt pointer; a commit halt with a
    persisted outcome writes a ``halted="commit"`` pointer naming it; any other landed halt for
    a known plan unlinks a stale one. A halt without a relative, contained plan touches nothing."""
    plan_rel, blocking, _items = _ledger_halt_plan_and_blocking(repo_root, halted)
    if plan_rel is None:
        return
    path = ledger_halt_path(repo_root, plan_rel)
    if halted["halted"] == "commit" and outcome_rel and sizing_rel:
        write_ledger_halt(path, LedgerHalt(run_id, plan_rel, sizing_rel, (), halted="commit", outcome=outcome_rel))
        return
    if halted["halted"] != "ledger-check":
        path.unlink(missing_ok=True)
        return
    if not sizing_rel:
        return
    write_ledger_halt(path, LedgerHalt(run_id, plan_rel, sizing_rel, blocking))


def _live_run_ids(repo_root: Path) -> list:
    """Run ids holding a live or claimed token; a read failure names none."""
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    try:
        return [r["run_id"] for r in abandoned_runs._token_records(repo_root)]
    except Exception:
        return []


HALT_RUN_REASON = "halted-run"


def _review_written(worktree_root: Path, session_id: Optional[str], run_id: str) -> Optional[dict]:
    """Review-stage writes since the run started; None when the source cannot answer."""
    from coordinator_core.ops.dispatch_emit.terminal_commit import _run_started_at

    try:
        return review_edits.review_writes(worktree_root, session_id, _run_started_at(worktree_root, run_id))
    except Exception:
        return None


def _integrity_findings(
    worktree_root: Path, run_id: str, base: Optional[str], paths: list, plan: Optional[str] = None
) -> list:
    """Parse and since-base-overlap findings over ``paths``; the overlap arm is skipped when the
    run has no recorded ``base``."""
    from coordinator_core.ops.dispatch_emit import landing_integrity

    found = landing_integrity.unparseable_py(worktree_root, paths)
    if base is not None and paths:
        found += landing_integrity.since_base_findings(
            worktree_root, paths, base, run_id=run_id, plan=plan
        )
    return found


def _land(
    worktree_root: Path, repo_root: Path, params: dict, run_id: str, record_dir_rel: str, halted: dict
) -> dict:
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error, _worktree_snapshot
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import split_sibling_paths
    from coordinator_core.ops.ceremony import commit_v2
    from coordinator_core.ops.dispatch_emit.terminal_commit import _own_prefix_files, _tracked_at_head
    from coordinator_core.ops.dispatch_emit import landing_integrity

    base = {"record_dir": record_dir_rel, "halted": halted["halted"]}
    run_base = landing_integrity.run_base_for(worktree_root, run_id, params)
    session_id: Optional[str] = params.get("session_id")
    if not (isinstance(session_id, str) and _UUID_RE.fullmatch(session_id)):
        session_id = None

    finished = done_chunks(worktree_root, params)
    declared: dict = {}  # path -> owning chunk id, first declarer wins
    report_cache: dict = {}
    for chunk in finished:
        for path in list(chunk.paths) + (_own_prefix_files(worktree_root, chunk, report_cache) or []):
            declared.setdefault(path, chunk.id)
    owner = declared
    home, _siblings = split_sibling_paths(list(owner), worktree_root)
    declared = [p for p in home if not p.startswith(record_dir_rel + "/")]
    sizing_rel = record_sizing_rel(worktree_root, run_id)
    if (
        sizing_rel
        and not Path(sizing_rel).is_absolute()
        and contained_path(worktree_root / sizing_rel, [worktree_root]) is not None
        and sizing_rel not in owner
        and not sizing_rel.startswith(record_dir_rel + "/")
    ):
        declared.append(sizing_rel)

    outcome_rel = _persist_run_outcome(worktree_root, run_id, halted, params)
    if (worktree_root / record_dir_rel).is_dir():
        plan_rel, blocking, blocking_items = (
            _ledger_halt_plan_and_blocking(repo_root, halted)
            if halted["halted"] == "ledger-check" else (None, (), [])
        )
        write_halt_marker(
            worktree_root / record_dir_rel,
            halted=halted["halted"],
            sizing_rel=sizing_rel,
            plan_rel=plan_rel,
            blocking=blocking if plan_rel else None,
            blocking_items=blocking_items if plan_rel else None,
            run_base_sha=run_base,
        )
    raw_docs = params.get(run_artifacts.DOC_PATHS_PARAM)
    artifacts = within_handoffs_home(worktree_root, run_artifacts.candidate_paths(
        worktree_root, run_id,
        sizing_rel=sizing_rel,
        plan_rel=halted.get(HALT_PLAN_FIELD) if isinstance(halted.get(HALT_PLAN_FIELD), str) else None,
        doc_paths=[d for d in raw_docs if isinstance(d, str)] if isinstance(raw_docs, list) else (),
    ))
    review_written = _review_written(worktree_root, session_id, run_id)
    review_literals = [] if review_written is None else [
        p for p in review_written if not review_edits._out_of_worktree(review_edits._norm(p))
    ]
    snapshot = _worktree_snapshot(
        worktree_root, declared + list(artifacts.paths()) + review_literals,
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
    paths, deleted = record_dir_changes(worktree_root, record_dir_rel, snapshot.changed)

    product: list = []
    gone: list = []
    for path in declared:
        if path not in snapshot.changed:
            continue
        (product if (worktree_root / path).is_file() else gone).append(path)
    tracked_gone = set(_tracked_at_head(worktree_root, gone))
    dropped = [{"path": p, "reason": "deletion-git-cannot-see"} for p in gone if p not in tracked_gone]
    gone = [p for p in gone if p in tracked_gone]
    withheld: dict = {}

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
    art_paths = list(extra_artifacts.present)
    art_deleted = list(extra_artifacts.deleted)

    holds: list = []
    warnings = commit_v2._peer_claim_warnings(
        worktree_root, product + gone + art_paths + art_deleted, holds, own_sid=session_id
    )
    held = {p for p, _sid in holds}
    withheld.update({p: "peer-held" for p in sorted(held)})
    # Warning text is commit_v2._peer_claim_warnings' contract: "claim state indeterminate (<exc>)" or "... for '<path>'".
    indeterminate = [w for w in warnings if w.startswith("claim state indeterminate")]
    if indeterminate:
        # Only declared product paths are withheld on an indeterminate claim; run artifacts (record, sizing) still land.
        everywhere = any(w.startswith("claim state indeterminate (") for w in indeterminate)
        for p in product + gone:
            if p not in held and (everywhere or f"claim state indeterminate for {p!r}" in indeterminate):
                withheld[p] = "peer-claim-indeterminate"
    pending = product + gone
    product = [p for p in product if p not in withheld]
    gone = [p for p in gone if p not in withheld]
    art_paths = [p for p in art_paths if p not in withheld]
    art_deleted = [p for p in art_deleted if p not in withheld]
    closure_open: dict = {}
    integrity: list = []
    if halted["halted"] != "ledger-check":
        withheld.update({p: "halt-record-only" for p in product + gone if p in owner})
    elif product or gone:
        from coordinator_core.authoring_leaks import import_closure, ts_import_closure
        from coordinator_core.ops.dispatch_emit import halt_closure

        partial = halt_closure.partial_chunks(owner, pending, product + gone)
        withheld.update({p: "chunk-write-set-partial" for p in product + gone if owner.get(p) in partial})
        product = [p for p in product if p not in withheld]
        gone = [p for p in gone if p not in withheld]

        closure_open = halt_closure.open_chunks(
            worktree_root, owner, product + gone + art_paths, (import_closure, ts_import_closure)
        )
        withheld.update({p: "import-closure-open" for p in product + gone if owner.get(p) in closure_open})
        plan = halted.get("plan") if isinstance(halted.get("plan"), str) else None
        if not plan:
            marker, _ = _run_request(worktree_root, params.get("script_path"))
            plan = getattr(marker, "plan_path", None)
        integrity = _integrity_findings(
            worktree_root, run_id, run_base, [p for p in product if p not in withheld], plan
        )
        if integrity:
            bad_chunks = {owner[f.path]: f.reason for f in reversed(integrity) if f.path in owner}
            withheld.update({
                p: bad_chunks[owner[p]] for p in product + gone
                if p not in withheld and owner.get(p) in bad_chunks
            })
    product = [p for p in product if p not in withheld]
    gone = [p for p in gone if p not in withheld]
    unlanded_chunks = {owner[p] for p in withheld if p in owner}
    landed_chunks = sorted({owner[p] for p in product + gone if p in owner} - unlanded_chunks)
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
            committed=set(paths) | set(deleted), sibling_declared={}, contested=set(),
            present=lambda p: (worktree_root / p).is_file(),
        )
        unlanded = {p: HALT_RUN_REASON for p in sorted([*verdict.land, *verdict.halt])}
    review_reply["review_edits_unlanded"] = unlanded

    if not paths and not deleted:
        _settle_ledger_pointer(repo_root, run_id, halted, sizing_rel, outcome_rel)
        return {
            "committed": False, "sha": None, "nothing_to_commit": True,
            **({"withheld": withheld} if withheld else {}), **review_reply, **base,
        }

    message = halted_record_message(run_id, halted)
    for path in unlanded:
        message += "\nReview-Edit-Unlanded: " + path
    if landed_chunks:
        message += "\nLands-Chunks: " + ", ".join(landed_chunks)
    if landed_classes:
        message += "\nLands-Artifacts: " + ", ".join(f"{c}={n}" for c, n in sorted(landed_classes.items()))
    if deleted:
        message += "\nRemoves: " + ", ".join(deleted)
    commit_params: dict = {"paths": paths, "message": message}
    if deleted:
        commit_params["deleted_paths"] = deleted
        commit_params["declared_reverts"] = deleted
    if product or art_paths:
        commit_params["ignored_untracked"] = snapshot.ignored_among(paths)
    if session_id is not None:
        commit_params["session_id"] = session_id
    try:
        reply = reentrant_dispatch("ceremony.commit_v2", commit_params, repo_root=repo_root)
    except OpUnavailableError:
        return _error("ceremony.commit_v2 is not registered", **base)
    if not isinstance(reply, dict):
        return _error(f"unexpected commit_v2 reply: {reply!r}", **base)
    extra: dict = dict(review_reply)
    if not reply.get("error"):
        _settle_ledger_pointer(repo_root, run_id, halted, sizing_rel, outcome_rel)
        left = run_artifacts.residue(
            artifacts, set(snapshot.changed) - set(paths) - set(deleted), paths + deleted, withheld
        )
        if left:
            extra["run_artifact_residue"] = left
    extra["run_artifacts"] = {"landed": sorted(paths + deleted), "withheld": withheld}
    if landed_chunks:
        extra["landed_chunks"] = landed_chunks
    if withheld:
        extra["withheld"] = withheld
    if closure_open:
        extra["closure_open"] = closure_open
    if dropped:
        extra["dropped"] = dropped
    if integrity:
        extra["integrity"] = [{"path": f.path, "reason": f.reason, "detail": f.detail} for f in integrity]
    return {**reply, "paths": paths + deleted, **extra, **base}  # paths: everything the commit names
