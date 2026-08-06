"""
coordinator_core.ops.queue_cluster -- the 'queue.cluster' op: caller-selectable
signal-set clustering over a single, caller-scoped queue family.

Purpose: expose coordinator_core.clustering.candidates' clustering logic as a
routable READ op with two additions over the pure function it is built on --
(a) an explicit, caller-supplied SIGNAL SET (a subset of "tag" / "directory" /
"keyword"; omitted == all three, today's fixed-order behavior byte-identical)
and (b) a caller-supplied FLOOR (omitted == MIN_CLUSTER_SIZE == 3, DR-209's
graduation gate). Family-scoped record loading routes entirely through
coordinator_core.ops.queue_family.load_family_records (the C1 read seam) --
this module owns no directory glob, no frontmatter parser, no record loader.

Response envelope (ratified 2026-07-23 by claude-central-em -- see
docs/plans/2026-07-23-queue-triage-terminus-ops.md § "Ratified response
envelopes"; this is the contract of record, matched exactly):

    [{signal, value, suggestedLabel, items: [{id, path, title}]}, ...]

A bare list, ordered tag-clusters-first / directory-second / keyword-last
(each signal's own clusters keep their internal first-seen-value order); an
empty result is `[]`, never `None`. `id` is the record's filename stem --
queue.append's write path mints no separate id field, "the filename is the
canonical entry handle" (coordinator_core/ops/queue_append.py module
docstring) -- derived here from the `path` query_records() already returns,
so callers never re-parse an id back out of a basename themselves.

Spec backlink: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C3

Negative-spec:
  - Does NOT add a `triage_mode` flag or any switch that bakes a caller's
    ceremony policy into this engine. Precision is entirely the caller's:
    a caller wanting the directory signal suppressed passes
    `signals=["tag", "keyword"]` -- this module never special-cases who is
    calling or why.
  - Does NOT tune STOP_WORDS or change MIN_CLUSTER_SIZE's *default* value --
    both are pinned byte-identical to coordinator_core.clustering.candidates
    (imported from there, never redefined here).
  - Does NOT glob a directory, parse YAML/frontmatter, or load records any
    way other than coordinator_core.ops.queue_family.load_family_records.
  - Does NOT mint a second queue-family map -- family normalization and
    record loading are entirely queue_family's.
  - Registration (the four shared surfaces: _registry_map.py, __init__.py's
    _EAGER_OP_MODULES, op_scopes.py, authz/classification.py) is NOT this
    module's job -- a dedicated later chunk owns those four files.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core.clustering.candidates import (
    MIN_CLUSTER_SIZE,
    _extract_keywords,
    _humanize,
    _normalize_tags,
    _parent_dir,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops.queue_family import load_family_records

SIGNAL_TAG = "tag"
SIGNAL_DIRECTORY = "directory"
SIGNAL_KEYWORD = "keyword"

ALL_SIGNALS: tuple[str, ...] = (SIGNAL_TAG, SIGNAL_DIRECTORY, SIGNAL_KEYWORD)
"""The closed signal enum, in output-ordering order (tag, directory, keyword).

