"""
coordinator_core.session.claim_index — the reverse claim index: path ->
claimant session id(s).

Plan: docs/plans/2026-08-08-claim-index-the-commit-gate-never-had.md, chunk
C1. That plan's Problem section is the full warrant for this module's
existence — the excised sink-side ownership gate (``de27716``, ``b56f3f3``)
re-derived path->claimant on every call by walking every claimant's log,
which cost 13.91s on a 918-live-claim tree and blew the 30s dispatch budget
on a ONE-FILE pathspec. This module inverts that: the claim substrate is
small (measured on this tree: 1012 ``touched.txt`` files, 9040 T/R event
lines, 1255 distinct claimed paths) and cheap to read in full (measured full
rebuild: 37.9ms, no git subprocess spawns), so a derived reverse index turns
a per-call O(claims) scan into an O(len(paths)) dict lookup.

SOURCE OF TRUTH stays the existing per-claimant logs — this module is a
derived cache, never a second authority. It never writes to a
``touched.txt``, and it writes nothing to disk of its own: the reverse
index is IN-MEMORY-ONLY, rebuilt fresh on every ``lookup()`` call.

SUBSTRATE FORMAT — ``touched.txt`` is a CURRENT-CLAIM record (DR-254),
append-only, last-event-wins across TWO event verbs sharing one file:
``T <iso8601> <repo-relative-path>`` (claim) and ``R <iso8601>
<repo-relative-path>`` (release, written by
``coordinator_core.session.scope.release_committed_claims`` and its sibling
write-side release path). Both verbs land in the SAME file, at BOTH
``<sessions-dir>/<sid>/touched.txt`` (one per session) AND
``<sessions-dir>/.agents/<agent-id>/touched.txt`` (one per dispatched
agent, back-pointed to its owning session via a same-directory
``em-session-id.txt`` whose first line is that session's id — an agent's
claims are attributed to ITS OWNER session in this index, never to the
agent id, because that is the identity C2's gate compares the calling
session against).

NEGATIVE SPEC — last-EVENT-wins, not last-T-wins. A reader that parses only
``T`` lines would assert a session still holds a path it legitimately
released: ``T`` then ``R`` must resolve UNCLAIMED, and ``T`` then ``R`` then
a re-``T`` must resolve CLAIMED to the re-claimant. This module tracks the
last verb seen per path, per claimant file, in file order (the log is
append-only, so file order already IS chronological order — no timestamp
comparison across files is needed or performed).

NO ``record()`` — rebuild-only by design. Wiring incremental writes would
need three call sites in ``coordinator_core/session/scope.py`` (``touch``,
``release_committed_claims``, and its write-side sibling), and this plan
authors no chunk that touches ``scope.py`` — an unwired ``record()`` would
ship a permanently-stale index. With a full rebuild measured at 37.9ms,
always-rebuild-on-staleness is simpler and cheap enough to just do.

NO LOCK. Concurrent-reader-safe by construction rather than by mutual
exclusion: each claimant's ``touched.txt`` is read as a single
line-oriented blob, and a reader that catches a writer mid-write discards
only the (possibly torn) trailing line rather than attempting to repair it
— see ``_read_lines_discard_torn_tail``. Re-adding the ceremony lock to
serialize this is explicitly out of scope (PM ruling 2026-08-07;
``ceremony_lock.py`` is a dead no-op shim with no automated recovery) —
see the plan's Anti-scope.

INDEX PERSISTENCE — REMOVED, NOT JUST UNUSED (C1c). An earlier revision of
this module persisted the rebuilt index to
``<sessions-dir>/.index/claims.idx`` and read it back via a
``_load_index()`` staleness-checked path. C1b made ``lookup()`` rebuild
unconditionally, which deleted the only CALLER of ``_load_index()`` but
left the WRITER (``rebuild()`` -> ``_write_index()``) still running on
every call. A producer with no reader is worse than no producer: it is a
stale artifact a future maintainer can stumble on and trust as live state
— the exact failure mode this repo has already been bitten by once (a
lookup-table read mistaken for live state). ``rebuild()`` therefore no
longer writes anything; the reverse index lives only in the
``_IndexState`` this call's ``rebuild()`` returns, and is discarded when
the caller's stack frame returns.

STALENESS RULE — DELETED, NOT REPAIRED (measured this session, C1b). The
original design stamped the index with the sessions-dir's own top-level
mtime and had ``lookup()`` re-rebuild only on divergence. That probe is
blind to the common case: an EXISTING session appending its 2nd-and-later
claim to ``<sid>/touched.txt`` (what ``scope.py::touch`` does on every call
after the first) changes THAT FILE's mtime, not its parent directory's —
neither the top-level sessions-dir mtime nor ``<sid>/``'s own mtime moves.
Only creating a brand-new session dir moved the stamp. So the probe reported
FRESH while live peers accumulated claims unseen, and the index served a
stale NEGATIVE — the one answer that authorizes a write — through a door
the positive/negative asymmetry rule (below) did not cover. Switching to a
max-mtime-over-session-dirs probe would not fix this: it is blind to the
identical append, since the append still never touches any directory's
mtime, only the file's. There is no cheap mtime-shaped probe that observes
an in-place file append; the fix is to stop relying on one.

``lookup()`` therefore REBUILDS UNCONDITIONALLY on every call — no mtime
stamp, no staleness comparison, no cached on-disk index read. This is
not a performance regression: the index's win was never caching, it was
replacing the excised gate's 24 git-subprocess call sites (13.91s measured)
with pure file reads (37.9ms measured full rebuild) — a 366x win that
unconditional rebuild keeps in full, since a full rebuild is what a fresh
call now always does anyway. Unconditional rebuild also collapses the
staleness window to zero, which makes the positive/negative asymmetry rule
below MOOT rather than load-bearing — it is kept and documented (not
deleted) because it is still the correct rule for anyone who reintroduces
any form of caching here later.

The 500ms hard wall-clock cap (``REBUILD_WALL_CLOCK_CAP_SECS``) is
UNCHANGED and, together with an I/O error surfaced while READING a
directory or file the walk actually reached (as opposed to that path
genuinely not existing — see ``_enumerate_touched_files`` /
``_agent_owner_sid``), is the module's ONLY degradation path: if a
rebuild exceeds the cap mid-walk, or hits an unreadable claim source, it
marks the resulting ``_IndexState`` incomplete rather than silently
treating the gap as "no claims", and every path the caller asked about
that the walk could not resolve comes back from ``lookup()`` as
``UNANSWERABLE``, a marker distinct from "unclaimed" (the caller, C2,
applies its own fail-closed policy to that marker; this module never
guesses on its behalf). A genuinely-absent directory (``FileNotFoundError``)
is not a degradation — it legitimately means "no claims here yet".

POSITIVE/NEGATIVE ASYMMETRY (design rule, kept for a future caching
reintroduction — moot, not load-bearing, under unconditional rebuild). A
POSITIVE (claimed) answer may be served from the index unconditionally — it
can only cause an over-refusal, which is safe. A NEGATIVE (unclaimed)
answer is the only one that authorizes a write, so if this module ever
caches again, ``lookup()`` must never serve a negative straight off a
cached stamp: it must force one more unconditional ``rebuild()`` pass
immediately before returning a negative for any requested path. Under the
current unconditional-rebuild design every answer already comes from a
just-built state, so this extra pass is a no-op in effect and is kept only
to preserve the invariant textually for whoever adds caching back.

AC10b — DOCUMENTED RESIDUAL, NOT FIXED HERE. A forged ``session_id`` that
names the ACTUAL live holder of a target path defeats this whole layer: it
gets the identical verdict the real holder would, because there is no
in-op secret to check — ``session_dir()`` is a bare string join over
repo-readable directory names, and any check this module could perform
reads the same filesystem an attacker already reads. This raises the bar
from "invoke the op" to "enumerate peer session dirs and impersonate the
specific holder," which is real, but it is not closed by this module or by
C2, and is not tested as a pass/fail criterion.

RECENCY-OF-EDIT WARN — TIMESTAMP CARRIED, NOT NEW I/O (C1d,
``state/audits/2026-08-13-edit-recency-spike.md``). ``_last_verb_per_path``
now carries ``(verb, timestamp)`` instead of a bare verb, ``rebuild()``
threads it into ``_IndexState.edit_ts`` (path -> {claimant_sid:
last_T_timestamp}), and ``lookup()`` re-keys it onto ``_LookupResult.
edit_ts`` for its caller — the timestamp was already being parsed off every
``touched.txt`` line and discarded one line before it reached a consumer;
carrying it adds zero I/O (same files, same walk, same parse), so the
37.9ms rebuild budget is unaffected. The stamp is the FIRST edit of a
claimant's current claim run, not its latest (``scope.touch()`` dedups by
returning early once a path's last event is already ``T``) — DR-296 (PM
ruling) is to read it AS-IS: no repair, no re-stamp-in-place, no second
field. See ``coordinator_core/ops/ceremony/scoped_git_commit.py``'s
``_warn_recent_edits`` for the sole consumer, which logs a WARN and never
gates on what it reads here.

NO GIT SUBPROCESS SPAWNS. Every read in this module is a plain file read —
that is the entire performance premise. The only exception is resolving the
sessions-dir path itself when a caller does not supply one explicitly (via
``coordinator_core.session.core.sessions_dir``, cached there); tests always
pass ``sessions_dir`` explicitly and never touch a real git repo.
"""

