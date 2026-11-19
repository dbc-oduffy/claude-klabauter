"""coordinator_core.ops.fleet._upstream_delivery -- deliver one new file onto a
receiver's branch, anchor it, and leave a durable push-pending record in the
sender's outbox.

Purpose: when the receiver's local branch is at or ahead of its tracking tip
the delivery lands ON that branch (bounded CAS, rebuilt on the new tip when it
loses); when local is behind tracking, or the bound is spent, the delivery is
an object-only commit on the tracking tip, pushable fast-forward.
Pure stdlib plus in-process git object and index I/O: no git spawn on the fast
path (`commit_authored_new_file` can fall back to one non-hook spawn).

Negative-spec:
    - Object-only arm: no receiver HEAD, branch ref, index or worktree is
      written. The only receiver writes are loose objects, the
      `refs/coordinator/inbox/` anchor (the memo blob) and the
      `refs/coordinator/inbox-pin/` pin (the commit, reachable through it
      until pushed).
    - Local-tip arm: one CAS of the branch ref (a non-HEAD branch: ref only;
      HEAD's branch: `commit_authored_new_file`, which refreshes the index,
      plus one O_EXCL worktree file). HEAD's symref never moves; no checkout,
      reset or stash.
    - `resolve_receiver_landing` only decides the branch name (the receiver
      repo's live work branch, never one named for the sender); a receiver
      with none is `STATUS_NO_BASE`. `apply_receiver_landing` is never called.
    - No push, no fetch: the push-pending record is the hand-off to the warm
      engine sweep.
    - A path already in the base tree is a `collision` outcome, never an
      overwrite; no record is written for it.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Union

from coordinator_core.git import git_objects, git_state
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.index_write import IndexWriteError, IndexWriteLockBusy, splice_index
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet import _memo_push_pending
from coordinator_core.ops.fleet import _receiver_landing
from coordinator_core.ops.fleet._memo_anchor import (
    ANCHOR_REF_PREFIX,
    read_pin,
    resolve_anchor,
    retire_anchor,
    retire_pin,
    write_anchor,
    write_pin,
)
from coordinator_core.ops.push_outstanding import resolve_push_remote
from coordinator_core.temp_layout import managed_tempdir

STATUS_DELIVERED = "delivered"
STATUS_COLLISION = "collision"
STATUS_NO_REMOTE = "no_remote"
STATUS_NO_BASE = "no_base"
STATUS_REFUSED = "refused"
STATUS_ANCHOR_FAILED = "anchor_failed"
STATUS_LANDING_LOST = "landing_lost"

_HEADS = "refs/heads/"
_REMOTES = "refs/remotes/"
#: Concurrent lands one delivery may race before it falls back to the object-only arm.
_CAS_ATTEMPTS = 3


class UpstreamDelivery(NamedTuple):
    """Outcome of `deliver_onto_upstream` / `rebuild_and_retarget`.

    `ok` is true only for `STATUS_DELIVERED`. `record` is the push-pending row
    (as written, or built when `rebuild_and_retarget` got no `sender_root`).
    `error` carries the refusal text for every non-delivered status.
    `on_branch` is true only when the delivery commit was landed on the
    receiver's local branch ref (the memo is on its working branch); false
    for the object-only, pinned-only arm. `warning` is set on an ok outcome
    whose anchor or pin could not be written after the branch landed: the memo
    is delivered, with no `record`, anchor or pin (the sweep pushes local's own
    history).
    """

    ok: bool
    status: str
    commit_sha: Optional[str] = None
    parent_sha: Optional[str] = None
    remote: Optional[str] = None
    branch: Optional[str] = None
    anchor_ref: Optional[str] = None
    blob_sha: Optional[str] = None
    record: Optional[dict] = None
    record_path: Optional[Path] = None
    error: str = ""
    on_branch: bool = False
    warning: str = ""


def _fail(status: str, error: str, **kw) -> UpstreamDelivery:
    return UpstreamDelivery(False, status, error=error, **kw)


def resolve_base(common_dir: Path, remote: str, branch: str) -> Optional[str]:
    """Tracking tip, else the local branch tip, else the `main`/`master` tip; `None` if none exist
    or `branch` is empty (a receiver with no deliverable branch)."""
    if not branch:
        return None
    for ref in (
        f"refs/remotes/{remote}/{branch}",
        f"{_HEADS}{branch}",
        f"{_HEADS}main",
        f"{_HEADS}master",
    ):
        sha = git_objects.read_ref_loose_or_packed(common_dir, ref)
        if sha:
            return sha
    return None


def path_in_tree(receiver_root: Path, commit_sha: str, rel_path: str) -> Optional[bool]:
    """Whether `rel_path` exists in `commit_sha`'s tree; `None` when the tree is unreadable."""
    info = git_state.read_commit(receiver_root, commit_sha)
    if info is None:
        return None
    normalized = rel_path.replace("\\", "/").strip("/")
    spine = git_state.read_tree_spine(receiver_root, [normalized], root_tree_sha=info.tree)
    if spine is None:
        return None
    parent_dir, _, leaf = normalized.rpartition("/")
    return spine.get(parent_dir, {}).get(leaf) is not None


