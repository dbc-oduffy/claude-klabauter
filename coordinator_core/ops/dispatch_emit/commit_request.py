"""
coordinator_core.ops.dispatch_emit.commit_request — the terminal-commit
request marker.

Purpose: § Design D2 of docs/plans/2026-09-27-emitter-dag-terminal-commit-
wake-digest.md. Emission (C12) no longer dispatches a
``coordinator:git-commit-agent`` per wave; instead it writes ONE marker
line into the emitted script recording what the run promises the terminal
commit (D3, ``dispatch.land_run``, built in C10): which chunks
landed which paths, under which own-report-claimed prefixes, and which
report file each chunk's own-prefix claim list lives in.

``CommitRequest.repo_root``: always null in emitted markers (no consumer reads
it, and a host path must not land in a committed script); the key stays for
pre-existing markers.

``CommitRequest.expected_branch``: branch the run promised to commit onto;
null = no promise. Trap: optional forever — pre-field v1 markers exist on
disk, so ``parse_marker`` reads it with ``payload.get``.

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

from coordinator_core.frontmatter.primitives import split_frontmatter

# The scaffolded sentinel `plan.schema.json` excludes from `deliverable_id`
# by negative lookahead -- a plan still carrying it has no id yet.
_DELIVERABLE_ID_PLACEHOLDER_PREFIX = "dlv-placeholder-replace-with"


# Mirrors inventory_mint._LITERAL_PATHSPEC_PREFIX (that module's import closure is heavy);
# test_receipt_and_request_import_set pins parity.
_LITERAL_PATHSPEC_PREFIX = ":(literal)"


def valid_deliverable_id(value: object) -> Optional[str]:
    """The stripped ``value`` when it is a ``dlv-``-prefixed string that is
    not the scaffolded placeholder, else ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate.startswith("dlv-"):
        return None
    if candidate.startswith(_DELIVERABLE_ID_PLACEHOLDER_PREFIX):
        return None
    return candidate


def plan_deliverable_id(plan_text: str) -> Optional[str]:
    """The plan's top-level frontmatter ``deliverable_id`` when valid, or
    ``None``.

    Fail-soft in every direction: no frontmatter, unparseable YAML, a
    non-mapping document, or an absent, null, non-string, empty,
    non-``dlv-`` or placeholder id all return ``None``. A commit prompt
    naming no id costs one hand-written ``disposition_ref``; one naming a
    placeholder stamps unrewritable shared history with an id that joins to
    nothing.
    """
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    import yaml

    try:
        doc = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    return valid_deliverable_id(doc.get("deliverable_id"))


def plan_trailer_deliverable_ids(plan_text: str) -> list[str]:
    """The plan frontmatter's optional ``trailer_deliverable_ids`` list: each entry through
    ``valid_deliverable_id``, deduped in order, the plan's own ``deliverable_id`` excluded.

    Fail-soft like ``plan_deliverable_id``: no frontmatter, bad YAML or a non-list value is ``[]``.
    """
    split = split_frontmatter(plan_text)
    if split is None:
        return []
    import yaml

    try:
        doc = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict) or not isinstance(doc.get("trailer_deliverable_ids"), list):
        return []
    own = valid_deliverable_id(doc.get("deliverable_id"))
    out: list[str] = []
    for raw in doc["trailer_deliverable_ids"]:
        did = valid_deliverable_id(raw)
        if did is not None and did != own and did not in out:
            out.append(did)
    return out


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
    expected_branch: Optional[str] = None
    version: int = 1
    # A review-only run reviews a landed delta and executes no row: its terminal commit leaves
    # every row disposition as it stands.
    review_only: bool = False
    # Review rows that were ``open`` at emit, and the run base their delta is measured from. A
    # marker without them codes no row.
    review_open_rows: tuple = ()
    review_run_base: Optional[str] = None
    # Sorted repo-relative paths dirty at a review-only emit: the review never read their bytes, so
    # the landing withholds them.
    review_carried: tuple = ()
    # Row ids a carrier sibling's marker holds for OTHER siblings. Trap: they are not this run's
    # work, and only a merged digest may grade them.
    sibling_scope: tuple = ()


# The next_action param `merge_digests` writes once the siblings' digests are merged.
SIBLINGS_MERGED_PARAM = "siblings_merged"


