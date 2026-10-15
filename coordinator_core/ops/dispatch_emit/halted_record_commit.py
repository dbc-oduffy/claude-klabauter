"""The halted-run record-only commit: ``dispatch.terminal_commit`` given ``halted_by``.

A halted engine-ask run leaves its ``state/warp/<run_id>/`` record directory behind; this lands it
as one ``ceremony.commit_v2`` call carrying that directory's top-level files, the declared writes
of every chunk the run finished, and the halt reason in the message. A finished chunk's tracked
deletion is declared, a deletion git cannot see is dropped, and a path a live peer's claim holds
is withheld and named. With no finished chunk the commit is record-only. No receipt, sizing ship,
plan stamp or close job runs, and the run token is redeemed then always released: a halt never
spends the token the resumed run's product commit needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    halted_record_message,
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
        return _land(worktree_root, repo_root, params, run_id, record_dir_rel, halted)
    finally:
        if token is not None:
            run_token.release(worktree_root, run_id, token, script_path)


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

    snapshot = _worktree_snapshot(worktree_root, declared, [record_dir_rel], [])
    if snapshot is None:
        return _error(f"git status over {record_dir_rel} failed", **base)
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
    landed_chunks = sorted({owner[p] for p in product + gone})
    paths = paths + product
    deleted = deleted + gone
    if not paths and not deleted:
        return {
            "committed": False, "sha": None, "nothing_to_commit": True,
            **({"withheld": withheld} if withheld else {}), **base,
        }

    message = halted_record_message(run_id, halted)
    if landed_chunks:
        message += "\nLands-Chunks: " + ", ".join(landed_chunks)
    if deleted:
        message += "\nRemoves: " + ", ".join(deleted)
    commit_params: dict = {"paths": paths, "message": message}
    if deleted:
        commit_params["deleted_paths"] = deleted
        commit_params["declared_reverts"] = deleted
    if product:
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
    if landed_chunks:
        extra["landed_chunks"] = landed_chunks
    if withheld:
        extra["withheld"] = withheld
    if dropped:
        extra["dropped"] = dropped
    return {**reply, "paths": paths + deleted, **extra, **base}  # paths: everything the commit names