from __future__ import annotations

import dataclasses
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from coordinator_core.session import core, touch_record
from coordinator_core.session.path_dialect import canonicalize_relative_path

#: C4 (AC18) — re-keyed from a wall-clock cap to a PROCESS-TIME cap. The
#: original ``time.monotonic()`` deadline fires under ordinary machine load
#: (50-70 concurrent sessions is this repo's load norm, not the peak — see
#: CLAUDE.md § Load norm) even when THIS rebuild's own CPU cost is trivial —
#: a wall-clock cap conflates "this walk is slow" with "the box is busy",
#: and this repo's own rule is that wall clock measures peer load, never
#: cost. ``time.process_time()`` only advances while this process is
#: actually executing, so a scheduler-preempted rebuild on a loaded box no
#: longer degrades to UNANSWERABLE purely because of contention it did not
#: cause. The name is kept (not renamed) because it is a public module
#: constant several tests reference by this name; the docstring and the two
#: call sites below are what actually changed. See AC18's dispatch note for
#: the measured C0 figure (541.48ms at 50 peers x 5000 lines/peer) this cap
#: exists to catch.
REBUILD_WALL_CLOCK_CAP_SECS = 0.5

#: Sentinel returned by lookup() for a path an (aborted or unresolvable)
#: rebuild could not answer for — distinct from "unclaimed" (an empty
#: claimant list). The caller (C2, a later plan chunk) applies its own
#: per-path fail-closed policy to this marker.
UNANSWERABLE = "__UNANSWERABLE__"

