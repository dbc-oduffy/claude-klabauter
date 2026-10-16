"""The halted-run commit: ``dispatch.terminal_commit`` given ``halted_by``.

A halted engine-ask run leaves its ``state/warp/<run_id>/`` record directory behind; this lands it
as one ``ceremony.commit_v2`` call carrying that directory's top-level files, the run's
``run_artifacts`` candidate set (sizing, baton, plan, evidence sidecar, slice sizings, superseded
record dirs, claimed docs-stage paths), the declared writes of every chunk the run finished, and
the halt reason in the message. Candidates are named paths, never the dirty tree. A finished chunk's tracked
deletion is declared, a deletion git cannot see is dropped, and a path a live peer's claim holds
is withheld and named. With no finished chunk the commit is record-only. No receipt, sizing ship,
plan stamp or close job runs, and the run token is redeemed then always released: a halt never
spends the token the resumed run's product commit needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit import run_artifacts
from coordinator_core.ops.warp_run_deliverable import record_sizing_rel
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_PLAN_FIELD,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    LedgerHalt,
    halted_record_message,
    ledger_halt_path,
    write_ledger_halt,
)


def is_halted_record_call(params: object) -> bool:
    """True when ``params[HALTED_RECORD_PARAM]`` is a dict with a non-empty str ``halted``."""
    halted = params.get(HALTED_RECORD_PARAM) if isinstance(params, dict) else None
    return isinstance(halted, dict) and isinstance(halted.get("halted"), str) and bool(halted["halted"])


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

    token = params.get("run_token")
    script_path = params.get("script_path")
    if token is not None and not run_token.redeem(worktree_root, run_id, token, script_path):
        return _error(
            "params.run_token does not match a live, unspent token minted for params.run_id and "
            "this script (a token admits one landed commit)",
            refused="run-token",
        )
    try:
        reply = _land(worktree_root, repo_root, params, run_id, record_dir_rel, halted)
        from coordinator_core.ops.dispatch_emit.run_close import sibling_release

        siblings = sibling_release(worktree_root, run_id, None)
        if siblings is not None:
            reply["siblings"] = siblings
        return reply
    finally:
        if token is not None:
            run_token.release(worktree_root, run_id, token, script_path)


def _settle_ledger_pointer(repo_root: Path, run_id: str, halted: dict, sizing_rel: Optional[str]) -> None:
    """A landed ledger-check halt writes its plan's ledger-halt pointer; any other landed halt for
    a known plan unlinks a stale one. A halt without a relative, contained plan touches nothing."""
    plan_rel = halted.get(HALT_PLAN_FIELD)
    if not isinstance(plan_rel, str) or not plan_rel or Path(plan_rel).is_absolute():
        return
    if contained_path(repo_root / plan_rel, [repo_root]) is None:
        return
    path = ledger_halt_path(repo_root, plan_rel)
    if halted["halted"] != "ledger-check":
        path.unlink(missing_ok=True)
        return
    if not sizing_rel:
        return
    items = halted.get("items")
    blocking = tuple(
        i["id"] for i in (items if isinstance(items, list) else [])
        if isinstance(i, dict) and isinstance(i.get("id"), str)
    )
    write_ledger_halt(path, LedgerHalt(run_id, plan_rel, sizing_rel, blocking))


def _live_run_ids(repo_root: Path) -> list:
    """Run ids holding a live or claimed token; a read failure names none."""
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    try:
        return [r["run_id"] for r in abandoned_runs._token_records(repo_root)]
    except Exception:
        return []


def _land(
    worktree_root: Path, repo_root: Path, params: dict, run_id: str, record_dir_rel: str, halted: dict
) -> dict:
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error, _worktree_snapshot
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import split_sibling_paths
    from coordinator_core.ops.dispatch_emit.terminal_commit import _own_prefix_files, _peer_held, _tracked_at_head

    base = {"record_dir": record_dir_rel, "halted": halted["halted"]}
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

    if (worktree_root / record_dir_rel).is_dir():
        write_halt_marker(worktree_root / record_dir_rel, halted=halted["halted"], sizing_rel=sizing_rel)
    raw_docs = params.get(run_artifacts.DOC_PATHS_PARAM)
    artifacts = run_artifacts.candidate_paths(
        worktree_root, run_id,
        sizing_rel=sizing_rel,
        plan_rel=halted.get(HALT_PLAN_FIELD) if isinstance(halted.get(HALT_PLAN_FIELD), str) else None,
        doc_paths=[d for d in raw_docs if isinstance(d, str)] if isinstance(raw_docs, list) else (),
    )
    snapshot = _worktree_snapshot(
        worktree_root, declared + list(artifacts.paths()), [record_dir_rel, RECORD_DIR_ROOT], []
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
    held = _peer_held(worktree_root, session_id, product + gone)
    withheld = {p: "peer-held" for p in sorted(held)}
    product = [p for p in product if p not in held]
    gone = [p for p in gone if p not in held]
    landed_chunks = sorted({owner[p] for p in product + gone if p in owner})
    paths = paths + product
    deleted = deleted + gone

    handled = set(paths) | set(deleted) | set(declared) | set(withheld)
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
    paths = paths + art_paths
    deleted = deleted + art_deleted
    landed_classes: dict = {}
    for p in art_paths + art_deleted:
        cls = artifacts.class_of(p)
        if cls:
            landed_classes[cls] = landed_classes.get(cls, 0) + 1
    if not paths and not deleted:
        _settle_ledger_pointer(repo_root, run_id, halted, sizing_rel)
        return {
            "committed": False, "sha": None, "nothing_to_commit": True,
            **({"withheld": withheld} if withheld else {}), **base,
        }

    message = halted_record_message(run_id, halted)
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
    extra: dict = {}
    if not reply.get("error"):
        _settle_ledger_pointer(repo_root, run_id, halted, sizing_rel)
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
    if dropped:
        extra["dropped"] = dropped
    return {**reply, "paths": paths + deleted, **extra, **base}  # paths: everything the commit names
