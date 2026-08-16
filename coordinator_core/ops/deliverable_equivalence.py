"""
coordinator_core.ops.deliverable_equivalence — shared deliverable-id equivalence read-model,
plus (sedge-06) the additive close-out ledger loader/validator over the same artifact.

Purpose: ONE join-key canonicalization mechanism shared by the fork-remediation
consumer set. Correcting the module's own long-stale claim (it used to name only 4
consumers): the research corpus for sedge-06
(`state/roadmap/sedge-2026-08-06/research-corpus/ledger-widening.md` §4) enumerates the
REAL count by grep + enclosing-function read as **8 production modules, 12–14 call
sites** — `deliverable_status.py`, `deliverable_rollup.py`, `migrate_handoff_vocabulary.py`,
`commit_anchors.py`, `draft_plan_aging.py`, `review_brightline_gate.py`,
`close_out_and_stamp.py`, and `workstream_complete/__init__.py` — plus two pass-through
callers (`coordinator_render_rollup.py`, `promote_shipped_in_flight_stubs.py`). Each of
those consumers compares raw `deliverable_id` strings for equality; when a deliverable
has been forked (minted twice for the same underlying work, per DD#1's declared-winner
tiebreak), that raw comparison silently treats the two legs as unrelated entities. This
module lets every reader treat a declared fork pair as one entity without touching what
any of those legs' own records carry on disk.

Reads the declared equivalence artifact at `state/deliverable-equivalence.yaml`
(relative to the worktree root), authored by chunk C3b and widened by the
retraction-as-observation pass (2026-08-14):

    entries:
      - loser: dlv-...
        winner: dlv-...
        evidence: "..."
        adjudicated_at: "2026-..."   # optional; ISO 8601
        evidence_commits: [...]      # optional structured evidence
        retracted_at: "2026-..."     # optional; ISO 8601, marks the row withdrawn

`load_equivalence_map` builds `{loser: winner}` from `entries`. A repeated `loser` is a
LEGITIMATE REVISION, not a violation — `_build_equivalence_map` resolves it to whichever
row carries the LATEST `adjudicated_at`, and a row carrying `retracted_at` removes its
mapping entirely rather than competing for it (see `_build_equivalence_map`'s own
docstring for the missing-stamp tie-break and the no-transitive-chains check, both of
which this loader still enforces). No `winner` ever appears elsewhere as a `loser` (no
transitive chains) — that invariant is still C3b's/every subsequent author's obligation,
not this module's to enforce by walking chains; this loader only guards against a
malformed artifact silently masquerading as a clean map (see `_build_equivalence_map`'s
duplicate-loser handling below).

Spec backlink: pln-deliverable-id-fork-remediatio-894e26 § C4 (AC6, AC6b, AC9, AC12)

Second responsibility — the close-out ledger (sedge-06)
---------------------------------------------------------
The SAME on-disk artifact (`state/deliverable-equivalence.yaml`) also carries a second,
unrelated top-level block, `ledger:`, keyed by `deliverable_id` rather than `loser` — a
close-out verdict transcription, not a fork-equivalence declaration. It shares only the
file with the fork-equivalence half above; the two are deliberately two row KINDS in one
file (`entries:` for forks, `ledger:` for close-out rows) rather than one row shape
carrying both key spaces, because a ledger keyed by `loser` could only ever describe the
19 forked deliverables — the overwhelming majority of deliverables were never forked, so
a one-wide-row shape structurally cannot hold the ledger's population. Two row kinds also
keeps the fork-adjudication invariants (unique `loser`; no `winner` also a `loser`)
undiluted by an unrelated key space.
`load_deliverable_ledger` is this responsibility's own loader, with its own memo
(`_reset_deliverable_ledger_cache`, distinct from `_reset_equivalence_map_cache` — see
its docstring for the memo-interaction hazard) and its own validator
(`validate_deliverable_ledger_rows` / `DeliverableLedgerValidationError`), documented at
each definition below. This second responsibility does NOT change `load_equivalence_map`
or `canonicalize` in any way — their signature, body, and `Dict[str, str]` return type
are byte-for-byte unchanged by this addition.

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
  - **The close-out ledger (`ledger:`) is never on the cockpit emission wire.** Nothing
    in this module, or in sedge-06's stub, puts a ledger column into
    `state/cockpit-emission.json` or a `contract/cockpit_schema` entity. That is a
    separate, later, unbundled move requiring external negotiation (research corpus §6) —
    not decided or done here.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_EQUIVALENCE_ARTIFACT_RELPATH = Path("state") / "deliverable-equivalence.yaml"

# ---------------------------------------------------------------------------
# Per-process memoization state for load_equivalence_map — mirrors
# deliverable_rollup.py's _central_initiatives_dir module-scope memoization
# convention (resolve once, reuse for the process lifetime), keyed on
# (worktree_root, artifact stat-key) — see `_artifact_cache_key` below.
# ---------------------------------------------------------------------------

#: (worktree_root, (st_mtime_ns, st_size) or None). Review: coordinatorreview-integrator
#: — sedge P2 fix, keyed on (mtime, size) rather than mtime alone (see
#: `_artifact_cache_key` docstring), matching the C4 `_marker_stat_key` precedent
#: in `_blanket_disarm.py` for the same same-tick-rewrite collision reason.
_EquivalenceCacheKey = Tuple[Path, Optional[Tuple[int, int]]]

_EQUIVALENCE_MAP_CACHE: Dict[_EquivalenceCacheKey, Dict[str, str]] = {}

#: Bounded-cache cap shared by every dict-shaped memo in this module (mirrors
#: `coordinator_core.cache._MAX_CACHE`'s "evict oldest half on overflow"
#: idiom — the existing bounded-memo shape in this codebase, reused rather
#: than inventing a second one). See `_evict_oldest_half` below.
_MAX_CACHE: int = 512


def _evict_oldest_half(cache: Dict[Any, Any]) -> None:
    """Evict the oldest half of `cache`'s entries (by insertion order) when it
    has reached `_MAX_CACHE` entries. Mirrors `coordinator_core.cache.
    read_revalidated`'s own bounded-eviction idiom verbatim (same cap, same
    "oldest half" shape) rather than introducing a distinct caching idiom for
    this module's several dict-shaped memos.

    Safe by construction for every cache this is applied to here: a cache
    miss always falls through to a fresh, correct read/compute (never a
    fail-open default) — eviction can only cost a redundant recompute, never
    change what a caller observes.
    """
    if len(cache) < _MAX_CACHE:
        return
    evict_keys = list(cache.keys())[: _MAX_CACHE // 2]
    for k in evict_keys:
        del cache[k]


def _artifact_cache_key(worktree_root: Path) -> _EquivalenceCacheKey:
    """``(worktree_root, (artifact st_mtime_ns, st_size))`` — the shared memo
    key for every memoized reader of ``state/deliverable-equivalence.yaml``
    in this module.

    Purpose (sedge P2 fix, C5): keying on root alone let a second call with a
    DIFFERENT worktree_root within the same process silently reuse the FIRST
    root's memoized data — a warn-then-serve-wrong-repo's-data defect, not a
    staleness one, since invalidation cannot fix a missing key dimension.
    Adding the artifact's own stat identity to the key (not just the root)
    means an in-process rewrite of the artifact for the SAME root also busts
    the memo, without requiring every caller to remember to call the
    `_reset_*` helpers. A missing artifact keys as ``None``, distinct from any
    real stat tuple.

    Review: coordinatorreview-integrator (failopen-caches P2) — keyed on
    ``(st_mtime_ns, st_size)`` rather than ``st_mtime`` alone. A bare float
    mtime collides for two rapid in-process rewrites landing within the
    filesystem's mtime resolution window (same defect class C4's
    `_marker_stat_key` in `_blanket_disarm.py` was re-keyed to avoid, in the
    same commit); adding `st_size` (and using the nanosecond-resolution
    field) closes the same collision here.
    """
    artifact_path = worktree_root / _EQUIVALENCE_ARTIFACT_RELPATH
    try:
        st = artifact_path.stat()
        stat_key: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
    except OSError:
        stat_key = None
    return (worktree_root, stat_key)


def _reset_equivalence_map_cache() -> None:
    """Test-only helper: clear the ``load_equivalence_map`` process-scope memo.

    Mirrors ``deliverable_rollup._reset_central_root_cache``. The per-process memo
    is correct under the spawn-per-call execution model, where the process exits
    after one op, but is NOT correct under pytest, where every test shares one
    interpreter — the first test to resolve a given (root, mtime) key pins that
    key's entry for the whole session unless this is called between tests.
    """
    global _EQUIVALENCE_MAP_CACHE
    _EQUIVALENCE_MAP_CACHE = {}


def _build_equivalence_map(entries: list) -> Dict[str, str]:
    """Build ``{loser: winner}`` from a parsed ``entries`` list.

    Purpose: isolates the entries -> map projection so ``load_equivalence_map`` stays
    focused on I/O and memoization. A malformed entry (missing loser/winner, non-string
    values) is skipped with a WARNING rather than raising — a single bad row in the
    declared artifact should not take down every consumer's read path (AC10).

    Retraction-as-observation resolution order (2026-08-14 widening — supersedes this
    function's earlier first-seen-wins behaviour): a repeated ``loser`` is no longer a
    uniqueness violation. It is resolved by LATEST ``adjudicated_at`` wins, and a row
    carrying a non-null ``retracted_at`` removes its mapping entirely (so
    ``canonicalize`` falls through to returning the raw id unchanged, exactly as for an
    id that was never mapped). The three sub-rules, in the order applied per ``loser``:

      1. **Retraction removes the mapping.** A row with a non-null ``retracted_at`` is
         never installed into the map for its ``loser`` on this pass; if a mapping was
         already installed for that ``loser`` from an earlier-processed row, the
         retraction removes it (it does not merely skip re-adding). Retraction is
         evaluated per row independently of ordering — a retracted row never wins a
         latest-``adjudicated_at`` comparison, whatever its own timestamp.
      2. **Latest ``adjudicated_at`` wins among live (non-retracted) rows.** When two
         live rows share a ``loser``, the one with the chronologically later
         ``adjudicated_at`` (ISO 8601 string compare, which is chronological for
         same-format strings) replaces the earlier one. This no longer logs a WARNING —
         a legitimate revision is not an artifact defect.
      3. **Missing-stamp tie-break (AC6) — not silent.** If a live row with no
         ``adjudicated_at`` competes for the same ``loser`` as another live row (whether
         or not that other row has a stamp), the pair is AMBIGUOUS: this function keeps
         the row already installed (first-processed-wins, mirroring the artifact's own
         entry order as the tie-break of last resort) but logs a WARNING naming the
         ambiguity, so a silently-arbitrary resolution is never mistaken for a resolved
         revision. This is the one case left that still warns on a duplicate ``loser``.
    """
    # loser -> row dict actually installed in the map on this pass (used to compare
    # adjudicated_at / retracted_at against a newly-seen row for the same loser).
    installed_rows: Dict[str, dict] = {}
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

        retracted_at = entry.get("retracted_at")
        is_retracted = isinstance(retracted_at, str) and retracted_at.strip() != ""
        adjudicated_at = entry.get("adjudicated_at")
        has_stamp = isinstance(adjudicated_at, str) and adjudicated_at.strip() != ""

        if is_retracted:
            # A retracted row never wins the map, and it withdraws a mapping already
            # installed for this loser by an earlier row.
            if loser in equivalence_map:
                del equivalence_map[loser]
                del installed_rows[loser]
            continue

        prior = installed_rows.get(loser)
        if prior is None:
            equivalence_map[loser] = winner
            installed_rows[loser] = entry
            continue

        prior_retracted_at = prior.get("retracted_at")
        prior_adjudicated_at = prior.get("adjudicated_at")
        prior_has_stamp = (
            isinstance(prior_adjudicated_at, str) and prior_adjudicated_at.strip() != ""
        )

        if not has_stamp or not prior_has_stamp:
            logger.warning(
                "deliverable_equivalence: ambiguous duplicate loser id %r in "
                "state/deliverable-equivalence.yaml — one or both competing rows lack "
                "'adjudicated_at' (existing: %r, incoming: %r); keeping the "
                "already-installed mapping to %r, ignoring %r. Add 'adjudicated_at' to "
                "both rows to resolve this deterministically.",
                loser,
                prior_adjudicated_at,
                adjudicated_at,
                equivalence_map[loser],
                winner,
            )
            continue

        if adjudicated_at > prior_adjudicated_at:
            equivalence_map[loser] = winner
            installed_rows[loser] = entry
        # else: prior row is already later-or-equal; keep it, no warning — this is the
        # ordinary "artifact lists rows out of chronological order" case, not ambiguity.

    # Review: coordinatorcode-reviewer-67ffaa7e Finding 2 — the no-transitive-chains
    # invariant (a winner must never also appear as a loser) previously had zero
    # enforcement signal, unlike the duplicate-loser case above which warns. This does
    # not walk/resolve chains (still every author's obligation, not the loader's to
    # enforce by walking) — it only makes the violation visible. Unaffected by the
    # retraction-as-observation widening above (AC8): still fires on the resolved map,
    # still does not walk chains.
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

    Memoized on ``(worktree_root, artifact stat-key)`` (see ``_artifact_cache_key``) —
    NOT on ``worktree_root`` alone. A second call with a different root, or the same
    root after the artifact was rewritten in-process, resolves and caches its OWN
    entry rather than serving a different root's/version's stale data. A test
    iterating multiple worktrees or rewriting the artifact in place may still call
    ``_reset_equivalence_map_cache()`` to force a clean slate, but correctness no
    longer depends on remembering to.
    """
    global _EQUIVALENCE_MAP_CACHE

    cache_key = _artifact_cache_key(worktree_root)
    if cache_key in _EQUIVALENCE_MAP_CACHE:
        return _EQUIVALENCE_MAP_CACHE[cache_key]

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

    _evict_oldest_half(_EQUIVALENCE_MAP_CACHE)
    _EQUIVALENCE_MAP_CACHE[cache_key] = equivalence_map
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


# ---------------------------------------------------------------------------
# Retraction-as-observation widening (2026-08-14) — negative-spec, load-bearing:
#   - Do NOT make canonicalize() walk chains; it stays single-level (AC8, and the
#     module's own negative-spec above).
#   - Do NOT canonicalize on any write path; this widening touches map-BUILD
#     resolution order only, never a write.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Close-out ledger (sedge-06) — a SECOND, additive read-model over the same
# artifact, keyed by `deliverable_id` rather than `loser`. Own memo, own
# validator, own exception type. Does not touch load_equivalence_map/
# canonicalize or their module globals above.
# ---------------------------------------------------------------------------

#: Closed enum for a ledger row's `status` column.
LEDGER_STATUS_VALUES = frozenset({"open", "shipped", "superseded", "abandoned"})

#: Closed enum for `closure_evidence.join_provenance`, mirroring
#: `close_out_and_stamp.DeliverableJoinStats`'s four-valued `join_provenance`
#: (`JOIN_PROVENANCE_JOINED`, `JOIN_PROVENANCE_NO_JOIN_KEY`,
#: `JOIN_PROVENANCE_NO_JOIN_CANDIDATES`, `JOIN_PROVENANCE_KEY_MISMATCH` — read directly
#: from coordinator_core/execute_plan_assemble/close_out_and_stamp.py before shipping
#: this enum, not guessed at).
LEDGER_JOIN_PROVENANCE_VALUES = frozenset(
    {"joined", "no_join_key", "no_join_candidates", "key_mismatch"}
)


class DeliverableLedgerValidationError(ValueError):
    """Raised by `validate_deliverable_ledger_rows` on any malformed ledger row.

    Purpose: the close-out ledger inverts the loader's own WARN-and-skip discipline.
    WARN-and-skip is tolerable for `loser`/`winner` (an absent/dropped fork-equivalence
    entry degrades to today's raw-id comparison — a safe, well-understood fallback). It
    is the WORSE failure mode for an authoritative close-out column: a silently-dropped
    ledger row reads as "no such verdict" rather than "a verdict exists but this file's
    encoding of it is broken" (research corpus §7's caveat). So a present-but-malformed
    ledger row must fail loud, not degrade quietly — this exception is that loud failure.
    """


_DELIVERABLE_LEDGER_CACHE: Dict[_EquivalenceCacheKey, List[Dict[str, Any]]] = {}

# Review: coordinatorcode-reviewer s1-ledger-seam Finding 2 — _ledger_artifact_readable
# used to re-read+re-parse the whole YAML artifact on every call, unmemoized, inside
# dual_read_deliverable_ids_for_corpus's per-record loop (N reads for N artifacts) while
# load_deliverable_ledger right beside it was already memoized. Memoized here, reset
# alongside the ledger memo below so tests don't see cross-test staleness.
_LEDGER_ARTIFACT_READABLE_CACHE: Dict[_EquivalenceCacheKey, bool] = {}

# Review: coordinatorcode-reviewer s1-ledger-seam Finding 1 — dual_read_deliverable_id
# never called validate_deliverable_ledger_rows, so a malformed row (non-string
# deliverable_id/evidence_source) was silently skipped by _ledger_evidence_index's own
# `continue` and surfaced as "genuine_miss" rather than the loud failure
# DeliverableLedgerValidationError exists for. Validated once per (root, mtime)
# ledger-load (not once per artifact) via this set, reset alongside the ledger memo.
# P2 fix (C5): previously a single bool, so only the FIRST root's ledger was ever
# validated — a second root's malformed rows silently skipped validation entirely.
_DELIVERABLE_LEDGER_VALIDATED: set = set()


def _reset_deliverable_ledger_cache() -> None:
    """Test-only helper: clear the ``load_deliverable_ledger`` process-scope memo.

    Deliberately separate from `_reset_equivalence_map_cache` and its globals —
    the two loaders read the same file but are two independent read-models with
    independent memoization state, per sedge-06's D2 (same module, but the ledger
    is its own responsibility, not a mutation of the fork-equivalence one).

    Also clears `_ledger_artifact_readable`'s own memo and the dual-read seam's
    once-per-(root, mtime) validation set (both additive to this same close-out-
    ledger responsibility) — see their definitions for why each exists.
    """
    global _DELIVERABLE_LEDGER_CACHE, _LEDGER_ARTIFACT_READABLE_CACHE, _DELIVERABLE_LEDGER_VALIDATED
    _DELIVERABLE_LEDGER_CACHE = {}
    _LEDGER_ARTIFACT_READABLE_CACHE = {}
    _DELIVERABLE_LEDGER_VALIDATED = set()


def load_deliverable_ledger(worktree_root: Path) -> List[Dict[str, Any]]:
    """Load the close-out ledger's full rows, memoized per-process.

    Purpose: reads the SAME artifact `load_equivalence_map` reads
    (`<worktree_root>/state/deliverable-equivalence.yaml`) but projects its `ledger:`
    top-level list instead of `entries:`, returning full rows (not a `{loser: winner}`
    projection — there is no existing seam a ledger column could arrive at readers
    through for free; see the module docstring's "Second responsibility" section).

    Degradation, same non-raising discipline as `load_equivalence_map`'s ABSENCE path
    (a missing artifact, missing `ledger:` key, or unparsable YAML is not this
    function's business to raise on — that is `validate_deliverable_ledger_rows`'s job,
    for the MALFORMED-but-present case only): missing artifact -> `[]`; read/parse
    exception -> logged WARNING -> `[]`; parsed-but-not-a-dict, or `ledger` missing/not
    a list -> `[]` silently. This function does NOT validate row shape — callers that
    need loud-failure-on-malformed-row semantics call
    `validate_deliverable_ledger_rows` themselves on the returned rows.

    Memoization (research corpus Constraint 7 / Open-question 3 — read before calling
    this from a session that also WRITES the ledger): memoized on
    ``(worktree_root, artifact stat-key)``, same key shape as `load_equivalence_map`'s
    (see `_artifact_cache_key`) — NOT root-insensitive, so a rewrite of the artifact
    changes its mtime and busts this cache entry rather than serving the pre-write
    rows. A caller that writes the ledger via a path that does not bump mtime (rare;
    most OSes update mtime on any write) may still call
    `_reset_deliverable_ledger_cache()` to force a re-read. This is a distinct cache
    from `load_equivalence_map`'s — writing the ledger does not invalidate the
    fork-equivalence memo and vice versa, since the two loaders share a file but not
    a cache.
    """
    global _DELIVERABLE_LEDGER_CACHE

    cache_key = _artifact_cache_key(worktree_root)
    if cache_key in _DELIVERABLE_LEDGER_CACHE:
        return _DELIVERABLE_LEDGER_CACHE[cache_key]

    artifact_path = worktree_root / _EQUIVALENCE_ARTIFACT_RELPATH

    if not artifact_path.is_file():
        ledger_rows: List[Dict[str, Any]] = []
    else:
        try:
            content = artifact_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(content)
        except Exception as exc:  # noqa: BLE001 — a bad artifact degrades, not raises
            logger.warning(
                "deliverable_equivalence: could not read/parse %s for the close-out "
                "ledger: %s; falling back to an empty ledger.",
                artifact_path,
                exc,
            )
            parsed = None

        if not isinstance(parsed, dict):
            ledger_rows = []
        else:
            ledger = parsed.get("ledger")
            if not isinstance(ledger, list):
                ledger_rows = []
            else:
                ledger_rows = [row for row in ledger]

    _evict_oldest_half(_DELIVERABLE_LEDGER_CACHE)
    _DELIVERABLE_LEDGER_CACHE[cache_key] = ledger_rows
    return ledger_rows


def validate_deliverable_ledger_rows(rows: List[Dict[str, Any]]) -> None:
    """Raise `DeliverableLedgerValidationError` on the first malformed ledger row.

    Purpose: the MALFORMED-row counterpart to `load_deliverable_ledger`'s ABSENT-data
    degradation. Where the fork-equivalence loader's `_build_equivalence_map` WARNs and
    skips a bad row (safe, because absence there degrades to raw-id comparison), a
    close-out verdict has no such safe fallback — a present-but-broken row must not be
    silently dropped, or its verdict quietly disappears while looking like "no verdict
    was ever asserted" (research corpus §7 / Constraint 6). So this function raises
    loudly rather than filtering.

    Validates, per row:
      - required keys present: `deliverable_id`, `status`, `adjudicator`,
        `evidence_source`.
      - `deliverable_id` is a non-blank string, unique across the whole `rows` list.
      - `status` is one of `LEDGER_STATUS_VALUES`.
      - `closed_at` is present (non-null) iff `status != "open"`.
      - `superseded_by` is present (non-null) iff `status == "superseded"`. This is
        supersession, NOT fork-equivalence — never treat it as an identity join the way
        `winner` is; a wrong `superseded_by` value must never collapse two distinct
        deliverables the way a wrong `winner` entry could (research corpus Constraint 3).
      - `closure_evidence`, if present and non-null, is a mapping whose
        `join_provenance` (if present) is one of `LEDGER_JOIN_PROVENANCE_VALUES`, and
        whose `realizing_commits` (if present) is a list of strings.

    Raises `DeliverableLedgerValidationError` naming the offending row's
    `deliverable_id` (or its index, when the key itself is what's missing/invalid) and
    the specific rule violated. Never warns-and-skips.
    """
    seen_ids: Dict[str, int] = {}
    for index, row in enumerate(rows):
        row_ref = f"index {index}"
        if not isinstance(row, dict):
            raise DeliverableLedgerValidationError(
                f"deliverable ledger row at {row_ref} is not a mapping: {row!r}"
            )

        deliverable_id = row.get("deliverable_id")
        if not isinstance(deliverable_id, str) or not deliverable_id.strip():
            raise DeliverableLedgerValidationError(
                f"deliverable ledger row at {row_ref} has a missing/invalid "
                f"'deliverable_id': {row!r}"
            )
        row_ref = f"deliverable_id {deliverable_id!r}"

        if deliverable_id in seen_ids:
            raise DeliverableLedgerValidationError(
                f"deliverable ledger row {row_ref} duplicates the deliverable_id first "
                f"seen at index {seen_ids[deliverable_id]} — deliverable_id must be "
                f"unique across the ledger block"
            )
        seen_ids[deliverable_id] = index

        status = row.get("status")
        if status not in LEDGER_STATUS_VALUES:
            raise DeliverableLedgerValidationError(
                f"deliverable ledger row {row_ref} has invalid 'status' {status!r}; "
                f"must be one of {sorted(LEDGER_STATUS_VALUES)}"
            )

        adjudicator = row.get("adjudicator")
        if not isinstance(adjudicator, str) or not adjudicator.strip():
            raise DeliverableLedgerValidationError(
                f"deliverable ledger row {row_ref} has a missing/invalid 'adjudicator' "
                f"— every close-out verdict must name who/what asserted it"
            )

        evidence_source = row.get("evidence_source")
        if not isinstance(evidence_source, str) or not evidence_source.strip():
            raise DeliverableLedgerValidationError(
                f"deliverable ledger row {row_ref} has a missing/invalid "
                f"'evidence_source' — every close-out verdict must name where the "
                f"assertion came from"
            )

        closed_at = row.get("closed_at")
        if status == "open":
            if closed_at is not None:
                raise DeliverableLedgerValidationError(
                    f"deliverable ledger row {row_ref} has status 'open' but a "
                    f"non-null 'closed_at' ({closed_at!r}); 'closed_at' must be "
                    f"absent/null when status is 'open'"
                )
        else:
            if not isinstance(closed_at, str) or not closed_at.strip():
                raise DeliverableLedgerValidationError(
                    f"deliverable ledger row {row_ref} has status {status!r} but a "
                    f"missing/invalid 'closed_at' — required whenever status != 'open'"
                )

        superseded_by = row.get("superseded_by")
        if status == "superseded":
            if not isinstance(superseded_by, str) or not superseded_by.strip():
                raise DeliverableLedgerValidationError(
                    f"deliverable ledger row {row_ref} has status 'superseded' but a "
                    f"missing/invalid 'superseded_by' — required when status is "
                    f"'superseded'"
                )
        else:
            if superseded_by is not None:
                raise DeliverableLedgerValidationError(
                    f"deliverable ledger row {row_ref} has status {status!r} but a "
                    f"non-null 'superseded_by' ({superseded_by!r}); 'superseded_by' is "
                    f"only meaningful when status is 'superseded'"
                )

        closure_evidence = row.get("closure_evidence")
        if closure_evidence is not None:
            if not isinstance(closure_evidence, dict):
                raise DeliverableLedgerValidationError(
                    f"deliverable ledger row {row_ref} has a 'closure_evidence' that "
                    f"is not a mapping: {closure_evidence!r}"
                )
            join_provenance = closure_evidence.get("join_provenance")
            if (
                join_provenance is not None
                and join_provenance not in LEDGER_JOIN_PROVENANCE_VALUES
            ):
                raise DeliverableLedgerValidationError(
                    f"deliverable ledger row {row_ref} has invalid "
                    f"'closure_evidence.join_provenance' {join_provenance!r}; must be "
                    f"one of {sorted(LEDGER_JOIN_PROVENANCE_VALUES)}"
                )
            realizing_commits = closure_evidence.get("realizing_commits")
            if realizing_commits is not None:
                if not isinstance(realizing_commits, list) or not all(
                    isinstance(sha, str) for sha in realizing_commits
                ):
                    raise DeliverableLedgerValidationError(
                        f"deliverable ledger row {row_ref} has a "
                        f"'closure_evidence.realizing_commits' that is not a list of "
                        f"strings: {realizing_commits!r}"
                    )


# ---------------------------------------------------------------------------
# sedge-07 — Step (1): read-only ledger seed, and Step (2): ledger-first /
# frontmatter-fallback dual read with hard-error mismatch.
#
# Neither of these touches load_equivalence_map/canonicalize/load_deliverable_
# ledger/validate_deliverable_ledger_rows above — they consume those as a pure
# overlay, per this module's own "Second responsibility" docstring section.
#
# Parser choice (named per the sedge-07 dispatch brief): both the seed and the
# dual reader read frontmatter via `backfill_deliverable_spine.extract_fm_field`
# (rest-of-line, first-fence-block only), NEVER
# `coordinator_core.ops._fm_util.extract_frontmatter_scalar` (first-token-only).
# The two are NOT interchangeable per each module's own docstring; using both
# across the seeder and the reader would reintroduce a value-divergence class
# at the parser layer, exactly the defect class this stub exists to close. One
# parser, used everywhere in this section.
# ---------------------------------------------------------------------------


# Review: coordinatorcode-reviewer c2f6a1ea — F1: YAML 1.1's null-literal set
# includes `Null`/`NULL` alongside lowercase `null`, not just `null`/`~`/empty.
# A hand-authored `deliverable_id: Null  # ...` line would otherwise normalize
# to the literal string "Null" and become a ledger primary key — the exact
# failure class this helper exists to close.
_YAML_NULL_LITERALS = frozenset({"null", "Null", "NULL", "~", ""})


def _normalize_extracted_deliverable_id(raw: Optional[str]) -> Optional[str]:
    """Normalize `backfill_deliverable_spine.extract_fm_field`'s rest-of-line output
    for a `deliverable_id` field into a clean id or `None`.

    Purpose: `extract_fm_field` is a rest-of-line parser (deliberately, per the
    parser-choice note above this section) — it has no notion of a trailing inline
    YAML comment or of quoting, so a line like
    ``deliverable_id: "dlv-x"  # some comment`` yields the comment and quotes as
    part of the "id" verbatim. Every read site in this section that consumes
    `extract_fm_field`'s output for `deliverable_id` MUST route through this one
    helper — applying it to only one side (seed vs. dual-read) would recreate the
    exact value-divergence class this module's parser-choice note exists to close.

    Steps, in order: strip a trailing ` #...` inline comment (a `#` inside a
    balanced pair of quotes is NOT treated as a comment start); strip one layer of
    surrounding matching single/double quotes; strip surrounding whitespace; treat
    a YAML null literal (`null`, `Null`, `NULL`, `~`, or empty) as absent,
    returning `None` so the caller's existing `if not raw_id: continue`-shaped
    skip-guard fires exactly as it does for a truly missing field. A well-formed
    bare id passes through unchanged.

    Spec backlink: pln-archive-side-corpus-remediatio-3ff30d § C2
    """
    if raw is None:
        return None

    value = raw.strip()

    if value and value[0] in ("'", '"'):
        quote = value[0]
        close = value.find(quote, 1)
        if close != -1:
            value = value[1:close]
        else:
            # Review: coordinatorcode-reviewer f292d223 — F7: an unterminated
            # quote (`"dlv-x` with no closing quote) previously fell straight
            # through to `value[1:]` with no comment stripping, leaving a
            # trailing inline comment un-stripped. Deliberate now: treat the
            # remainder past the opening quote the same way the unquoted branch
            # does, stripping a ` #...` inline comment if present.
            value = value[1:]
            comment_match = re.search(r"\s#", value)
            if comment_match is not None:
                value = value[: comment_match.start()]
    else:
        # Unquoted value: an inline YAML comment is only ever introduced by
        # whitespace immediately followed by '#' (YAML convention) — a bare '#'
        # with no preceding whitespace is part of the value itself, so this
        # never corrupts an unquoted value that legitimately contains '#'.
        comment_match = re.search(r"\s#", value)
        if comment_match is not None:
            value = value[: comment_match.start()]

    value = value.strip()

    if value in _YAML_NULL_LITERALS:
        return None

    return value


def _is_immutable_path_local(path: str) -> bool:
    """Local reimplementation of `backfill_deliverable_spine.is_immutable_path`'s
    archive predicate — deliberately NOT imported from that module.

    Belt-and-braces only: nothing in this section ever opens a file in write
    mode, so no artifact is ever at risk of a write regardless of this
    predicate's answer. It exists because the research corpus (§4) measured
    that `_stamp_file` has no immutability guard of its own — the entire
    protection over there is a caller-side branch in `backfill_deliverable_
    spine.main`, and the subagent archive-write guard does not intercept an
    in-process `open()`/`os.replace()` call from an op. A function that reused
    `is_immutable_path` by import would inherit a predicate with no
    enforcement teeth of its own; reimplementing it here keeps the "zero
    writes to archive/" property visibly local to the one section of this
    module that walks the archived corpus, rather than resting on an imported
    helper's good behaviour elsewhere.
    """
    norm = path.replace(os.sep, "/")
    return "/archive/handoffs/" in norm or "/archive/completed/" in norm


def seed_deliverable_ledger_rows(worktree_root: Path) -> List[Dict[str, Any]]:
    """Step (1) — read-only seed of close-out ledger rows from the artifact corpus.

    Walks the board-relevant corpus via `backfill_deliverable_spine.
    enumerate_corpus`/`classify_artifact` (reusable read-side helpers per the
    research corpus §1), reads each artifact's frontmatter `deliverable_id` via
    `extract_fm_field` (see the parser-choice note above this function), and
    routes every non-empty raw id through `deliverable_equivalence.canonicalize`
    against the FULL 19-entry equivalence map (`load_equivalence_map`) so a
    declared fork loser seeds under its winner's id, never its own.

    Hard prohibitions (each with a named reason in the research corpus §1):
      - Does NOT import or call `backfill_deliverable_spine.group_corpus` — it
        buckets every already-`deliverable_id`-bearing artifact into
        `already_threaded` and EXCLUDES it, which is precisely where all 19
        fork legs live, and it calls `canonicalize()` nowhere in that module.
      - Does NOT call `_find_group_id` — first-wins-in-list-order with no
        divergence check, an independent fork-recreation vector.
      - Does NOT call `_stamp_file` or mint anything (`mint_deliverable_id.
        mint()` is never imported here) — a seeder that mints is a seeder
        that forks (D1 carry-not-remint).

    Winners are NOT 1:1 with losers (research corpus §0): distinct canonical
    ids seeded here are fewer than 19 by construction (`dlv-pickup-skill-
    code-driven-branch-result-acd867` alone absorbs three losers). This is
    expected deduplication, not a merge defect — a single ledger row is
    emitted per DISTINCT canonical id, with every artifact path that
    canonicalized to it folded into that row's `evidence_source`.

    The two null-loser forks (`dlv-qsub-02`/`dlv-qsub-03`) are NOT seeded by
    this function and cannot be: `canonicalize()` only redirects a WRONG-BUT-
    PRESENT raw id, and both of their losing artifacts carry `deliverable_id:
    null` — there is no wrong id here to route through the equivalence map,
    only a missing one. See this stub's completion notes (Q3) for the full
    disposition; they remain covered only by
    `state/audits/2026-08-01-null-deliverable-id-plans.md`.

    Every emitted row carries `status: "open"` (a seed transcribes existing
    artifact-borne identity; it asserts no close-out verdict),
    `adjudicator` naming this function as the seeding mechanism, and
    `evidence_source` naming the artifact path(s) (`;`-joined, repo-relative,
    forward-slash-normalized, sorted) the canonical id was observed on. Every
    row also carries `closed_at: None` and `superseded_by: None` so the
    returned rows pass `validate_deliverable_ledger_rows` unmodified — an
    "open" row is REQUIRED to carry a null `closed_at`/`superseded_by` by that
    validator's own rules.

    Zero writes: this function never opens a file in any mode but read
    (`"r"`), never calls `os.replace`, and never imports `_stamp_file`. Every
    archived artifact (`_is_immutable_path_local`) is still READ for its
    `deliverable_id` (research corpus §4 confirms archived deliverables can
    be seeded read-only with no write-back — the value is already on disk)
    but the archive-ness of a path plays no role in whether this function
    writes to it, because this function never writes to ANY path.
    """
    # Deferred import: `backfill_deliverable_spine` is a large module with its
    # own CLI surface; importing at call time (not module scope) avoids a
    # module-load-order coupling between the two ops modules for the common
    # case where a caller only wants the fork-equivalence half of this file.
    from coordinator_core.ops.backfill_deliverable_spine import (  # noqa: PLC0415
        classify_artifact,
        enumerate_corpus,
        extract_fm_field,
    )

    equivalence_map = load_equivalence_map(worktree_root)
    corpus = enumerate_corpus(str(worktree_root))

    winners: Dict[str, List[str]] = {}
    for path in corpus:
        # classify_artifact / _is_immutable_path_local are consulted for
        # completeness (every corpus entry is a real, classified artifact)
        # but neither gates whether this function reads or seeds it — see
        # this function's own "Zero writes" docstring section. Both return
        # values are deliberately discarded (Review: coordinatorcode-reviewer
        # s1-ledger-seam Finding 3 — no gating on either call, by design).
        _ = classify_artifact(path)
        _ = _is_immutable_path_local(path)

        raw_id = _normalize_extracted_deliverable_id(extract_fm_field(path, "deliverable_id"))
        if not raw_id:
            continue

        canonical_id = canonicalize(raw_id, equivalence_map)
        if not canonical_id:
            continue

        rel_path = os.path.relpath(path, str(worktree_root)).replace(os.sep, "/")
        winners.setdefault(canonical_id, []).append(rel_path)

    rows: List[Dict[str, Any]] = []
    for canonical_id in sorted(winners):
        evidence_paths = sorted(set(winners[canonical_id]))
        rows.append(
            {
                "deliverable_id": canonical_id,
                "status": "open",
                "closed_at": None,
                "superseded_by": None,
                "adjudicator": (
                    "coordinator_core.ops.deliverable_equivalence."
                    "seed_deliverable_ledger_rows (sedge-07)"
                ),
                "evidence_source": "; ".join(evidence_paths),
            }
        )
    return rows


class DeliverableDualReadMismatchError(RuntimeError):
    """Raised by `dual_read_deliverable_id` when the ledger and the artifact's own
    frontmatter give two genuinely different (canonicalized) answers for the same
    artifact's `deliverable_id`.

    Departure from the only dual-read precedent in this repo
    (`ownership_index.build_ownership_index`), argued rather than cited: that
    module deliberately chose `_LOG.warning` over a raise, and its own
    rationale is sound FOR ITS OWN SHAPE — the claim-store decision is final
    either way, so its frontmatter `claimed_by`/`consumed_by` mirror is
    informationally redundant, and a disagreement there is staleness, not
    ambiguity, because there is only ONE live writer (the claim store) plus a
    demoted mirror. Mid-transition `deliverable_id` is a structurally
    different shape: it has TWO genuinely live writers — the artifact
    frontmatter (written by every existing carry/mint call site) and the new
    ledger (seeded here, and appended to by later stubs) — not one store plus
    a redundant mirror. A warn-and-continue posture over a second live writer
    is the EXACT mechanism that produced the 19-row fork population this
    roadmap exists to close (two independently-authored producers of the same
    join key, silently diverging — see `DivergentDeliverableIdError`'s own
    docstring for the sibling failure mode on the plan/predecessor axis).
    Warning here, rather than raising, would reproduce that defect, not
    merely fail to catch a rare edge case. That is why `dual_read_deliverable_
    id` departs from `build_ownership_index`'s mismatch-response precedent
    while still following its store-first / frontmatter-fallback / three-way-
    outcome STRUCTURE (see that function's own docstring for what IS carried
    forward).
    """


_LEDGER_OUTCOME_HIT = "hit"
_LEDGER_OUTCOME_GENUINE_MISS = "genuine_miss"
_LEDGER_OUTCOME_UNREADABLE = "ledger_unreadable"

#: Public re-export of the three-way outcome vocabulary `dual_read_deliverable_id`
#: and `dual_read_deliverable_ids_for_corpus` return — hit / genuine-miss /
#: ledger-unreadable are REQUIRED to stay distinguishable by the caller (research
#: corpus §6): collapsing "unreadable" into "miss" makes the migration invisibly
#: revert to frontmatter-only reads with no signal that it has done so.
DUAL_READ_OUTCOME_VALUES = frozenset(
    {_LEDGER_OUTCOME_HIT, _LEDGER_OUTCOME_GENUINE_MISS, _LEDGER_OUTCOME_UNREADABLE}
)


def _ledger_artifact_readable(worktree_root: Path) -> bool:
    """True unless `state/deliverable-equivalence.yaml` is PRESENT but could not be
    read/parsed as a YAML mapping.

    A MISSING artifact is the normal steady state before the ledger exists at
    all (mirrors `load_equivalence_map`'s own missing-artifact-is-not-an-error
    contract) — this returns True for that case, not "unreadable". Only a
    present-but-broken file (I/O error, unparsable YAML, or a parsed document
    that is not a mapping) returns False. This is a DELIBERATELY separate
    read from `load_deliverable_ledger`'s own memoized load: that function's
    ABSENT/malformed degradation collapses every failure mode to `[]`, with no
    signal left over for a caller that needs to tell "empty ledger" apart from
    "broken ledger file" (research corpus §6's `errors` arm requirement).

    Memoized on ``(worktree_root, artifact stat-key)`` — mirrors
    `load_deliverable_ledger`'s own memo shape (Review: coordinatorcode-reviewer
    s1-ledger-seam Finding 2 — this used to re-read+re-parse the artifact on
    every call inside `dual_read_deliverable_ids_for_corpus`'s per-record
    loop). Cleared by `_reset_deliverable_ledger_cache`, same as that memo.
    """
    global _LEDGER_ARTIFACT_READABLE_CACHE

    cache_key = _artifact_cache_key(worktree_root)
    if cache_key in _LEDGER_ARTIFACT_READABLE_CACHE:
        return _LEDGER_ARTIFACT_READABLE_CACHE[cache_key]

    artifact_path = worktree_root / _EQUIVALENCE_ARTIFACT_RELPATH
    if not artifact_path.is_file():
        result = True
    else:
        try:
            content = artifact_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(content)
        except Exception as exc:  # noqa: BLE001 — any read/parse failure means "unreadable"
            # Review: coordinatorcode-reviewer s1-ledger-seam Finding 4 — this used to
            # swallow the exception with no logging, unlike load_deliverable_ledger's
            # identical failure mode which logs a WARNING. This False becomes the
            # caller-visible "ledger_unreadable" outcome, so log at the same level
            # with the same exception detail to keep that outcome diagnosable.
            logger.warning(
                "deliverable_equivalence: could not read/parse %s for the ledger "
                "readability check: %s; treating the ledger as unreadable.",
                artifact_path,
                exc,
            )
            result = False
        else:
            result = isinstance(parsed, dict)

    _evict_oldest_half(_LEDGER_ARTIFACT_READABLE_CACHE)
    _LEDGER_ARTIFACT_READABLE_CACHE[cache_key] = result
    return result


def _ledger_evidence_index(ledger_rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """repo-relative artifact path -> `deliverable_id`, built from every ledger
    row's `;`-joined `evidence_source` field (the shape `seed_deliverable_ledger_
    rows` emits). A row with a non-string `deliverable_id`/`evidence_source` is
    skipped — this index is a read convenience only, not a second validator;
    `validate_deliverable_ledger_rows` is the loud-failure path for a malformed
    row, called separately by whichever caller needs that guarantee.

    Accepted, documented edge case (Review: coordinatorcode-reviewer
    s1-ledger-seam Finding 6): if two rows' `evidence_source` name the same
    artifact path, the LATER row in `ledger_rows` wins — silently, with a
    logged WARNING. Not reachable from `seed_deliverable_ledger_rows` today
    (it groups by canonical id before emitting, so no overlap within one seed
    call), but a later stub that appends rows without dedup could hit it.
    """
    index: Dict[str, str] = {}
    for row in ledger_rows:
        deliverable_id = row.get("deliverable_id")
        evidence_source = row.get("evidence_source")
        if not isinstance(deliverable_id, str) or not isinstance(evidence_source, str):
            continue
        for raw_path in evidence_source.split(";"):
            path = raw_path.strip()
            if not path:
                continue
            if path in index and index[path] != deliverable_id:
                logger.warning(
                    "deliverable_equivalence: duplicate evidence_source path %r "
                    "across ledger rows — overwriting %r with %r (last row wins).",
                    path,
                    index[path],
                    deliverable_id,
                )
            index[path] = deliverable_id
    return index


def dual_read_deliverable_id(
    worktree_root: Path,
    artifact_path: str,
    equivalence_map: Dict[str, str],
    read_frontmatter_field=None,
) -> Tuple[Optional[str], str]:
    """Step (2), per-record — ledger-first, frontmatter-fallback read of ONE
    artifact's canonical `deliverable_id`. HARD-ERRORS (raises, never warns) on
    a ledger/frontmatter disagreement — see `DeliverableDualReadMismatchError`'s
    own docstring for the full departure argument from `ownership_index.
    build_ownership_index`'s WARN precedent; do not cite that function as
    supporting this raise, it does the opposite, and it is cited here only for
    the store-first / fallback / three-way-outcome STRUCTURE this function
    follows, not the mismatch response.

    Structured like `ownership_index.build_ownership_index`'s own data flow
    (store call -> join to the walked corpus -> index): the ledger IS the
    store here (via `load_deliverable_ledger` + `_ledger_evidence_index`,
    joined on `artifact_path`'s repo-relative form), frontmatter is the
    fallback/mirror leg, read via `extract_fm_field` (see this section's
    parser-choice note) and canonicalized via `equivalence_map` before any
    comparison — never compared raw, mirroring `resolve_deliverable_and_
    initiative`'s own canonicalize-both-sides-before-comparing discipline.

    Returns `(deliverable_id, outcome)`, `outcome` one of
    `DUAL_READ_OUTCOME_VALUES`:
      - `"hit"` — the ledger names a `deliverable_id` for this artifact path
        (frontmatter agrees, or is silent). The ledger's id is returned.
      - `"genuine_miss"` — the ledger is READABLE but has no row whose
        `evidence_source` names this artifact path. Falls back to the
        artifact's own (canonicalized) frontmatter `deliverable_id`, which may
        itself be `None`.
      - `"ledger_unreadable"` — `state/deliverable-equivalence.yaml` is
        PRESENT but could not be read/parsed. This is NEVER silently
        collapsed into `"genuine_miss"` (research corpus §6) — a caller that
        needs to know whether it is reading a real migrated steady state or a
        broken store must see this distinctly. Also falls back to frontmatter
        (there is nothing else to fall back to), but the outcome tag makes
        that fallback visible rather than indistinguishable from an ordinary
        miss.

    Raises `DeliverableDualReadMismatchError` when the ledger DOES name a
    `deliverable_id` for this path AND the artifact's own canonicalized
    frontmatter value is present and DIFFERENT — the hard-error case this
    stub (AC5) mandates. A ledger hit with an ABSENT frontmatter value (the
    artifact never carried `deliverable_id` in the first place, or it was
    removed) is not a mismatch — there is nothing on the frontmatter side to
    disagree with the ledger's answer, so this returns `"hit"` cleanly.
    """
    if read_frontmatter_field is None:
        from coordinator_core.ops.backfill_deliverable_spine import (  # noqa: PLC0415
            extract_fm_field as read_frontmatter_field,
        )

    # Review: coordinatorcode-reviewer c2f6a1ea — F2: the `or None` is redundant;
    # _normalize_extracted_deliverable_id already maps "" to None via
    # _YAML_NULL_LITERALS and accepts None directly.
    raw_fm_id = _normalize_extracted_deliverable_id(
        read_frontmatter_field(artifact_path, "deliverable_id")
    )
    fm_canonical = canonicalize(raw_fm_id, equivalence_map) if raw_fm_id else None

    if not _ledger_artifact_readable(worktree_root):
        return fm_canonical, _LEDGER_OUTCOME_UNREADABLE

    ledger_rows = load_deliverable_ledger(worktree_root)

    # Review: coordinatorcode-reviewer s1-ledger-seam Finding 1 — this seam used to
    # consume ledger_rows straight through _ledger_evidence_index, which silently
    # `continue`s past any row with a non-string deliverable_id/evidence_source. A
    # malformed-but-present row then surfaced as "genuine_miss" — exactly the
    # "reads as no verdict was ever asserted, rather than broken encoding" failure
    # DeliverableLedgerValidationError's own docstring says this design exists to
    # prevent. Validated once per (root, mtime)/ledger-load (not once per artifact,
    # so this stays O(1) per corpus walk rather than O(corpus size)) via the module
    # set below, keyed like every other memo in this section (P2 fix, C5) so a
    # second root's ledger is validated too, not silently skipped because the
    # FIRST root already flipped a single process-wide bool; load_deliverable_
    # ledger itself stays non-validating per its own documented contract.
    global _DELIVERABLE_LEDGER_VALIDATED
    validation_key = _artifact_cache_key(worktree_root)
    if validation_key not in _DELIVERABLE_LEDGER_VALIDATED:
        validate_deliverable_ledger_rows(ledger_rows)
        # Bounded like the dict-shaped memos above (Review: coordinatorreview-
        # integrator, failopen-caches P2) — a set has no reliable insertion
        # order to evict "oldest half" from, so this caps via a full clear on
        # overflow instead. Safe: an evicted key is simply re-validated once
        # on its next call, never silently skipped (the membership test above
        # still gates every call).
        if len(_DELIVERABLE_LEDGER_VALIDATED) >= _MAX_CACHE:
            _DELIVERABLE_LEDGER_VALIDATED.clear()
        _DELIVERABLE_LEDGER_VALIDATED.add(validation_key)

    evidence_index = _ledger_evidence_index(ledger_rows)

    rel_path = os.path.relpath(artifact_path, str(worktree_root)).replace(os.sep, "/")
    ledger_id = evidence_index.get(rel_path)

    if ledger_id is None:
        return fm_canonical, _LEDGER_OUTCOME_GENUINE_MISS

    if fm_canonical is not None and fm_canonical != ledger_id:
        raise DeliverableDualReadMismatchError(
            f"deliverable_id dual-read mismatch for {artifact_path!r}: ledger "
            f"names {ledger_id!r}, artifact frontmatter (canonicalized) names "
            f"{fm_canonical!r} — two live writers of the same join key have "
            "diverged; refusing to silently pick one (see "
            "DeliverableDualReadMismatchError's own docstring)."
        )

    return ledger_id, _LEDGER_OUTCOME_HIT


def dual_read_deliverable_ids_for_corpus(
    worktree_root: Path,
    artifact_paths: List[str],
    equivalence_map: Dict[str, str],
    read_frontmatter_field=None,
) -> Tuple[Dict[str, Tuple[Optional[str], str]], List[str]]:
    """Step (2), batch/corpus level — modeled on `ownership_index.
    build_ownership_index`'s own `(result, scan_errors)` return shape.

    Op-fail-vs-read-fail (the corpus-level open question this stub's baton
    carries forward — answered here, not silently defaulted): `dual_read_
    deliverable_id` raises PER RECORD, because a single record's identity
    being genuinely unresolvable is exactly the case where a caller that
    proceeds anyway is a caller that guesses (AC5's hard-error mandate). But
    raising THAT SAME exception out of a corpus-wide walk would take an entire
    emission/report down on one bad row — the corpus's own measured hazard.
    This wrapper is the split-by-altitude answer: it calls the per-record
    function for every path, CATCHES `DeliverableDualReadMismatchError` per
    record into `errors` (a list of the exception's own message strings, one
    per mismatched path) rather than letting it propagate, and continues the
    walk — so one broken row never aborts every other row's read. No emit-
    path caller is wired to this wrapper in this stub; that wiring, and
    whether it should re-raise on ANY error rather than just collect them, is
    a later stub's decision and must re-derive against this seam rather than
    assume this wrapper's choice.

    Returns `(results, errors)`: `results` maps EVERY input path that did not
    raise to its own `(deliverable_id, outcome)` pair (see `dual_read_
    deliverable_id`'s docstring for `outcome`'s three values); a path that
    raised is present in `errors` (as a message string) and ABSENT from
    `results` — mirroring `build_ownership_index`'s own "an error is reported,
    not silently folded into a false-negative result" discipline.
    """
    results: Dict[str, Tuple[Optional[str], str]] = {}
    errors: List[str] = []
    for artifact_path in artifact_paths:
        try:
            results[artifact_path] = dual_read_deliverable_id(
                worktree_root, artifact_path, equivalence_map, read_frontmatter_field
            )
        except DeliverableDualReadMismatchError as exc:
            errors.append(str(exc))
    return results, errors