def _build_on_base(
    receiver_root: Path,
    rel_path: str,
    content: str,
    msg_file: Union[str, Path],
    base: str,
    *,
    remote: str,
    branch: str,
    message: str,
    land: Optional[Callable[[str], bool]] = None,
) -> UpstreamDelivery:
    """Build `rel_path` on `base`, anchor and pin it; with `land`, move the branch ref to the commit
    after the pin is written and retire the anchor and pin if `land` returns False
    (`STATUS_LANDING_LOST`)."""
    present = path_in_tree(receiver_root, base, rel_path)
    if present is None:
        return _fail(
            STATUS_NO_BASE,
            f"base {base} is not a readable commit with a readable tree",
            parent_sha=base, remote=remote, branch=branch,
        )
    if present:
        return _fail(
            STATUS_COLLISION,
            f"{rel_path!r} already exists in {base}",
            parent_sha=base, remote=remote, branch=branch,
        )
    built = git_native.build_new_file_commit(
        rel_path, content, msg_file, receiver_root, parent_sha=base
    )
    commit_sha = built.stdout.strip()
    if not built.ok or not commit_sha:
        return _fail(
            STATUS_REFUSED, built.stderr.strip() or "build_new_file_commit failed",
            parent_sha=base, remote=remote, branch=branch,
        )
    return _anchor_and_record(
        receiver_root, rel_path, content, commit_sha, base,
        remote=remote, branch=branch, message=message, land=land,
    )


def _anchor_and_record(
    receiver_root: Path,
    rel_path: str,
    content: str,
    commit_sha: str,
    base: str,
    *,
    remote: str,
    branch: str,
    message: str,
    land: Optional[Callable[[str], bool]] = None,
) -> UpstreamDelivery:
    filename = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    common_dir = resolve_git_common_dir(receiver_root)
    blob_sha = write_anchor(common_dir, filename, commit_sha, content.encode("utf-8"))
    if blob_sha is None:
        return _fail(
            STATUS_ANCHOR_FAILED,
            f"anchor for {filename!r} at {commit_sha} could not be written",
            commit_sha=commit_sha, parent_sha=base, remote=remote, branch=branch,
        )
    if not write_pin(common_dir, commit_sha):
        retire_anchor(common_dir, filename, commit_sha)
        return _fail(
            STATUS_ANCHOR_FAILED,
            f"pin for {commit_sha} could not be written",
            commit_sha=commit_sha, parent_sha=base, remote=remote, branch=branch,
        )
    if land is not None and not land(commit_sha):
        retire_pin(common_dir, commit_sha)
        retire_anchor(common_dir, filename, commit_sha)
        return _fail(
            STATUS_LANDING_LOST, f"branch ref moved before {commit_sha} landed",
            parent_sha=base, remote=remote, branch=branch,
        )
    record = {
        "receiver_root": str(receiver_root),
        "remote": remote,
        "branch": branch,
        "path": rel_path.replace("\\", "/"),
        "blob_sha": blob_sha,
        "message": message,
        "commit_sha": commit_sha,
        "parent_sha": base,
    }
    return UpstreamDelivery(
        True, STATUS_DELIVERED, commit_sha, base, remote, branch,
        ANCHOR_REF_PREFIX + filename + "/" + commit_sha, blob_sha, record,
    )