def unmerged_sibling_scope(request: "CommitRequest", params) -> frozenset:
    """The request's ``sibling_scope`` ids while ``params`` carries no non-empty
    ``SIBLINGS_MERGED_PARAM`` list; empty once the digests are merged."""
    merged = params.get(SIBLINGS_MERGED_PARAM) if isinstance(params, dict) else None
    if isinstance(merged, list) and merged:
        return frozenset()
    return frozenset(request.sibling_scope)


def _chunk_to_dict(chunk: ChunkCommit) -> dict:
    return {
        "id": chunk.id,
        "title": chunk.title,
        "paths": list(chunk.paths),
        "prefixes": list(chunk.prefixes),
        "report": chunk.report,
    }


def _bare_path(path: str) -> str:
    """A marker path as a repo-relative file path. Trap: emit writes
    ``as_git_pathspec``-wrapped paths into the marker, and every consumer of
    ``ChunkCommit.paths`` keys the filesystem or a tree with them, where a
    pathspec prefix reads as a missing file. Any other pathspec magic names
    no single file and is refused."""
    bare = path[len(_LITERAL_PATHSPEC_PREFIX):] if path.startswith(_LITERAL_PATHSPEC_PREFIX) else path
    if bare.startswith(":("):
        raise MalformedCommitRequestError(
            f"chunk path {path!r} carries pathspec magic other than `:(literal)`"
        )
    return bare


def _chunk_from_dict(obj: dict) -> ChunkCommit:
    try:
        return ChunkCommit(
            id=obj["id"],
            title=obj["title"],
            paths=tuple(_bare_path(p) for p in obj["paths"]),
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

    A chunk with neither ``paths`` nor ``prefixes`` contributes no file but is
    kept beside a chunk that does: the terminal commit closes its row coded as a
    no-change row. If no chunk declares a path, there is nothing to commit and
    the caller emits no marker at all.
    """
    if not any(c.paths or c.prefixes for c in req.chunks):
        return None
    kept = req.chunks
    payload = {
        "version": req.version,
        "chunks": [_chunk_to_dict(c) for c in kept],
        "deliverable_id": req.deliverable_id,
        "session_id": req.session_id,
        "repo_root": req.repo_root,
        "plan_path": req.plan_path,
        "expected_branch": req.expected_branch,
    }
    if req.review_only:
        payload["review_only"] = True
        if req.review_open_rows and req.review_run_base:
            payload["review_open_rows"] = sorted(req.review_open_rows)
            payload["review_run_base"] = req.review_run_base
        if req.review_carried:
            payload["review_carried"] = sorted(p.replace("\\", "/") for p in req.review_carried)
    if req.sibling_scope:
        payload["sibling_scope"] = sorted(req.sibling_scope)
    body = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f"{MARKER_PREFIX}{body}"


def parse_manifest_marker(script_text: str) -> Optional[str]:
    """The repo-relative manifest path an ask-run script names, or ``None``
    when it carries no ``ASK_MANIFEST_MARKER`` line. More than one such line,
    or an empty path, raises ``MalformedCommitRequestError``."""
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER

    lines = [ln for ln in script_text.splitlines() if ln.startswith(ASK_MANIFEST_MARKER)]
    if not lines:
        return None
    if len(lines) > 1:
        raise MalformedCommitRequestError(
            f"expected at most one {ASK_MANIFEST_MARKER!r} marker, found {len(lines)}"
        )
    path = lines[0][len(ASK_MANIFEST_MARKER) :].strip()
    if not path:
        raise MalformedCommitRequestError("ask-run-manifest marker names no path")
    return path


def _open_rows_from(value) -> tuple:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return ()
    return tuple(value)


def _carried_from(value) -> tuple:
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))


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
            expected_branch=payload.get("expected_branch"),
            version=payload["version"],
            review_only=payload.get("review_only") is True,
            review_open_rows=_open_rows_from(payload.get("review_open_rows")),
            review_run_base=(
                payload["review_run_base"] if isinstance(payload.get("review_run_base"), str) else None
            ),
            review_carried=_carried_from(payload.get("review_carried")),
            sibling_scope=_open_rows_from(payload.get("sibling_scope")),
        )
    except (KeyError, TypeError) as exc:
        raise MalformedCommitRequestError(
            f"malformed terminal-commit-request marker payload: {exc!r}"
        ) from exc
