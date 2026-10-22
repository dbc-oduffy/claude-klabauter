"""Memo-send row predicates and receipt lookups, in a leaf with no yaml/schema/render imports.

``spine_read`` re-exports every public name here; ``terminal_commit`` imports from this module
so the commit path does not load ``spine_read``. Imports nothing from ``spine_read``.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Callable, Mapping, Optional


def _is_memo_send_row(raw: dict) -> bool:
    """True when the row's deliverable is a cross-repo memo send.

    Decided by ``kind``, by a surface or write that is a staged memo-outbox
    draft, by a surface or ``writes_under`` that is the outbox directory or
    inside it, or by a receiver-inbox surface that is not an inbound close.
    A prose mention of the send verb never classifies a row.
    """
    if raw.get("kind") in ("memo-send", "memo_send"):
        return True
    paths = [raw.get("surface"), *(raw.get("writes") or ())]
    from coordinator_core.session.machinery_paths import LEGACY_MEMO_OUTBOX_RELDIR, MEMO_OUTBOX_RELDIR

    for p in [raw.get("surface"), *(raw.get("writes_under") or ())]:
        if isinstance(p, str):
            posix = p.replace("\\", "/").rstrip("/")
            if any(posix == d or posix.startswith(d + "/") for d in (MEMO_OUTBOX_RELDIR, LEGACY_MEMO_OUTBOX_RELDIR)):
                return True
    if _is_receiver_inbox_surface(raw) and inbound_close_path(raw) is None:
        return True
    if any(isinstance(p, str) and "outbox" in p for p in paths):
        from coordinator_core.ops.fleet.memo_wire import memo_outbox_topic

        if any(isinstance(p, str) and memo_outbox_topic(p) for p in paths):
            return True
    return False


def memo_row_dispatchable(raw: dict) -> bool:
    """True when a memo-send row can stage as an executor chunk: open, undeferred, not declared
    ``performer: em``, not a receiver-inbox close, keyed on a slug, and naming a receiver
    (``memo_to``). Pure: no stat, no registry."""
    if not _is_memo_send_row(raw):
        return False
    if raw.get("disposition", "open") != "open":
        return False
    if raw.get("deferred") is True or raw.get("deferred_until"):
        return False
    if raw.get("performer") == "em":
        return False
    if inbound_close_path(raw) is not None or _is_receiver_inbox_surface(raw):
        return False
    if memo_row_slug(raw) is None:
        return False
    to = raw.get("memo_to")
    return isinstance(to, str) and bool(to.strip())


def memo_send_step_block(row_id: str, fields: Mapping[str, str]) -> str:
    """The executor's steps for memo-send row ``row_id``; ``fields`` is ``{slug, to, title, kind}``."""
    from coordinator_core.session.machinery_paths import MEMO_OUTBOX_RELDIR

    slug = fields["slug"]
    draft = f"{MEMO_OUTBOX_RELDIR}/{slug}.md"
    receipt = f"{MEMO_OUTBOX_RELDIR}/sent/{slug}.md"
    draft_cmd = (
        f"cross-repo-memo draft {shlex.quote(slug)} --to {shlex.quote(fields['to'])}"
        f" --kind {shlex.quote(fields['kind'])} --title {shlex.quote(fields['title'])}"
    )
    return (
        f"Memo send for row {row_id}:\n"
        f"1. If `{draft}` is absent, run `{draft_cmd}`, then Edit its body to carry the row body.\n"
        f"2. Run `cross-repo-memo send {shlex.quote(slug)}`.\n"
        f"3. Confirm `{receipt}` exists and report its path.\n"
        f"4. If send prints `REFUSED`, stop and report the refusal verbatim; take no other route.\n"
        f"5. Touch no other file and run no git commit: the send commits its own receipt.\n"
    )


_RECEIVER_INBOX_SEGMENT = "/state/cross-repo/inbox/"


def _is_receiver_inbox_surface(raw: dict) -> bool:
    """True when ``surface`` or a ``writes_under`` entry has the path segment
    ``state/cross-repo/inbox``, with or without a repo prefix or trailing slash."""
    for p in (raw.get("surface"), *(raw.get("writes_under") or ())):
        if isinstance(p, str) and _RECEIVER_INBOX_SEGMENT in "/" + p.replace("\\", "/").strip("/") + "/":
            return True
    return False


