"""coordinator_core.clustering.candidates — unattached-record clustering core.

Groups an unattached record set (from `records_query.query_records`, or a
caller-supplied equivalent) by shared signal (tag/directory/keyword) and
emits CANDIDATE clusters with a suggested label. Pure computation — never
writes, never queries on its own; callers own I/O (see
`coordinator/bin/detect-initiative-candidates` for the CLI caller).

Floor: DR-209's >=3-items-per-cluster threshold. A cluster below
MIN_CLUSTER_SIZE does not surface as a candidate.

Moved verbatim (no behavior/signature change) from
`coordinator/bin/detect-initiative-candidates` — this is a pure relocation;
STOP_WORDS/MIN_CLUSTER_SIZE are pinned byte-identical to their prior values
(see `coordinator_core/clustering/tests/test_candidates_pin.py`), and any
tuning of the clustering heuristic itself rides in a separate, reviewable
change — not this move.

Spec: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C2
"""
from __future__ import annotations

import os
import re

# DR-209 floor: clusters with fewer than this many items are not surfaced.
#
# Spec backlink: docs/plans/2026-07-04-initiative-govern-sweep-prioritize-doe-d.md § C4
MIN_CLUSTER_SIZE = 3

# Stop words for title-keyword clustering — common English function words plus
# coordinator-domain verbs that are too ubiquitous to be meaningful cluster signals.
STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "can", "not", "no", "nor",
    "so", "yet", "both", "either", "each", "few", "more", "most", "other",
    "some", "such", "than", "too", "very", "just", "add", "fix", "update",
    "new", "old", "via", "per", "into", "onto", "about", "after", "before",
    "between", "through", "during", "without", "within", "also", "this",
    "that", "these", "those", "then", "when", "where", "which", "while",
}

# ---------------------------------------------------------------------------
# Core clustering logic
# ---------------------------------------------------------------------------


def detect_candidates(records: list[dict]) -> list[dict]:
    """Cluster a set of unattached records by shared signal and return candidate clusters.

    Purpose: graduation-gate surface — groups items that may warrant becoming an
    initiative so a human can author the cut (surface-and-confirm; this function
    never writes anything).

    Three signals are evaluated in order; a record may appear in multiple clusters
    across different signals:
      1. tag       — shared value in frontmatter.tags / .topic / .areas
      2. directory — shared parent directory path (first two path segments)
      3. keyword   — shared significant word extracted from frontmatter.title

    Args:
        records: unattached records from query-records, each {"path": str, "frontmatter": dict}.

    Returns:
        Candidate clusters. Each cluster has >=MIN_CLUSTER_SIZE items (DR-209 floor).
        Results are ordered: tag clusters first, directory second, keyword last.
    """
    clusters: list[dict] = []

    # --- Signal 1: shared tag ---
    # Records sharing a tag value in frontmatter.tags / .topic / .areas (string or array).
    tag_map: dict[str, list[dict]] = {}
    for rec in records:
        for tag in _normalize_tags(rec.get("frontmatter")):
            tag_map.setdefault(tag, []).append(rec)
    for tag, recs in tag_map.items():
        if len(recs) >= MIN_CLUSTER_SIZE:
            clusters.append({
                "signal": "tag",
                "value": tag,
                "suggestedLabel": _humanize(tag),
                "items": [_item(r) for r in recs],
            })

    # --- Signal 2: directory ---
    # Records sharing the same parent directory (e.g., all state/bug-backlog/ items).
    dir_map: dict[str, list[dict]] = {}
    for rec in records:
        dir_ = _parent_dir(rec.get("path", ""))
        dir_map.setdefault(dir_, []).append(rec)
    for dir_, recs in dir_map.items():
        if len(recs) >= MIN_CLUSTER_SIZE:
            clusters.append({
                "signal": "directory",
                "value": dir_,
                "suggestedLabel": _humanize(os.path.basename(dir_)),
                "items": [_item(r) for r in recs],
            })

    # --- Signal 3: title keyword ---
    # Records sharing a significant word (length >=4, not a stop word) from frontmatter.title.
    kw_map: dict[str, list[dict]] = {}
    for rec in records:
        fm = rec.get("frontmatter") or {}
        title = fm.get("title") or ""
        for kw in _extract_keywords(title):
            kw_map.setdefault(kw, []).append(rec)
    for kw, recs in kw_map.items():
        if len(recs) >= MIN_CLUSTER_SIZE:
            clusters.append({
                "signal": "keyword",
                "value": kw,
                "suggestedLabel": _humanize(kw),
                "items": [_item(r) for r in recs],
            })

    return clusters


def _item(rec: dict) -> dict:
    fm = rec.get("frontmatter") or {}
    return {"path": rec.get("path", ""), "title": fm.get("title") or ""}


# ---------------------------------------------------------------------------
# Helpers (prefixed _ to distinguish from exported API)
# ---------------------------------------------------------------------------


def _normalize_tags(fm: dict | None) -> list[str]:
    """Extract a flat list of lowercase tag strings from a frontmatter object.
    Reads .tags, .topic, or .areas; each may be a string (comma-separated) or list.

    Design: exclusive-alternatives — the three fields are checked in priority order
    (.tags first, then .topic, then .areas) and only the first truthy field contributes.
    A record with both .tags and .areas contributes only the .tags signal. This is
    intentional: mixing multiple tag-style fields from a single record would allow
    loosely-related records to cluster on spurious multi-field coincidences.
    """
    if not fm:
        return []
    raw = fm.get("tags") or fm.get("topic") or fm.get("areas")
    if not raw:
        return []
    if isinstance(raw, list):
        tags = [str(t).lower().strip() for t in raw if str(t).strip()]
    elif isinstance(raw, str):
        tags = [t.lower().strip() for t in raw.split(",") if t.strip()]
    else:
        return []
    return _dedupe_preserve_order(tags)


def _parent_dir(file_path: str) -> str:
    """Return the parent directory of a path, normalized to forward slashes.
    Used as the directory-cluster key.
    """
    return os.path.dirname(file_path).replace("\\", "/")


def _extract_keywords(title: str) -> list[str]:
    """Extract significant words from a title string.
    Words under 4 chars or in the stop-word list are excluded. A keyword
    list is conceptually a set of signals a title raises, not a bag of
    occurrences — a word repeated within one title is returned once, so a
    single record contributes at most one entry to any downstream
    keyword-cluster bucket (see the double-count negative-spec below).

    Negative-spec: a title repeating a significant word (e.g. "...repo ...
    repo") must NOT cause the caller's keyword-cluster bucket for that word
    to count the same record twice, which would manufacture a
    floor-violating false-positive cluster out of fewer distinct records
    than MIN_CLUSTER_SIZE actually requires.
    """
    cleaned = re.sub(r"[^a-z0-9\s]", " ", title.lower())
    words = [w for w in cleaned.split() if len(w) >= 4 and w not in STOP_WORDS]
    return _dedupe_preserve_order(words)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """Return items with duplicates removed, preserving first-seen order.

    Used wherever a list is conceptually a set of signals rather than a bag
    of occurrences (keyword extraction, tag normalization) — a bare `set()`
    would discard the deterministic ordering downstream code relies on.
    """
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _humanize(s: str) -> str:
    """Convert a snake_case, kebab-case, or slash-separated identifier to Title Case.
    Used to produce the suggestedLabel for each cluster.
    """
    spaced = re.sub(r"[-_/]", " ", s)
    titled = re.sub(r"\b\w", lambda m: m.group(0).upper(), spaced)
    return titled.strip()
