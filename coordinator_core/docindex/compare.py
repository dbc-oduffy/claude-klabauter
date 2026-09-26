"""
coordinator_core.docindex.compare

Diffs emitted-versus-recorded for one generated index (C2b) — a pure function
of (document_text, IndexSpec, entries) -> CompareResult naming the directory
and the specific disagreement, plus a standalone digest primitive for AC6b's
zero-spawn fast-tier leg.

Spec backlink:
  claude-klabauter: docs/plans/2026-08-14-registry-indexes-are-emitted-from-their-directory.md,
                  plan-spine id C2b

HAZARD (state/lessons/0000-00-00-hand-edit-of-generated-artifact-survives-until-next-emit.yaml,
DoE-claude `d97c8aa7`): a hand-edit made INSIDE the delimited region reads as landed and
reviewed, then is silently destroyed by the next regeneration. The naive predicate
("pre-emit region content disagrees with a fresh render") cannot discriminate a hand-edit
from ordinary drift — both present as region-bytes-disagree-with-fresh-render.

MECHANISM (AC12): ``render.py`` (C2a) stamps a sha256 digest of the emitted region's bytes
into the closing sentinel, computed OUTSIDE the digested span. ``compare`` reads that
recorded digest back and discriminates two ways:
  - digest **mismatch** (on-disk region bytes' sha256 != recorded closing-sentinel digest)
    -> **hand-edit**: refused and named, never diffed further.
  - digest **match** but the region disagrees with a fresh render of the currently recorded
    entries -> **ordinary drift**: the exact condition this plan exists to detect, reported
    as added/removed/changed entries.
This is deterministic and AC1-safe (the digest is a function of bytes, not of wall time).

``region_digest`` is exposed as its OWN entry point taking region bytes alone — C4a's
zero-spawn fast-tier leg (AC6b) calls exactly that (plus ``extract_region``) and nothing
else: no directory listing, no entry file, no subprocess.

COMMITTED-CONTENT COMPARISON (F6, feeding C4b): ``compare`` accepts already-read document
text and already-read entry frontmatter rather than reading paths itself, so C4b's drift
test can hand it ``HEAD``-sourced content without this module knowing or caring where the
bytes came from — the direct payoff of C2a's I/O-free constraint (AC15's batched blob read
lives entirely in the caller).

VOCABULARY: modeled on ``coordinator_core.cartography.churn.AtlasComparison`` (a frozen,
purely-additive comparison dataclass) as a NEW type — per Anti-scope, ``churn.py``'s own
comparator is never extended; it has a different contract (per-system file counts under a
recorded mapping rule), and bending it to also compare index entries would give one function
two contracts and two failure vocabularies.

Negative-spec:
  - No file reads, no file writes, no subprocess, no env reads. Document text, IndexSpec,
    and entries all arrive as already-resolved arguments (matching render.py's purity, C2a).
  - Does not itself decide which tier or cadence a caller runs it at (C4a/C4b's concern).
  - Does not extend ``churn.AtlasComparison`` or ``churn.py``'s comparator (Anti-scope).
  - A hand-edit result carries no added/removed/changed detail — the region's on-disk state
    is untrusted the moment its digest disagrees, so no further diff is computed or implied.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from coordinator_core.docindex.render import (
    _CLOSE_RE,
    OPEN_SENTINEL,
    _render_region,
)
from coordinator_core.docindex.spec import IndexSpec

__all__ = [
    "CompareError",
    "OnDiskNotIndexed",
    "IndexedNotOnDisk",
    "FieldChanged",
    "CompareResult",
    "region_digest",
    "extract_region",
    "compare",
]


class CompareError(ValueError):
    pass


@dataclass(frozen=True)
class OnDiskNotIndexed:

    identity: str


@dataclass(frozen=True)
class IndexedNotOnDisk:

    identity: str


@dataclass(frozen=True)
class FieldChanged:

    identity: str
    field: str
    on_disk: str
    indexed: str


@dataclass(frozen=True)
class CompareResult:
    """Purely-additive comparison of an index document's on-disk region
    against a fresh render of its currently recorded entries — see module
    docstring "MECHANISM" and "VOCABULARY".

    Attributes:
        index_source_dir: the index's own declared source directory
            (IndexSpec.index_source_dir) — names WHICH index this result is
            about.
        hand_edit: True when the on-disk region's bytes disagree with the
            digest recorded in its own closing sentinel (AC12) — a hand-edit,
            refused and named, never diffed further. When True, added,
            removed, and changed are all empty; the on-disk state is
            untrusted and no further comparison is computed.
        added: entries recorded (present in the ``entries`` argument) but
            missing from the on-disk region — "indexed but not on disk",
            sorted by identity.
        removed: rows present in the on-disk region with no matching
            recorded entry — "on disk but not indexed", sorted by identity.
        changed: FieldChanged entries for rows present on both sides whose
            rendered cell text disagrees, sorted by (identity, field).
    """

    index_source_dir: str
    hand_edit: bool
    added: list[IndexedNotOnDisk] = field(default_factory=list)
    removed: list[OnDiskNotIndexed] = field(default_factory=list)
    changed: list[FieldChanged] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return not self.hand_edit and bool(self.added or self.removed or self.changed)


def region_digest(region_bytes: bytes) -> str:
    return hashlib.sha256(region_bytes).hexdigest()


def extract_region(document_text: str) -> tuple[str, str]:
    open_idx = document_text.find(OPEN_SENTINEL)
    if open_idx == -1:
        raise CompareError(
            f"no delimited region found: missing opening sentinel {OPEN_SENTINEL!r}"
        )
    region_start = open_idx + len(OPEN_SENTINEL)
    if document_text[region_start : region_start + 1] == "\n":
        region_start += 1

    close_match = _CLOSE_RE.search(document_text, region_start)
    if close_match is None:
        raise CompareError(
            "no delimited region found: missing or malformed closing sentinel"
        )

    region_text = document_text[region_start : close_match.start()]
    recorded_digest = _digest_from_close_sentinel(close_match.group(0))
    return region_text, recorded_digest


def _digest_from_close_sentinel(close_sentinel: str) -> str:
    match = re.search(r"sha256:([0-9a-f]{64})", close_sentinel)
    assert match is not None  # _CLOSE_RE already matched this exact shape
    return match.group(1)


_ROW_SPLIT_RE = re.compile(r"(?<!\\)\|")


def _split_row(line: str) -> list[str]:
    parts = _ROW_SPLIT_RE.split(line)
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return [p.strip().replace("\\|", "|") for p in parts]


def _region_rows(region_text: str) -> list[list[str]]:
    lines = [line for line in region_text.splitlines() if line.strip()]
    return [_split_row(line) for line in lines[2:]]


def compare(
    document_text: str,
    spec: IndexSpec,
    entries: Sequence[Mapping[str, object]],
) -> CompareResult:
    region_text, recorded_digest = extract_region(document_text)
    actual_digest = region_digest(region_text.encode("utf-8"))

    if actual_digest != recorded_digest:
        return CompareResult(index_source_dir=spec.index_source_dir, hand_edit=True)

    fresh_region_text = _render_region(spec, entries)

    on_disk_rows = _region_rows(region_text)
    fresh_rows = _region_rows(fresh_region_text)

    on_disk_by_identity = {row[0]: row for row in on_disk_rows if row}
    fresh_by_identity = {row[0]: row for row in fresh_rows if row}

    fields = [ef.field for ef in spec.entry_fields]

    removed = [
        OnDiskNotIndexed(identity=identity)
        for identity in sorted(set(on_disk_by_identity) - set(fresh_by_identity))
    ]
    added = [
        IndexedNotOnDisk(identity=identity)
        for identity in sorted(set(fresh_by_identity) - set(on_disk_by_identity))
    ]

    changed: list[FieldChanged] = []
    for identity in sorted(set(on_disk_by_identity) & set(fresh_by_identity)):
        old_row = on_disk_by_identity[identity]
        new_row = fresh_by_identity[identity]
        for field_name, old_cell, new_cell in zip(fields, old_row, new_row):
            if old_cell != new_cell:
                changed.append(
                    FieldChanged(
                        identity=identity,
                        field=field_name,
                        on_disk=old_cell,
                        indexed=new_cell,
                    )
                )
    changed.sort(key=lambda c: (c.identity, c.field))

    return CompareResult(
        index_source_dir=spec.index_source_dir,
        hand_edit=False,
        added=added,
        removed=removed,
        changed=changed,
    )