#: ABORT-CAUSE CARRIER (C1, docs/plans/2026-08-11-claim-index-abort-cause-and-
#: cli-blindness.md). Plain string constants on ``_IndexState.abort_cause`` /
#: ``_LookupResult.abort_cause``, not an ``enum.Enum``. Chosen over an enum
#: because every existing consumer of this module treats ``_LookupResult``
#: as ``UNANSWERABLE``-flavored dict-plus-``complete`` (a plain str/bool
#: pair, per that class's own docstring) and a CLI (``session-claim-cli``)
#: is the only other reader added by this plan chunk — it just needs a
#: printable token, not a type to branch on. A plain module-level string
#: constant is import-free for that CLI (no ``from claim_index import
#: AbortCause`` enum dependency) and trivially ``==``-comparable in a test
#: without an import of the enum member. ``None`` means "not aborted" —
#: distinct from any of the three string causes below, so
#: ``abort_cause is None`` doubles as the completeness check without
#: re-reading ``.complete``.
ABORT_CAUSE_EMPTY_BASE = "empty_base"
ABORT_CAUSE_CAP_EXCEEDED = "cap_exceeded"
ABORT_CAUSE_IO_ERROR = "io_error"

_AGENTS_SUBDIR = ".agents"
#: C4 — the substrate this module walks flips from the bash-dialect
#: ``touched.txt`` to C3's self-describing record. AC7: this module reads
#: the SAME seam ``scope.compute_scope`` reads (``touch_record``'s family +
#: decode primitives), with no independent path-construction or line-dialect
#: parsing of its own left in this file.
#:
#: NEGATIVE SPEC — what a path's ABSENCE from this index does NOT mean
#: (2026-08-26, ``state/audits/2026-08-26-touch-ledger-coverage-and-the-
#: published-dialect-split.md``). Absence is NOT evidence that no session
#: authored the path. This module reads ONE dialect, and since the compat
#: union came out (2026-08-26) so does every claim reader in this package —
#: a pre-cutover ``touched.txt`` is no longer a claim surface anywhere,
#: having been drained (``legacy_touch_corpus_migrate``, verified by both
#: ``legacy_touch_corpus_drain_check`` and the content-level
#: ``legacy_touch_corpus_straggler_check``) with no writer left that can
#: recreate one. ``bash_guards/dispatch_checks.py`` keeps its own union; it
#: is an advisory guard, not a claim authority. Two live classes of
#: authored-but-absent path remain: any file written by a shell redirect,
#: heredoc, or spawned third-party CLI, which no writer observes at all;
#: and — whenever the mirror is percolated with a reader ahead of its
#: writer — every Edit/Write-authored path in the fleet. That second class
#: was live for hours on 2026-08-26 and produced a fully-populated
#: ``complete: True`` answer while omitting the caller's own work. Do NOT
#: read a missing entry as "unclaimed"; read it as "this index cannot say".
_TOUCHED_FILENAME = "touch-record.jsonl"


def _has_claim_surface(sink_path: str) -> bool:
    """True when *sink_path*'s directory holds any readable claim surface: the
    live jsonl sink or one of its rotated family members.

    Stat-only, no content read, matching this enumerator's contract.
    """
    return bool(os.path.isfile(sink_path) or touch_record.discover_family(sink_path))


def _normalize_key(path: str) -> str:
    """Canonicalize a path string to the SAME dialect ``touched.txt`` is
    written in, so a key parsed from disk and a caller-supplied lookup path
    compare equal regardless of separator dialect.

    A thin call-through to
    ``coordinator_core.session.path_dialect.canonicalize_relative_path`` —
    the shared, subprocess-free canonicalizer both this module and
    ``coordinator_core.session.scope`` (its ``normalize_touch_path`` relative
    arm, and ``classify_touch_entry``'s inline copy) now import, closing the
    two-dialect divergence a backslashed relative pathspec used to hit (C1,
    docs/plans/2026-08-11-claim-release-and-the-gate-that-cannot-clear.md).
    This module's inputs are already repo-relative (either written by
    ``scope.py``'s own writer, or a caller-supplied relative pathspec), so no
    absoluteness handling is needed here — that stays out of scope for this
    call, matching ``path_dialect.canonicalize_relative_path``'s own
    contract.
    """
    return canonicalize_relative_path(path)


