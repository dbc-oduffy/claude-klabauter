"""
coordinator_core.ops.slug_prefix_family — slug-prefix-family collision predicate.

Purpose: `mint_deliverable_id.mint`'s "mint-from-slug" path mints
`dlv-<slug>-<6hex>`. The same workstream can be scaffolded more than once,
each time truncating the same underlying slug at a different length before
the hash suffix is appended — producing several distinct `deliverable_id`s
that are all prefixes of one shared source string (the Problem section's own
40/42/45 triple: `…fence-inve-fc3678` / `…fence-invent-903224` /
`…fence-inventory-df74c5`, cut at three different lengths of the same
`coordinator-ops-buildout-from-fence-inventory` slug). This module gives that
shape one home so `cascade_backstop_sweep` (C4) and `deliverable_fork_detect`
(C7) test the identical predicate against the same fixture rather than each
re-deriving string-prefix/truncation-length logic independently and risking
drift between call sites.

Spec backlink: docs/plans/2026-08-14-baton-closes-when-its-plan-ships.md § C4
(staff-eng-063f0261 finding 7)

Pure function module: no I/O, no imports outside the standard library — C4
and C7 both import it read-only.

Known false-positive surface (coordinator:code-reviewer fee2b8ee, finding 4):
a genuine, intentionally-short slug that happens to be a complete literal
prefix of an unrelated longer slug (e.g. `dlv-auth-service-abc123` vs
`dlv-auth-service-migration-def456`) will cluster as one family even though
neither is a truncation of the other. `cluster_slug_prefix_families`'s
transitive closure compounds this: a short "bridge" id can merge two
otherwise-unrelated ids into one group. This is accepted because both call
sites (`cascade_backstop_sweep`, `deliverable_fork_detect`) only ever
report a candidate family for human adjudication — they never write, merge,
or pick a winner — so a false-positive costs a reviewer's glance, not a
silent state mutation.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List

_HASH_SUFFIX_RE = re.compile(r"-[0-9a-f]{6}$")


def _slug_of(deliverable_id: str) -> str:
    """Strip the `dlv-` prefix and the trailing `-<6hex>` mint-time hash
    suffix, leaving the slug `mint_deliverable_id.mint`'s "mint-from-slug"
    path truncated at authoring time. An id with no recognizable hash suffix
    (e.g. a mint-from-stub id, `dlv-<stub_id>`) is returned with only the
    `dlv-` prefix stripped, so it still compares on its own literal text
    rather than raising."""
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
    """Group a corpus of deliverable_ids into slug-prefix families.

    Membership is the transitive closure of `is_slug_prefix_family` — an id
    joins a group if it is a family member of ANY existing member of that
    group, via a union-find over pairwise relations, so the 40/42/45 triple
    clusters as one group. Singletons (no family partner) are omitted — a
    group of one reports nothing, matching the sweep/detector's shared
    "report only a collision" contract.

    Returns groups sorted by their sorted member list, each group's members
    sorted — deterministic output a fixture can assert byte-for-byte.
    """
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