def inbound_close_path(raw: dict) -> Optional[str]:
    """The one repo-relative ``state/cross-repo/inbox/<name>.md`` path that every non-empty
    ``surface`` and ``writes`` entry is exactly, for a row naming no memo slug; else ``None``.
    Such a row only closes an inbound memo: the close-out memo-lifecycle leg owns it, never a
    memo-send. A row that also writes any other path is real work and stays dispatchable."""
    if memo_row_slug(raw) is not None:
        return None
    from coordinator_core.ops.dispatch_emit.source_memo_ref import inbox_memo_refs

    found: dict = {}
    for p in (raw.get("surface"), *(raw.get("writes") or ())):
        if isinstance(p, str) and p.strip():
            norm = p.strip().replace("\\", "/")
            if inbox_memo_refs(norm) != (norm,):
                return None
            found.setdefault(norm)
    return next(iter(found)) if len(found) == 1 else None


def memo_receipt_slug(ref: object) -> Optional[str]:
    """The topic when ``ref`` (either separator) is exactly
    ``<outbox>/sent/<slug>.md`` under the canonical or legacy outbox and ``<slug>``
    is a valid topic slug; ``None`` otherwise. A pure string parse, no I/O."""
    if not isinstance(ref, str):
        return None
    from coordinator_core.ops.fleet._memo_compose import _TOPIC_SLUG_RE
    from coordinator_core.session.machinery_paths import LEGACY_MEMO_OUTBOX_RELDIR, MEMO_OUTBOX_RELDIR

    posix = ref.strip().replace("\\", "/")
    for root in (MEMO_OUTBOX_RELDIR, LEGACY_MEMO_OUTBOX_RELDIR):
        prefix = root + "/sent/"
        if posix.startswith(prefix):
            leaf = posix[len(prefix):]
            if "/" in leaf or not leaf.endswith(".md"):
                return None
            topic = leaf[: -len(".md")]
            return topic if _TOPIC_SLUG_RE.match(topic) else None
    return None


def memo_row_slug(raw: dict) -> Optional[str]:
    """The memo topic a memo-send row is keyed on, or ``None``.

    ``receipt:`` (a ``.../sent/<slug>.md`` path or a bare slug) wins; then a
    ``disposition_ref`` that is a sent receipt; else the topic of a staged
    ``memo-outbox/<topic>.md`` draft on ``surface:``/``writes:``.
    """
    from coordinator_core.ops.fleet.memo_wire import memo_outbox_topic

    receipt = raw.get("receipt")
    if isinstance(receipt, str) and receipt.strip():
        leaf = receipt.strip().replace("\\", "/").rsplit("/", 1)[-1]
        return leaf[:-3] if leaf.endswith(".md") else leaf
    resolved = memo_receipt_slug(raw.get("disposition_ref"))
    if resolved:
        return resolved
    for path in (raw.get("surface"), *(raw.get("writes") or ())):
        if isinstance(path, str):
            topic = memo_outbox_topic(path)
            if topic:
                return topic
    return None


def memo_receiver_unregistered(receiver: object, classify: Optional[Callable[[str, bool], object]] = None) -> bool:
    """True when ``memo.draft`` would refuse ``receiver`` as UNKNOWN RECEIVER.

    Answered by memo.draft's own classifier (``classify`` overrides it, for a caller that
    injects the registry), so the registry has one reader. Any other outcome (resolved,
    rerouted, auto-accepted, registry error, ambiguous) is ``False``.
    """
    if not isinstance(receiver, str) or not receiver.strip():
        return False
    from coordinator_core.ops.fleet.memo_draft import REJECTION_CLASS_UNKNOWN_RECEIVER

    if classify is None:
        from coordinator_core.ops.fleet.memo_draft import _classify_receiver_for_draft as classify

    from coordinator_core.ops.fleet._outbox_enum import quiet_setup_errors

    with quiet_setup_errors():
        verdict = classify(receiver.strip(), True)
    return isinstance(verdict, dict) and verdict.get("rejection_class") == REJECTION_CLASS_UNKNOWN_RECEIVER


def is_http_url_ref(ref: object) -> bool:
    """True when ``ref`` is an ``http(s)://<host>...`` URL with no whitespace. Shape only, no I/O."""
    if not isinstance(ref, str):
        return False
    from urllib.parse import urlsplit

    text = ref.strip()
    if not text or any(c.isspace() for c in text):
        return False
    parts = urlsplit(text)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def memo_receipt_exists(repo_root: Optional[Path], slug: str) -> bool:
    """True when the sender-side receipt ``memo-outbox/sent/<slug>.md`` exists under ``repo_root``."""
    if repo_root is None:
        return False
    from coordinator_core.ops.fleet.memo_wire import memo_send_performed_paths

    return (Path(repo_root) / memo_send_performed_paths(slug)[0]).is_file()
