"""
coordinator_core.ops.deliverable_equivalence — shared deliverable-id equivalence read-model.

Purpose: ONE join-key canonicalization mechanism shared by all four raw-comparison
consumers named in the fork-remediation audit (`deliverable_status.py`,
`deliverable_rollup.py`, `migrate_handoff_vocabulary.py`, and — transitively, via its
in-process call into `deliverable_rollup._handler` — `coordinator_render_rollup.py`).
Each of those consumers today compares raw `deliverable_id` strings for equality; when a
deliverable has been forked (minted twice for the same underlying work, per DD#1's
declared-winner tiebreak), that raw comparison silently treats the two legs as unrelated
entities. This module lets every reader treat a declared fork pair as one entity without
touching what any of those legs' own records carry on disk.

Reads the declared equivalence artifact at `state/deliverable-equivalence.yaml`
(relative to the worktree root), authored by chunk C3b:

    entries:
      - loser: dlv-...
        winner: dlv-...
        evidence: "..."

`load_equivalence_map` builds `{loser: winner}` from `entries`. Every `loser` is unique
and no `winner` ever appears elsewhere as a `loser` (no transitive chains) — that
invariant is C3b's authoring obligation, not this module's to enforce by walking chains;
this loader only guards against a malformed artifact silently masquerading as a clean map
(see `_build_equivalence_map`'s duplicate-loser handling below).

Spec backlink: docs/plans/2026-08-01-deliverable-id-fork-remediation.md § C4 (AC6, AC6b, AC9, AC12)

Negative-spec (hard-won, load-bearing — do not narrow this back):
  - **Join-key transform only. NEVER a field mutation.** No caller of `canonicalize()` may
    write a canonicalized value back to disk, into a record's own `deliverable_id` field,
    or into any emitted output. The plan's anti-scope forbids mutating archived records;
    `canonicalize()` is precisely what makes every READER treat a fork as one entity
    without touching what is on disk. Wiring this into a consumer's write path instead of
    its read/compare path is a scope violation of the chunk this module ships under.
  - **A fork with no declared entry canonicalizes to itself.** Absence of an entry in the
    equivalence artifact is never treated as a silent merge — `canonicalize()` returns the
    input unchanged for any id it does not recognise as a declared loser.
  - **A missing artifact is not an error.** `load_equivalence_map` returns an empty map
    (`{}`) when `state/deliverable-equivalence.yaml` does not exist — every id then
    canonicalizes to itself, i.e. today's raw-comparison behaviour, unchanged. This is the
    expected steady state until C3b lands in the next wave.
  - **`canonicalize()` performs zero I/O.** It is a pure function over its two arguments.
    `load_equivalence_map` is the ONLY I/O in this module, and it is memoized per-process
    (mirrors `deliverable_rollup.py`'s own `_central_initiatives_dir` module-scope
    memoization convention: resolve once, reuse for the process lifetime) so repeat
    callers within one spawn-per-call process do not re-read the artifact.
  - **Idempotent by construction.** Because `canonicalize()` is pure and the equivalence
    map carries no transitive chains (a winner never also appears as a loser — C3b's
    authoring obligation), `canonicalize(canonicalize(x, m), m) == canonicalize(x, m)`
    for every `x` and every well-formed map `m`. No caller needs to re-canonicalize a
    result — but doing so is harmless, which is the AC12 idempotence-by-construction
    property this module ships for its own read-path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

import yaml

logger = logging.getLogger(__name__)

_EQUIVALENCE_ARTIFACT_RELPATH = Path("state") / "deliverable-equivalence.yaml"

# ---------------------------------------------------------------------------
# Per-process memoization state for load_equivalence_map — mirrors
# deliverable_rollup.py's _central_initiatives_dir module-scope memoization
# convention (resolve once, reuse for the process lifetime).
# ---------------------------------------------------------------------------

_RESOLVED_EQUIVALENCE_MAP: Optional[Dict[str, str]] = None
_EQUIVALENCE_MAP_RESOLVED: bool = False
_RESOLVED_FOR_ROOT: Optional[Path] = None


def _reset_equivalence_map_cache() -> None:
    """Test-only helper: clear the ``load_equivalence_map`` process-scope memo.

    Mirrors ``deliverable_rollup._reset_central_root_cache``. The "at most once per
    process lifetime" memoization contract is correct under the spawn-per-call
    execution model, where the process exits after one op, but is NOT correct under
    pytest, where every test shares one interpreter — the first test to resolve pins
    the map for the whole session unless this is called between tests.
    """
    global _RESOLVED_EQUIVALENCE_MAP, _EQUIVALENCE_MAP_RESOLVED, _RESOLVED_FOR_ROOT
    _RESOLVED_EQUIVALENCE_MAP = None
    _EQUIVALENCE_MAP_RESOLVED = False
    _RESOLVED_FOR_ROOT = None


def _build_equivalence_map(entries: list) -> Dict[str, str]:
    """Build ``{loser: winner}`` from a parsed ``entries`` list.

    Purpose: isolates the entries -> map projection so ``load_equivalence_map`` stays
    focused on I/O and memoization. A malformed entry (missing loser/winner, non-string
    values) is skipped with a WARNING rather than raising — a single bad row in the
    declared artifact should not take down every consumer's read path. A duplicate
    ``loser`` (violating C3b's uniqueness obligation) keeps the FIRST mapping seen and
    logs a WARNING rather than silently overwriting it, since silently resolving a
    duplicate one way is exactly the kind of one-level chain-resolution this module
    must not do quietly.
    """
    equivalence_map: Dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning(
                "deliverable_equivalence: skipping non-mapping entry in "
                "state/deliverable-equivalence.yaml: %r",
                entry,
            )
            continue
        loser = entry.get("loser")
        winner = entry.get("winner")
        if not isinstance(loser, str) or not loser.strip():
            logger.warning(
                "deliverable_equivalence: skipping entry with missing/invalid 'loser': %r",
                entry,
            )
            continue
        if not isinstance(winner, str) or not winner.strip():
            logger.warning(
                "deliverable_equivalence: skipping entry with missing/invalid 'winner': %r",
                entry,
            )
            continue
        loser = loser.strip()
        winner = winner.strip()
        if loser in equivalence_map:
            logger.warning(
                "deliverable_equivalence: duplicate loser id %r in "
                "state/deliverable-equivalence.yaml — keeping first-seen mapping to %r, "
                "ignoring %r. This violates C3b's uniqueness obligation; the artifact "
                "should be corrected.",
                loser,
                equivalence_map[loser],
                winner,
            )
            continue
        equivalence_map[loser] = winner

    # Review: coordinatorcode-reviewer-67ffaa7e Finding 2 — the no-transitive-chains
    # invariant (a winner must never also appear as a loser) previously had zero
    # enforcement signal, unlike the duplicate-loser case above which warns. This does
    # not walk/resolve chains (still C3b's authoring obligation) — it only makes the
    # violation visible.
    chained_ids = set(equivalence_map.values()) & set(equivalence_map.keys())
    if chained_ids:
        logger.warning(
            "deliverable_equivalence: transitive chain detected in "
            "state/deliverable-equivalence.yaml — the following id(s) appear as both a "
            "'winner' and a 'loser', violating the no-transitive-chains invariant: %r. "
            "canonicalize() will only resolve one level; the artifact should be corrected.",
            sorted(chained_ids),
        )
    return equivalence_map


def load_equivalence_map(worktree_root: Path) -> Dict[str, str]:
    """Load the declared fork-equivalence map, memoized per-process.

    Purpose: reads ``<worktree_root>/state/deliverable-equivalence.yaml`` (C3b's
    declared artifact) and returns ``{loser_id: winner_id}``. This is the ONLY I/O in
    this module. Resolution is memoized at module scope for the process lifetime —
    mirrors ``deliverable_rollup.py``'s ``_central_initiatives_dir`` convention — so a
    spawn-per-call process reads the artifact at most once regardless of how many
    consumers call this function.

    A missing artifact is NOT an error: returns ``{}`` so every id canonicalizes to
    itself (today's raw-comparison behaviour). A present-but-unparsable artifact (bad
    YAML) also degrades to ``{}`` with a logged WARNING, rather than raising and taking
    down the calling consumer's whole read path.

    Note: the memo is keyed to the FIRST worktree_root this is called with in a given
    process; a second call with a different root during the same process returns the
    memoized map from the first (spawn-per-call processes never see more than one
    worktree root in their lifetime, so this is not a correctness concern in
    production — it is surfaced here only so a test iterating multiple roots knows to
    call ``_reset_equivalence_map_cache()`` between them).
    """
    global _RESOLVED_EQUIVALENCE_MAP, _EQUIVALENCE_MAP_RESOLVED, _RESOLVED_FOR_ROOT

    if _EQUIVALENCE_MAP_RESOLVED:
        assert _RESOLVED_EQUIVALENCE_MAP is not None
        # Review: coordinatorcode-reviewer-67ffaa7e Finding 4 — _RESOLVED_FOR_ROOT was
        # assigned but never read. A later call with a different worktree_root silently
        # returns the first root's memoized map; this makes that mismatch visible.
        if _RESOLVED_FOR_ROOT is not None and worktree_root != _RESOLVED_FOR_ROOT:
            logger.warning(
                "deliverable_equivalence: load_equivalence_map called with worktree_root "
                "%s but the memoized map was resolved for %s; returning the memoized map "
                "for the FIRST root, not this one. Call _reset_equivalence_map_cache() "
                "between roots if this is a test iterating multiple worktrees.",
                worktree_root,
                _RESOLVED_FOR_ROOT,
            )
        return _RESOLVED_EQUIVALENCE_MAP

    artifact_path = worktree_root / _EQUIVALENCE_ARTIFACT_RELPATH

    if not artifact_path.is_file():
        equivalence_map: Dict[str, str] = {}
    else:
        try:
            content = artifact_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(content)
        except Exception as exc:  # noqa: BLE001 — a bad artifact degrades, not raises
            logger.warning(
                "deliverable_equivalence: could not read/parse %s: %s; "
                "falling back to empty equivalence map (every id canonicalizes to itself).",
                artifact_path,
                exc,
            )
            parsed = None

        if not isinstance(parsed, dict):
            equivalence_map = {}
        else:
            entries = parsed.get("entries")
            if not isinstance(entries, list):
                equivalence_map = {}
            else:
                equivalence_map = _build_equivalence_map(entries)

    _RESOLVED_EQUIVALENCE_MAP = equivalence_map
    _EQUIVALENCE_MAP_RESOLVED = True
    _RESOLVED_FOR_ROOT = worktree_root
    return equivalence_map


def canonicalize(raw_id: Optional[str], equivalence_map: Dict[str, str]) -> Optional[str]:
    """Return the declared winner id for a known loser, else ``raw_id`` unchanged.

    Purpose: the sole join-key transform every consumer calls at its existing raw
    `deliverable_id` read/compare point. None-safe (``None`` maps to ``None`` — a
    record with no `deliverable_id` at all stays that way; there is nothing to
    canonicalize). Pure function, no I/O — performs a single dict lookup only.

    Idempotent by construction: because a winner never also appears as a loser
    (C3b's no-transitive-chains authoring obligation), calling this again on its own
    result is a no-op — ``canonicalize(canonicalize(x, m), m) == canonicalize(x, m)``.
    """
    if raw_id is None:
        return None
    return equivalence_map.get(raw_id, raw_id)