def verify_delivery(receiver_root: Union[str, Path], out: UpstreamDelivery) -> Optional[str]:
    """Read back every claim of a delivered `out`; the error text, or `None` when all hold.

    Checks the pin value, the anchor value, the file in the commit's tree (an
    unreadable tree fails) and, when `out.record_path` is set, the parsed record.
    """
    receiver_root = Path(receiver_root)
    common_dir = resolve_git_common_dir(receiver_root)
    sha = out.commit_sha or ""
    if out.warning:
        landed = git_objects.read_ref_loose_or_packed(common_dir, f"{_HEADS}{out.branch}")
        if landed != sha:
            return f"{_HEADS}{out.branch} does not resolve to the delivery commit {sha!r}"
        return None
    if read_pin(common_dir, sha) != sha:
        return f"pin ref for {sha!r} does not resolve to the delivery commit"
    if not out.anchor_ref or git_objects.read_ref_loose_or_packed(common_dir, out.anchor_ref) != out.blob_sha:
        return f"anchor {out.anchor_ref!r} does not resolve to the memo blob {out.blob_sha!r}"
    rel_path = (out.record or {}).get("path", "")
    if not rel_path or path_in_tree(receiver_root, sha, rel_path) is not True:
        return f"{rel_path!r} is not readable in the tree of {sha!r}"
    if out.record_path is not None:
        try:
            row = json.loads(Path(out.record_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return f"push-pending record {out.record_path} is unreadable"
        if not isinstance(row, dict) or row.get("commit_sha") != sha:
            return f"push-pending record {out.record_path} does not name {sha!r}"
    return None


def retire_delivery_refs(receiver_root: Union[str, Path], out: UpstreamDelivery) -> None:
    """Retire the pin and anchor a refused `out` wrote."""
    common_dir = resolve_git_common_dir(Path(receiver_root))
    if out.commit_sha:
        retire_pin(common_dir, out.commit_sha)
        if out.anchor_ref and out.anchor_ref.startswith(ANCHOR_REF_PREFIX):
            filename = out.anchor_ref[len(ANCHOR_REF_PREFIX):].rsplit("/", 1)[0]
            retire_anchor(common_dir, filename, out.commit_sha)


def _local_tip_to_land_on(common_dir: Path, remote: str, branch: str) -> Optional[str]:
    """The local branch tip when it is at or ahead of its tracking tip (or has none); else `None`."""
    local = git_objects.read_ref_loose_or_packed(common_dir, f"{_HEADS}{branch}")
    if not local:
        return None
    tracking = git_objects.read_ref_loose_or_packed(common_dir, f"{_REMOTES}{remote}/{branch}")
    if tracking is None or tracking == local or _receiver_landing._is_ancestor(common_dir, tracking, local):
        return local
    return None


def _land_on_local_ref(
    receiver_root: Path, rel_path: str, content: str, msg_file: Union[str, Path],
    *, remote: str, branch: str, message: str,
) -> Optional[UpstreamDelivery]:
    """Non-HEAD branch: build on the local tip and CAS the branch ref, rebuilding on the new tip
    when the CAS loses, `_CAS_ATTEMPTS` times. `None` when local is not at or ahead of tracking
    or the bound is spent."""
    common_dir = resolve_git_common_dir(receiver_root)
    ref = f"{_HEADS}{branch}"
    for _ in range(_CAS_ATTEMPTS):
        tip = _local_tip_to_land_on(common_dir, remote, branch)
        if tip is None:
            return None

        def _land(sha: str, tip: str = tip) -> bool:
            return git_objects.cas_ref(common_dir, ref, tip, sha)

        out = _build_on_base(
            receiver_root, rel_path, content, msg_file, tip,
            remote=remote, branch=branch, message=message, land=_land,
        )
        if out.status != STATUS_LANDING_LOST:
            return out._replace(on_branch=True) if out.ok else out
    return None


def _refresh_index_entry(receiver_root: Path, rel_path: str, content: str) -> None:
    """Stage `rel_path` in the receiver's own index by the in-process `splice_index` (zero spawns;
    the index is located from `receiver_root`, never from an inherited `GIT_INDEX_FILE`). An index
    the splice refuses or a peer-held lock leaves the index untouched: best-effort, never raises."""
    try:
        blob = git_objects.write_object(resolve_git_common_dir(receiver_root), b"blob", content.encode("utf-8"))
        splice_index(receiver_root, {rel_path.replace("\\", "/"): (0o100644, blob)})
    except (OSError, IndexWriteError, IndexWriteLockBusy):
        pass


def _land_on_head_branch(
    receiver_root: Path, rel_path: str, content: str, msg_file: Union[str, Path],
    *, remote: str, branch: str, message: str,
) -> Optional[UpstreamDelivery]:
    """HEAD's own branch: one O_EXCL worktree file, then `commit_authored_new_file` (index refreshed),
    retried `_CAS_ATTEMPTS` times on a lost CAS. `None` (worktree file removed) when the file is already
    on disk, local is not at or ahead of tracking, or the bound is spent."""
    common_dir = resolve_git_common_dir(receiver_root)
    first_tip = _local_tip_to_land_on(common_dir, remote, branch)
    if first_tip is None or path_in_tree(receiver_root, first_tip, rel_path) is None:
        return None
    target = receiver_root / rel_path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except OSError:
        return None

    def _rollback() -> None:
        try:
            target.unlink()
        except OSError:
            pass

    for _ in range(_CAS_ATTEMPTS):
        tip = _local_tip_to_land_on(common_dir, remote, branch)
        if tip is None:
            break
        if path_in_tree(receiver_root, tip, rel_path):
            _rollback()
            return _fail(
                STATUS_COLLISION, f"{rel_path!r} already exists in {tip}",
                parent_sha=tip, remote=remote, branch=branch,
            )
        landed = git_native.commit_authored_new_file(
            rel_path, content, msg_file, receiver_root, refresh_shared_index=False
        )
        sha = landed.stdout.strip()
        if not landed.ok or not sha:
            continue
        info = git_state.read_commit(receiver_root, sha)
        if info is None:
            _rollback()
            return _fail(
                STATUS_REFUSED, f"landed commit {sha} is not a readable object; not sent",
                commit_sha=sha, parent_sha=tip, remote=remote, branch=branch,
            )
        _refresh_index_entry(receiver_root, rel_path, content)
        parent = info.parents[0] if info.parents else tip
        out = _anchor_and_record(
            receiver_root, rel_path, content, sha, parent,
            remote=remote, branch=branch, message=message,
        )
        if not out.ok:
            return UpstreamDelivery(
                True, STATUS_DELIVERED, sha, parent, remote, branch,
                on_branch=True, warning=out.error,
            )
        return out._replace(on_branch=True)
    _rollback()
    return None


def deliver_onto_upstream(
    receiver_root: Union[str, Path],
    sender_root: Union[str, Path],
    rel_path: str,
    content: str,
    msg_file: Union[str, Path],
) -> UpstreamDelivery:
    """Deliver `content` at `rel_path`: onto the receiver's local branch tip (`on_branch`) when local
    is at or ahead of tracking, else as an object-only commit on the upstream tip.

    Writes the anchor and one push-pending record under `sender_root`'s outbox.
    Returns a non-`ok` outcome (and writes no record) for a collision, a
    missing remote or base, a clean-pipeline refusal, or a failed anchor.
    """
    receiver_root = Path(receiver_root)
    landing = _receiver_landing.resolve_receiver_landing(receiver_root)
    if landing.mode == _receiver_landing.MODE_NONE:
        return _fail(STATUS_NO_BASE, "no deliverable branch in the receiver repo (no work/*, HEAD, main or master)")
    branch = landing.ref_relpath[len(_HEADS):]
    remote = resolve_push_remote(receiver_root, branch)
    if remote is None:
        return _fail(STATUS_NO_REMOTE, f"no push remote resolvable for {branch!r}", branch=branch)
    common_dir = resolve_git_common_dir(receiver_root)
    base = resolve_base(common_dir, remote, branch)
    if base is None:
        return _fail(
            STATUS_NO_BASE, f"no tracking, local or main/master tip for {branch!r}",
            remote=remote, branch=branch,
        )
    message = Path(msg_file).read_text(encoding="utf-8")
    land_local = _land_on_head_branch if landing.mode == _receiver_landing.MODE_HEAD else _land_on_local_ref
    out = land_local(
        receiver_root, rel_path, content, msg_file,
        remote=remote, branch=branch, message=message,
    )
    if out is None:
        out = _build_on_base(
            receiver_root, rel_path, content, msg_file, base,
            remote=remote, branch=branch, message=message,
        )
    if not out.ok or out.record is None:
        return out
    written = _memo_push_pending.write_pending(sender_root, out.record)
    return out._replace(record_path=written)


def rebuild_and_retarget(
    record: dict,
    new_parent: str,
    *,
    sender_root: Union[str, Path, None] = None,
) -> UpstreamDelivery:
    """Rebuild `record`'s commit onto `new_parent` (a moved upstream tip).

    The content is read back from the receiver's anchored blob. With
    `sender_root`, the new record is written and the old one removed
    (`retarget_pending`); without it the new record is only returned. A
    `new_parent` tree already holding the path yields `STATUS_COLLISION` and
    changes no record: the caller retires the old one.
    """
    receiver_root = Path(record["receiver_root"])
    data = resolve_anchor(resolve_git_common_dir(receiver_root), record["blob_sha"])
    if data is None:
        return _fail(
            STATUS_REFUSED, f"anchored blob {record['blob_sha']} is unreadable",
            parent_sha=new_parent, remote=record["remote"], branch=record["branch"],
        )
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", prefix="memo-rebuild-msg-", suffix=".txt",
        dir=managed_tempdir(), delete=False,
    )
    try:
        with tmp:
            tmp.write(record["message"])
        out = _build_on_base(
            receiver_root, record["path"], data.decode("utf-8"), tmp.name, new_parent,
            remote=record["remote"], branch=record["branch"], message=record["message"],
        )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    if not out.ok or sender_root is None or out.record is None:
        return out
    written = _memo_push_pending.retarget_pending(sender_root, record["commit_sha"], out.record)
    return out._replace(record_path=written)
