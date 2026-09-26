
from __future__ import annotations

import re
from typing import Dict, Iterable, List

_HASH_SUFFIX_RE = re.compile(r"-[0-9a-f]{6}$")


def _slug_of(deliverable_id: str) -> str:
    value = deliverable_id.strip()
    if value.startswith("dlv-"):
        value = value[len("dlv-") :]
    return _HASH_SUFFIX_RE.sub("", value)


def is_slug_prefix_family(id_a: str, id_b: str) -> bool:
    """True when `id_a` and `id_b` are DISTINCT deliverable_ids whose
    mint-time slugs are prefixes of one another — the shape this incident's
    40/42/45 triple reproduces: one shared source string, cut at three
    different truncation lengths before the hash suffix. Equal ids are NOT a
    family here — that is the pre-existing exact-equality join
    (`cascade_backstop_sweep`'s divergence check), a different and
    already-handled case."""
    if id_a == id_b:
        return False
    slug_a = _slug_of(id_a)
    slug_b = _slug_of(id_b)
    if not slug_a or not slug_b:
        return False
    return slug_a.startswith(slug_b) or slug_b.startswith(slug_a)


def cluster_slug_prefix_families(deliverable_ids: Iterable[str]) -> List[List[str]]:
    ids = sorted({v.strip() for v in deliverable_ids if v and v.strip()})
    parent: Dict[str, str] = {v: v for v in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for i, id_a in enumerate(ids):
        for id_b in ids[i + 1 :]:
            if is_slug_prefix_family(id_a, id_b):
                union(id_a, id_b)

    groups_by_root: Dict[str, List[str]] = {}
    for v in ids:
        groups_by_root.setdefault(find(v), []).append(v)

    return sorted(sorted(group) for group in groups_by_root.values() if len(group) >= 2)