class _LookupResult(dict):
    """``dict`` subclass returned by ``lookup()`` — identical in every
    respect to a plain ``{path: [claimant, ...]}`` dict for every consumer
    that predates C10 (equality via ``==`` against a bare dict, ``.get()``,
    ``in``, iteration all behave exactly as a bare dict would, since
    ``dict.__eq__`` compares contents regardless of subclass), plus one
    extra attribute, ``complete``.

    C10 (docs/plans/2026-08-08-claim-index-the-commit-gate-never-had.md, the
    fail-OPEN closed alongside that plan): ``_check_claim_conflicts`` in
    ``coordinator_core/ops/ceremony/scoped_git_commit.py`` needs to know
    whether the walk behind THIS call's answers was complete, to refuse a
    path whose only resolved claimant is the caller itself when the walk
    that produced that positive answer aborted before it could have reached
    a live peer's claim. A dict subclass carries that one extra bit without
    changing ``lookup()``'s call signature or return shape — deliberately,
    to keep the single pinned call site (``coordinator_core/hooks/tests/
    test_c4_ownership_inherited_at_dispatch_tripwires.py``,
    ``TestClaimIndexLookupCallSitesDoNotWiden``) matching the literal
    ``claim_index.lookup(`` form it censuses, and to avoid a second
    ``rebuild()`` pass (this module's binding cost-class negative spec —
    see module docstring).

    ``complete`` mirrors this call's own ``rebuild()``'s
    ``_IndexState.complete`` — False iff the walk that produced this dict's
    answers was aborted by the wall-clock cap or hit an unreadable claim
    source (see module docstring's degradation-path section), or the
    sessions dir itself could not be resolved. A caller that never reads
    ``.complete`` (every consumer before C10) observes no behavior change.

    ``abort_cause`` (C1) mirrors ``_IndexState.abort_cause`` — one of
    ``ABORT_CAUSE_EMPTY_BASE`` / ``ABORT_CAUSE_CAP_EXCEEDED`` /
    ``ABORT_CAUSE_IO_ERROR`` when ``complete`` is False, else ``None``. A
    consumer that never reads it (every consumer before this chunk,
    including C10) observes no behavior change — it is an additional
    attribute, never a change to ``complete`` or to the ``claimants``
    membership contract (``UNANSWERABLE in claimants`` is untouched).

    ``edit_ts`` (C1d) maps each requested path (caller's ORIGINAL string,
    same keying as the ``claimants`` mapping itself) -> ``{claimant_sid:
    last_T_timestamp}`` for that path's currently-claiming sessions —
    a straight passthrough of ``_IndexState.edit_ts`` re-keyed onto the
    caller's original path strings, added so ``scoped_git_commit.py``'s
    recency-of-EDIT warn (AC1d) can read timestamps off the SAME rebuild
    this call already performed, without a second ``rebuild()`` pass. A
    consumer that never reads it (every consumer before C1d) observes no
    behavior change.
    """

    complete: bool = True
    abort_cause: Optional[str] = None
    edit_ts: Dict[str, Dict[str, datetime]] = None  # type: ignore[assignment]


@dataclasses.dataclass
class _IndexState:
    """In-memory-only reverse index snapshot.

    ``claims`` maps repo-relative path -> sorted list of claimant session
    ids currently holding it (last-event-wins). ``complete`` is False iff
    the walk that produced this snapshot was aborted by the wall-clock cap
    OR could not fully enumerate a directory it was able to reach (e.g. a
    permission error, as opposed to that directory genuinely not existing)
    — callers must never treat an absent path in an incomplete snapshot as
    "unclaimed".

    ``abort_cause`` (C1) is one of ``ABORT_CAUSE_EMPTY_BASE`` /
    ``ABORT_CAUSE_CAP_EXCEEDED`` / ``ABORT_CAUSE_IO_ERROR`` when
    ``complete`` is False, else ``None``. When more than one cause is
    technically true of a single walk (e.g. an I/O error during enumeration
    followed by the cap also expiring in the walk that follows), the FIRST
    cause encountered in walk order wins — this module never overwrites an
    already-set ``abort_cause``.

    ``edit_ts`` (C1d) maps path -> ``{claimant_sid: last_T_timestamp}`` for
    every claimant CURRENTLY holding a ``T`` claim on that path (mirrors
    ``claims`` but carries the timestamp of each claimant's last-recorded
    edit event instead of just membership). That stamp is the FIRST edit of
    the claimant's current claim run, not its latest — DR-296 (PM ruling,
    see ``lookup()``'s docstring) reads it AS-IS regardless; a session
    editing one path for two hours carries a two-hour-old stamp here, by
    design. A claimant with no parseable timestamp on its last ``T`` event
    (legacy/malformed line) is absent from this mapping even though it is
    still present in ``claims``.
    """

    claims: Dict[str, List[str]]
    complete: bool
    abort_cause: Optional[str] = None
    edit_ts: Dict[str, Dict[str, datetime]] = dataclasses.field(default_factory=dict)


