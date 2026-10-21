"""
coordinator_core.ops.fleet.memo_migrate_group_em_inbox — memo.migrate_group_em_inbox MUTATING op.

Purpose: the Group EM home never receives memos (A-GROUP-EM-SENDS-MEMOS-AND-NEVER-RECEIVES-THEM).
Memos already sitting in the home's inbox move to the inbox `resolve_receiver_inbox(SENDER_ID)`
names — the same call `memo.send` makes, so the reroute rule and the migration rule cannot drift.
Each memo is delivered like a `memo.send` delivery (landing ref, O_EXCL write, zero-hook commit
in the receiver repo, anchor) and only then removed from the home.

Per home-inbox `*.md` (both layouts, `gem_home.home_inbox_dirs`):
  - absent at the target -> delivered and committed there, home copy removed (committed when the
    home's git tracks it, unlinked otherwise): `moved`.
  - present at the target with identical bytes -> home copy removed only: `already_present`.
  - present with different bytes, or already archived there -> both left: `collision`.
  - identical bytes in the receiver worktree but not committed at the landing tip -> both left:
    `pending` (a worktree file is not delivery; the home copy is the only durable one).
  - target unresolvable, a mirror, or no checkout -> nothing moves: `refused`.

Negative-spec:
  - Takes `dry_run` only; the home and the target are derived, never named by the caller.
  - Never writes without a commit: a declined or unverifiable commit rolls the write back and
    leaves the home copy.
  - Never scans beyond the home's two inbox directories; an absent or empty inbox spawns nothing.
  - Never spawns on the delivery path (`commit_authored_new_file` is zero-hook); the only spawn
    site is the removal commit of a home copy that the home's own git tracks.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from coordinator_core.git import git_state
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_ref_loose_or_packed
from coordinator_core.group_em import gem_home
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet import _receiver_landing
from coordinator_core.ops.fleet._common import (
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
)
from coordinator_core.ops.fleet._memo_anchor import write_anchor
from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    never_inbox_mirror_refusal,
    resolve_receiver_inbox,
    undeliverable_checkout_refusal,
)
from coordinator_core.ops.fleet.archive_actioned_memos import memo_archive_dest
from coordinator_core.ops.fleet.memo_heal import _blob_sha1
from coordinator_core.ops.fleet.memo_send import (
    _delivery_commit_is_object,
    _landing_tree_has,
    _record_delivery_in_receiver_index,
    _rollback_unwritten_file,
    _write_msg_file,
)
from coordinator_core.warm import push_cadence

_MODE = "migrate_group_em_inbox"
_KNOWN_PARAM_KEYS = frozenset({"dry_run"})
_HEADS_PREFIX = "refs/heads/"

ACTION_MOVED = "moved"
ACTION_ALREADY_PRESENT = "already_present"
REASON_COLLISION = "collision"
REASON_REFUSED = "refused"
REASON_PENDING = "pending"

MUTATES = [
    "state/cross-repo/inbox/*.md",
    "cross-repo/inbox/*.md",
]


def _validate_params(params: dict):
    dry_run = params.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, False,
            "memo.migrate_group_em_inbox: dry_run must be bool, got " + repr(type(dry_run).__name__),
        )
    unknown = set(params) - _KNOWN_PARAM_KEYS
    if unknown:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.migrate_group_em_inbox: unrecognized param(s) {sorted(unknown)} — "
            f"known params: {sorted(_KNOWN_PARAM_KEYS)}.",
        )
    return dry_run


def _landing_blob_sha(receiver: Path, landing, rel: str) -> Optional[str]:
    """Blob sha of `rel` in the tree the delivery would parent onto, else None."""
    common_dir = resolve_git_common_dir(receiver)
    tip = read_ref_loose_or_packed(common_dir, landing.ref_relpath) or landing.mint_sha
    info = git_state.read_commit(receiver, tip) if tip else None
    spine = (
        git_state.read_tree_spine(receiver, [rel], root_tree_sha=info.tree)
        if info is not None else None
    )
    if spine is None:
        return None
    parent, _, leaf = rel.rpartition("/")
    entry = spine.get(parent, {}).get(leaf)
    return entry[1] if entry else None


def _home_copy_tracked(home: Path, rel: str) -> bool:
    return (home / ".git").exists() and git_native._head_entry_for(home, rel) is not None


def _remove_home_copy(home: Path, src: Path, data: bytes, filename: str) -> Optional[str]:
    """Remove the home copy; None on success, else the failure reason (copy restored)."""
    rel = src.relative_to(home).as_posix()
    tracked = _home_copy_tracked(home, rel)
    try:
        src.unlink()
    except OSError as exc:
        return f"home copy could not be removed: {exc}"
    if not tracked:
        return None
    msg_file = _write_msg_file(f"cross-repo memo migrated out of the group-em home: {filename}\n")
    try:
        result = git_native.commit_scoped([rel], msg_file, home)
    finally:
        try:
            msg_file.unlink()
        except OSError:
            pass
    if result.ok:
        return None
    src.write_bytes(data)
    return f"home removal commit declined: {result.stderr}; home copy restored"


def _deliver(
    receiver: Path, target_file: Path, filename: str, content: str, landing,
) -> Optional[str]:
    """Deliver `content` like memo.send does; None on success, else the failure reason."""
    rel_path = os.path.relpath(target_file, receiver).replace(os.sep, "/")
    landing = _receiver_landing.apply_receiver_landing(landing)
    ref_direct = landing.mode == _receiver_landing.MODE_REF_DIRECT
    if not ref_direct:
        target_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(target_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except FileExistsError:
            return f"collision (race): {target_file} appeared before the write"
        except OSError as exc:
            return f"write-failed: {exc}"

    msg_file = _write_msg_file(
        f"cross-repo memo delivery: {filename}\n\nMigrated by memo.migrate_group_em_inbox.\n"
    )
    try:
        commit = git_native.commit_authored_new_file(
            rel_path, content, msg_file, receiver,
            refresh_shared_index=False,
            onto_ref=landing.ref_relpath if ref_direct else None,
        )
    finally:
        try:
            msg_file.unlink()
        except OSError:
            pass
    sha = commit.stdout.strip() if commit.ok else None
    if not commit.ok or not sha or not _delivery_commit_is_object(receiver, sha):
        detail = commit.stderr if not commit.ok else f"{sha!r} is not a commit object"
        rollback = "" if ref_direct else _rollback_unwritten_file(target_file)
        return f"receiver-side commit failed: {detail};{rollback}"

    if not ref_direct:
        _record_delivery_in_receiver_index(receiver, rel_path, content)
    write_anchor(
        resolve_git_common_dir(receiver), filename, sha,
        content.encode("utf-8") if ref_direct else target_file.read_bytes(),
    )
    push_cadence.note_foreign_delivery(receiver, landing.ref_relpath[len(_HEADS_PREFIX):])
    return None


def migrate_group_em_inbox(dry_run: bool = False) -> dict:
    """Move the group-em home's inbox memos to the G-EM reroute target; returns the op envelope.

    `acted` carries `{id, action: "moved" | "already_present"}`; `failed` carries
    `{id, reason}` with a `collision:` or `refused:` reason prefix, or a failure detail.
    """
    try:
        home = gem_home.resolve_home()
    except gem_home.HomeRefused as exc:
        return build_setup_error_result(_MODE, dry_run, f"memo.migrate_group_em_inbox: {exc}")

    sources: List[Path] = []
    for inbox in gem_home.home_inbox_dirs(home):
        if inbox.is_dir():
            sources.extend(sorted(p for p in inbox.glob("*.md") if p.is_file()))
    if not sources:
        return build_dry_run_result(_MODE, []) if dry_run else build_act_result(_MODE, [], [], [])

    try:
        inbox_dir, receiver, _ = resolve_receiver_inbox(gem_home.SENDER_ID)
    except (AmbiguousReceiverError, RegistryReadError) as exc:
        return build_setup_error_result(_MODE, dry_run, f"memo.migrate_group_em_inbox: {exc}")
    refusal = (
        never_inbox_mirror_refusal(gem_home.SENDER_ID, receiver)
        or undeliverable_checkout_refusal(gem_home.SENDER_ID, receiver)
        if receiver is not None else
        f"memo: {gem_home.SENDER_ID!r} resolves to no receiver"
    )

    acted: List[dict] = []
    failed: List[dict] = []
    candidates: List[dict] = []
    for src in sources:
        name = src.name
        if refusal:
            failed.append({"id": name, "reason": f"{REASON_REFUSED}: {refusal}"})
            continue
        try:
            data = src.read_bytes()
            content = data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            failed.append({"id": name, "reason": f"{REASON_REFUSED}: home copy unreadable as UTF-8 ({exc})"})
            continue

        target_file = inbox_dir / name
        rel = os.path.relpath(target_file, receiver).replace(os.sep, "/")
        archive_rel = os.path.relpath(memo_archive_dest(receiver, target_file), receiver).replace(os.sep, "/")
        landing = _receiver_landing.resolve_receiver_landing(receiver)
        ref_direct = landing.mode == _receiver_landing.MODE_REF_DIRECT
        if ref_direct:
            present_sha = _landing_blob_sha(receiver, landing, rel)
            same = present_sha is not None and present_sha == _blob_sha1(data)
            archived = _landing_tree_has(receiver, landing, [archive_rel])
        else:
            present = target_file.is_file()
            same = present and target_file.read_bytes() == data
            present_sha = "worktree" if present else None
            if same and _landing_blob_sha(receiver, landing, rel) != _blob_sha1(data):
                failed.append({"id": name, "reason": f"{REASON_PENDING}: {target_file} is identical but uncommitted in the receiver; home copy left"})
                continue
            archived = (receiver / archive_rel).exists()

        if present_sha is not None and not same:
            failed.append({"id": name, "reason": f"{REASON_COLLISION}: {target_file} exists with different bytes; both left"})
            continue
        if present_sha is None and archived:
            failed.append({"id": name, "reason": f"{REASON_COLLISION}: {name} is archived at the target; both left"})
            continue

        action = ACTION_ALREADY_PRESENT if same else ACTION_MOVED
        if dry_run:
            candidates.append({"id": name, "action": action, "target_path": str(target_file)})
            continue
        if not same:
            err = _deliver(receiver, target_file, name, content, landing)
            if err:
                failed.append({"id": name, "reason": err})
                continue
        err = _remove_home_copy(home, src, data, name)
        if err:
            failed.append({"id": name, "reason": err})
            continue
        acted.append({"id": name, "action": action})

    if dry_run:
        return build_dry_run_result(_MODE, candidates)
    return build_act_result(_MODE, acted, [], failed)


@register_op("memo.migrate_group_em_inbox")
def _memo_migrate_group_em_inbox(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'memo.migrate_group_em_inbox' MUTATING op handler.

    Params:
        dry_run (bool, optional, default false): preview (true) vs. act (false).

    repo_root is unused: the home and the receiver are derived from the registry.
    """
    validated = _validate_params(params)
    if isinstance(validated, dict):
        return validated
    return migrate_group_em_inbox(validated)
