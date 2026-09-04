"""One named resolver for every memo-corpus root construction in coordinator_core.

Purpose: coordinator_core has ~32 call sites that used to hardcode the `cross-repo`
directory literal directly. During the C10a fleet-wide migration window the
canonical root is moving repo-by-repo from `<repo>/cross-repo/` to
`<repo>/state/cross-repo/`, so a hardcoded literal is wrong for THIS repo the
moment it migrates, and wrong for a PEER the moment ANY receiver migrates while
others have not. This module is the single place that answers "where is the
memo corpus", so every caller resolves through one implementation instead of
32 independently-drifting copies.

Two distinct jobs, two functions -- conflating them is the defect this plan
exists to avoid:

- `memo_corpus_root` -- THIS repo's own corpus root.
- `receiver_inbox_root` -- a PEER's inbox root, probed per-receiver (lifted
  from `coordinator/bin/cross-repo-memo.py::_receiver_inbox_root`).

Negative-spec: do not add a third helper that "unifies" these two into one
signature. They have different callers and different write-time defaults (only
`memo_corpus_root`'s callers create a directory; `receiver_inbox_root` only
ever reads).

Both resolve PER INVOCATION -- neither is memoized. `memo_corpus_root` was
process-lifetime `lru_cache`d when this module landed; that cache was removed
(coordinator:overengineering-reviewer, 2026-09-03) because it defended against
a call pattern the BRIGHTLINE paragraph below already bans, and because it
made `group_em/watch.py` -- a long-lived process that re-resolves per tick by
design -- freeze the root at its first resolution. Two `is_dir()` calls are
not worth a cache that has to be reasoned about, and uniformity removes the
asymmetry a reader had to hold in their head.

BRIGHTLINE CONSTRAINT: resolve once per invocation (one op dispatch, or one
watch tick) and pass the result down through the call chain. Do not call
either resolver from behind each of the ~32 downstream call sites -- that is
a filesystem-probe storm on a box running 50+ concurrent sessions. Callers own
threading the resolved root through their own call graph; this module does not
do that threading for them.
"""

from __future__ import annotations

import os

__all__ = ["memo_corpus_root", "receiver_inbox_root"]


def memo_corpus_root(repo_root: str) -> str:
    """Resolve THIS repo's own memo-corpus root, given its repo root path.

    Prefers `state/cross-repo/` (the C10a-migrated convention), falls back to
    the legacy `cross-repo/` when the new root does not exist on disk. Callers
    derive inbox/archive/outbox paths from this single root, preserving
    siblinghood between them (an inbox resolved against the new root must
    never pair with an archive resolved against the legacy one).

    Resolved per invocation, not memoized: at most two `is_dir()` calls, and
    the BRIGHTLINE constraint above already forbids the per-call-site probing
    a cache would exist to absorb. Resolve once per invocation and thread the
    result down, as that paragraph says.

    Write-when-neither-exists: when NEITHER `state/cross-repo/` nor
    `cross-repo/` exists on disk (a new or freshly-cloned repo), this
    resolver returns the NEW root (`state/cross-repo/`), never the legacy
    one -- a caller that then creates the directory mints it under the
    migrated convention, so the engine never mints a fresh legacy inbox for
    any repo it touches first.
    """
    new_root = os.path.join(repo_root, "state", "cross-repo")
    if os.path.isdir(new_root):
        return new_root
    legacy_root = os.path.join(repo_root, "cross-repo")
    if os.path.isdir(legacy_root):
        return legacy_root
    return new_root


def receiver_inbox_root(repo_path: str) -> "tuple[str, bool]":
    """Resolve the cross-repo inbox root for ONE specific receiver repo.

    Fleet-wide convention move (PM ruling 2026-09-02, C10a notice): the
    canonical inbox root is moving from `<repo>/cross-repo/` to
    `<repo>/state/cross-repo/`, repo-by-repo, during a migration window --
    not all at once. A fixed `<repo>/cross-repo/` literal is wrong for every
    sender the moment ANY receiver has moved, so this probes the SPECIFIC
    receiver repo passed in rather than assuming a fleet-wide constant:
    prefers the new `state/cross-repo/` root when it exists on disk, falls
    back to the legacy `cross-repo/` root otherwise (receiver not yet
    migrated, or a pre-move sibling on an older machine). A peer that has not
    migrated still resolves to its legacy root, which is what keeps
    unmigrated senders working.

    Returns `(root, root_isdir)` -- `root_isdir` is the `os.path.isdir(root)`
    result this probe already computed, so callers don't re-run the identical
    check the new-root branch just performed. The legacy-root branch does NOT
    verify existence beyond that isdir call -- a legacy root's existence (or
    lack of it) is the caller's business.

    NOT process-lifetime-memoized, and this one must never become so even if
    a future reader decides two `is_dir()` calls are worth caching. Peers migrate on their own schedule,
    asynchronously, per this plan's own fleet strategy (C11) -- a
    process-lifetime memo of a peer's root would be stale-by-design during
    exactly the window this plan opens, and the failure mode is silent and
    directional: a peer that migrates mid-process would keep receiving our
    deliveries into its stale-cached legacy inbox, recreating this plan's own
    defect one layer out. Callers MAY cache the result themselves for the
    span of a single invocation (one op dispatch, one watch tick) per the
    brightline constraint above, but MUST NOT persist that cache across
    invocations. This costs one extra `is_dir()` per peer per delivery,
    nowhere near the 500ms brightline.
    """
    new_root = os.path.join(repo_path, "state", "cross-repo")
    if os.path.isdir(new_root):
        return new_root, True
    legacy_root = os.path.join(repo_path, "cross-repo")
    return legacy_root, os.path.isdir(legacy_root)