def _read_stream_claims(sink_path: str) -> tuple:
    """C4 (AC7) — the seam read: one claimant's on-disk family (the live
    ``touch-record.jsonl`` plus zero or more rotated siblings), decoded and
    folded to ITS OWN last-verb-wins claim map via
    ``touch_record._read_stream_claims`` — the exact primitive
    ``scope.compute_scope`` now reads through too (C4's Step 3/3b). No
    independent path-construction, family discovery, or line-dialect parse
    is done in this module any more; this is a thin call-through.

    Returns ``(claims_by_path, read_ok)`` — ``read_ok`` mirrors this
    module's pre-existing ``(value, complete)`` convention (see
    ``_agent_owner_sid``/``_enumerate_touched_files``): ``False`` iff
    ``touch_record``'s own degrade flag fired (an unreadable family member,
    or a complete line that failed to decode — AC6's typed signal), matching
    the bug-backlog concern the deleted ``_read_lines_discard_torn_tail``
    docstring named (an unreadable claimant file must never resolve
    identically to "no claims from this claimant"). A torn/unterminated
    trailing line is never a degrade signal here either — ``touch_record``
    already drops it before it reaches decode.
    """
    claims, degraded, _reasons = touch_record._read_stream_claims(sink_path)
    return claims, not degraded


def _agent_owner_sid(agent_dir_path: str) -> tuple:
    """First line of ``<agent_dir_path>/em-session-id.txt`` — the session
    id that owns this agent's fan-out claims. Returns ``(sid, complete)``:
    ``sid`` is ``""`` when the back-pointer is missing or unreadable,
    ``complete`` is False iff the back-pointer could not be read for a
    reason OTHER than it genuinely not existing (e.g. a permission error)
    — Review: coordinator:code-reviewer P1, an I/O error reading a claim
    file must surface as UNANSWERABLE, not silently collapse to "no
    claims". An agent dir with no resolvable owner contributes no claims
    to the index (its ``touched.txt``, if any, is simply skipped by
    ``rebuild()``)."""
    backptr = os.path.join(agent_dir_path, "em-session-id.txt")
    try:
        with open(backptr, "r", encoding="utf-8") as fh:
            first_line = fh.readline()
    except FileNotFoundError:
        return "", True
    except OSError:
        return "", False
    return first_line.strip(), True


def _enumerate_touched_files(base: str) -> tuple:
    """Enumerate every ``(touched.txt path, claimant session id)`` pair
    under ``base`` — session dirs directly, agent dirs via their
    ``em-session-id.txt`` back-pointer. Deterministically ordered (sorted
    by entry name) so a capped-out walk aborts at a reproducible point.
    Pure ``os.scandir``/``os.path`` calls — no file CONTENT is read here.

    Returns ``(pairs, complete)``. ``complete`` is False iff a directory
    this walk was able to REACH could not be fully enumerated for a reason
    other than it genuinely not existing — Review: coordinator:code-reviewer
    P1, a bare ``except OSError`` here previously collapsed permission
    errors and other I/O failures into "zero claims" (same verdict as a
    directory that legitimately does not exist), silently authorizing a
    write for a path this walk never actually resolved. A genuinely-absent
    directory (``FileNotFoundError``) is still "no claims" and does not
    mark the walk incomplete.
    """
    pairs: List[tuple] = []
    complete = True

    def _scandir_sorted(path):
        nonlocal complete
        try:
            return sorted(os.scandir(path), key=lambda e: e.name)
        except FileNotFoundError:
            return []
        except OSError:
            complete = False
            return []

    for entry in _scandir_sorted(base):
        if entry.name == _AGENTS_SUBDIR:
            continue
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if not is_dir:
            continue
        touched_path = os.path.join(entry.path, _TOUCHED_FILENAME)
        # C4: a claimant with no LIVE file but a rotated-away sibling (C2's
        # rotation, AC17) still has real claims on disk -- the live-file-
        # only `isfile` check would silently drop them. `discover_family`
        # is a cheap `glob` + `exists`, not a content read. `_has_claim_surface`
        # adds the third case: a legacy-only claimant, readable through the
        # compat union and otherwise unreleasable by any route.
        if _has_claim_surface(touched_path):
            pairs.append((touched_path, entry.name))

    agents_base = os.path.join(base, _AGENTS_SUBDIR)
    for agent_entry in _scandir_sorted(agents_base):
        try:
            is_dir = agent_entry.is_dir()
        except OSError:
            continue
        if not is_dir:
            continue
        owner_sid, owner_complete = _agent_owner_sid(agent_entry.path)
        if not owner_complete:
            complete = False
        if not owner_sid:
            continue
        touched_path = os.path.join(agent_entry.path, _TOUCHED_FILENAME)
        if _has_claim_surface(touched_path):
            pairs.append((touched_path, owner_sid))

    return pairs, complete


def _resolve_base(sessions_dir: Optional[str], cwd: Optional[str]) -> str:
    if sessions_dir is not None:
        return sessions_dir
    return core.sessions_dir(cwd)


