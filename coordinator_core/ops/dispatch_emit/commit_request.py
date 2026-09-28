"""
coordinator_core.ops.dispatch_emit.commit_request — the terminal-commit
request marker.

Purpose: § Design D2 of docs/plans/2026-09-27-emitter-dag-terminal-commit-
wake-digest.md. Emission (C12) no longer dispatches a
``coordinator:git-commit-agent`` per wave; instead it writes ONE marker
line into the emitted script recording what the run promises the terminal
commit (D3, ``dispatch.terminal_commit``, built in C10): which chunks
landed which paths, under which own-report-claimed prefixes, and which
report file each chunk's own-prefix claim list lives in.

This module is pure — no I/O, no subprocess, no filesystem read — so C10
and C12 can both build against it without pulling in the emitter or the
terminal-commit op.

``PREFIX_CLAIM_LABEL`` lives here (not in ``emit.py``) so the executor
prompt (emit.py) and the terminal commit (C10) read the same literal and
cannot drift apart. ``emit.py`` currently carries its own copy
(``_PREFIX_CLAIM_LABEL``); C12 deletes it and imports this one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

# The label an executor report uses to list files it created under a
# declared prefix surface — the terminal commit (D3) trusts only files a
# DONE chunk's own report lists under its OWN prefix.
PREFIX_CLAIM_LABEL = "created-under-prefix:"

# One-line sentinel a script carries at most once. The trailing space
# means the JSON payload starts immediately after the prefix, on the same
# line — render_marker's json.dumps call never emits U+2028/U+2029 or a
# literal newline (ensure_ascii=True escapes them), so "one line" is a
# property of the encoding, not an assumption checked after the fact.
MARKER_PREFIX = "// coordinator:terminal-commit-request v1 "


class MalformedCommitRequestError(ValueError):
    """Raised by parse_marker when the script carries more than one
    terminal-commit-request marker, or a marker whose payload does not
    decode into a well-formed CommitRequest."""


@dataclass(frozen=True)
class ChunkCommit:
    """One plan-spine chunk's contribution to the terminal commit.

    ``paths``: the chunk's own declared write paths (post-gitignore
    filter), in row order.
    ``prefixes``: prefix surfaces this chunk may create files under; the
    terminal commit trusts only files the chunk's OWN report lists under
    ``PREFIX_CLAIM_LABEL``, scoped to these prefixes.
    ``report``: the chunk's dispatch-report path, read for its
    ``PREFIX_CLAIM_LABEL`` list when ``prefixes`` is non-empty.
    """

    id: str
    title: str
    paths: tuple = field(default_factory=tuple)
    prefixes: tuple = field(default_factory=tuple)
    report: str = ""


@dataclass(frozen=True)
class CommitRequest:
    """What one emitted run promises the terminal commit (D3 consumes
    this; C12 builds it)."""

    chunks: tuple
    deliverable_id: Optional[str] = None
    session_id: Optional[str] = None
    repo_root: Optional[str] = None
    plan_path: Optional[str] = None
    version: int = 1


def _chunk_to_dict(chunk: ChunkCommit) -> dict:
    return {
        "id": chunk.id,
        "title": chunk.title,
        "paths": list(chunk.paths),
        "prefixes": list(chunk.prefixes),
        "report": chunk.report,
    }


def _chunk_from_dict(obj: dict) -> ChunkCommit:
    try:
        return ChunkCommit(
            id=obj["id"],
            title=obj["title"],
            paths=tuple(obj["paths"]),
            prefixes=tuple(obj["prefixes"]),
            report=obj["report"],
        )
    except (KeyError, TypeError) as exc:
        raise MalformedCommitRequestError(
            f"malformed chunk entry in terminal-commit-request marker: {exc!r}"
        ) from exc


def render_marker(req: CommitRequest) -> Optional[str]:
    """Render ``req`` as one marker line, or ``None`` if it has no chunk
    worth committing.

    A chunk with neither ``paths`` nor ``prefixes`` contributes nothing to
    the terminal commit and is omitted; if every chunk is omitted this way,
    there is nothing to commit and the caller emits no marker at all.
    """
    kept = tuple(c for c in req.chunks if c.paths or c.prefixes)
    if not kept:
        return None
    payload = {
        "version": req.version,
        "chunks": [_chunk_to_dict(c) for c in kept],
        "deliverable_id": req.deliverable_id,
        "session_id": req.session_id,
        "repo_root": req.repo_root,
        "plan_path": req.plan_path,
    }
    body = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f"{MARKER_PREFIX}{body}"


def parse_marker(script_text: str) -> Optional[CommitRequest]:
    """Parse the single terminal-commit-request marker out of an emitted
    script's text.

    Returns ``None`` when no marker is present. Raises
    ``MalformedCommitRequestError`` when more than one marker line is
    present, or when the (sole) marker's payload does not decode into a
    well-formed ``CommitRequest``.
    """
    lines = [
        line for line in script_text.splitlines() if line.startswith(MARKER_PREFIX)
    ]
    if not lines:
        return None
    if len(lines) > 1:
        raise MalformedCommitRequestError(
            f"expected at most one {MARKER_PREFIX!r} marker, found {len(lines)}"
        )
    body = lines[0][len(MARKER_PREFIX) :]
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise MalformedCommitRequestError(
            f"terminal-commit-request marker payload is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise MalformedCommitRequestError(
            "terminal-commit-request marker payload must be a JSON object"
        )
    try:
        chunks = tuple(_chunk_from_dict(c) for c in payload["chunks"])
        return CommitRequest(
            chunks=chunks,
            deliverable_id=payload["deliverable_id"],
            session_id=payload["session_id"],
            repo_root=payload["repo_root"],
            plan_path=payload["plan_path"],
            version=payload["version"],
        )
    except (KeyError, TypeError) as exc:
        raise MalformedCommitRequestError(
            f"malformed terminal-commit-request marker payload: {exc!r}"
        ) from exc
