"""Enumerate and classify this repo's live peer sessions for the Group EM read pass.

PURPOSE. This is the in-plane port of the Group EM read pass: it lists the
other sessions running against this repo, classifies each with claude-klabauter's own
`coordinator_core.session.receiver_state.read_receiver_state` reader where a
stored record exists, falls back to the live in-engine peer roster's status
plus a bounded transcript-tail read otherwise, and returns a bounded
candidate roster for a human to look at. Ported from the DoE-claude sibling
repo's `coordinator/skills/group-em/read_pass.py` (read-only source, never
imported from; resolve via the machine-local repo registry, not a hardcoded
path) per `docs/plans/2026-08-30-group-em-entry-fires-one-warm-op.md` chunk
C1, layered over the already-live `session.peer_roster` read (chunk C6) in
place of a second `claude agents --json` enumeration. The one required
behaviour change from the source: the reader leg no longer imports
`coordinator.lib.receiver_state_reader` (a cross-plane import into
DoE-claude) — it calls claude-klabauter's own `read_receiver_state(sid, cwd)` instead.

ENUMERATION SOURCE (chunk C6). `fetch_live_agents` used to spawn `claude
agents --json` as a child process on every call — measured 856-1536ms wall,
one interpreter start, on the Group EM entry path (over the 500ms brightline
by 2-3x on its own). It now calls
`coordinator_core.session.peer_roster.build_roster(repo_root=...)`, which
reads `harness_registry.snapshot()` in-process: zero subprocesses, ~42ms
wall. `build_roster` already filters to sessions whose `cwd` is within
`repo_root` and already marks `is_self` -- `enumerate_repo_peers` no longer
re-does either check by hand over raw agent dicts; it only applies the
caller-exclusion this module still owns (`exclude_session_id`), so the
caller never appears in its own roster even when `is_self` resolution is
`"unresolved"`. `PeerRow.status` carries the same raw `busy`/`idle`
vocabulary the classification ladder below branches on --
`harness_registry.RegistryRecord.status` is parsed as a display-only,
string-or-None passthrough of the harness's own record file, the same
source `claude agents --json` itself reads, never re-derived or translated
here. A `build_roster` failure degrades to `[]` (its own documented
contract), never a subprocess fallback -- reintroducing the spawn to cover a
registry-read failure would restore the exact cost this chunk removes.

READER SHAPE DIFFERENCE (the substance of this port). The source's reader
returns a dict with an explicit `verdict` of `"UNAVAILABLE"` when it has no
record for a peer, distinguishing "checked, nothing there" from "checked,
found PAUSED/PRODUCING/UNKNOWN". Claude-Klabauter's `read_receiver_state` instead
returns `None` for that same "nothing there" case (absent, unreadable, or
malformed record) and a raw stored record — `{"verdict": ..., "reason": ...,
...}`, with `verdict` always a bare `"PAUSED"` / `"PRODUCING"` / `"UNKNOWN"`
tag (never a compound "PAUSED:turn-ended" spelling) — otherwise. This module
maps `None` to the fallback leg identically to how the source mapped
`UNAVAILABLE` to it: the two are the same outcome under different spellings,
not a behaviour change. Claude-Klabauter's reader also takes no `now` parameter;
nothing in the classification ladder read that value.

NEGATIVE SPEC -- what this module deliberately does NOT do:

- **No shouldn't-be adjudication.** The output is a read-only candidate list.
  Nothing here judges whether a paused peer "should" have kept moving.
- **No send, no nudge, no write to any peer's state.** This module only reads
  the live peer roster, the receiver-state reader, and (bounded) transcript
  tails. Auto-send/nudge is a separate module (`send_pass`, not this one).
- **No CPU-delta leg.** No CPU-band signal is read or used anywhere in this
  module.
- **No dependence on `state`/`waitingFor`.** Neither field is read anywhere
  in this module's classification path.
- **No caching or batching of the enumeration read.** `fetch_live_agents`
  re-invokes `peer_roster.build_roster` on every call.
- **The caller never appears in its own roster.** `enumerate_repo_peers`
  drops the caller's own `sessionId` before classification runs.
- **No whole-transcript reads.** The fallback leg's tail read goes through
  `receiver_state.reduce_transcript_tail`, which seeks from the end and
  reads a bounded byte window -- never the full file.

CLASSIFICATION LADDER:

- The reader is tried first, per peer. Only when it has no record for THIS
  peer (`read_receiver_state` returns `None`) does the fallback leg run at
  all -- deliberately per-peer, not gated once on any repo-wide check.
- A reader `PAUSED` verdict is cross-checked against this same read pass's
  own live `peer_roster.build_roster` status for the peer: `status == "busy"`
  contradicts it, so the peer is reported (state `PAUSED`, reason
  `live-busy-contradicts-paused`) but never a candidate.
- A reader `PAUSED` verdict is ALSO cross-checked on the `idle` side, against
  the reader record's own `stamped_at` write time
  (state/audits/2026-08-30-group-em-cooldown-vs-candidacy-window.md's
  observed failure: a peer offered mid-turn on a ~3.5-minute-stale PAUSED
  snapshot while the harness read `idle` -- `live_status == "busy"` alone
  never catches this). If the snapshot is older than
  `STALE_SNAPSHOT_SECONDS` -- or its age cannot be established at all, e.g. a
  missing/unparseable `stamped_at` -- the peer is reported (state `PAUSED`,
  reason `stale-snapshot-unresolved`/`stale-snapshot-contradicts-paused`) but
  never a candidate. FAIL CLOSED: an indeterminate staleness is treated the
  same as a stale one, never the same as a fresh one. `STALE_SNAPSHOT_SECONDS`
  is pinned at the measured **p50 (108s)** turn-ended dwell from the cooldown
  audit above, not p75/p90 -- the audit's own observed failure was ~210s
  stale, which sits ABOVE p50 but BELOW p75 (274s); a p75+ threshold would
  have missed the exact case this guard exists to close.
- `busy` on the fallback leg is mapped straight to `STATE_PRODUCING`, never a
  candidate -- no transcript read needed.
- `idle` is NOT terminal on the fallback leg: `receiver_state.reduce_transcript_tail`
  + `receiver_state.classify` are called on the peer's own transcript, and the
  returned `Verdict` is mapped onto this module's `{state, reason}` spelling
  (`classify_fallback_status`). `STATE_PAUSED` is the only candidate outcome on
  this leg; `STATE_PRODUCING` and `STATE_UNKNOWN` are not.
- Any other harness status string classifies straight to `STATE_UNKNOWN`.
- A reader `PRODUCING` verdict is cross-checked against its own `stamped_at`
  using the SAME evidence the idle-side PAUSED guard above already reads --
  `_transcript_moved_since` -- never a second comparison and never against
  `now` (defect 4, state/dispatch-briefs/2026-08-31-the-group-em-tick-
  carries-standing-obligations/C7.md): `write_receiver_state` has exactly one
  writer (the Stop hook), so a session that stops taking turns leaves this
  verdict frozen forever with nothing to refresh it. If the peer's own
  transcript has grown since `stamped_at`, the frozen PRODUCING verdict no
  longer describes the peer and resolves to `STATE_UNKNOWN` -- NEVER
  `STATE_PAUSED` (a stale PRODUCING is "could not classify", not "guessed
  idle"). Because plain `STATE_UNKNOWN` rows are invisible to
  `build_candidate_roster`, this case is additionally marked
  `unclassifiable: True` and surfaced in the roster payload (reason
  `stale-producing-unresolved`) rather than silently dropped -- the exact gap
  that left a stopped peer invisible to the Group EM for over an hour.

CLASSIFIER COLLAPSE (overengineering review finding 1, 2026-08-30): this
module used to carry its own second bounded transcript reader and classifier
(`transcript_path_for` + `read_transcript_tail` + `classify_transcript_tail`,
a 40-line/64KB window, a three-case hardcoded type check) duplicating
`receiver_state.reduce_transcript_tail` + `receiver_state.classify`, which
were already public, in-plane, and strictly more capable: a 64-line/256KB
window, and an open-vocabulary ALLOW-LIST (walk past any unrecognised line
type, including `atis-latch`) rather than a closed set of type checks. The
KNOWN-GAP `atis-latch` blindness this module used to carry (a peer's real
last-substantive line pushed out of the narrower 40-line window by a burst of
`atis-latch` control lines, `state/audits/2026-08-30-group-em-classifier-
blindness.md` § 2-3) is closed by this collapse, not worked around: the wider
window is what recovers the real line, since both classifiers already walk
past an unrecognised type rather than stopping on it. Deleted with it: the
duplicated `stop_reason`-on-`end_turn` case this module's own commit c78a4570
had hand-copied from `receiver_state.py`'s ladder step 4 instead of calling
it -- the fallback leg now gets that case (and `away`/`asking-human`/
`tool-unanswered`, which the local copy never had at all) for free from the
shared ladder.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from coordinator_core.session import peer_roster
from coordinator_core.session.receiver_state import activity_epoch_from_reduced
from coordinator_core.session.receiver_state import parse_iso_timestamp
from coordinator_core.session.receiver_state import classify as receiver_state_classify
from coordinator_core.session.receiver_state import read_receiver_state
from coordinator_core.session.receiver_state import reduce_transcript_tail

#: The peer is actively producing -- either `status == "busy"` directly, or
#: an `idle` peer whose transcript tail shows live conversational activity.
STATE_PRODUCING = "PRODUCING"

#: The wire spelling of a paused verdict, on both legs -- the reader leg
#: returns this directly; the fallback leg maps `receiver_state.classify`'s
#: `Verdict(verdict="PAUSED", ...)` onto it in `classify_fallback_status`.
STATE_PAUSED = "PAUSED"

#: Neither the reader, nor the status leg, nor the transcript tail could
#: place this peer. First-class and expected -- never a paused-like guess.
STATE_UNKNOWN = "UNKNOWN"

#: How old a reader-leg `stamped_at` snapshot may be, on the `idle` side,
#: before a PAUSED verdict it carries is no longer trusted as a candidate.
#: Pinned at the **p50 (108s)** turn-ended dwell measured in
#: state/audits/2026-08-30-group-em-cooldown-vs-candidacy-window.md over 222
#: live episodes -- deliberately NOT p75 (274s) or p90 (546s): the audit's
#: own observed failure (peer `30342983`, offered mid-turn on a snapshot
#: stamped ~210s earlier) sits above p50 but below p75, so a p75+ threshold
#: would not have caught the exact case this guard exists to close. A
#: snapshot older than the median lifetime of the state it reports is, more
#: likely than not, already stale.
STALE_SNAPSHOT_SECONDS = 108

_PATH_SEP_RE = re.compile(r"[/\\:]")


def _parse_iso_stamp(value: Any) -> Optional[datetime]:
    """Parse a `stamped_at`-shaped ISO8601 string to a tz-aware `datetime`.

    Returns `None` on anything that is not a non-empty, parseable ISO8601
    string -- missing field, wrong type, or malformed text. A naive result
    (no explicit offset) is assumed UTC. Never raises.

    Review: coordinator:overengineering-reviewer -- the body was a second
    implementation of `receiver_state.parse_iso_timestamp`, character for
    character. Kept as a name because this module's callers read `stamped_at`
    (a receiver-state field) rather than a transcript `timestamp`, and that
    distinction is worth a name; the PARSE is one implementation, over there,
    because this module already imports that one and not the reverse.
    """
    parsed = parse_iso_timestamp(value)
    return parsed


def _staleness_seconds(stamped_at: Any, now: Optional[datetime]) -> Optional[float]:
    """Seconds between `stamped_at` and `now`, or `None` when undeterminable.

    `None` covers: unparseable/missing `stamped_at`, or a negative delta
    (clock skew / a stamp claiming to be from the future) -- neither is
    trustworthy staleness evidence, so both fail closed to "unknown" rather
    than "fresh". Callers treat `None` the same as "stale" (see
    `STALE_SNAPSHOT_SECONDS`'s docstring) -- never the same as "known fresh".
    """
    stamp_dt = _parse_iso_stamp(stamped_at)
    if stamp_dt is None:
        return None
    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    delta = (reference - stamp_dt).total_seconds()
    if delta < 0:
        return None
    return delta


def caller_session_id(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """This session's own id, so it can be excluded from its own roster.

    Never resolved from `claude agents --json` itself -- that would require
    guessing which entry is "us" from shape alone. The harness exports it
    directly.
    """
    env = os.environ if env is None else env
    return env.get("CLAUDE_CODE_SESSION_ID")


def fetch_live_agents(repo_root: str) -> list[dict[str, Any]]:
    """Read the live, already-cwd-filtered peer roster fresh. Never cache.

    Sources `coordinator_core.session.peer_roster.build_roster(repo_root=...)`
    -- an in-process `harness_registry.snapshot()` read, zero subprocesses --
    in place of the former `claude agents --json` child-process spawn. Each
    `PeerRow` is mapped to the `{"sessionId", "status", "cwd", "name"}`-shaped
    dict this module's classification ladder already reads, so no downstream
    function needs to change its dict-key assumptions.

    `name` was added 2026-09-01 and is the reason a whole downstream feature
    read null. `PeerRow.name` is populated upstream; this projection dropped
    it, so `watch._holder_name` -- written specifically to put the crown's
    name on the heartbeat for a reader that cannot reach this box's registry
    -- could only ever return `None`, and `state/group-em-watch.json` read
    `holder_name: null` on every tick including ones its own watch stamped.
    Established by doe-claude-27. A projection that silently narrows its
    source is invisible to every caller downstream of it. Returns `[]` on any
    internal `build_roster` failure -- a read pass with no peers to show is a
    legitimate, quiet outcome, not a raised exception; `build_roster` itself
    already degrades to `[]` rather than raising, so no extra try/except is
    needed here.
    """
    rows = peer_roster.build_roster(repo_root=repo_root)
    return [
        {
            "sessionId": row.session_id,
            "status": row.status,
            "cwd": row.cwd,
            "name": row.name,
        }
        for row in rows
    ]


def enumerate_repo_peers(
    agents: list[dict[str, Any]],
    exclude_session_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Drop the caller's own entry from an already-repo-filtered roster.

    `agents` is expected to already be cwd-filtered to `repo_root` (as
    `fetch_live_agents` -> `peer_roster.build_roster` already does) -- this
    function no longer re-derives that filter itself. `exclude_session_id` is
    the only mechanism that removes an entry: there is no "shouldn't be here"
    inference beyond the caller's own exclusion.
    """
    peers = []
    for agent in agents:
        session_id = agent.get("sessionId")
        if exclude_session_id is not None and session_id == exclude_session_id:
            continue
        peers.append(agent)
    return peers


def _transcript_path_for(session_id: str, cwd: str) -> str:
    """The on-disk transcript path the harness itself writes to for a peer.

    Convention observed under `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`:
    every path separator and drive-letter colon in `cwd` is replaced with `-`.
    Not a public contract -- if this ever drifts, `receiver_state.reduce_transcript_tail`
    fails closed (missing file) to `[]`, never a crash. Private: nothing outside
    this module derives a peer transcript path from a bare session id, so this
    stays an internal helper rather than the small public surface the classifier
    collapse (overengineering review finding 1) retired.
    """
    projects_root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    encoded_cwd = _PATH_SEP_RE.sub("-", cwd)
    return os.path.join(projects_root, encoded_cwd, f"{session_id}.jsonl")


def _transcript_mtime_epoch(session_id: str, cwd: str) -> Optional[float]:
    """When this peer last wrote to its transcript, as an epoch float.

    The one place that turns a session id into a transcript mtime -- which is
    NOT this module's activity clock. Review:
    coordinator:overengineering-reviewer -- this docstring used to argue its
    shape from "two callers want that number for DIFFERENT questions", and
    both of those callers were rerouted through
    `transcript_activity_epoch` by the change that demoted mtime. What
    remains is one number with one job: the upper bound
    `transcript_activity_epoch` falls back to when no record in the tail
    carries a timestamp. Not evidence of activity, and never promoted to it.

    NEGATIVE SPEC: `None` means the mtime could not be established (absent or
    unreadable transcript), and it must never be read as "has not moved" or
    as an age of zero. Absence of evidence is not evidence of stillness --
    every caller here fails toward leaving its existing verdict standing.
    """
    try:
        return os.path.getmtime(_transcript_path_for(session_id, cwd))
    except OSError:
        return None


def transcript_activity_epoch(session_id: str, cwd: str) -> tuple[Optional[float], bool]:
    """PUBLIC BY NAME BECAUSE IT IS PUBLIC BY USE. `group_em.send_pass` and
    `group_em.watch` both call this, and both module docstrings name it as the
    ONE place a session id becomes a transcript activity instant -- the rule
    that stops a third clock site from appearing. It carried an underscore
    anyway, so the symbol said "private to this module" while two siblings
    depended on its signature and nothing pinned it; an edit here with no
    reason to look for foreign callers could have broken either silently.

    When this peer last actually MOVED, as `(epoch, trusted)`.

    `trusted` is True only when the number came from a record's own
    `timestamp`. The mtime fallback is returned with `trusted=False` and is an
    UPPER BOUND, never evidence: it is the newest moment the file could have
    been touched, including by a bookkeeping rewrite the session did not
    perform. Callers must not let an untrusted value win a comparison that
    makes a peer look busier -- see `send_pass._dwell_seconds`, where doing
    exactly that is what hid a 12-minute stall from four consecutive ticks.

    `(None, False)` means nothing could be established at all -- absent or
    unreadable transcript -- and, as with `_transcript_mtime_epoch`, must
    never be read as "has not moved" or as an age of zero.

    The clock this replaces, and the measurement that condemned it, are
    documented once at `receiver_state.activity_epoch_from_reduced`.
    """
    reduced, _unparseable, _cap = reduce_transcript_tail(_transcript_path_for(session_id, cwd))
    epoch = activity_epoch_from_reduced(reduced)
    if epoch is not None:
        return epoch, True
    return _transcript_mtime_epoch(session_id, cwd), False


def _transcript_moved_since(
    session_id: str, cwd: str, stamp_dt: Optional[datetime]
) -> Optional[bool]:
    """Has this peer written to its transcript since `stamp_dt`?

    The evidence question behind the idle-side stale-snapshot guard: an old
    snapshot is only misleading if the peer ACTED after it was written. A
    parked session's snapshot ages without the session moving; a mid-turn
    session's transcript keeps growing. Comparing the two separates the case
    the guard exists to catch from the case it was accidentally suppressing.

    Returns True (moved since), False (has not moved), or None when that
    cannot be established -- an unreadable/absent transcript, an unparseable
    stamp, or a transcript whose tail carries no timestamped record and whose
    mtime has run PAST the stamp. That last case used to answer True from file
    mtime, which is not an activity clock (`transcript_activity_epoch`): a
    bookkeeping rewrite moved mtime past the stamp and this reported "the peer
    acted" for a peer that had done nothing, suppressing exactly the candidate
    the guard was reinstating. An untrusted mtime that has NOT passed the
    stamp still answers False -- see the one-directional-bias note at that
    branch. `None` is never read as "has not moved": the caller
    leaves the age verdict standing, so this can only ever REINSTATE a
    candidate on positive evidence of stillness, never admit one on the
    absence of evidence.
    """
    if stamp_dt is None:
        return None
    activity_epoch, trusted = transcript_activity_epoch(session_id, cwd)
    if activity_epoch is None:
        return None
    if trusted:
        return activity_epoch > stamp_dt.timestamp()
    # Review: coordinator:code-reviewer (P2) -- the untrusted (mtime) fallback
    # is barred from answering "moved", never from answering "has not moved".
    # The bias is ONE-DIRECTIONAL: a bookkeeping rewrite can only push mtime
    # forward, and any real write is at or before it, so `mtime <= stamp`
    # bounds the peer's true last activity at or before the stamp too -- real
    # evidence of stillness, and the reinstatement case a blanket `None` here
    # silently gave up. Only the forward direction is unsafe, because that is
    # the one a rewrite can manufacture.
    if activity_epoch <= stamp_dt.timestamp():
        return False
    return None


def classify_fallback_status(
    status: Any,
    reduced_lines: Optional[list] = None,
    *,
    now_epoch: Optional[float] = None,
    transcript_activity_epoch: Optional[float] = None,
) -> tuple[str, str]:
    """Map a raw harness `status` string (plus, for `idle`, the peer's already-reduced
    transcript tail) to the fallback ladder's `(state, reason)`.

    The `idle` arm calls `receiver_state.classify` directly on `reduced_lines`
    (a list of `receiver_state._ReducedLine`, from `receiver_state.reduce_transcript_tail`)
    rather than carrying a second classifier -- see the module docstring's CLASSIFIER
    COLLAPSE note. `delegation_evidence` is always passed `False`: this module has no
    delegation signal to offer and none of its own negative spec (no CPU-delta leg, no
    `state`/`waitingFor` dependence) changes by declining to guess one.
    """
    if status == "busy":
        return STATE_PRODUCING, "status-busy"
    if status == "idle":
        verdict = receiver_state_classify(
            reduced_lines or [],
            now_epoch=now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp(),
            transcript_activity_epoch=transcript_activity_epoch,
            delegation_evidence=False,
        )
        if verdict.verdict == "PRODUCING":
            return STATE_PRODUCING, f"tail-{verdict.reason}"
        if verdict.verdict == "PAUSED":
            return STATE_PAUSED, f"tail-{verdict.reason}"
        return STATE_UNKNOWN, "tail-unresolved"
    return STATE_UNKNOWN, "unrecognized-status"


def classify_peer(
    repo_root: str,
    peer: dict[str, Any],
    now: Optional[datetime] = None,
    read_tail: Optional[Callable[[str, str], list]] = None,
) -> dict[str, Any]:
    """One peer in, one verdict out -- the preference check itself.

    Calls the receiver-state reader first. Only when that reader has no
    stored record for THIS peer (`read_receiver_state` returns `None` --
    the in-plane equivalent of the source's `UNAVAILABLE` verdict) does this
    fall back to the `peer_roster.build_roster` status leg -- deliberately
    per-peer, not gated once on any repo-wide check.

    `now` is not passed to `read_receiver_state` -- the raw stored-record
    reader takes no staleness parameter -- but IS used by this function
    itself (test-injection point) to evaluate the reader record's own
    `stamped_at` against `STALE_SNAPSHOT_SECONDS` on the `idle` cross-check
    below (module docstring's mid-turn/idle-side note). `None` (the default)
    resolves to `datetime.now(timezone.utc)` at evaluation time.

    A peer with no `sessionId` at all cannot be keyed against either the
    reader or a transcript, and is `UNKNOWN` rather than reaching either.

    `read_tail` is the fallback leg's test-injection seam: `(session_id, cwd) ->
    reduced_lines`, standing in for `receiver_state.reduce_transcript_tail`
    (module docstring's CLASSIFIER COLLAPSE note) without touching disk in a
    test.
    """
    session_id = peer.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return {
            "session_id": session_id,
            "source": "fallback",
            "state": STATE_UNKNOWN,
            "reason": "no-session-id",
            "candidate": False,
            "unclassifiable": False,
            # Review: coordinator:code-reviewer (finding 1) -- `cwd` threaded
            # onto every verdict row so `send_pass._dwell_seconds` can use the
            # peer's own cwd (`peer.get("cwd") or repo_root`), matching this
            # module's own pattern, instead of always assuming `repo_root`.
            "cwd": peer.get("cwd"),
        }

    reader_record = read_receiver_state(session_id, repo_root)
    if reader_record is not None:
        reader_verdict = reader_record.get("verdict")
        reader_reason = reader_record.get("reason")
        live_status = peer.get("status")

        live_busy_contradicts = reader_verdict == STATE_PAUSED and live_status == "busy"

        # Idle-side close (defect B): a stale PAUSED snapshot with harness
        # `idle` is exactly the failure mode `live_busy_contradicts` cannot
        # see -- the audit's peer 30342983 was mid-turn while the harness read
        # `idle`, so live status is NOT trustworthy corroboration here.
        # Staleness is measured against the reader record's OWN write time
        # (`stamped_at`) -- never a status field -- and an indeterminate
        # staleness fails CLOSED (never a candidate), same as an
        # over-threshold one. See `STALE_SNAPSHOT_SECONDS`'s docstring for the
        # p50 pin.
        #
        # AGE ALONE IS NOT THE QUESTION (2026-08-30). Age was standing in for
        # "has this peer done anything since the snapshot?", and it answers
        # that question wrongly in one direction: `receiver-state.json` is
        # written by the peer's Stop hook at turn end, so a genuinely PARKED
        # peer's snapshot does nothing but age. Past 108s every such peer was
        # disqualified permanently, and the longer one sat stuck the more
        # certain the roster was to hide it -- measured live on this box, the
        # roster oscillated 0 -> 3 -> 0 across consecutive ticks and read
        # empty while five peers sat idle, one blocked for hours on a gate
        # only the Group EM could clear.
        #
        # The transcript answers it directly. A peer that has written nothing
        # since its snapshot has not moved, however old the snapshot is; a
        # peer whose transcript is NEWER than its snapshot has acted since,
        # which is the mid-turn case defect B exists to catch and catches it
        # on evidence rather than on elapsed time. Fail-closed is preserved
        # end to end: an unreadable transcript mtime leaves the age verdict
        # standing, and an unresolvable `stamped_at` is still never a
        # candidate.
        staleness = None
        stale_idle_contradicts = False
        if reader_verdict == STATE_PAUSED and live_status == "idle":
            staleness = _staleness_seconds(reader_record.get("stamped_at"), now)
            stale_idle_contradicts = staleness is None or staleness > STALE_SNAPSHOT_SECONDS
            if stale_idle_contradicts and staleness is not None:
                stamp_dt = _parse_iso_stamp(reader_record.get("stamped_at"))
                moved = _transcript_moved_since(
                    session_id, peer.get("cwd") or repo_root, stamp_dt
                )
                # `None` = could not establish; leave the age verdict standing.
                if moved is False:
                    stale_idle_contradicts = False

        contradicted = live_busy_contradicts or stale_idle_contradicts
        if live_busy_contradicts:
            reason = "live-busy-contradicts-paused"
        elif stale_idle_contradicts:
            reason = (
                "stale-snapshot-unresolved"
                if staleness is None
                else "stale-snapshot-contradicts-paused"
            )
        else:
            reason = reader_reason

        # Defect 4 close: a frozen PRODUCING verdict (typically the step-7
        # delegation override, `classify`'s "delegated (overrides ...)"
        # reason) is never rewritten once the session stops taking turns --
        # `write_receiver_state` has exactly one writer, the Stop hook, and a
        # stopped session never fires it again. Freshness is knowable from
        # the SAME evidence bd96b64b already reads for the idle-side PAUSED
        # guard above (`_transcript_moved_since`: has the peer's own
        # transcript grown since `stamped_at`?) -- reused verbatim, not a
        # second comparison, and never against `now` (a clock threshold is
        # exactly what bd96b64b replaced). A transcript that kept moving
        # after the verdict was frozen is positive evidence the frozen
        # PRODUCING snapshot no longer describes the peer's current state.
        #
        # NEGATIVE SPEC: this must resolve to UNKNOWN, never PAUSED -- a
        # stale PRODUCING is "could not classify", not "guessed idle". And
        # UNKNOWN alone is invisible to `build_candidate_roster` (only
        # `candidate: True` rows survive), so this is surfaced as an
        # explicit `unclassifiable: True` row instead of a silent drop --
        # the Group EM is told "no verdict, and why" rather than nothing.
        if reader_verdict == STATE_PRODUCING:
            stamp_dt = _parse_iso_stamp(reader_record.get("stamped_at"))
            moved = _transcript_moved_since(
                session_id, peer.get("cwd") or repo_root, stamp_dt
            )
            if moved is True:
                return {
                    "session_id": session_id,
                    "source": "reader",
                    "state": STATE_UNKNOWN,
                    "reason": (
                        "stale-producing-unresolved (frozen "
                        f"{reader_reason!r} at stamped_at="
                        f"{reader_record.get('stamped_at')!r}, transcript "
                        "active since)"
                    ),
                    "candidate": False,
                    "unclassifiable": True,
                    "cwd": peer.get("cwd"),
                }

        return {
            "session_id": session_id,
            "source": "reader",
            "state": reader_verdict,
            "reason": reason,
            "candidate": reader_verdict == STATE_PAUSED and not contradicted,
            "unclassifiable": False,
            "cwd": peer.get("cwd"),
        }

    status = peer.get("status")
    reduced_lines: list = []
    transcript_activity_epoch: Optional[float] = None
    if status == "idle":
        cwd = peer.get("cwd") or repo_root
        if read_tail is not None:
            reduced_lines = read_tail(session_id, cwd)
        else:
            transcript_path = _transcript_path_for(session_id, cwd)
            reduced_lines, _any_unparseable, _cap_reached = reduce_transcript_tail(transcript_path)
        # Derived from the tail ALREADY reduced above -- no second read, and
        # no `transcript_activity_epoch` call, which would re-reduce the same
        # file. Falls back to mtime only when no line in that tail carries a
        # timestamp, which is the same upper-bound-not-evidence fallback
        # `transcript_activity_epoch` makes, kept identical on both paths.
        transcript_activity_epoch = activity_epoch_from_reduced(list(reduced_lines or []))
        if transcript_activity_epoch is None and read_tail is None:
            transcript_activity_epoch = _transcript_mtime_epoch(session_id, cwd)

    now_epoch = (now if now is not None else datetime.now(timezone.utc)).timestamp()
    state, reason = classify_fallback_status(
        status,
        reduced_lines,
        now_epoch=now_epoch,
        transcript_activity_epoch=transcript_activity_epoch,
    )

    return {
        "session_id": session_id,
        "source": "fallback",
        "state": state,
        "reason": reason,
        "candidate": state == STATE_PAUSED,
        "unclassifiable": False,
        "cwd": peer.get("cwd"),
        # Review: coordinator:code-reviewer (P2, double read) -- this leg
        # already reduced the tail and derived the epoch; threading it onto the
        # verdict (the same way `cwd` is) lets `watch._parked_line` report the
        # peer's idle time without re-reducing the same file in the same tick.
        # `None` on the reader leg, which never reduced a tail to reuse -- the
        # consumer falls back to reading, which is then a FIRST read, not a
        # second.
        "activity_epoch": transcript_activity_epoch,
    }


def build_candidate_roster(
    repo_root: str,
    agents: Optional[list[dict[str, Any]]] = None,
    caller_session_id_value: Optional[str] = None,
    now: Optional[datetime] = None,
    read_tail: Optional[Callable[[str, str], list[str]]] = None,
) -> list[dict[str, Any]]:
    """Present the bounded candidate population, plus any peer this pass
    could not classify at all.

    Read-only end to end: enumerates via `fetch_live_agents` (or the injected
    `agents`, for tests), classifies each surviving peer, and returns the
    verdicts marked `candidate` UNION the verdicts marked `unclassifiable`
    (defect 4: a stale, frozen PRODUCING reader verdict -- see
    `classify_peer`'s reader leg). A plain `STATE_PRODUCING` or
    `STATE_UNKNOWN` peer that classify_peer could NOT further distinguish is
    still never included here, and the two rows are never folded into each
    other -- callers branch on `candidate` to decide nudge eligibility and on
    `unclassifiable` to decide whether "no row for this peer" would have been
    a silent drop rather than a genuine all-clear. This is a candidate list
    for a human to adjudicate, not a filtered verdict about who "shouldn't"
    be paused.
    """
    if agents is None:
        agents = fetch_live_agents(repo_root)
    if caller_session_id_value is None:
        caller_session_id_value = caller_session_id()

    peers = enumerate_repo_peers(agents, caller_session_id_value)
    verdicts = [
        classify_peer(repo_root, peer, now=now, read_tail=read_tail) for peer in peers
    ]
    return [
        verdict
        for verdict in verdicts
        if verdict["candidate"] or verdict.get("unclassifiable")
    ]