Pinned by coordinator_core/ops/tests/test_queue_cluster.py::test_signal_enum_pinned --
Example-doctrine-repo's `/debt-triage` Step 6b suppresses the "directory" signal by literal
string comparison against this exact value. Renaming or re-casing a member
here does not raise on their side; it silently stops matching, and the next
triage run hands their ceremony one un-suppressed, oversized cluster as a
themed-baton candidate instead of the disposal their policy intended. A
signal-set rename is a cross-repo contract change -- coordinate it with
claude-central-em via a cross-repo memo before touching these values, don't
just rename and ship.
"""

_VALID_SIGNALS = frozenset(ALL_SIGNALS)


class InvalidSignalError(ValueError):
    """Raised for a ``signals`` entry outside the closed ``ALL_SIGNALS`` set."""


def _item_with_id(rec: dict) -> dict:
    """Build one ``{id, path, title}`` row from a ``query_records()`` record.

    ``id`` is the record's filename stem -- queue.append's write path mints
    no separate id field, so the filename stem IS the canonical handle; this
    is the one place that convention gets surfaced to callers, so they never
    need to re-derive it from ``path`` themselves.
    """
    path = rec.get("path", "")
    fm = rec.get("frontmatter") or {}
    return {
        "id": Path(path).stem,
        "path": path,
        "title": fm.get("title") or "",
    }


def _cluster_by_tag(records: list[dict], min_cluster_size: int) -> list[dict]:
    tag_map: dict[str, list[dict]] = {}
    for rec in records:
        for tag in _normalize_tags(rec.get("frontmatter")):
            tag_map.setdefault(tag, []).append(rec)
    return [
        {
            "signal": SIGNAL_TAG,
            "value": tag,
            "suggestedLabel": _humanize(tag),
            "items": [_item_with_id(r) for r in recs],
        }
        for tag, recs in tag_map.items()
        if len(recs) >= min_cluster_size
    ]


def _cluster_by_directory(records: list[dict], min_cluster_size: int) -> list[dict]:
    dir_map: dict[str, list[dict]] = {}
    for rec in records:
        dir_ = _parent_dir(rec.get("path", ""))
        dir_map.setdefault(dir_, []).append(rec)
    return [
        {
            "signal": SIGNAL_DIRECTORY,
            "value": dir_,
            "suggestedLabel": _humanize(os.path.basename(dir_)),
            "items": [_item_with_id(r) for r in recs],
        }
        for dir_, recs in dir_map.items()
        if len(recs) >= min_cluster_size
    ]


def _cluster_by_keyword(records: list[dict], min_cluster_size: int) -> list[dict]:
    kw_map: dict[str, list[dict]] = {}
    for rec in records:
        fm = rec.get("frontmatter") or {}
        title = fm.get("title") or ""
        for kw in _extract_keywords(title):
            kw_map.setdefault(kw, []).append(rec)
    return [
        {
            "signal": SIGNAL_KEYWORD,
            "value": kw,
            "suggestedLabel": _humanize(kw),
            "items": [_item_with_id(r) for r in recs],
        }
        for kw, recs in kw_map.items()
        if len(recs) >= min_cluster_size
    ]


_SIGNAL_CLUSTERERS = {
    SIGNAL_TAG: _cluster_by_tag,
    SIGNAL_DIRECTORY: _cluster_by_directory,
    SIGNAL_KEYWORD: _cluster_by_keyword,
}


def _normalize_signals(signals: Optional[list[str]]) -> tuple[str, ...]:
    """Validate and order a caller-supplied signal set.

    ``None`` (the default) means "all three, today's behavior" -- returns
    ``ALL_SIGNALS`` verbatim. A non-empty subset is validated against the
    closed enum and re-ordered to ``ALL_SIGNALS``'s fixed output order
    (tag, directory, keyword) regardless of the order the caller listed
    them in, so cluster ordering is never caller-order-dependent.
    """
    if signals is None:
        return ALL_SIGNALS
    unknown = sorted(set(signals) - _VALID_SIGNALS)
    if unknown:
        valid = ", ".join(ALL_SIGNALS)
        raise InvalidSignalError(
            f"queue.cluster: unknown signal(s) {unknown!r} -- valid signals are: {valid}"
        )
    requested = set(signals)
    return tuple(s for s in ALL_SIGNALS if s in requested)


def cluster_records(
    records: list[dict],
    *,
    signals: Optional[list[str]] = None,
    min_cluster_size: int = MIN_CLUSTER_SIZE,
) -> list[dict]:
    """Cluster ``records`` over ``signals`` (default: all three) at ``min_cluster_size``.

    Requesting the default signal set at the default floor reproduces
    ``coordinator_core.clustering.candidates.detect_candidates``'s output
    exactly, aside from the additional ``id`` key each item now carries.
    """
    ordered_signals = _normalize_signals(signals)
    clusters: list[dict] = []
    for signal in ordered_signals:
        clusters.extend(_SIGNAL_CLUSTERERS[signal](records, min_cluster_size))
    return clusters


@register_op("queue.cluster")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> list[dict]:
    """Handler for the 'queue.cluster' op.

    Params:
        family (str, required) -- queue-family directory name
            ("improvement-queue" / "debt-backlog" / "bug-backlog"), resolved
            via coordinator_core.ops.queue_family.load_family_records.
        signals (list[str], optional) -- subset of ALL_SIGNALS; omitted ==
            all three (today's behavior).
        min_cluster_size (int, optional) -- floor; omitted == MIN_CLUSTER_SIZE.
        where / since / limit (optional) -- forwarded to
            load_family_records unchanged.

    Returns the ratified envelope directly (a bare list -- see module
    docstring); raises ValueError (queue_family.UnknownQueueFamilyError or
    this module's InvalidSignalError) for a bad family/signal. Every op gets
    the same not-a-crash treatment at the IPC dispatch layer
    (coordinator_core/ipc.py's dispatch_message catches BaseException around
    every handler call, uniformly) -- this is not a distinguishing property
    of this handler. Note also that `where`/`since` are forwarded verbatim to
    query_records (via load_family_records), whose parsing grammar can raise
    SystemExit on a malformed value -- caught and converted the same way as
    any other exception escaping this handler.
    """
    family = params.get("family")
    if not family:
        raise ValueError("queue.cluster: 'family' param is required")
    if repo_root is None:
        raise ValueError("queue.cluster: requires a resolved repo_root")

    signals = params.get("signals")
    min_cluster_size = params.get("min_cluster_size", MIN_CLUSTER_SIZE)

    records = load_family_records(
        family,
        repo_root,
        where=params.get("where"),
        since=params.get("since"),
        limit=params.get("limit", 0),
    )
    return cluster_records(records, signals=signals, min_cluster_size=min_cluster_size)