def rebuild(sessions_dir: Optional[str] = None, cwd: Optional[str] = None) -> _IndexState:
    """Full walk over every claimant's ``touched.txt`` — the ONLY O(claims)
    operation in this module. Resolves last-event-wins per path per
    claimant file (see ``_last_verb_per_path``), aggregates across every
    session-dir and agent-dir claimant, and returns the resulting
    in-memory ``_IndexState`` — nothing is persisted to disk (see module
    docstring, INDEX PERSISTENCE — REMOVED, NOT JUST UNUSED).

    Aborts (setting ``complete=False`` on the returned/persisted state) if
    the walk exceeds ``REBUILD_WALL_CLOCK_CAP_SECS`` — the paths a caller
    asked about that this walk had not yet reached come back from
    ``lookup()`` as ``UNANSWERABLE``, never silently "unclaimed".

    ``sessions_dir``, when given explicitly, is read as-is with no git
    subprocess spawn — this is how tests point the walk at a synthetic
    ``tmp_path`` fixture instead of a real repo. When omitted, resolves via
    ``core.sessions_dir(cwd)`` (which may spawn ``git`` once, cached there).
    """
    base = _resolve_base(sessions_dir, cwd)
    if not base:
        state = _IndexState(claims={}, complete=False, abort_cause=ABORT_CAUSE_EMPTY_BASE)
        return state

    # AC18 — process-time deadline, not wall-clock (see the constant's own
    # docstring for why).
    process_deadline = time.process_time() + REBUILD_WALL_CLOCK_CAP_SECS
    claims: Dict[str, set] = {}
    edit_ts: Dict[str, Dict[str, datetime]] = {}
    touched_pairs, complete = _enumerate_touched_files(base)
    abort_cause: Optional[str] = None if complete else ABORT_CAUSE_IO_ERROR

    for touched_path, claimant_sid in touched_pairs:
        if time.process_time() > process_deadline:
            complete = False
            if abort_cause is None:
                abort_cause = ABORT_CAUSE_CAP_EXCEEDED
            break
        stream_claims, content_read_ok = _read_stream_claims(touched_path)
        if not content_read_ok:
            complete = False
            if abort_cause is None:
                abort_cause = ABORT_CAUSE_IO_ERROR
            continue
        for path, event in stream_claims.items():
            if event.verb == touch_record.VERB_TOUCH:
                claims.setdefault(path, set()).add(claimant_sid)
                # AC21: a legacy line with no parseable timestamp arrives as
                # `timestamp=0.0` ("unknown time"). `edit_ts`'s own contract
                # (see `_IndexState`) is that such a claimant is ABSENT from
                # this mapping while still present in `claims` -- recording
                # it as a 1970 instant would make an active claimant read as
                # the stalest on the box.
                if event.timestamp > 0:
                    edit_ts.setdefault(path, {})[claimant_sid] = datetime.fromtimestamp(
                        event.timestamp, tz=timezone.utc
                    )
            else:  # RELEASE — a release only ever removes the claimant from
                # the aggregate bucket. A same-file re-claim after a release
                # can't reach this branch: this per-file scan already
                # collapsed to one verb per path (touch_record's own
                # last-verb-wins fold).
                claims.get(path, set()).discard(claimant_sid)
                edit_ts.get(path, {}).pop(claimant_sid, None)

    result_claims = {path: sorted(sids) for path, sids in claims.items() if sids}
    result_edit_ts = {
        path: dict(sids_ts) for path, sids_ts in edit_ts.items() if sids_ts
    }
    return _IndexState(
        claims=result_claims,
        complete=complete,
        abort_cause=abort_cause,
        edit_ts=result_edit_ts,
    )


def lookup(
    paths, sessions_dir: Optional[str] = None, cwd: Optional[str] = None
) -> "_LookupResult":
    """``{path: [claimant_session_id, ...]}`` for every path in ``paths``.

    Rebuilds the index UNCONDITIONALLY on every call — see module
    docstring's Staleness rule for why the earlier mtime-staleness probe
    was deleted rather than repaired (C1b: it never observed an existing
    session appending a 2nd-and-later claim, the common case, so it served
    stale negatives). There is no cached-read path left in this function;
    every call is a fresh ``rebuild()``, budgeted at the measured 37.9ms
    against ``REBUILD_WALL_CLOCK_CAP_SECS``.

    A path with no live claimant resolves to ``[]``. A path this call could
    not resolve (unresolvable sessions dir, or an aborted rebuild that
    never reached it) resolves to ``[UNANSWERABLE]`` — check membership via
    ``UNANSWERABLE in claimants``, never index-0, since a resolved-unclaimed
    path is ``[]`` and indexing it raises.

    Every ``paths`` entry is normalized via ``_normalize_key`` (same
    separator-normalize + ``posixpath.normpath`` dialect ``touched.txt``
    keys are parsed into, see that function's docstring) before it is
    compared against the index — a backslashed caller pathspec (routine on
    Windows, this repo's first-class platform) against forward-slashed
    index keys must not silently miss and read as "unclaimed", which is
    what authorizes a write. The RETURNED dict is keyed on each entry's
    ORIGINAL, caller-supplied string (not the normalized form), so a caller
    that built ``paths`` from its own pathspec and looks results back up by
    that same original string (e.g. ``_warn_recent_edits`` in
    ``coordinator_core/ops/ceremony/scoped_git_commit.py``, C1d's
    recency-of-EDIT warn) still finds its key. Two distinct original
    strings that normalize to the same key both
    receive that key's claimant list.

    Returns a ``_LookupResult`` — a ``dict`` subclass that also carries a
    ``.complete`` bool mirroring this call's own rebuild's completeness
    (C10; see that class's docstring). Every pre-C10 consumer that only
    ever used this return value as a plain dict is unaffected.
    """
    base = _resolve_base(sessions_dir, cwd)
    if not base:
        result = _LookupResult((path, [UNANSWERABLE]) for path in paths)
        result.complete = False
        result.abort_cause = ABORT_CAUSE_EMPTY_BASE
        result.edit_ts = {}
        return result

    state = rebuild(sessions_dir=base)

    result = _LookupResult()
    result_edit_ts: Dict[str, Dict[str, datetime]] = {}
    for path in paths:
        normalized = _normalize_key(path)
        claimants = state.claims.get(normalized)
        if claimants:
            result[path] = list(claimants)
        elif not state.complete:
            result[path] = [UNANSWERABLE]
        else:
            result[path] = []
        path_edit_ts = state.edit_ts.get(normalized)
        if path_edit_ts:
            result_edit_ts[path] = dict(path_edit_ts)
    result.complete = state.complete
    result.abort_cause = state.abort_cause
    result.edit_ts = result_edit_ts

    return result


