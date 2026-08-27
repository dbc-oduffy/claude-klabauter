"""
coordinator_core.ops.ownership_index — session-keyed ownership index over the
shared handoff walker (COMPUTE_ONLY).

Companion to `coordinator_core.reconcile.gate_eval`'s `_index_by_id` (the gate
index), NOT a shared structure with it. Ownership and gating share no
consistency invariant — a session owning a blocked baton is normal, a blocked
baton being unowned is normal — and they join on different keys: gating
resolves on `stub_id`/`handoff_id` found IN the frontmatter, ownership
resolves on session id via the claim-record STORE, with the frontmatter's
`claimed_by`/`consumed_by` demoted to a validated mirror — flagged (via
`_LOG.warning`) when it disagrees with the claim-store session id, but never
allowed to override the claim-store's own membership decision, i.e.
consulted for staleness detection, never the source of truth. This module
must never import `coordinator_core.reconcile.gate_eval`,
and nothing in that module may import this one — the type signatures alone
make handing one index to the other's consumer inexpressible.

Data flow (claim-store-first, never claimed_by-frontmatter-first):
  claims.list_claims_by_session_checked(sid) (authoritative: session ->
  handoff-class basenames, plus an `errors` arm distinguishing "genuinely
  owns nothing" from "the claim store could not be resolved", e.g. an
  unresolvable cwd) -> `handoff_corpus._collect_all_handoffs_for_gate_index`
  (the ONE shared live+archive walk over the handoff corpus — this module
  adds no second traversal) -> a basename->(path, frontmatter) join built
  from that call's `_path`-bearing output -> this index.

The corpus is walked exactly once, by that shared function; this module's
only job is the claim-store join on top of it. (Earlier revision re-walked
the corpus itself with `collect_live_handoff_paths`/`_walk_archive_md_files`
directly, because `_collect_all_handoffs_for_gate_index` did not yet attach
a path to each entry — that gap is closed, so the second walk is gone.)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Tuple

from coordinator_core.reconcile.handoff_corpus import _collect_all_handoffs_for_gate_index
from coordinator_core.session import claims

_LOG = logging.getLogger(__name__)

_HANDOFF_CLAIM_CLASS = "handoff-claims"


def _basename_index(worktree_root: Path) -> "Tuple[Dict[str, Tuple[Path, dict]], List[str]]":
    """basename -> (path, frontmatter) built from the ONE shared walk over the
    live+archived handoff corpus (`handoff_corpus._collect_all_handoffs_for_
    gate_index` — spans `state/handoffs/` + `archive/handoffs/` +
    `archive/completed/`). No second traversal happens here: every entry that
    function returns already carries `_path`, so this is a pure re-keying pass,
    not a walk. `scan_errors` is exactly what that call returned — non-empty
    whenever an archive subtree could not be fully scanned, meaning a claimed
    basename living under it may be missing from the returned index,
    indistinguishable from "no such basename was ever claimed" without this
    signal.
    """
    all_handoffs, scan_errors = _collect_all_handoffs_for_gate_index(worktree_root)

    index: Dict[str, Tuple[Path, dict]] = {}
    for meta in all_handoffs:
        raw_path = meta.get("_path")
        if not raw_path:
            continue
        path = Path(raw_path)
        if path.name in index:
            # Review: code-reviewer — Finding 5 (nit): basenames are asserted
            # unique elsewhere by convention (timestamp+slug), so this is
            # unlikely to fire, but a collision across live/archive roots
            # (e.g. a handoff duplicated during a concurrent ceremony race)
            # would otherwise silently last-write-win with no signal at all.
            prior_path, _prior_meta = index[path.name]
            _LOG.warning(
                "ownership_index: basename %r seen more than once during "
                "_basename_index construction — %s is being replaced by %s "
                "(last-write-wins; the convention of unique basenames "
                "elsewhere may have been violated)",
                path.name, prior_path, path,
            )
        index[path.name] = (path, meta)

    return index, scan_errors


def build_ownership_index(
    worktree_root: Path, sid: str,
) -> "Tuple[List[Tuple[Path, dict]], List[str]]":
    """(owned_batons, scan_errors). Data flow:
    claims.list_claims_by_session_checked(sid) (authoritative, session ->
    handoff-class basenames) -> a live+archive basename -> (path, frontmatter)
    resolution across `state/` + `archive/` -> this index. NEVER derives
    ownership from `claimed_by` inside the walked frontmatter directly — the
    claim-record store decides membership; the frontmatter is consulted only
    to hand the caller the (path, fm) pair for an already-decided-owned
    basename, per this module's own DECIDING RATIONALE. A claimed basename
    with no matching walked path (a stale claim on a deleted/relocated
    handoff) is recorded via a warning rather than silently dropped, and
    never raises — an unresolvable claim is a data question for the caller,
    not a crash. A resolved basename whose `claimed_by`/`consumed_by`
    frontmatter mirror disagrees with `sid` is also warning-logged (not
    excluded from `owned` — the claim-store decision above is final either
    way; this is staleness detection, not a second membership gate). If the
    claim store itself could not be resolved (e.g. an
    unresolvable `worktree_root`), `list_claims_by_session_checked` reports
    that via its `errors` arm rather than silently returning an empty owned
    set indistinguishable from "this session genuinely holds no claims";
    those errors are merged into `scan_errors`, ahead of the walk's own
    scan errors.
    """
    claimed, claim_errors = claims.list_claims_by_session_checked(
        sid, cwd=str(worktree_root)
    )
    basename_index, walk_scan_errors = _basename_index(worktree_root)
    scan_errors = claim_errors + walk_scan_errors

    owned: List[Tuple[Path, dict]] = []
    unresolved: List[str] = []
    mirror_disagreements: List[str] = []
    for class_, basename in claimed:
        if class_ != _HANDOFF_CLAIM_CLASS:
            continue
        entry = basename_index.get(basename)
        if entry is None:
            unresolved.append(basename)
            continue
        _path, meta = entry
        mirror = meta.get("claimed_by") or meta.get("consumed_by")
        # Review: code-reviewer — Finding 2 (P2): the frontmatter mirror is
        # consulted here for a disagreement check only — the claim-store
        # membership decision above (`claimed`/`basename_index.get`) is
        # already final and unaffected by this check either way. A mismatch
        # is exactly the staleness class this module's docstring exists to
        # defend against, so it is surfaced (not silently accepted) rather
        # than validated in the sense of gating the ownership decision.
        if mirror is not None and mirror != sid:
            mirror_disagreements.append(f"{basename} (mirror={mirror!r}, claim-store={sid!r})")
        owned.append(entry)

    if mirror_disagreements:
        _LOG.warning(
            "ownership_index: %d claimed handoff basename(s) for session %s "
            "have a `claimed_by`/`consumed_by` frontmatter mirror disagreeing "
            "with the claim-store session id (stale mirror — claim-store "
            "wins, mirror is consulted/not authoritative): %s",
            len(mirror_disagreements), sid, sorted(mirror_disagreements),
        )

    if unresolved:
        _LOG.warning(
            "ownership_index: %d claimed handoff basename(s) held by session "
            "%s have no matching path in the live+archive walk (stale claim, "
            "or the handoff was relocated/deleted after being claimed): %s",
            len(unresolved), sid, sorted(unresolved),
        )

    return owned, scan_errors
