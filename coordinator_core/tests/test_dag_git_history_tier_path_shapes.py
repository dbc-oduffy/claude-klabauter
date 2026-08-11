"""Regression: resolve_target's git-history tier must resolve a disk-absent
lineage target that was ever git-tracked at ANY path — including a path
different from the one the reference's own directory implies.

The defect this pins (2026-08-10): the corpus lint
``test_no_dangling_handoff_lineage_references_in_corpus`` declared a real,
git-tracked predecessor "provably never-existed" — a verdict whose remedy text
instructs the operator to clear a valid lineage pointer, i.e. to destroy data.

Two independent causes, both covered below:

1. **Separator mismatch (Windows-only, the live failure).** A cache from
   ``build_git_history_cache`` is keyed with git's forward-slash repo-relative
   paths on every platform, but ``resolve_target`` derives its tier-3 lookup
   keys by slicing ``os.path.normpath``-ed absolute candidates — backslashed on
   Windows. Every lookup missed, and because a ``complete=True`` cache treats a
   miss as AUTHORITATIVE ("never tracked", no subprocess fallback), the miss was
   promoted straight to the false verdict.

2. **Month-foldered archive absent from tier 3.** The month-folder sweep
   enumerates only directories that still EXIST on disk; a target age-pruned
   from disk leaves none to enumerate, so ``archive/handoffs/YYYY-MM/<name>``
   was never offered to the git-history tier at all.

Negative-spec: does NOT assert anything about a genuinely never-tracked
target beyond the one case below — that path must still return None, or the
lint stops catching real rot.

Spec backlink: coordinator_core/dag.py::resolve_target,
coordinator_core/dag.py::_memoized_ever_tracked
"""
from __future__ import annotations

import os

from coordinator_core.dag import GitHistoryCache, resolve_target

_NAME = '2026-07-21_194404_6ffb9c2d-2568-4f12-b1fc-4c727912053f.md'


def _cache(*paths: str) -> GitHistoryCache:
    """A confirmed-complete history cache keyed the way git emits paths:
    forward slashes, repo-relative, on every platform."""
    return GitHistoryCache(set(paths), complete=True)


def test_target_tracked_only_under_state_resolves_from_archive_referrer(tmp_path):
    """A record living in archive/handoffs/YYYY-MM/ whose predecessor was
    git-tracked under state/handoffs/ resolves via the git-history tier."""
    repo_root = str(tmp_path)
    handoff_dir = os.path.join(repo_root, 'archive', 'handoffs', '2026-07')

    resolved = resolve_target(
        _NAME, handoff_dir, repo_root, _cache(f'state/handoffs/{_NAME}')
    )

    assert resolved == 'git-history'


def test_target_tracked_only_under_month_foldered_archive_resolves(tmp_path):
    """A live record whose predecessor was pruned from disk but git-tracked at
    archive/handoffs/YYYY-MM/<name> resolves — the month folder need not exist
    on disk, which is precisely the age-pruned case."""
    repo_root = str(tmp_path)
    handoff_dir = os.path.join(repo_root, 'state', 'handoffs')

    resolved = resolve_target(
        _NAME, handoff_dir, repo_root, _cache(f'archive/handoffs/2026-07/{_NAME}')
    )

    assert resolved == 'git-history'


def test_never_tracked_target_still_unresolvable(tmp_path):
    """The lint must keep catching real rot: a target absent from a complete
    history cache resolves to None."""
    repo_root = str(tmp_path)
    handoff_dir = os.path.join(repo_root, 'state', 'handoffs')

    resolved = resolve_target(
        'never-authored-00000000.md',
        handoff_dir,
        repo_root,
        _cache(f'state/handoffs/{_NAME}'),
    )

    assert resolved is None
