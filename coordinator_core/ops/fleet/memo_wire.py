"""memo_wire: the one engine-owned memo wire contract.

Turns a memo's `to:` into a receiver inbox and a delivered filename, names the
refusals that stop a send before any byte moves, and names the sender-side
files `memo.send` writes. `memo.send` and the dispatch emitter both call it, so
the two cannot drift.

Composes `_memo_resolver`, `_memo_compose`, `memo_draft` and
`session.machinery_paths`; derives nothing they already own. Spawns no process
and scans no directory: every filesystem touch is a stat or a read of one named
file. Must not import `memo_send` (it imports this module).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.fleet._memo_compose import _TOPIC_SLUG_RE, _memo_filename
from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    PR_COMMENT_FALLBACK,
    never_inbox_mirror_refusal,
    resolve_receiver_inbox,
    same_repo_path,
    suggest_nearest_receiver,
    undeliverable_checkout_refusal,
)
from coordinator_core.ops.fleet.memo_draft import resolve_outbox_draft_path
from coordinator_core.session import machinery_paths as _machinery_paths

#: Stands for the send-day date in `delivered_filename_pattern`.
DATE_PLACEHOLDER = "<YYYY-MM-DD>"


class WireRefusal(NamedTuple):
    """A refusal that stops the send before any write. `message` is the
    agent-facing text, ready for `build_setup_error_result`."""

    message: str


class ReceiverTarget(NamedTuple):
    """A resolved, deliverable receiver. `inbox_dir` and `receiver_repo_path`
    are never None; `all_repos` is the registry's full `repos.*` map."""

    name: str
    inbox_dir: Path
    receiver_repo_path: Path
    all_repos: dict


class DeliveryTarget(NamedTuple):
    """A `ReceiverTarget` plus the delivered `filename` and whether the
    receiver is the sender's own worktree (`self_send`)."""

    name: str
    inbox_dir: Path
    receiver_repo_path: Path
    all_repos: dict
    filename: str
    self_send: bool


def resolve_receiver(name: str) -> "ReceiverTarget | WireRefusal":
    """Resolve `name` to a deliverable receiver, or the refusal `memo.send`
    gives for it: unreadable registry, ambiguous central receiver, publish
    mirror, undeliverable checkout, then unknown receiver, in that order."""
    try:
        inbox_dir, receiver_repo_path, all_repos = resolve_receiver_inbox(name)
    except RegistryReadError as exc:
        return WireRefusal(
            f"memo.send: machine-local registry could not be read: {exc.reason} "
            f"(no folder-scan fallback — fix the registry file or re-run "
            f"machine-local setup)."
        )
    except AmbiguousReceiverError as exc:
        return WireRefusal(f"memo.send: {exc}")

    mirror_refusal = never_inbox_mirror_refusal(name, receiver_repo_path)
    if mirror_refusal is not None:
        return WireRefusal(mirror_refusal)

    if receiver_repo_path is not None:
        checkout_refusal = undeliverable_checkout_refusal(name, receiver_repo_path)
        if checkout_refusal is not None:
            return WireRefusal(checkout_refusal)

    if inbox_dir is None:
        suggestion = suggest_nearest_receiver(name, all_repos)
        suggestion_clause = f" Did you mean {suggestion!r}?" if suggestion else ""
        return WireRefusal(
            f"memo.send: UNKNOWN RECEIVER — {name!r} does not resolve to any "
            f"registered receiver on this machine.{suggestion_clause} Register "
            f"the receiver repo first (machine-local set repos.<name> "
            f"<abs-path-to-repo>), or check for a typo in the draft's `to:`. "
            f"{PR_COMMENT_FALLBACK}"
        )

    return ReceiverTarget(name, inbox_dir, receiver_repo_path, all_repos)


def resolve_delivery_target(
    to: str, *, sender_worktree: Path, from_id: str, topic: str, today: str,
) -> "DeliveryTarget | WireRefusal":
    """Filename first, then `resolve_receiver`, then `self_send`. A `from_id`
    that sanitizes to an empty slug is a refusal, never a raise; it is checked
    before the receiver, so it wins over an unknown `to:`."""
    try:
        filename = _memo_filename(today, from_id, topic)
    except ValueError as exc:
        return WireRefusal(f"memo.send: cannot name the delivered file: {exc}")

    receiver = resolve_receiver(to)
    if isinstance(receiver, WireRefusal):
        return receiver

    return DeliveryTarget(
        receiver.name,
        receiver.inbox_dir,
        receiver.receiver_repo_path,
        receiver.all_repos,
        filename,
        same_repo_path(receiver.receiver_repo_path, sender_worktree),
    )


def memo_outbox_topic(relpath: str) -> Optional[str]:
    """The topic when `relpath` (either separator) is exactly
    `<outbox>/<topic>.md` under the canonical or legacy outbox and `<topic>`
    is a valid slug; `None` for `sent/`, nested, non-`.md` and non-slug paths."""
    posix = relpath.replace("\\", "/")
    for root in (
        _machinery_paths.MEMO_OUTBOX_RELDIR,
        _machinery_paths.LEGACY_MEMO_OUTBOX_RELDIR,
    ):
        prefix = root + "/"
        if not posix.startswith(prefix):
            continue
        leaf = posix[len(prefix):]
        if "/" in leaf or not leaf.endswith(".md"):
            return None
        topic = leaf[: -len(".md")]
        return topic if _TOPIC_SLUG_RE.match(topic) else None
    return None


def read_staged_to(sender_worktree: Path, topic: str) -> Optional[str]:
    """The staged draft's `to:`, or `None` when no draft is staged, it cannot
    be read or parsed, or it has no non-empty string `to:`."""
    draft_path = resolve_outbox_draft_path(Path(sender_worktree), topic)
    try:
        content = draft_path.read_text(encoding="utf-8")
    except OSError:
        return None
    frontmatter = parse_frontmatter(content).get("frontmatter")
    if not isinstance(frontmatter, dict):
        return None
    to = frontmatter.get("to")
    if isinstance(to, str) and to.strip():
        return to.strip()
    return None


def memo_send_performed_paths(topic: str) -> tuple[str, ...]:
    """Repo-relative forward-slash sender-side paths `memo.send` writes for
    `topic`: the `sent/<topic>.md` copy and the sent-ledger."""
    anchor = "anchor"
    sent_copy = os.path.join(_machinery_paths.memo_outbox_sent_dir(anchor), f"{topic}.md")
    ledger = _machinery_paths.memo_outbox_sent_ledger_path(anchor)
    return tuple(
        os.path.relpath(path, anchor).replace(os.sep, "/")
        for path in (sent_copy, ledger)
    )


def delivered_filename_pattern(from_id: str, topic: str) -> str:
    """`<YYYY-MM-DD>-<sender-slug>-<topic>.md`, the literal placeholder
    standing for the send-day date. Raises `ValueError` on an empty sender
    slug, as `_memo_filename` does."""
    return _memo_filename(DATE_PLACEHOLDER, from_id, topic)