@dataclasses.dataclass
class CommitSet:
    """What one session is responsible for committing right now.

    ``paths`` is the answer: repo-relative paths this session currently holds
    a ``T`` claim on and NO other session does. Sorted, so a caller rendering
    it twice gets the same order.

    ``contested`` is every path this session holds that a peer ALSO holds. It
    is deliberately NOT in ``paths`` -- a peer's path is not yours, so it is
    not offered -- but it is named here so a caller can tell the operator WHY
    something they edited is missing, rather than leaving them to wonder. That
    distinction is the whole job: the answer exists to remove doubt, and a
    silent omission reintroduces it.

    ``peers`` is every path this session does NOT hold that at least one
    other session does. It is NOT part of the "what belongs to me" answer and
    a caller rendering that answer should ignore it; it exists because a
    caller classifying an ARBITRARY path -- a dirty-tree sweep deciding what
    it may touch -- needs to tell "a peer owns this" from "nobody has claimed
    this", and those two are indistinguishable from ``paths`` and
    ``contested`` alone. Sized by the claim ledger, not by the tree: ~405
    entries on this repo, 2026-08-21. Keep it OUT of anything that crosses
    the op wire (~72KB as JSON at that size) -- in-process consumers only.

    ``complete`` mirrors ``_IndexState.complete``. False means the walk behind
    this answer was aborted or could not fully enumerate, so ``paths`` may be
    SHORT and both ``contested`` and ``peers`` may under-report. A caller must
    say so rather than presenting a partial answer as the answer;
    ``abort_cause`` names why.
    """

    paths: List[str]
    contested: Dict[str, List[str]]
    complete: bool
    abort_cause: Optional[str] = None
    peers: Dict[str, List[str]] = dataclasses.field(default_factory=dict)


def commit_set(
    session_id: str,
    sessions_dir: Optional[str] = None,
    cwd: Optional[str] = None,
) -> CommitSet:
    """Answer "what belongs to me to commit?" for ``session_id``. NO git spawns.

    This is a pure projection of :func:`rebuild`'s existing reverse index --
    it enumerates nothing of its own and spawns no subprocess, so its cost is
    exactly one index rebuild (measured 96.5ms best-of-3 over 41 touched.txt
    files / 2487 T/R lines on a loaded box, zero ``subprocess.run`` calls).
    The mechanism it replaces cost 73 processes and 5,609ms of CPU per call.

    NEGATIVE SPEC -- dirtiness is not part of this answer, by PM ruling
    (2026-08-21). "What belongs to me" and "what is dirty" are two different
    questions and must not be fused: a path claimed here that no longer
    differs from HEAD simply produces an empty commit, which
    ``ceremony.scoped_git_commit`` reports as ``empty-commit-set`` and which
    lands nothing. That is harmless. Paying a git spawn to pre-empt a harmless
    no-op is precisely the trade that justified the mechanism this replaces --
    do not reintroduce it here, however cheap the single call looks.

    Agent fan-out needs no special handling: :func:`rebuild` already resolves
    ``.agents/<aid>/em-session-id.txt`` back-pointers to the owning session, so
    a path an agent touched on this session's behalf is already claimed BY this
    session in the index.

    ``complete`` attests that the WALK finished (no cap, no IO abort) — never
    that the answer covers everything the session wrote. See
    ``_TOUCHED_FILENAME``'s negative spec for the three live classes of
    authored-but-absent path. A caller that reports "these are your files"
    off a ``complete=True`` answer is overstating what this returns.

    Last-event-wins is likewise already applied (``_last_verb_per_path``): a
    path this session touched and then RELEASED is absent from ``claims``, so
    it is absent here. Committing a path releases its claim, which is why this
    answers "still outstanding" rather than "ever touched" -- and why it is not
    a history query.
    """
    state = rebuild(sessions_dir=sessions_dir, cwd=cwd)
    mine: List[str] = []
    contested: Dict[str, List[str]] = {}
    peer_only: Dict[str, List[str]] = {}
    for path, claimants in state.claims.items():
        others = [c for c in claimants if c != session_id]
        if session_id not in claimants:
            if others:
                peer_only[path] = others
            continue
        if others:
            contested[path] = others
        else:
            mine.append(path)
    mine.sort()
    return CommitSet(
        paths=mine,
        contested=contested,
        complete=state.complete,
        abort_cause=state.abort_cause,
        peers=peer_only,
    )


