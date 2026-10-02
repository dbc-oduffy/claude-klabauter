"""Memo-shaped rows: resolve a staged memo draft's receiver through
`memo_wire` at emit time and render the fence clause that admits the files
`memo.send` writes for that topic.

A row is memo-shaped when its `surface` is a staged memo-outbox draft. The
receiver path appears only in the rendered fence text, never in a row's
`writes`, `writes_under` or the terminal-commit pathspec. Spawns no process and
scans no directory; each distinct `to:` is resolved once.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.fleet.memo_draft import resolve_outbox_draft_path
from coordinator_core.ops.fleet.memo_wire import (
    WireRefusal,
    delivered_filename_pattern,
    memo_outbox_topic,
    memo_send_performed_paths,
    read_staged_to,
    resolve_receiver,
)

_UNRESOLVED_RECEIVER = "the receiver inbox the draft's `to:` names"


class MemoRowReceiverError(ValueError):
    """A memo-shaped row's staged draft addresses a receiver `memo.send`
    would refuse. Names the row id, the topic and the refusal text."""


class MemoDelivery(NamedTuple):
    """What `memo.send` writes for one memo-shaped row. `inbox_dir` and
    `filename_pattern` are `None` when no draft with a `to:` is staged at emit
    (and `filename_pattern` also when the draft has no usable `from:`)."""

    topic: str
    inbox_dir: Optional[str]
    filename_pattern: Optional[str]
    sender_paths: tuple


def _staged_from(repo_root: Path, topic: str) -> Optional[str]:
    try:
        content = resolve_outbox_draft_path(repo_root, topic).read_text(encoding="utf-8")
    except OSError:
        return None
    frontmatter = parse_frontmatter(content).get("frontmatter")
    if not isinstance(frontmatter, dict):
        return None
    from_id = frontmatter.get("from")
    return from_id.strip() if isinstance(from_id, str) and from_id.strip() else None


def _filename_pattern(repo_root: Path, topic: str) -> Optional[str]:
    from_id = _staged_from(repo_root, topic)
    if from_id is None:
        return None
    try:
        return delivered_filename_pattern(from_id, topic)
    except ValueError:
        return None


def check_memo_rows(rows, repo_root: Optional[Path]) -> "dict[str, MemoDelivery]":
    """Map each memo-shaped row id to its `MemoDelivery`. Raises
    `MemoRowReceiverError` when a staged draft's `to:` resolves to a
    `WireRefusal`. No-op returning `{}` when `repo_root` is `None`, as
    `check_cross_repo_writes` is."""
    if repo_root is None:
        return {}
    root = Path(repo_root)
    resolved: dict = {}
    deliveries: dict = {}
    for row in rows:
        topic = memo_outbox_topic(row.surface or "")
        if topic is None:
            continue
        sender_paths = memo_send_performed_paths(topic)
        to = read_staged_to(root, topic)
        if to is None:
            deliveries[row.id] = MemoDelivery(topic, None, None, sender_paths)
            continue
        if to not in resolved:
            resolved[to] = resolve_receiver(to)
        target = resolved[to]
        if isinstance(target, WireRefusal):
            raise MemoRowReceiverError(
                f"row {row.id} (memo topic {topic!r}): {target.message}"
            )
        deliveries[row.id] = MemoDelivery(
            topic,
            Path(target.inbox_dir).as_posix(),
            _filename_pattern(root, topic),
            sender_paths,
        )
    return deliveries


def memo_fence_clause(delivery: MemoDelivery) -> str:
    """One sentence admitting the files `memo.send` writes for this row's
    topic. Names `memo.send` as the only writer; grants nothing beyond that
    one call."""
    if delivery.inbox_dir is not None and delivery.filename_pattern is not None:
        delivered = f"`{delivery.inbox_dir}/{delivery.filename_pattern}`"
    elif delivery.inbox_dir is not None:
        delivered = f"a file in `{delivery.inbox_dir}/`"
    else:
        delivered = f"a file in {_UNRESOLVED_RECEIVER}"
    sender = ", ".join(f"`{p}`" for p in delivery.sender_paths)
    return (
        f"Sending this memo with `memo.send` writes {delivered}, plus {sender}. "
        "These are inside this chunk's footprint, only `memo.send` may write "
        "them (never the Write or Edit tool), and the delivered path is quoted "
        "from the `memo.send` result in your DONE report."
    )
