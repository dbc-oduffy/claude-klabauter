"""Worktree 3-way sync after a `land_blobs` landing: each landed path in the worktree ends equal to HEAD, or
carrying only dirt rebased on HEAD; a path that cannot be reconciled is reported and left byte-intact.

Per path, with base = the path's blob at the landing's parent, theirs = the landed blob, ours = the worktree:
ours == base writes theirs; ours carrying dirt merges (base, ours, theirs) in process and writes the result;
a conflicting merge writes nothing. The merge and the guarded write are `git.worktree_sync`; this module
builds the plan from the landing and shapes the reply. Zero git spawns.

Invariant: the landing stands whatever the sync reports; the reply carries `worktree_sync_refused` and
`sync_incomplete: True` for a refused path, never a rollback and never a `refused` kind.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping, Optional, Tuple

#: `(mode, blob sha)`; None is absent at that side (a creation has no parent entry, a deletion no landed one).
Entry = Optional[Tuple[int, str]]


def sync_plan(spine: Mapping, trees: Mapping, changed: list, deleted: list) -> dict:
    """`{path: (parent entry, landed entry)}` for the landed `changed` and `deleted` paths: the parent entry from
    the HEAD `spine` the landing was built on, the landed entry from the staged `trees` (`{dir: {name: entry}}`)."""
    plan: dict = {}
    for rel in changed:
        d, _, name = rel.rpartition("/")
        plan[rel] = (spine.get(d, {}).get(name), trees[d][name])
    for rel in deleted:
        d, _, name = rel.rpartition("/")
        plan[rel] = (spine.get(d, {}).get(name), None)
    return plan


def _is_landed_blob(root: Path, rel: str, landed: Entry) -> bool:
    """True when the worktree file's raw bytes ARE the landed blob: already HEAD-equal, whatever the check-in
    filter would say of them (a CR-bearing file under `autocrlf=false` is refused by the 3-way sync's reader,
    though nothing needs merging)."""
    if landed is None:
        return False
    target = root / rel
    try:
        if target.is_symlink() or not target.is_file():
            return False
        data = target.read_bytes()
    except OSError:
        return False
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest() == landed[1]


def sync(root: Path, plan: Mapping) -> dict:
    """Sync every planned path into `root`'s worktree; the reply fragment (empty when every path is HEAD-equal).

    `worktree_rebased`: paths now holding the landed hunks plus the worktree's own dirt. `worktree_sync_refused`
    plus `sync_incomplete: True`: paths that could not be reconciled, bytes untouched."""
    from coordinator_core.git import commit as gc
    from coordinator_core.git.worktree_sync import sync_landed_paths

    plan = {rel: pair for rel, pair in plan.items() if not _is_landed_blob(root, rel, pair[1])}
    if not plan:
        return {}
    outcome = sync_landed_paths(root, plan)
    already = tuple(p for p in outcome.refused if gc._landed_hunks_already_in_worktree(root, p, *plan[p]))
    if outcome.head_equal:
        try:
            gc.refresh_stat_in_process(root, list(outcome.head_equal))
        except Exception:  # noqa: BLE001 -- a stale stat only makes git re-hash; the landing stands
            pass
    refused = tuple(p for p in outcome.refused if p not in already)
    reply: dict = {}
    rebased = [*outcome.rebased, *already]
    if rebased:
        reply["worktree_rebased"] = rebased
    return {**reply, **gc.sync_report(_Refused(refused))}


class _Refused:
    """`commit.sync_report` reads `sync_refused` off an outcome; this carries just that."""

    def __init__(self, paths: tuple) -> None:
        self.sync_refused = paths


def fallback_report(reply: dict) -> dict:
    """The same reply fragment for a fallback commit, whose `commit_paths` already synced the worktree and
    returned its refusals as `sync_refused`."""
    from coordinator_core.git import commit as gc

    return gc.sync_report(_Refused(tuple(reply.get("sync_refused") or ())))