#: The four verdicts :func:`classify_paths` returns, one per queried path.
#: Named constants rather than bare strings because a consumer branches on
#: them to build an operator-facing refusal, and a typo in a literal reads as
#: "no branch matched" rather than failing.
OWNERSHIP_MINE = "mine"
OWNERSHIP_PEER = "peer"
OWNERSHIP_UNCLAIMED = "unclaimed"
OWNERSHIP_UNANSWERABLE = "unanswerable"


@dataclasses.dataclass
class PathOwnership:
    """One path's answer to "may this session commit it?".

    ``verdict`` is one of the four ``OWNERSHIP_*`` constants. ``peers`` names
    every OTHER claimant found, and is populated ONLY on ``OWNERSHIP_PEER``
    -- a refusal that cannot name who it is protecting is the refusal an
    operator overrides.
    """

    verdict: str
    peers: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class OwnershipAnswer:
    """:func:`classify_paths`'s return -- ``by_path`` keyed by the CALLER's
    own path strings (not the normalized keys), so a caller can look its
    pathspec back up without re-normalizing.

    ``complete``/``abort_cause`` mirror :class:`_IndexState`'s, and are
    already folded into the per-path verdicts: an incomplete walk cannot
    return ``OWNERSHIP_UNCLAIMED`` or ``OWNERSHIP_MINE`` for any path, so a
    caller that ignores these two fields still cannot fail open. They are
    carried for narration.
    """

    by_path: Dict[str, PathOwnership]
    complete: bool
    abort_cause: Optional[str] = None


def classify_paths(
    session_id: str,
    paths: Sequence[str],
    sessions_dir: Optional[str] = None,
    cwd: Optional[str] = None,
) -> OwnershipAnswer:
    """Answer "who holds each of these paths?" for ``session_id``. NO git spawns.

    The sibling of :func:`commit_set`, and the same pure projection of
    :func:`rebuild`'s reverse index: ``commit_set`` answers the open question
    ("what is mine?"), this answers the closed one ("is THIS path mine?") for
    a pathspec a caller already has in hand. One index rebuild, no
    enumeration of its own, no subprocess. Both exist because a gate handed a
    pathspec cannot use ``commit_set``'s answer as a membership test: a path
    NOBODY has claimed is absent from ``paths`` for the same reason a peer's
    path is, and collapsing those two into one "not yours" is what makes an
    ownership refusal unreadable.

    FAIL-CLOSED, in two directions, and neither is negotiable:

      - An INCOMPLETE walk (:data:`ABORT_CAUSE_CAP_EXCEEDED`,
        :data:`ABORT_CAUSE_IO_ERROR`, :data:`ABORT_CAUSE_EMPTY_BASE`) can
        still return :data:`OWNERSHIP_PEER` -- a peer claim this walk DID
        reach is a fact the abort does not undo -- but never
        :data:`OWNERSHIP_MINE` or :data:`OWNERSHIP_UNCLAIMED`, which are both
        claims about what the walk did NOT find. A walk that stopped early
        found nothing it did not reach. This is C10's fail-open, closed here
        by construction instead of by a caller remembering to read
        ``complete``.

      - A peer claim denies whether or not that peer is still LIVE. This is a
        deliberate narrowing versus the mechanism killed at ``e927d9463``,
        which pruned a dead peer's claim -- but only in combination with a
        git dirty read, gating the prune on the path being clean as well as
        the holder dead. Dirtiness is out of this answer by PM ruling
        (2026-08-21, see :func:`commit_set`), so the paired half of that gate
        is gone, and honouring only the liveness half would prune a dead
        peer's claim on a path still carrying their in-flight edit -- the
        exact work-loss this gate exists to prevent. Strictly more
        conservative than what it replaces, never less; a stale claim is the
        reaper's job, and the refusal names the holder so it can be done
        deliberately.

    Agent fan-out and last-event-wins need no handling here for the same
    reason they need none in :func:`commit_set` -- ``rebuild`` has already
    applied both.
    """
    state = rebuild(sessions_dir=sessions_dir, cwd=cwd)
    by_path: Dict[str, PathOwnership] = {}
    for path in paths:
        claimants = state.claims.get(_normalize_key(path)) or []
        peers = [c for c in claimants if c != session_id]
        if peers:
            by_path[path] = PathOwnership(verdict=OWNERSHIP_PEER, peers=list(peers))
        elif not state.complete:
            by_path[path] = PathOwnership(verdict=OWNERSHIP_UNANSWERABLE)
        elif claimants:
            by_path[path] = PathOwnership(verdict=OWNERSHIP_MINE)
        else:
            by_path[path] = PathOwnership(verdict=OWNERSHIP_UNCLAIMED)
    return OwnershipAnswer(
        by_path=by_path,
        complete=state.complete,
        abort_cause=state.abort_cause,
    )
