"""Model-free clustering of ArtifactRecords into one TopicDigest per topic key.

Topic key: the path stem with its date prefix and trailing hex6 id stripped and slugged; a decision
record `DR-NNN-*` keys as `dr-NNN`. Records with identical normalised content (path excluded) collapse
to the first by path. Output is sorted by topic key, records by path: deterministic, no embeddings.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from coordinator_core.backlog.record_schema import ArtifactRecord, TopicDigest, token_proxy

__all__ = ["topic_key", "cluster_records"]

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-?")
_HEX_SUFFIX = re.compile(r"-[0-9a-f]{6}$")
_DR = re.compile(r"^dr-?(\d+)", re.IGNORECASE)
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def topic_key(path: str) -> str:
    """Deterministic topic key for a repo-relative path."""
    stem = PurePosixPath(path.replace("\\", "/")).stem
    dr = _DR.match(stem)
    if dr:
        return f"dr-{int(dr.group(1)):03d}"
    stem = _HEX_SUFFIX.sub("", _DATE_PREFIX.sub("", stem.lower()))
    return _NON_SLUG.sub("-", stem).strip("-") or "untitled"


def _content_hash(record: ArtifactRecord) -> str:
    parts = [*record.headings, *record.decisions, *record.outcomes, *record.tasks_ledger]
    norm = "\n".join(" ".join(p.lower().split()) for p in parts)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _render(key: str, records: tuple[ArtifactRecord, ...]) -> str:
    out = [f"# topic: {key}"]
    for rec in records:
        out.append(f"## {rec.path}")
        out.extend(rec.headings)
        out.extend(rec.decisions)
        out.extend(rec.outcomes)
        out.extend(f"task: {t}" for t in rec.tasks_ledger)
    return "\n".join(out) + "\n"


def cluster_records(records: list[ArtifactRecord]) -> list[TopicDigest]:
    """One TopicDigest per topic key, duplicates collapsed, ordered by key then path."""
    seen: set[str] = set()
    groups: dict[str, list[ArtifactRecord]] = {}
    for rec in sorted(records, key=lambda r: r.path):
        digest = _content_hash(rec)
        if digest in seen:
            continue
        seen.add(digest)
        groups.setdefault(topic_key(rec.path), []).append(rec)
    digests = []
    for key in sorted(groups):
        recs = tuple(groups[key])
        text = _render(key, recs)
        digests.append(TopicDigest(topic_key=key, records=recs, text=text, tokens=token_proxy(text)))
    return digests
