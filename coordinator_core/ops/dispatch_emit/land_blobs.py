"""`dispatch.land_blobs`: lands a reviewed run's declared blobs, and only the run's recorded hunks.

Shape: one scandir per declared directory, one read per declared file, the tree, pack and index written in
process by `run_landing`'s writer, one compare-and-swap on the ref, then the worktree 3-way sync of the landed
paths (`land_blobs_worktree_sync`), no git spawn by default. Product branch only: the record modes stay on
their modules.

Invariant: a declared path whose check-in blob is not in the run's own recorded set (`kept.json`), and a
modified path with no own record that carries a hunk another run recorded, refuses the whole landing
(`FOREIGN_HUNK`) before any object is written.
Trap: `kept.json` blobs are RAW bytes and the staged blob is check-in form; the two are compared only after
lifting the recorded blob through `kept_work.checkin_form`.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import land_blobs_worktree_sync as wsync
from coordinator_core.ops.dispatch_emit import run_landing as rl
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha as _sha
from coordinator_core.ops.workflow_fire.finalizer import terminal_action as _private_terminal_action

from coordinator_core.ops.dispatch_emit.land_blobs_contract import (
    DISPATCH_ABANDONED,
    FOREIGN_HUNK,
    HEAD_MOVED,
    INDEX_LOCK_BUSY,
    MODE_AMBIGUOUS,
    MODE_KEYS,
    OP_NAME,
    RETRIED_REFUSALS,
    RETRY_ATTEMPTS,
    SECRET_IN_DIFF,
    TEMP_OP_NAME,
)


@register_op(TEMP_OP_NAME)
@_private_terminal_action(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Op handler: `land(params, repo_root)` under the call's I/O scope."""
    from coordinator_core.git.call_io_scope import call_io_scope

    with call_io_scope():
        return land(params, repo_root)


