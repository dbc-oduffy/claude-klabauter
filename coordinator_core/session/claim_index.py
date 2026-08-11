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

NO GIT SUBPROCESS SPAWNS. Every read in this module is a plain file read —
that is the entire performance premise. The only exception is resolving the
sessions-dir path itself when a caller does not supply one explicitly (via
``coordinator_core.session.core.sessions_dir``, cached there); tests always
pass ``sessions_dir`` explicitly and never touch a real git repo.
"""

from __future__ import annotations

import dataclasses
import os
import posixpath
import time
from typing import Dict, List, Optional

from coordinator_core.session import core

#: Hard wall-clock cap on one rebuild() walk. Measured full rebuild on this
#: tree (1012 touched.txt files, 9040 event lines): 37.9ms — ~13x inside
#: this cap. Exceeding it aborts the walk rather than blocking the commit
#: path; see module docstring's Staleness rule.
REBUILD_WALL_CLOCK_CAP_SECS = 0.5

#: Sentinel returned by lookup() for a path an (aborted or unresolvable)
#: rebuild could not answer for — distinct from "unclaimed" (an empty
#: claimant list). The caller (C2, a later plan chunk) applies its own
#: per-path fail-closed policy to this marker.
UNANSWERABLE = "__UNANSWERABLE__"

_AGENTS_SUBDIR = ".agents"
_TOUCHED_FILENAME = "touched.txt"


def _normalize_key(path: str) -> str:
    """Canonicalize a path string to the SAME dialect ``touched.txt`` is
    written in, so a key parsed from disk and a caller-supplied lookup path
    compare equal regardless of separator dialect.

    No reusable, subprocess-free callable exists in
    ``coordinator_core.session.scope`` to import here:
    ``scope.normalize_touch_path`` shells out to ``git ls-files`` on its
    absolute-path branch, and ``scope.classify_touch_entry`` /
    ``scope.normalize_historical_touch_entry`` both call it in turn --
    unacceptable on this module's pure-file-read performance premise (see
    module docstring, NO GIT SUBPROCESS SPAWNS). This module's inputs are
    already repo-relative (either written by ``scope.py``'s own writer, or
    a caller-supplied relative pathspec), so only the separator-normalize +
    ``posixpath.normpath`` step is needed -- the exact single canonical-value
    expression ``scope.classify_touch_entry`` documents and uses inline
    (``posixpath.normpath(entry.replace("\\\\", "/"))``, see that function's
    docstring and its module docstring's SOURCE OF TRUTH framing): "the
    comparison and the value are never separately computed". This is that
    same expression, kept here rather than imported because no standalone,
    subprocess-free function wrapping it exists to import -- see
    ``coordinator_core/session/scope.py::classify_touch_entry`` as the
    authority this must track if that expression ever changes there.
    """
    return posixpath.normpath(path.replace("\\", "/"))


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
    """

    claims: Dict[str, List[str]]
    complete: bool


def _read_lines_discard_torn_tail(path: str) -> List[str]:
    """Read ``path`` as text and split it into complete lines, discarding a
    torn (no trailing newline) final fragment rather than parsing it.

    Used on every substrate read (``touched.txt``) — a reader that opens
    the file while a writer is mid-append (no lock is ever taken — see
    module docstring) may observe an incomplete last line. That fragment is
    silently dropped on THIS read, never repaired; a subsequent read after
    the writer finishes sees it whole.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return []
    if not raw:
        return []
    if not raw.endswith("\n"):
        cut = raw.rfind("\n")
        raw = raw[: cut + 1] if cut >= 0 else ""
    return [line for line in raw.split("\n") if line != ""]


def _last_verb_per_path(lines: List[str]) -> Dict[str, str]:
    """Scan one claimant's raw ``touched.txt`` lines and keep, per path, the
    verb of its LAST event in file order (append-only log -> file order IS
    chronological order; no cross-file timestamp comparison is needed).

    A line that does not match ``'<T|R> <timestamp> <path>'`` (via
    ``str.split(None, 2)``, so a path containing a space is never split)
    is skipped rather than guessed at — this is a NEW reader with no
    legacy bare-path-line compatibility obligation, unlike
    ``coordinator_core.session.scope.parse_touch_event``.
    """
    last_verb: Dict[str, str] = {}
    for line in lines:
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        verb, _ts, path = parts
        if verb not in ("T", "R"):
            continue
        last_verb[_normalize_key(path)] = verb
    return last_verb


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
        if os.path.isfile(touched_path):
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
        if os.path.isfile(touched_path):
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
        state = _IndexState(claims={}, complete=False)
        return state

    deadline = time.monotonic() + REBUILD_WALL_CLOCK_CAP_SECS
    claims: Dict[str, set] = {}
    touched_pairs, complete = _enumerate_touched_files(base)

    for touched_path, claimant_sid in touched_pairs:
        if time.monotonic() > deadline:
            complete = False
            break
        last_verb = _last_verb_per_path(_read_lines_discard_torn_tail(touched_path))
        for path, verb in last_verb.items():
            if verb == "T":
                claims.setdefault(path, set()).add(claimant_sid)
            else:  # "R" — a release only ever removes the claimant from the
                # aggregate bucket. A same-file re-claim after a release
                # can't reach this branch: this per-file scan already
                # collapsed to one verb per path (see _last_verb_per_path).
                claims.get(path, set()).discard(claimant_sid)

    result_claims = {path: sorted(sids) for path, sids in claims.items() if sids}
    return _IndexState(claims=result_claims, complete=complete)


def lookup(
    paths, sessions_dir: Optional[str] = None, cwd: Optional[str] = None
) -> Dict[str, List[str]]:
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
    that same original string (e.g. ``_check_claim_conflicts`` in
    ``coordinator_core/ops/ceremony/scoped_git_commit.py``) still finds its
    key. Two distinct original strings that normalize to the same key both
    receive that key's claimant list.
    """
    base = _resolve_base(sessions_dir, cwd)
    if not base:
        return {path: [UNANSWERABLE] for path in paths}

    state = rebuild(sessions_dir=base)

    result: Dict[str, List[str]] = {}
    for path in paths:
        normalized = _normalize_key(path)
        claimants = state.claims.get(normalized)
        if claimants:
            result[path] = list(claimants)
        elif not state.complete:
            result[path] = [UNANSWERABLE]
        else:
            result[path] = []

    return result
