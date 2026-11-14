"""coordinator_core.ops.fleet._upstream_delivery -- deliver one new file onto a
receiver's upstream tip as an object-only commit, anchor it, and leave a
durable push-pending record in the sender's outbox.

Purpose: a delivery commit built on the receiver's fetched tracking tip can
always be pushed fast-forward; one landed on a stale local branch strands.
Pure stdlib plus in-process git object I/O: zero spawns.

Negative-spec:
    - No receiver HEAD, branch ref, index or worktree is written. The only
      receiver writes are loose objects and the `refs/coordinator/inbox/`
      anchor (the commit is reachable only through it until pushed).
    - `resolve_receiver_landing` only decides the branch name;
      `apply_receiver_landing` is never called.
    - No push, no fetch: the push-pending record is the hand-off to the warm
      engine sweep.
    - A path already in the base tree is a `collision` outcome, never an
      overwrite; no record is written for it.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import NamedTuple, Optional, Union

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_state import read_commit, read_tree_spine
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet import _memo_push_pending
from coordinator_core.ops.fleet import _receiver_landing
from coordinator_core.ops.fleet._memo_anchor import ANCHOR_REF_PREFIX, resolve_anchor, write_anchor
from coordinator_core.ops.push_outstanding import resolve_push_remote
from coordinator_core.temp_layout import managed_tempdir

STATUS_DELIVERED = "delivered"
STATUS_COLLISION = "collision"
STATUS_NO_REMOTE = "no_remote"
STATUS_NO_BASE = "no_base"
STATUS_REFUSED = "refused"
STATUS_ANCHOR_FAILED = "anchor_failed"

_HEADS = "refs/heads/"


class UpstreamDelivery(NamedTuple):
    """Outcome of `deliver_onto_upstream` / `rebuild_and_retarget`.

    `ok` is true only for `STATUS_DELIVERED`. `record` is the push-pending row
    (as written, or built when `rebuild_and_retarget` got no `sender_root`).
    `error` carries the refusal text for every non-delivered status.
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


def _fail(status: str, error: str, **kw) -> UpstreamDelivery:
    return UpstreamDelivery(False, status, error=error, **kw)


def resolve_base(common_dir: Path, remote: str, branch: str) -> Optional[str]:
    """Tracking tip, else the local branch tip, else the `main`/`master` tip; `None` if none exist."""
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
    info = read_commit(receiver_root, commit_sha)
    if info is None:
        return None
    normalized = rel_path.replace("\\", "/").strip("/")
    spine = read_tree_spine(receiver_root, [normalized], root_tree_sha=info.tree)
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
) -> UpstreamDelivery:
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
    filename = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    blob_sha = write_anchor(
        resolve_git_common_dir(receiver_root), filename, commit_sha, content.encode("utf-8")
    )
    if blob_sha is None:
        return _fail(
            STATUS_ANCHOR_FAILED,
            f"anchor for {filename!r} at {commit_sha} could not be written",
            commit_sha=commit_sha, parent_sha=base, remote=remote, branch=branch,
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


def deliver_onto_upstream(
    receiver_root: Union[str, Path],
    sender_root: Union[str, Path],
    rel_path: str,
    content: str,
    msg_file: Union[str, Path],
) -> UpstreamDelivery:
    """Build `content` at `rel_path` as a commit on the receiver's upstream tip.

    Writes the anchor and one push-pending record under `sender_root`'s outbox.
    Returns a non-`ok` outcome (and writes no record) for a collision, a
    missing remote or base, a clean-pipeline refusal, or a failed anchor.
    """
    receiver_root = Path(receiver_root)
    landing = _receiver_landing.resolve_receiver_landing(receiver_root)
    branch = landing.ref_relpath[len(_HEADS):]
    remote = resolve_push_remote(receiver_root, branch)
    if remote is None:
        return _fail(STATUS_NO_REMOTE, f"no push remote resolvable for {branch!r}", branch=branch)
    base = resolve_base(resolve_git_common_dir(receiver_root), remote, branch)
    if base is None:
        return _fail(
            STATUS_NO_BASE, f"no tracking, local or main/master tip for {branch!r}",
            remote=remote, branch=branch,
        )
    message = Path(msg_file).read_text(encoding="utf-8")
    out = _build_on_base(
        receiver_root, rel_path, content, msg_file, base,
        remote=remote, branch=branch, message=message,
    )
    if not out.ok:
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
    if not out.ok or sender_root is None:
        return out
    written = _memo_push_pending.retarget_pending(sender_root, record["commit_sha"], out.record)
    return out._replace(record_path=written)