def land(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Land one run's declared blobs; the reply is `{committed, sha, landed_paths, changed_paths}` or a refusal with `refused`.

    `repo_root` is the git common dir, as the op handler receives it."""
    if repo_root is None:
        return rl._error(f"{OP_NAME} requires a common_dir-keyed dispatch; repo_root was not supplied")
    if not isinstance(params, dict):
        return rl._error("params must be an object")
    if any(params.get(k) for k in MODE_KEYS):
        return rl._error("land_blobs lands the product branch only", refused=MODE_AMBIGUOUS)

    from coordinator_core.lifecycle import main_worktree_root

    root = main_worktree_root(repo_root)
    refusal = rl._review_floor(params.get("inline_review"))
    if refusal is not None:
        return refusal
    token = rl._claim_token(root, params)
    if isinstance(token, dict):
        return token
    landed, sha = False, None
    try:
        reply = _land(root, params)
        landed, sha = bool(reply.get("committed")), reply.get("sha")
    except rl._Refused as exc:
        reply = rl._error(str(exc), refused=exc.kind, **exc.extra)
    finally:
        rl._settle_token(root, params, token, landed, sha)
    return reply


def _land(root: Path, params: dict) -> dict:
    request, ask_run_dir, script_text = rl._read_request(root, params)
    incomplete = set(params.get("incomplete_chunks") or []) - set(params.get("landed_chunks") or [])
    landing, stranded, chunk_ids, _by_chunk = rl._declared(root, request, incomplete)
    run_id = rl._run_id(params, ask_run_dir, params["script_path"])
    if not landing:
        return {"committed": False, "nothing_to_commit": True, "ok": True, "stranded": stranded}
    reply: dict = {}
    for attempt in range(RETRY_ATTEMPTS):
        reply = _attempt(root, params, request, script_text, landing, chunk_ids, run_id)
        if reply.get("refused") not in RETRIED_REFUSALS:
            break
        time.sleep(0.005 * (attempt + 1))
    reply.update(stranded=stranded, chunks_committed=chunk_ids, ok=bool(reply.get("committed")))
    gap = _unreviewed(params["inline_review"], landing)
    if gap:
        reply["unreviewed_paths"] = gap
    return reply


def _unreviewed(inline_review: dict, paths: list) -> list:
    """Declared paths no reviewed slice covers (RL-03): a reply field until the APM rules it a refusal."""
    from coordinator_core.ops.dispatch_emit import review_floor

    prep = inline_review.get("prep")
    reviewed = prep.get("slice_files") if isinstance(prep, dict) else None
    return list(review_floor.slice_gap_paths(paths, reviewed)) if isinstance(reviewed, list) and reviewed else []


# --- the recorded set ------------------------------------------------------------------------------------


def _chunk_blobs(run_dir: Path) -> dict:
    """`{path: {blob sha}}` from `chunks.<id>.paths` of the record dir's `kept.json`; empty when absent."""
    try:
        with open(run_dir / "kept.json", "rb") as handle:
            kept = json.loads(handle.read())
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for chunk in (kept.get("chunks") or {}).values() if isinstance(kept, dict) else ():
        for path, sha in ((chunk or {}).get("paths") or {}).items():
            if isinstance(sha, str):
                out.setdefault(rl._norm(path), set()).add(sha)
    return out


def _peer_records(root: Path, own_run: str, rels: set) -> dict:
    """`{path: {run id: {blob sha}}}` the most recent other runs' `kept.json` hold for `rels`: one scandir, one
    open per run (`stranded_sibling.recent_run_dirs` bounds the runs)."""
    from coordinator_core.ops.dispatch_emit import stranded_sibling
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    found: dict = {}
    for name, run_dir in stranded_sibling.recent_run_dirs(root / RECORD_DIR_ROOT, own_run):
        for path, shas in _chunk_blobs(run_dir).items():
            if path in rels:
                found.setdefault(path, {})[name] = shas
    return found


def _matches_recorded(root: Path, common: Path, form, rel: str, sha: str, blobs: set) -> bool:
    """True when the staged check-in blob `sha` is one of the recorded blobs, once each raw recorded blob
    is lifted to check-in form."""
    from coordinator_core.git.git_objects import read_object

    if sha in blobs:
        return True
    d, _, name = rel.rpartition("/")
    for blob in blobs:
        stored = read_object(common, blob)
        if stored is not None and stored[0] == "blob" and _sha(form(d, name, rel, stored[1])) == sha:
            return True
    return False


def _foreign_runs(common: Path, peers: dict, rel: str, head_blob: Optional[str], data: bytes) -> set:
    """Runs whose recorded blobs of `rel` made a hunk the staged `data` also carries over HEAD."""
    from coordinator_core.git.git_objects import read_object

    stored = read_object(common, head_blob) if head_blob else None
    if stored is None:
        return set()
    runs = set()
    for run, shas in peers.get(rel, {}).items():
        peer_bytes = [b[1] for b in (read_object(common, s) for s in shas) if b is not None]
        if peer_bytes and rl._own_view(stored[1], data, peer_bytes) != data:
            runs.add(run)
    return runs


def _own_cleared(root: Path, common: Path, run_id: str, rows: dict, suspects: set) -> tuple:
    """`(cleared, {path: [{lines, owner}]})` over the suspect foreign paths: a path whose every hunk the run
    itself recorded (even one a peer recorded too) is cleared; the others keep the hunks that name an owner."""
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.ops.dispatch_emit import own_hunk_candidate as ohc

    cleared: set = set()
    named: dict = {}
    for rel in suspects:
        row = rows.get(rel)
        if row is None:
            continue
        head_entry = row[5]
        stored = read_object(common, head_entry[1]) if head_entry else None
        if head_entry and stored is None:
            continue
        res = ohc.candidate(root, run_id, rel, stored[1] if stored else b"", row[2], declared=False)
        if "excluded" in res and not res["excluded"]:
            cleared.add(rel)
        else:
            named[rel] = res.get("foreign") or res.get("excluded") or []
    return cleared, named


# --- one landing attempt ---------------------------------------------------------------------------------


def _attempt(root: Path, params: dict, request, script_text: str, landing: list, chunk_ids: list,
             run_id: str) -> dict:
    from coordinator_core.git import commit as gc
    from coordinator_core.git import git_objects, git_state
    from coordinator_core.git.call_io_scope import call_io_scope
    from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir

    spine = git_state.read_tree_spine(root, landing)
    if spine is None:
        raise rl._Refused("head-unreadable", "HEAD's tree could not be read")
    scans = rl._scandir_all(root, {p.rpartition("/")[0] for p in landing})
    present, deleted, dropped = rl._live_paths(root, landing, scans, spine)
    if deleted:
        spine = git_state.read_tree_spine(root, [*present, *deleted]) or spine
    gitdir, common = resolve_git_dir(root), resolve_git_common_dir(root)
    old_head = git_state.head_sha(root)
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    recorded = _chunk_blobs(record_dir(root, run_id))

    by_dir: dict = {}
    for rel in present:
        d, _, name = rel.rpartition("/")
        by_dir.setdefault(d, []).append(name)
    all_dirs = {""}
    for rel in (*present, *deleted):
        d = rel.rpartition("/")[0]
        while d:
            all_dirs.add(d)
            d = d.rpartition("/")[0]
    trees = {d: dict(spine.get(d, {})) for d in all_dirs}

    pending: dict = {}
    index_new: dict = {}
    buffers: dict = {}
    staged: dict = {}
    flagged: dict = {}
    mismatched: list = []
    unrecorded_modified: list = []
    symlinked = False
    with call_io_scope():
        form = rl._CheckinForm(root, by_dir)
        for d, names in by_dir.items():
            head_dir = spine.get(d, {})
            for name in names:
                rel = f"{d}/{name}" if d else name
                entry = scans[d][name]
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    symlinked = True
                    continue
                data, st = rl._read_file(entry)
                data = form(d, name, rel, data)
                sha = _sha(data)
                head_entry = head_dir.get(name)
                blobs = recorded.get(rel)
                row = (name, d, data, sha, st, head_entry)
                if blobs:
                    if not _matches_recorded(root, common, form, rel, sha, blobs):
                        mismatched.append(rel)
                        flagged[rel] = row
                        continue
                elif head_entry is not None and head_entry[1] != sha:
                    unrecorded_modified.append((rel, data, head_entry[1]))
                staged[rel] = row

        runs_by_path: dict = {}
        if mismatched or unrecorded_modified:
            peers = _peer_records(root, run_id, {*mismatched, *(r for r, _d, _h in unrecorded_modified)})
            for rel in mismatched:
                runs_by_path[rel] = set(peers.get(rel, {}))
            for rel, data, head_blob in unrecorded_modified:
                runs = _foreign_runs(common, peers, rel, head_blob, data)
                if runs:
                    runs_by_path[rel] = runs
        foreign_hunks: dict = {}
        if runs_by_path:
            cleared, foreign_hunks = _own_cleared(root, common, run_id, {**staged, **flagged}, set(runs_by_path))
            for rel in cleared:
                runs_by_path.pop(rel)
                if rel in flagged:
                    staged[rel] = flagged[rel]
        foreign_paths = set(runs_by_path)
        foreign_runs = set().union(*runs_by_path.values()) if runs_by_path else set()
        if foreign_paths:
            shown = ", ".join(sorted(foreign_paths)[:5])
            named = f"; recorded by {', '.join(sorted(foreign_runs))}" if foreign_runs else ""
            lines = "".join(
                f"; {rel} lines {h['lines']} owner {h['owner']}"
                for rel in sorted(foreign_paths)[:5] for h in foreign_hunks.get(rel, ())[:3]
            )
            return rl._error(f"{len(foreign_paths)} staged path(s) carry a hunk outside run {run_id}'s recorded "
                             f"set: {shown}{named}{lines}; landing it would take a peer run's hunk",
                             refused=FOREIGN_HUNK,
                             foreign_paths=sorted(foreign_paths), foreign_runs=sorted(foreign_runs),
                             foreign_hunks={rel: foreign_hunks[rel] for rel in sorted(foreign_paths) if rel in foreign_hunks})

        for rel, (name, d, data, sha, st, head_entry) in staged.items():
            rl._stage(rel, name, d, data, sha, st, head_entry, trees, pending, index_new, buffers, spine)

    findings = rl._secret_findings(common, spine, buffers)
    if findings:
        from coordinator_core.ops.dispatch_emit import landing_secrets as ls

        return rl._error(ls.refusal(findings) or "secret in diff", refused=SECRET_IN_DIFF,
                         secret_findings=[f._asdict() for f in findings[:10]])

    message = rl._message(root, params, request, chunk_ids, deleted, script_text, run_id, None)
    if old_head is None or symlinked or rl._needs_fallback(root, gitdir, {}):
        reply = rl._fallback_commit(root, present, deleted, message, {})
        if reply.get("committed"):
            reply.update(landed_paths=sorted([*present, *deleted]), changed_paths=sorted([*buffers, *deleted]),
                         **wsync.fallback_report(reply))
        return reply

    for rel in deleted:
        d, _, name = rel.rpartition("/")
        trees[d].pop(name, None)
        index_new[rel.encode("utf-8")] = None
    root_tree = rl._write_trees(trees, pending)
    if root_tree == git_state.head_tree_sha(root):
        return {"committed": False, "nothing_to_commit": True}
    name, email = gc._identity(root)
    who = f"{name} <{email}> {gc._stamp()}"
    body = f"tree {root_tree}\nparent {old_head}\nauthor {who}\ncommitter {who}\n\n{message}".encode("utf-8")
    commit_sha = hashlib.sha1(b"commit %d\x00" % len(body) + body).hexdigest()
    pending[commit_sha] = (b"commit", body)
    git_objects._flush_pack(common, pending)
    plan = wsync.sync_plan(spine, trees, sorted(buffers), deleted)
    reply = _publish(root, gitdir, old_head, commit_sha, message, index_new, (name, email))
    if reply is None:
        reply = {"committed": True, "sha": commit_sha, "landed_paths": sorted([*present, *deleted]),
                 "changed_paths": sorted([*buffers, *deleted])}
        if dropped:
            reply["dropped_absent"] = dropped
        reply.update(wsync.sync(root, plan))
    return reply


def _publish(root: Path, gitdir: Path, old_head: str, commit_sha: str, message: str, index_new: dict,
             identity: tuple) -> Optional[dict]:
    """Splice the index and move the ref by one compare-and-swap; a refusal reply, or None when the ref moved."""
    from coordinator_core import dispatch_abandon
    from coordinator_core.git import commit as gc
    from coordinator_core.git import git_objects
    from coordinator_core.git import index_write as iw

    target = gc._cas_target(root)
    index_bytes = rl._spliced_index(gitdir / "index", index_new)
    if target is None or index_bytes is None:
        return rl._error("the ref or the index is not a shape the in-process writer serves; nothing was committed",
                         refused=INDEX_LOCK_BUSY)
    ref_gitdir, ref = target
    if dispatch_abandon.is_abandoned():
        return rl._error("dispatch gave up before the ref update; the commit object is left unreferenced",
                         refused=DISPATCH_ABANDONED, sha=None)
    try:
        fd = os.open(str(gitdir / "index.lock"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return rl._error("index.lock is held by a peer; nothing was committed", refused=INDEX_LOCK_BUSY)
    prepared = iw.PreparedSplice(gitdir / "index.lock", gitdir / "index")
    try:
        with os.fdopen(fd, "wb") as handle:
            prepared._claim_owner()
            handle.write(index_bytes)
        name, email = identity
        if not git_objects.cas_ref(ref_gitdir, ref, old_head, commit_sha, reflog_committer=f"{name} <{email}>",
                                   reflog_message=f"commit: {message.splitlines()[0] if message else ''}",
                                   head_gitdir=gitdir):
            prepared.abort()
            return rl._error("HEAD moved while landing; nothing was overwritten", refused=HEAD_MOVED)
    except BaseException:
        prepared.abort()
        raise
    prepared.publish()
    return None
