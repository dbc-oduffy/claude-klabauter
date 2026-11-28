"""repo.induct registration steps: the repos.<key> entry and the receiver inbox it routes memos to."""
from __future__ import annotations

import os
from pathlib import Path

from coordinator_core import machine_resolver
from coordinator_core.ops.fleet import _memo_resolver
from coordinator_core.ops.repo_induct._contract import StepRecord, make_step

REGISTER_STEP = "register_repo_key"
RECEIVER_STEP = "receiver_inbox"
INBOX_PARTS = ("state", "cross-repo", "inbox")


def _canon(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def register_repo_key(root: str, repo_key: str) -> StepRecord:
    """Set repos.<key> to `root` and read it back; a key holding another root is refused, never overwritten."""
    root_path = os.path.abspath(root)
    if not os.path.isdir(root_path):
        return make_step(REGISTER_STEP, "failed", f"root is not a directory: {root_path}")
    workspace = machine_resolver.external_workspace_containing(root_path)
    if workspace is not None:
        return make_step(
            REGISTER_STEP, "failed", f"root is inside external workspace {workspace!r}, which is not a repo"
        )
    existing = machine_resolver.registry_get(repo_key)
    if existing:
        if _canon(existing) == _canon(root_path):
            return make_step(REGISTER_STEP, "skipped", f"{repo_key} already set to {existing}")
        return make_step(
            REGISTER_STEP, "failed", f"{repo_key} already holds {existing}; not overwritten with {root_path}"
        )
    try:
        machine_resolver.registry_set(repo_key, root_path)
    except (ValueError, OSError) as exc:
        return make_step(REGISTER_STEP, "failed", f"registry_set refused: {exc}")
    read_back = machine_resolver.registry_get(repo_key)
    if not read_back or _canon(read_back) != _canon(root_path):
        return make_step(
            REGISTER_STEP, "failed", f"read-back of {repo_key} is {read_back!r}, expected {root_path}"
        )
    return make_step(REGISTER_STEP, "done", f"{repo_key} set to {root_path}", [repo_key])


def ensure_receiver_inbox(root: str, repo_key: str) -> StepRecord:
    """Create <root>/state/cross-repo/inbox and verify the repo's EM id resolves to it."""
    root_path = Path(os.path.abspath(root))
    inbox = root_path.joinpath(*INBOX_PARTS)
    created = not inbox.is_dir()
    try:
        inbox.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return make_step(RECEIVER_STEP, "failed", f"cannot create {inbox}: {exc}")
    changed = [str(inbox)] if created else []
    em_id = machine_resolver.repo_key_to_em_id(repo_key)
    try:
        resolved_inbox, resolved_repo, _ = _memo_resolver.resolve_receiver_inbox(em_id)
    except (_memo_resolver.RegistryReadError, _memo_resolver.AmbiguousReceiverError) as exc:
        return make_step(RECEIVER_STEP, "failed", f"{em_id} does not resolve: {type(exc).__name__}: {exc}", changed)
    if resolved_inbox is None or resolved_repo is None:
        return make_step(RECEIVER_STEP, "failed", f"{em_id} resolves to no receiver", changed)
    if _canon(resolved_inbox) != _canon(inbox) or _canon(resolved_repo) != _canon(root_path):
        return make_step(
            RECEIVER_STEP, "failed", f"{em_id} resolves to {resolved_inbox}, expected {inbox}", changed
        )
    return make_step(
        RECEIVER_STEP, "done" if created else "skipped", f"{em_id} resolves to {inbox}", changed
    )


def register_and_route(root: str, repo_key: str) -> list[StepRecord]:
    """Run the registration step then the receiver step; a failed registration fails the receiver step unattempted."""
    registered = register_repo_key(root, repo_key)
    if registered["status"] == "failed":
        return [
            registered,
            make_step(RECEIVER_STEP, "failed", f"not attempted: {REGISTER_STEP} failed"),
        ]
    return [registered, ensure_receiver_inbox(root, repo_key)]
