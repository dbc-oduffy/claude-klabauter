"""coordinator_core.group_em.watch -- the standing watch (chunk C2, plan
`docs/plans/2026-08-31-the-group-em-tick-carries-standing-obligations.md`).

PURPOSE. A naked-Python (3.11+) runnable the Group EM session arms once with
the harness `Monitor` tool. Once armed it runs for the life of the session
(`persistent: true`, replacing `notify_when_idle` rather than re-arming it --
see the C2 dispatch brief's "HOW WITHOUT OPERATOR MEMORY IS ACTUALLY
DISCHARGED" section) and emits one stdout line per PARKED transition. Each
stdout line becomes a `Monitor` notification, so the filter IS the contract:
this module emits only what the Group EM would act on.

WHY THIS EXISTS. `/group-em` fires `groupem.enter` once and stops -- nothing
re-fires without the operator remembering to look again, the exact shape the
north star names as unfinished work. This module is the artifact that
discharges "the EM does not have to remember": armed once, it never decays,
because it never disarms (a `Monitor` runs until `TaskStop` or session end;
nothing here re-arms it, because nothing here needs to).

SOURCE OF TRUTH: `coordinator_core.session.harness_registry.snapshot()` via
`read_pass.fetch_live_agents` -> `peer_roster.build_roster` -- an in-process,
zero-subprocess read (measured 4.6ms for 25 `sessions/*.json` records; see
this module's own `_measure_snapshot_ms`, which re-measures at arm time
rather than trusting that number cold). Never `claude agents --json`
(measured 820ms plus one process spawn for the same answer).

PARKED IS DERIVED, NEVER READ OFF REGISTRY `status`. This module reuses
`read_pass.classify_peer` verbatim as the parked predicate -- the full
reader-then-fallback ladder, including the reader's stale-snapshot and
stale-PRODUCING cross-checks, and the fallback leg's `receiver_state.classify`
tail-type consult (an `assistant`+`tool_use` or `user`+`tool_result` tail is
NOT parked, however long idle; only an `assistant`+`text` tail can be). This
module adds no second classifier and no elapsed-time predicate of its own --
see `docs/wiki` and the C2 brief for why neither `busy/idle` alone nor
transcript-idleness alone is sufficient, and why raising a threshold only
trades one false-positive class for a worse miss.

TRANSITIONS, the pure function this module's tests centre on:
  - not-parked -> parked (peer known on the PRIOR tick, unparked, now parked)
    => emit a PARKED line.
  - parked -> parked        => emit nothing (the firehose that gets a
    `Monitor` auto-stopped, silently returning the mode to no watch at all).
  - a peer absent from the PRIOR tick (a spawn) that already reads parked on
    its first sighting => emit nothing. `transitions` declines it by
    requiring membership in BOTH snapshots.
  - a peer present on the PRIOR tick and absent now => emit a GONE line
    (`gone`, the second pure function, added 2026-09-01 -- see below).

GONE IS THE EVENT A ROSTER CANNOT REPORT ABOUT ITSELF. Until 2026-09-01 an
exit emitted nothing here, deferred to `baseline.diff_and_persist`'s
`exited` list -- which is real, and is reached from `ops/group_em_enter`
only: the once-per-`/group-em` entry op, never the standing watch's tick
path. So on the surface that ticks, a session's disappearance was detected
by exactly one mechanism: failing to send to it. Measured twice on
2026-09-01 in this repo -- `claude-klabauter-c7` was listed by `ListAgents`
and refused a `SendMessage` seconds later, and `claude-klabauter-3e` vanished
mid-workstream with nothing announced -- both costing a peer a lost message
rather than a roster row. A crown's roster silently retains peers that no
longer exist, and the honest failure only arrives for whoever happens to be
sending.

Unlike PARKED, GONE is terminal and self-limiting: the session id leaves
`cur_parked` the tick it is reported and is absent from the NEXT tick's
prior map, so it can never repeat for the same disappearance. It therefore
takes no cooldown gate -- the send cooldown suppresses an OFFER to nudge a
stopped peer, and there is nothing to nudge.

WHAT GONE DOES NOT CLAIM. Absence from this roster is absence from
`build_roster(repo_root=...)` -- a registry row this repo's cwd filter
kept. A session that ended, one that `/cd`'d out of the repo, and one whose
record was rewritten all read identical here, so the line says "absent from
this repo's roster", never "the session is dead". The one thing it does
assert is the actionable half: that name will not resolve for a send.

A BROKEN READ MUST NOT REPORT THE WHOLE FLEET GONE. `fetch_live_agents`
degrades an unreadable registry to `[]`, which a differ reads as a
simultaneous mass exit -- the single worst false positive this line can
produce, and it fires exactly when the box is least healthy. `_current_agents`
therefore reads with `raise_on_failure=True` AND `raise_on_empty_snapshot=True`:
a failed registry read raises, becomes a POLL-ERROR line, and leaves the prior
map unwritten, so the next tick diffs against the last GOOD roster rather than
against a hole.

THE SECOND FLAG IS THE ONE THAT FIRES; why it and not `raise_on_failure`
alone: `peer_roster.EmptySnapshotError`, the fact's home.

AND A BLIND TICK MUST NOT STAMP A HEARTBEAT -- the same defect one level up,
and the more dangerous half: a failed read published as a coverage figure
says "all well" and retires the suspicion that would otherwise have caught
it. Full incident (`example-game-workbench-repo-95`, 22 minutes, `peers: 0` while
`ListAgents` showed 36): `peer_roster.EmptySnapshotError`.

THE STRUCTURE THAT PREVENTS IT IS LOAD-BEARING AND MUST NOT BE TIDIED. In
both entry points the `watch_heartbeat.stamp` call sits INSIDE the `try`
that `poll_once` raises out of, so a tick that could not read stamps nothing
at all: the record ages, and `--status` answers STALE rather than a
confident zero. That placement predates this note and was accidental; it is
now intent. Do not hoist the stamp out of the try, do not add an
`except`-branch stamp, and do not stamp a "degraded" tick with a zero count
-- each of those turns a blind tick back into a published coverage figure.
Pinned by `test_a_blind_tick_stamps_no_heartbeat_and_keeps_the_last_good_prior`,
which asserts both halves: no record written, and the carried prior map
still holding the last GOOD roster.

THE WATCH MUST NOT RE-FLAG AN ANSWERED PEER. Before emitting, this module
checks `send_pass`'s own per-peer offer cooldown (`read_send_log` +
`_cooldown_remaining`, the SAME clock `build_send_digest` arms on every
offer) and stays silent while it is armed -- not a second mechanism, not an
operator-maintained mute list (which would drift the moment a peer's
situation changed): a peer answered on either path is answered on both, and
the cooldown expires on its own.

CONCURRENT `--once` WAKES ARE NOT LOCKED. `load_prev_parked`/`save_prev_parked`
are two separate unlocked I/O ops; `watch_heartbeat.write_atomic`'s
temp-then-`os.replace` only makes each individual write atomic, not the
read-modify-write pair across them. Two `--once` ticks racing against the
same `repo_root` can both load the same stale prior map, both independently
compute the same transition, and both emit the same PARKED line before
either send suppresses it -- `_cooldown_active` reads `send_pass`'s offer
log, written only once an offer is actually sent, not by this module on
line emission. Last writer of `save_prev_parked` wins and silently discards
the other tick's map update, but that content is re-derivable next tick, so
this is a duplicate notification, never a wrong `poll_once` decision. No
lockfile is added here; the dedup burden is named explicitly as the
Group-EM's send-cooldown's job, not this module's writer's.
(Review: coordinator:code-reviewer.a04f2c7f6c502b313, P2.)

THE PARKED LINE STATES WHAT WAS OBSERVED AND ASKS -- IT NEVER ASSERTS. A
verdict here can never buy certainty (a peer that ended its turn awaiting its
own long-running async work looks identical, on every available signal, to
one that is genuinely stuck), so the line carries its evidence -- the
verdict reason, the reader snapshot's age (when available), and the
peer's own transcript idle time (when available) -- each named, and frames
the whole thing as observation, not conclusion. An unreadable age or idle
time is rendered `unknown`, never invented and never promoted to evidence of
parking.

OBLIGATION NAMES, not a count (`obligations.for_peer`, chunk C1). `None`
(no ledger) is annotated literally `no ledger` -- absence of evidence is not
evidence the peer is fine, the precise error this plan's Problem section
records. `[]` (a ledger with nothing currently owed) is annotated `none`.
Otherwise each record's `obligation_id` (falling back to `next_action`, then
a bare `"obligation"` placeholder) is joined, comma-separated.

COVERAGE: a poll that raises emits a `POLL-ERROR` line and continues --
never dies silently. A watcher that exits without a trace is
indistinguishable from a quiet repo, the exact false-green class this plan's
predecessor session was caught by repeatedly.

DO NOT ADD A THIRD TRANSCRIPT-CLOCK SITE. `read_pass.transcript_activity_epoch`
is the one place a session id becomes a last-activity instant; this module
calls it and does not add its own `getmtime`.

NEGATIVE SPEC -- what this module deliberately does not do:

- No re-arming step, no cron, no second entry point. The watcher is armed
  once, by the session, with the harness `Monitor` tool -- nothing in this
  module fires itself, and nothing here tells a future session to remember
  to arm it (that prose instruction is the defect this chunk replaces).
- No CPU-delta leg, no `state`/`waitingFor` read, no shouldn't-be
  adjudication -- same negative spec `read_pass`/`send_pass` already carry.
- No send, no nudge, no write to any peer's state. This module only reads
  the registry, the receiver-state reader, transcript tails, the ledger, and
  the send log's cooldown. It writes exactly two things: stdout, and this
  repo's own `state/group-em-watch.json` presence stamp (`watch_heartbeat`)
  -- a record ABOUT the watch, addressed to no peer, never a peer's state.
- No per-peer entry point beyond `main`'s own loop; `transitions` and `gone`
  are pure functions over two already-computed maps, never over raw agents.
- No liveness probe, no send, and no `harness_registry.status` read behind
  GONE. The absence itself is the whole signal; `status` is banned as a
  liveness input by ratified ruling (`session/harness_registry.py`) and
  nothing here consults it.
- No mass-arrival (spawn) sibling to GONE -- `transitions` requires
  membership in both maps and `gone` only reads `prev`, so there is nothing
  to guard rather than a guard that was skipped. A spawn/NEW line added here
  inherits the empty-snapshot problem in the opposite sign and must set the
  same refusals `_current_agents` sets.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, TextIO

from coordinator_core.group_em import obligations
from coordinator_core.group_em import read_pass
from coordinator_core.group_em import repo_root_arg
from coordinator_core.group_em import send_pass
from coordinator_core.group_em import watch_heartbeat
from coordinator_core.group_em import watch_spool
from coordinator_core.session.receiver_state import read_receiver_state

#: Keep the watcher's own duty cycle far under the load norm's 200ms-needs-
#: a-fix line: the poll interval is 1000x the MEASURED `snapshot()` cost
#: (arm-time, this box), so the watcher spends well under 0.1% of wall time
#: reading the registry. Floored at 5s so a near-zero measured cost (the
#: registry read is a handful of small JSON files) never tightens the loop
#: toward a busy-poll -- 5s is itself well inside the load norm for a
#: zero-subprocess, in-process read. `main` prints the resulting interval on
#: `ARMED`, never a chosen round number standing alone.
_POLL_INTERVAL_FLOOR_SECONDS = 5.0
_POLL_INTERVAL_MEASURED_MULTIPLIER = 1000.0

# Review: coordinatorcode-reviewer.a9e1410288878bea9 -- `_poll_interval_seconds`
# is measured exactly once, at arm time, and reused unchanged for the rest of
# the session. A single transient arm-time spike (disk contention, the box
# momentarily at 50-70 concurrent sessions) can commit the watch to an
# unreasonably long cadence with no correction for the rest of the session --
# a measurement taken once cannot be trusted to be representative forever.
# This ceiling bounds how far one bad sample can push the interval; it does
# NOT introduce periodic re-measurement inside the loop (that would be a
# design change to the backoff, considered and deliberately not done here).
_POLL_INTERVAL_CEILING_SECONDS = 300.0

#: `send_pass.build_send_digest`'s own default -- the watch reuses the SAME
#: clock rather than a second cooldown window, per the module docstring.
_COOLDOWN_SECONDS = send_pass.DEFAULT_COOLDOWN_SECONDS

#: Where the memo inbox lives, relative to a repo root -- the same tree
#: `cross-repo-memo` delivers into and `records_query` reads.
_INBOX_RELATIVE_PATH = os.path.join("cross-repo", "inbox")

#: Measured (C6 brief): 145 files, `os.scandir` 0.16ms median / 0.34ms max,
#: reading the first 14 lines of every file 9.4ms total. Frontmatter's
#: `status:` key is always inside the first 14 lines of every memo this
#: repo has ever written; 14 is a generous margin over the observed max
#: (title/from/to/created/status is 5 lines in), not a tight fit.
_INBOX_FRONTMATTER_HEAD_LINES = 14

#: The one status value that counts as OPEN for this count. Every other
#: value (`actioned`, `delivered`, `draft`, `draft-awaiting-pm-relay`,
#: `superseded`) is not a pending item this instrument reports on.
_INBOX_OPEN_STATUS = "open"


def _inbox_frontmatter_status(path: str) -> Optional[str]:
    """The `status:` value from a memo's frontmatter head, or `None`.

    Reads only the first `_INBOX_FRONTMATTER_HEAD_LINES` lines -- a full
    parse is not needed for one scalar key, and this repo's own memos never
    put `status:` past line 5. `None` on any read failure or absent key:
    an unreadable memo is not an open one, but it is also not silently
    dropped from `total_count` -- the caller counts the file either way.
    """
    # Review: coordinatorcode-reviewer (finding #2) -- UnicodeDecodeError is a
    # ValueError subclass, not OSError; an undecodable memo must degrade to
    # None per this function's own contract, not propagate through
    # `_inbox_counts`'s uncaught per-entry call and abort the poll tick.
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= _INBOX_FRONTMATTER_HEAD_LINES:
                    break
                stripped = line.strip()
                if stripped.startswith("status:"):
                    return stripped[len("status:"):].strip().strip("'\"")
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _inbox_counts(repo_root: str) -> tuple[int, int, float]:
    """`(open_count, total_count, taken_at_epoch)` for `repo_root`'s inbox.

    A COUNT WITHOUT THE INSTANT IT WAS TAKEN IS THE DEFECT, NOT A NICETY
    (C6 brief) -- two correct readings minutes apart cost real
    reconciliation time when neither carries when it was struck. The
    instant is read at the START of the scan, before either count is
    known, so a caller that logs it alongside the counts is dating the
    read, not the report.

    An absent or unreadable inbox directory answers `(0, 0, taken_at)`
    rather than raising -- the same posture as `load_prev_parked`'s
    absent-file answer: a poll that has not yet seen an inbox is not a
    poll error.
    """
    taken_at_epoch = time.time()
    inbox_dir = os.path.join(str(repo_root), _INBOX_RELATIVE_PATH)
    total_count = 0
    open_count = 0
    try:
        with os.scandir(inbox_dir) as it:
            entries = [e.path for e in it if e.is_file() and e.name.endswith(".md")]
    except OSError:
        return 0, 0, taken_at_epoch
    total_count = len(entries)
    for entry_path in entries:
        if _inbox_frontmatter_status(entry_path) == _INBOX_OPEN_STATUS:
            open_count += 1
    return open_count, total_count, taken_at_epoch


def _inbox_line(open_count: int, total_count: int, taken_at_epoch: float) -> str:
    """One INBOX line: count + population name + struck instant, spelled
    `counts_struck_at`, not a bespoke `taken_at` (C6 brief, C5's ownership).

    Review: overengineering-reviewer finding 1 (ACCEPTED) -- `render_struck_count`
    is inlined here, its one remaining production consumer. The helper existed to
    stop three surfaces spelling count+population+instant three ways; DoE-claude's
    contract ruling took `summary_line` off it and the ARMED line spells its own
    divergent format, so the unification premise no longer held and the helper was
    surviving on the finding that created it.
    """
    population = f"inbox memos open, of {total_count} total"
    return (
        f"INBOX {open_count} ({population}) "
        f"counts_struck_at={watch_heartbeat.iso_instant(taken_at_epoch)}"
    )


#: The carried parked map, next to the heartbeat record it accompanies. A held
#: poll loop keeps `prev_parked` in memory for the life of the session; a
#: single-tick wake (`--once`, `tick_once`) has no memory at all, and a
#: transition is a DIFF -- with no prior tick to diff against, every wake sees
#: either an empty prior (flagging nothing, since `transitions` requires
#: membership in both) or the whole roster (flagging everyone). Neither is the
#: answer, so the prior tick is written down.
_PARKED_STATE_RELATIVE_PATH = os.path.join("state", "group-em-watch-parked.json")

#: What `--once` promises the reader about the NEXT wake, when its caller does
#: not say. The Group-EM's cron floor is ~23 minutes (the group-em entry
#: sequence's own cadence), so a wake that named the poll loop's few-second
#: interval instead would stamp a deadline it cannot meet and read STALE to
#: every other session within the minute -- the watch reporting itself absent
#: while working correctly.
_CRON_FLOOR_INTERVAL_SECONDS = 23 * 60.0

#: `watch_heartbeat._STAMP_FORMAT`, matched deliberately: a reader comparing a
#: GONE line's `last_seen` against the heartbeat record's `last_tick_at`
#: should not have to reconcile two renderings of the same instant.
_GONE_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

def _measure_snapshot_ms(repo_root: str) -> tuple[float, list]:
    """Time one `fetch_live_agents` call on THIS box; return (ms, agents).

    Re-measures rather than trusting the plan's cited 4.6ms cold -- that
    number was measured on a different box, on a different tick, and this
    module's poll interval is derived from ITS OWN measurement so the
    denominator printed on `ARMED` is always this box's own evidence.

    Returns the enumeration itself, not just its length. The arm sequence
    needs one more fact out of it -- the Group-EM's own display name, for the
    heartbeat's self-description leg -- and re-reading the registry to get a
    string this call already held would bill the box twice for one answer.
    """
    started = time.monotonic()
    agents = read_pass.fetch_live_agents(repo_root)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    return elapsed_ms, list(agents)


def _holder_name(agents: list, session_id: Optional[str]) -> Optional[str]:
    """The Group-EM's display name off an enumeration already in hand, or None.

    Resolved ONCE, at arm time. Never per tick: a name on the heartbeat is
    self-description for a reader that cannot reach this box's registry, and
    paying a registry read every tick to keep a string fresh would put the
    load norm's cost on the cheapest thing the watch does.
    """
    if not session_id:
        return None
    for agent in agents:
        if isinstance(agent, dict) and agent.get("sessionId") == session_id:
            name = agent.get("name")
            return name if isinstance(name, str) and name else None
    return None


def _poll_interval_seconds(snapshot_ms: float) -> float:
    """Derive the poll interval from this tick's measured `snapshot_ms`.

    See the module-level constants' docstring for the derivation. Never a
    chosen round number standing alone -- it is always a function of a
    measurement taken this call.
    """
    derived = (snapshot_ms / 1000.0) * _POLL_INTERVAL_MEASURED_MULTIPLIER
    bounded = max(_POLL_INTERVAL_FLOOR_SECONDS, derived)
    return min(_POLL_INTERVAL_CEILING_SECONDS, bounded)


def _current_agents(
    repo_root: str,
    caller_session_id: Optional[str],
    group_em_session_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """This tick's repo-filtered peer set, with the watch's own side excluded.

    Sourced from `read_pass.fetch_live_agents` (-> `peer_roster.build_roster`,
    already case-folded `cwd` containment -- see that module's `_normalize_path`)
    and `read_pass.enumerate_repo_peers` (exclusion by session id, never by
    name). No second enumeration and no second cwd filter is built here.

    TWO IDS, BECAUSE THE WATCH CAN BE HELD BY A TEAMMATE. When a Group-EM
    dispatches a watcher rather than holding the poller in its own session,
    the roster must drop BOTH: the watcher (a session sitting in a `Monitor`
    poll presents exactly like a parked peer, so a single-id exclusion has it
    flagging itself) and the Group-EM (which is the recipient of every line this
    watch emits -- reporting the Group-EM to the Group-EM is noise by construction).
    `enumerate_repo_peers` excludes one id per call, so it is called twice
    rather than gaining a second parameter it does not otherwise need.

    READ WITH BOTH REFUSALS SET, and they are load-bearing for GONE --
    module docstring's "A BROKEN READ MUST NOT REPORT THE WHOLE FLEET GONE".
    The default degrade answers `[]` for an unreadable registry, which is
    indistinguishable from an empty repo to everything downstream; a differ
    turns that one bad read into a line per peer.

    `raise_on_empty_snapshot` is the one that actually fires; both refusals
    set, why the second is the one that fires: `peer_roster.EmptySnapshotError`.
    """
    agents = read_pass.fetch_live_agents(
        repo_root, raise_on_failure=True, raise_on_empty_snapshot=True
    )
    peers = read_pass.enumerate_repo_peers(agents, caller_session_id)
    if group_em_session_id is not None and group_em_session_id != caller_session_id:
        peers = read_pass.enumerate_repo_peers(peers, group_em_session_id)
    return peers


def _classify_all(
    repo_root: str,
    agents: Iterable[dict[str, Any]],
    now: Optional[datetime] = None,
) -> dict[str, dict[str, Any]]:
    """`{session_id: classify_peer(...) verdict}` for every peer with a usable id.

    One call per peer into `read_pass.classify_peer` -- the shared ladder,
    never a second one. A peer with no usable `sessionId` cannot be keyed and
    is dropped from the map entirely (it can never transition, so it can
    never need a line).
    """
    verdicts: dict[str, dict[str, Any]] = {}
    for peer in agents:
        session_id = peer.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            continue
        verdicts[session_id] = read_pass.classify_peer(repo_root, peer, now=now)
    return verdicts


def transitions(prev: dict[str, bool], cur: dict[str, bool]) -> list[str]:
    """Peers that moved not-parked -> parked this tick, sorted for determinism.

    Membership in BOTH maps is required -- a peer absent from `prev` (a
    spawn, including one born already parked) or absent from `cur` (an exit)
    never appears here, matching the module docstring's transition table.
    `parked -> parked` (both `True`) and `not-parked -> not-parked` (both
    `False`) are excluded by the boolean comparison itself.
    """
    return sorted(
        session_id
        for session_id, parked_now in cur.items()
        if parked_now and session_id in prev and not prev[session_id]
    )


def gone(prev: dict[str, bool], cur: dict[str, bool]) -> list[str]:
    """Peers present on the prior tick and absent from this one, sorted.

    The mirror of `transitions`, and the same shape of answer: a pure
    set-difference over two already-computed maps, no roster read, no
    liveness probe, no `status`. Membership in `prev` and absence from `cur`
    IS the event -- see the module docstring for what that absence does and
    does not claim, and for why an unreadable registry must raise upstream
    rather than arrive here as an empty `cur`.

    `prev`'s VALUES are unread. Whether a peer was parked or working when it
    was last seen changes nothing about its having left; the parked map is
    reused as the prior peer set only because it is already the thing this
    module carries across ticks, not because parking is part of the
    predicate.
    """
    return sorted(session_id for session_id in prev if session_id not in cur)


def _obligation_summary(repo_root: str, session_id: str) -> str:
    """The PARKED line's obligations field -- NAMES, never a count.

    `None` (no ledger at all -- a producer coverage gap) renders literally
    `no ledger`, distinct from `[]` (a ledger with nothing currently owed,
    `none`) per `obligations.for_peer`'s own negative spec.
    """
    records = obligations.for_peer(repo_root, session_id)
    if records is None:
        return "no ledger"
    if not records:
        return "none"
    names = []
    for record in records:
        name = record.get("obligation_id") or record.get("next_action") or "obligation"
        names.append(str(name))
    return ",".join(names)


def _stamped_age_seconds(repo_root: str, session_id: str, now: datetime) -> Optional[float]:
    """The reader's own `stamped_at` age in seconds, or `None` if unreadable.

    Read directly rather than trusting `verdict["reason"]` to carry it --
    the reader record may not exist at all (fallback-leg peers), and even
    when it does, `classify_peer`'s verdict dict does not surface the raw
    stamp. `read_pass._staleness_seconds` is the same private arithmetic
    `classify_peer` already uses for its own stale-snapshot cross-check;
    reused here rather than duplicated.
    """
    record = read_receiver_state(session_id, repo_root)
    if record is None:
        return None
    return read_pass._staleness_seconds(record.get("stamped_at"), now)


def _transcript_idle_seconds(
    repo_root: str,
    session_id: str,
    cwd: Optional[str],
    now: datetime,
    activity_epoch: Optional[float] = None,
) -> Optional[float]:
    """Seconds since this peer last MOVED, or `None` if that is unreadable.

    Calls `read_pass.transcript_activity_epoch` -- the one place a session id
    becomes a last-activity instant, extracted for exactly this reuse (module
    docstring). No second transcript-clock site is added here.

    This number appears on the PARKED line as the peer's own evidence, so it
    must be the activity clock and not file mtime: mtime runs ahead precisely
    when a peer is parked (see that function), which would print a stalled
    peer as freshly active on the one surface a reader uses to overturn the
    verdict. An untrusted (mtime-fallback) reading is still reported -- it is
    an upper bound on idleness, so it can only ever UNDERSTATE how stuck a
    peer is, never manufacture a stall that is not there.

    `activity_epoch` is the value `classify_peer` already derived for this peer
    this tick, threaded through on the verdict. Review:
    coordinator:code-reviewer (P2) -- re-deriving it here read the same
    transcript a second time in the same tick, which the module docstring's
    single-site note did not prevent (it is one site, called twice). `None`
    means nobody has read it yet (the reader leg, which reduces no tail), and
    only then is a read paid here -- a first read, not a second.
    """
    if activity_epoch is None:
        # Review: coordinator:code-reviewer.a89481390696514f7 (nitpick, accepted) --
        # a bare `_` loses the reader's cue that the discarded element is a
        # trust/confidence flag, not just "the other tuple slot". `_trusted`
        # documents the discard; Pyright's unused-variable complaint is
        # satisfied because it still starts with `_`.
        activity_epoch, _trusted = read_pass.transcript_activity_epoch(
            session_id, cwd or repo_root
        )
    if activity_epoch is None:
        return None
    return now.timestamp() - activity_epoch


def _fmt_seconds(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    return f"{value:.0f}s"


def _cooldown_active(
    repo_root: str,
    caller_session_id: str,
    peer_session_id: str,
    now: float,
) -> bool:
    """Is this peer within `send_pass`'s own offer cooldown right now?

    Reads the SAME log and SAME key `build_send_digest` arms on every offer
    (module docstring: "a peer answered on either path is answered on
    both") -- never a second mechanism and never an operator-maintained mute
    list.
    """
    log = send_pass.read_send_log(repo_root, caller_session_id)
    key = send_pass.offer_key(caller_session_id, peer_session_id)
    remaining = send_pass._cooldown_remaining(log, key, now, _COOLDOWN_SECONDS)
    return remaining > 0


def _parked_line(
    repo_root: str,
    session_id: str,
    verdict: dict[str, Any],
    cwd: Optional[str],
    now: datetime,
    name: Optional[str] = None,
) -> str:
    """Compose one PARKED line -- observed evidence, framed as evidence.

    Review: review-integrator -- `caller_session_id` was accepted but never
    read in this body (Pyright: reportUnusedVariable-adjacent, unused param);
    the caller never needed the callee to see its own id here. Removed rather
    than kept for signature parity nobody was relying on.

    `name` IS PROVENANCE, NOT AN ADDRESS, and the line says so. A reader
    cannot act on a session uuid -- `SendMessage` takes a name -- so a line
    carrying only the uuid asks the Group-EM to go resolve one, and the resolve
    is a second read of a registry this tick already held. The name goes on.

    What the line must NOT do is tell the reader to re-resolve from the
    printed sid, which reads like a check and performs like a ritual: in the
    case that matters -- the peer has re-pointed or gone -- that sid is
    precisely the one that no longer resolves, so the instruction is
    guaranteed to fail exactly when it is needed, and its failure looks
    identical to the peer simply being gone. `verify before sending` is the
    honest qualifier; `re-resolve from this id` is not
    (`DoE-claude docs/wiki/session-facade.md`, amended 2b6df17e6c, via
    claude-klabauter-a9).
    """
    stamped_age = _stamped_age_seconds(repo_root, session_id, now)
    transcript_idle = _transcript_idle_seconds(
        repo_root, session_id, cwd, now, activity_epoch=verdict.get("activity_epoch")
    )
    obligations_summary = _obligation_summary(repo_root, session_id)
    reason = verdict.get("reason")
    who = f"{name} [{session_id}]" if name else str(session_id)
    return (
        f"PARKED session={who} reason={reason} "
        f"stamped_age={_fmt_seconds(stamped_age)} "
        f"transcript_idle={_fmt_seconds(transcript_idle)} "
        f"obligations={obligations_summary} "
        f"(observed, not asserted -- overturn if wrong; "
        f"the name is how it was known this tick, verify before sending)"
    )


def _gone_line(
    session_id: str,
    watched_repo: str,
    now: datetime,
    name: Optional[str] = None,
    last_seen_epoch: Optional[float] = None,
) -> str:
    """Compose one GONE line: what was observed, and the one thing it implies.

    THE NAME IS THE POINT OF THIS LINE, and it is the one field a reader
    cannot recover afterwards. `SendMessage` takes a name; the roster row
    that held it is gone by the time this fires, so a line carrying only the
    uuid tells a Group-EM that SOMETHING left and leaves it unable to say
    what -- which is materially no better than the silence this replaces.
    The name comes off the prior tick's own carried record (`prev_names`),
    never a re-resolve: re-resolving a departed session is guaranteed to
    fail exactly here.

    `last_seen` is the prior tick's stamp, so the reader can tell a peer
    that left seconds ago from one a long-stalled watch is only now
    reporting. `unknown` when the prior record predates this field --
    absence, never an invented `now`, which would read as a fresh exit.

    The line asserts exactly one thing, deliberately: that the name will not
    resolve. Everything else it states is an observation, because a session
    that ended, one that moved out of the repo, and one whose record was
    rewritten are indistinguishable from here (module docstring).
    """
    who = f"{name} [{session_id}]" if name else str(session_id)
    if last_seen_epoch is None:
        seen = "last_seen=unknown"
    else:
        gap = now.timestamp() - last_seen_epoch
        seen = (
            f"last_seen={datetime.fromtimestamp(last_seen_epoch, timezone.utc).strftime(_GONE_STAMP_FORMAT)}"
            f" gap={_fmt_seconds(gap)}"
        )
    return (
        f"GONE session={who} {seen} "
        f"(absent from {watched_repo}'s roster this tick -- ended, or moved out of the repo; "
        f"drop it from the roster, do not send)"
    )


class WatchAlreadyHeldError(RuntimeError):
    """Raised by `main` when arming would create a second live watcher.

    DISTINCT FROM C1 (`watch_heartbeat.stamp`'s own fresh-and-foreign
    decline), deliberately: that guard stops a WRITE from clobbering a
    newer record once two watches are already both running. This guard
    stops the SECOND ARM from ever starting -- the case C1 alone cannot
    reach, because two armed watchers just keep declining each other's
    writes forever instead of one of them never existing. Same predicate
    (`watch_heartbeat.is_fresh_and_foreign`), two call sites, per that
    function's own docstring.

    `cross-repo/inbox/2026-08-31-doe-claude-em-watch-arm-refusal-yes-please.md`
    accepts this repo's own proposal: half a handover -- crown and watcher
    both armed, each believing the other holds it -- is their doctrine's
    worse-than-neither case. Landing this is what lets the sibling repo
    delete the operator-remembers prose in `coordinator/skills/group-em/SKILL.md`
    and `coordinator/agents/fleet-watch.md`, written there as an explicit
    stopgap ("that prose retires the day arming refuses").
    """


def _refuse_if_already_armed(
    repo_root: str,
    holder_session_id: str,
    writer_session_id: str,
    now_epoch: Optional[float] = None,
) -> None:
    """Raise `WatchAlreadyHeldError` iff a FRESH, FOREIGN holder already
    holds this repo's watch; otherwise return silently.

    Reads the SAME on-disk record `watch_heartbeat.stamp` reads before its
    own decline, via the same tolerant reader (`_read_record` -- absent and
    unreadable both answer "no record", which arms cleanly) and the SAME
    shared predicate (`is_fresh_and_foreign`) C1 already exports for exactly
    this reuse. A second freshness/foreignness opinion invented here would
    drift from the write-side guard the first time either changed.

    Never improvises a second poller and never arms anyway with a warning --
    the module docstring's negative spec ("No re-arming step ... nothing in
    this module fires itself") extends to this: an arm that cannot win the
    check does not fall back to arming quietly, it refuses.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    record = watch_heartbeat._read_record(watch_heartbeat.watch_path(repo_root))
    if not watch_heartbeat.is_fresh_and_foreign(
        record, now_epoch, holder_session_id, writer_session_id
    ):
        return
    holder = (record or {}).get("holder_name") or (record or {}).get(
        "holder_session_id"
    ) or "an unknown holder"
    raise WatchAlreadyHeldError(
        f"a watch is already armed for this repo, held by {holder} -- "
        "refusing to arm a second one (it would silently start a half "
        "handover: two watchers, each believing the other holds it)"
    )


def _declination(session_id: str, gate: str, reason: str) -> dict[str, Any]:
    """One heartbeat declination row.

    `name` is always `None`: a name is an address that re-points, and the
    record's reader already prefers the live registry row over any stored
    copy (`watch_heartbeat`'s WHO THE HOLDER IS note).
    """
    return {"session_id": session_id, "name": None, "gate": gate, "reason": reason}


def poll_once(
    repo_root: str,
    caller_session_id: str,
    prev_parked: dict[str, bool],
    now: Optional[datetime] = None,
    emit: Callable[[str], None] = print,
    group_em_session_id: Optional[str] = None,
    prev_names: Optional[dict[str, dict[str, Any]]] = None,
    prev_inbox_open: Optional[int] = None,
) -> tuple[dict[str, bool], list[dict[str, Any]], dict[str, dict[str, Any]], int]:
    """One poll: classify, diff against `prev_parked`, emit PARKED and GONE lines.

    Returns `(parked_map, declinations, peer_notes, inbox_open)`: this tick's
    `{session_id: parked_bool}` -- the caller's new `prev_parked` -- this
    tick's declination rows, `{session_id: {"name", "last_seen"}}` for
    every peer seen, which is what the NEXT tick's GONE lines are named
    from, and this tick's inbox open count -- the caller's new
    `prev_inbox_open`. RAISES on an unreadable registry (`_current_agents`),
    which `main`'s loop and `tick_once` both turn into a POLL-ERROR line;
    every other failure mode stays a plain computation the caller can drive
    without a live registry.

    `prev_inbox_open` is the previous tick's inbox open count -- `None` is
    the honest first-tick answer (no prior to compare against), and an
    INBOX line is emitted ONLY when the count RISES over a known prior,
    same transition discipline as PARKED (module docstring): a tick that
    re-reports the same or a falling depth is the firehose that gets a
    `Monitor` auto-stopped.

    `prev_names` is the previous tick's `peer_notes`, and it is only ever
    read for peers that have LEFT -- a departed session's name cannot be
    resolved any other way. `None` is the honest first-tick answer and
    renders those lines without a name rather than inventing one.

    `group_em_session_id` is the session whose OFFER LOG suppresses lines, which
    is not necessarily the process running this poll -- see `main`. It
    defaults to `caller_session_id`, the case where the Group-EM holds the watch
    itself.

    The declination rows carry the `{session_id, name, gate, reason}` shape the
    heartbeat record wants: one row per peer this tick looked at and did NOT
    emit a line for, with the gate that stopped it -- which is what lets a
    reader tell "looked, nothing to do" apart from "did not look".

    Review: coordinator:overengineering-reviewer -- these were an out-parameter
    on the argument that no caller had to unpack a tuple, which was already
    false (the same change added `group_em_session_id` and rewrote the call site).
    A function whose product is split between a return value and a mutated
    argument is harder to read for a compatibility that was never bought. Not
    taken from the same finding: collapsing the per-peer rows to one aggregate.
    The sibling writer this record is read by stamps a row per peer with these
    exact gates, and a reader joining the two sources should not have to know
    which producer wrote the tick.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    now_epoch = now.timestamp()

    if group_em_session_id is None:
        group_em_session_id = caller_session_id

    agents = _current_agents(repo_root, caller_session_id, group_em_session_id)
    agents_by_id = {
        a.get("sessionId"): a for a in agents if isinstance(a.get("sessionId"), str)
    }
    verdicts = _classify_all(repo_root, agents, now=now)
    cur_parked = {sid: bool(v.get("candidate")) for sid, v in verdicts.items()}
    peer_notes = {
        sid: {"name": agents_by_id.get(sid, {}).get("name"), "last_seen": now_epoch}
        for sid in cur_parked
    }

    # GONE FIRST. A tick that reports a departure and a parking reads in the
    # order the fleet changed: the peer that left is no longer a candidate
    # for anything below, and a reader scanning `Monitor` output should not
    # meet a PARKED line for a roster that has since shrunk.
    # Review: review-integrator, per overengineering-reviewer finding #1
    # (accepted) -- GONE was previously gated on `report_gone`, a caller-set
    # flag that suppressed this loop for a tick whose on-disk prior was
    # judged too old. GONE is terminal and self-limiting (module docstring:
    # a departed session id leaves `cur_parked` the tick it is reported and
    # cannot recur for the same disappearance), so a burst after an outage is
    # N truthful lines, once, never a repeating firehose -- the Monitor
    # auto-stop failure mode this used to guard against belongs to `main`,
    # which holds its own prior in memory and never reaches this on-disk
    # path at all. Nothing replaces the gate.
    watched_repo = os.path.basename(os.path.abspath(str(repo_root))) or str(repo_root)
    for session_id in gone(prev_parked, cur_parked):
        # The watcher's own id and the Group-EM's are excluded from BOTH
        # rosters by `_current_agents`, so neither can appear here --
        # unless the exclusion set itself changed between ticks (a wake
        # given a different `--group-em-session-id` than the last one).
        # That is a changed question, not a departed peer, and reporting
        # the Group-EM as gone to the Group-EM is the worst way to say so.
        if session_id in (caller_session_id, group_em_session_id):
            continue
        prior = (prev_names or {}).get(session_id) or {}
        last_seen = prior.get("last_seen")
        emit(
            _gone_line(
                session_id,
                watched_repo,
                now,
                name=prior.get("name"),
                last_seen_epoch=last_seen if isinstance(last_seen, (int, float)) else None,
            )
        )

    declinations: list[dict[str, Any]] = []
    transitioned = transitions(prev_parked, cur_parked)
    for session_id in transitioned:
        if _cooldown_active(repo_root, group_em_session_id, session_id, now_epoch):
            declinations.append(
                _declination(session_id, "cooldown", "answered-within-cooldown")
            )
            continue
        peer = agents_by_id.get(session_id, {})
        line = _parked_line(
            repo_root,
            session_id,
            verdicts[session_id],
            peer.get("cwd"),
            now,
            name=peer.get("name"),
        )
        emit(line)

    transitioned_set = set(transitioned)
    for session_id, verdict in verdicts.items():
        if session_id in transitioned_set:
            continue
        declinations.append(
            _declination(
                session_id, "not-a-candidate", str(verdict.get("reason") or "not-parked")
            )
        )

    cur_inbox_open, inbox_total, inbox_taken_at = _inbox_counts(repo_root)
    if prev_inbox_open is not None and cur_inbox_open > prev_inbox_open:
        emit(_inbox_line(cur_inbox_open, inbox_total, inbox_taken_at))

    return cur_parked, declinations, peer_notes, cur_inbox_open


def parked_state_path(repo_root: str) -> str:
    """Absolute path of the carried parked map for `repo_root`."""
    return os.path.join(repo_root, _PARKED_STATE_RELATIVE_PATH)


def load_prev_parked(repo_root: str) -> dict[str, bool]:
    """The prior tick's `{session_id: parked}`, or `{}` when there is none.

    Absent, unreadable, and malformed all answer `{}` -- the same answer as a
    first tick. A wake that cannot read its own prior state must not be able to
    turn that into a flood of PARKED lines for peers nobody just observed
    changing: `transitions` requires membership in BOTH maps, so an empty prior
    emits nothing and the NEXT wake reports the transitions honestly.

    Deliberately NOT aged out. A prior map written hours ago is stale, but a
    peer that parked in the meantime is exactly what the fleet wants surfaced,
    late rather than never; the send-pass cooldown is what stops an
    already-answered peer being raised twice, and it does that on its own
    clock rather than this one.
    """
    payload = _load_prev_record(repo_root)
    parked = payload.get("parked")
    if not isinstance(parked, dict):
        return {}
    return {
        str(sid): bool(value)
        for sid, value in parked.items()
        if isinstance(sid, str)
    }


def load_prev_peers(repo_root: str) -> dict[str, dict[str, Any]]:
    """The prior tick's `{session_id: {"name", "last_seen"}}`, or `{}`.

    Projected off the SAME record `load_prev_parked` reads (see
    `_load_prev_record`), tolerant of its absence: a record written before
    this field existed carries `parked` and nothing else, and the honest
    degrade is GONE lines without a name, never a refusal to report the
    departure at all. The parked map alone is a sufficient prior peer SET
    (`gone` reads only its keys); this adds only what the line needs to be
    actionable.
    """
    payload = _load_prev_record(repo_root)
    peers = payload.get("peers")
    if not isinstance(peers, dict):
        return {}
    return {
        str(sid): note
        for sid, note in peers.items()
        if isinstance(sid, str) and isinstance(note, dict)
    }


def _load_prev_record(repo_root: str) -> dict[str, Any]:
    """Open, parse, and shape-check the carried prior-state record ONCE.

    Review: review-integrator, folding in a finding overengineering-reviewer
    raised outside its own scope but flagged as staff-eng's -- `load_prev_parked`
    and `load_prev_peers` used to each independently `open()`/`json.load()` the
    same `parked_state_path(repo_root)` file, so `tick_once` paid two opens and
    two parses of one small record on the same tick, and the record's shape was
    asserted in two places that had to agree. One reader, two projections.
    Absent, unreadable, and malformed all answer `{}` -- the same answer as a
    first tick.
    """
    try:
        with open(parked_state_path(repo_root), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_prev_parked(
    repo_root: str,
    parked: dict[str, bool],
    peers: Optional[dict[str, dict[str, Any]]] = None,
) -> bool:
    """Write this tick's parked map for the next wake to diff against.

    `peers` carries each seen peer's name and last-seen epoch forward, so the
    NEXT tick can name a session that has left by then -- the one fact a
    departed peer's line cannot re-derive.

    Same posture as the heartbeat stamp it sits beside: returns False on I/O
    failure, never raises. A lost map costs one tick's transitions, never the
    watch.
    """
    payload: dict[str, Any] = {"parked": dict(parked)}
    if peers is not None:
        payload["peers"] = dict(peers)
    return watch_heartbeat.write_atomic(parked_state_path(repo_root), payload)


# ---------------------------------------------------------------------------
# Boilerplate shared verbatim between `main` and `tick_once` -- hoisted per
# overengineering-reviewer (finding #5, nitpick, accepted). The two entry
# points are genuinely two lifecycles (loop-vs-exit, prior state on disk vs
# in memory, loud vs swallowed failure -- see each docstring); only the
# surrounding id-defaulting, emit closure, and error-line format were a
# character-identical fork that would drift the first time one of them
# changed and not the other.
# ---------------------------------------------------------------------------


def _resolve_caller_and_gem_ids(
    caller_session_id: Optional[str], group_em_session_id: Optional[str]
) -> tuple[str, str]:
    """`main` and `tick_once` share this defaulting exactly: an unset caller
    id resolves off the harness's own `CLAUDE_CODE_SESSION_ID`
    (`read_pass.caller_session_id()`), never guessed from roster shape; an
    unset Group-EM id defaults to the caller's own -- see each entry point's
    own docstring for why the two are a separate question at all.

    BOTH LEGS ANSWER A STRING, including the case the environment cannot.
    `read_pass.caller_session_id()` returns `Optional[str]` -- it reads an env
    var that a process launched outside the harness simply does not carry --
    and both callers hand the result to `poll_once`, which is typed for a
    string and uses it as a roster exclusion key. An unresolved id excludes
    nobody, which is the honest degrade; `None` leaking into that key is not,
    and every downstream reader would have to re-ask the same question.
    """
    if caller_session_id is None:
        caller_session_id = read_pass.caller_session_id() or ""
    if group_em_session_id is None:
        group_em_session_id = caller_session_id
    return caller_session_id, group_em_session_id


def _emit_for(stream: TextIO) -> Callable[[str], None]:
    """The `emit(line)` closure both entry points build over their own
    resolved output stream -- print + flush, nothing else."""

    def emit(line: str) -> None:
        print(line, file=stream, flush=True)

    return emit


def _poll_error_line() -> str:
    """The `POLL-ERROR <one-line traceback>` both entry points report from
    the current exception -- `limit=1`, collapsed to one line so a broken
    stream cannot make reporting the error fail worse than the error itself.
    """
    return "POLL-ERROR " + traceback.format_exc(limit=1).strip().replace("\n", " | ")


def _prune_spool(repo_root: str) -> None:
    """Age-bound the spool now that this tick has classified off the live registry.

    BOTH TICK PATHS PRUNE, and that is the whole answer to "who bounds the
    spool": `tick_once` (the `--once` wake) and `main`'s held loop alike. A
    repo whose watch is a healthy held `Monitor` and whose cron never fires
    would otherwise spool into a file nothing ever shortened -- and at the
    volume the sibling plane's producer actually writes (`PAUSED:turn-ended`,
    one record per turn end per session) that is unbounded growth presenting
    as a perfectly healthy watch. `poll_once` still classifies off the live
    registry every tick with no debounce (`watch_spool` module docstring,
    "THE SPOOL IS A DOORBELL"); this call is pure housekeeping, unrelated to
    that classify. It is no longer a blind truncate: `watch_spool.prune`
    keeps a `_RETENTION_WINDOW_SECONDS` window so the sibling DoE plane's
    `coordinator:fleet-watch` reader -- which polls this file and triages it
    -- cannot see it emptied between its own polls (`watch_spool` module
    docstring, "RETENTION IS AGE-BOUNDED").

    NEVER RAISES, and the `except` below is what makes that true rather than
    inherited. `watch_spool.prune` catches the I/O classes it names and
    returns False, which covers the failures anyone predicted; it does not
    promise the ones nobody did. This runs inside `main`'s held loop, where an
    uncaught exception does not cost a tick -- it ends the watch process, and a
    watch that died while housekeeping reads from outside exactly like a quiet
    fleet. A spool that could not be pruned costs disk space, never
    correctness. Same posture as `watch_heartbeat.stamp`: a failed housekeeping
    write must not be able to end a working watch.
    """
    try:
        watch_spool.prune(repo_root)
    except Exception:
        pass


def tick_once(
    repo_root: str,
    caller_session_id: Optional[str] = None,
    group_em_session_id: Optional[str] = None,
    stream: Optional[TextIO] = None,
    tick_interval_seconds: float = _CRON_FLOOR_INTERVAL_SECONDS,
    now: Optional[datetime] = None,
) -> int:
    """One wake: poll once against the carried prior map, then exit.

    THE POINT OF THIS ENTRY IS THAT NOTHING IS HELD. `main` is a watch only
    while its process lives, and a process that never started, exited, or
    returned instead of blocking presents from outside exactly like a quiet
    fleet -- the failure `cross-repo/inbox/2026-09-01-example-game-repo-em-group-em-fleet-watch-wake-on-session-state.md`
    reproduces. A wake that carries its state on disk and exits has no held
    thing to lapse: the next caller -- the Group-EM's cron floor, or any
    session-state-transition wake wired above this line -- supplies the
    liveness, and the heartbeat record says which clock last fired.

    NOT A SECOND WATCHER. Every judgement here is `poll_once`'s, unchanged:
    same parked predicate, same cooldown gate, same line format. The only
    thing this adds is where `prev_parked` comes from and goes.

    `tick_interval_seconds` is the CALLER's cadence, not a measurement --
    it sets the staleness deadline the HEARTBEAT record promises (`--status`),
    unrelated to the carried parked map, which is never aged out (see
    `load_prev_parked`).

    Returns a process exit code: 0 for a tick that ran, 1 for one that raised
    (reported as a POLL-ERROR line first). A failed wake exits LOUD -- there
    is no loop left to carry on into, and a silent zero here would rebuild the
    exact indistinguishability this entry exists to remove.
    """
    caller_session_id, group_em_session_id = _resolve_caller_and_gem_ids(
        caller_session_id, group_em_session_id
    )
    out = sys.stdout if stream is None else stream
    emit = _emit_for(out)

    prev_parked = load_prev_parked(repo_root)

    # ONE instant for the whole tick, captured before the classify and reused
    # for the heartbeat stamp below -- `poll_once` would otherwise take its
    # own. Not threaded into `_prune_spool`: `watch_spool.prune` takes its
    # own `now_epoch` default and is a lazy, hysteresis-gated housekeeping
    # call, not a per-tick drain against this instant (`watch_spool` module
    # docstring, "LAZY, WITH HYSTERESIS").
    tick_now = now if now is not None else datetime.now(timezone.utc)

    try:
        # `prev_inbox_open=None`: a single-tick wake carries no memory
        # across wakes (same posture as `prev_parked` in the module
        # docstring's CONCURRENT WAKES note) and there is no on-disk carry
        # for this count today, so every `tick_once` wake is a first tick
        # for the inbox line -- it never fires here, only on `main`'s held
        # loop, which is the surface a rise is worth a line on.
        cur_parked, declinations, peer_notes, _cur_inbox_open = poll_once(
            repo_root,
            caller_session_id,
            prev_parked,
            now=tick_now,
            emit=emit,
            group_em_session_id=group_em_session_id,
            prev_names=load_prev_peers(repo_root),
        )
    except Exception:
        try:
            emit(_poll_error_line())
        except Exception:
            pass
        return 1

    # Review: coordinatorcode-reviewer.a933f243c20654e60 -- emit happens
    # inside `poll_once`, above, strictly BEFORE this persist step, and that
    # ordering is deliberate, not incidental. Work both failure directions:
    # if persistence raised AFTER a successful emit, the current order
    # leaves the departed peer in the OLD prior map, so the next tick reports
    # it again (a DUPLICATE line); the reviewer's suggested reorder
    # (persist-then-emit) would instead retire the peer from the map before
    # any line was ever printed, so a persistence failure at that point loses
    # the departure SILENTLY -- the exact "fleet went quiet and nobody said
    # so" failure this module exists to remove. A duplicate GONE is noise a
    # reader can discard; a dropped one is not. Kept as emit-then-persist on
    # that basis (accepted risk: `watch_heartbeat.write_atomic`, the thing
    # `save_prev_parked`/`stamp` bottom out in, is contractually non-raising
    # today; see `test_gone_emits_even_when_persistence_raises` below, which
    # pins that emission does not depend on persistence succeeding).
    save_prev_parked(repo_root, cur_parked, peers=peer_notes)
    watch_heartbeat.stamp(
        repo_root,
        holder_session_id=group_em_session_id or "",
        declinations=declinations,
        interval_seconds=tick_interval_seconds,
        tick_source="cron",
        subscribed_peers=len(cur_parked),
        writer_session_id=caller_session_id,
        # No `holder_name`: a wake makes no enumeration of its own, so it has
        # no name to write. `stamp` carries the armed poller's forward rather
        # than blanking it -- a cheaper answer than a registry read per wake.
    )
    _prune_spool(repo_root)
    return 0


def main(
    repo_root: str,
    caller_session_id: Optional[str] = None,
    stream: Optional[TextIO] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_iterations: Optional[int] = None,
    group_em_session_id: Optional[str] = None,
    now_epoch: Optional[float] = None,
) -> None:
    """Arm the watch: print `ARMED`, then poll forever (or `max_iterations`
    times, for tests), emitting one line per PARKED transition and one per
    peer that has left the roster since the previous tick.

    REFUSES FIRST, before any read or write of its own, when a FRESH FOREIGN
    holder already holds this repo's watch (`WatchAlreadyHeldError`) -- see
    `_refuse_if_already_armed`. `now_epoch` is exposed only for that check's
    determinism in tests; every other clock read in this function is the
    real one.

    `caller_session_id` defaults to `read_pass.caller_session_id()` (the
    harness's own `CLAUDE_CODE_SESSION_ID`) -- never guessed from roster
    shape. Coverage: any exception raised inside one poll iteration is
    caught here, reported as a `POLL-ERROR` line, and the loop continues --
    a poll that raises must never silently end the watch (module docstring).

    `group_em_session_id` IS A SEPARATE ID ON PURPOSE, and defaults to
    `caller_session_id`. Two different questions were being answered by one
    value: which session to leave out of the watched roster (this process),
    and whose offer log already answered a peer (the Group-EM). They are the
    same session only when the Group-EM holds the poller itself. A Group-EM that
    dispatches a teammate to hold the watch must pass its OWN id here --
    otherwise the watcher reads an empty send log, every offer the Group-EM
    already made stops suppressing a line, and one stopped peer gets nudged
    twice. Passed explicitly rather than inferred: a dispatched process's
    `CLAUDE_CODE_SESSION_ID` is the harness's to define, and a watch that is
    wrong about who answered a peer is worse than one that must be told.

    Each poll also stamps `state/group-em-watch.json` via `watch_heartbeat`
    -- the presence record other sessions read. Arming this watch is
    supposed to REPLACE hand-ticking, and until it stamped, doing the right
    thing made the fleet's watch-presence surface report no watch at all.
    """
    caller_session_id, group_em_session_id = _resolve_caller_and_gem_ids(
        caller_session_id, group_em_session_id
    )
    _refuse_if_already_armed(
        repo_root, group_em_session_id, caller_session_id, now_epoch=now_epoch
    )

    # LATE-BOUND, deliberately. `stream: TextIO = sys.stdout` freezes whatever
    # stdout was at IMPORT time, so anything that replaces it afterwards -- a
    # harness wrapping the stream, a test capturing it -- is written past rather
    # than to. For a process whose entire product is its stdout lines, a stream
    # captured before the caller existed is the wrong one by default.
    out = sys.stdout if stream is None else stream
    emit = _emit_for(out)

    snapshot_ms, agents = _measure_snapshot_ms(repo_root)
    peer_count = len(agents)
    holder_name = _holder_name(agents, group_em_session_id)
    interval = _poll_interval_seconds(snapshot_ms)
    # Review: coordinatorcode-reviewer.a9e1410288878bea9 -- the ARMED line is
    # operator-facing; "denominator" is an internal metric name from the
    # interval derivation and reads oddly next to "peers" on that surface.
    # The repo NAME is read off `repo_root`, never written as a literal. A literal
    # here is rewritten by the publish transform, so source says "claude-klabauter peers" and
    # the shipped mirror says "claude-klabauter peers" -- and both are printed
    # whatever `--repo-root` the operator passed. doe-claude-80 measured it against
    # the published engine 2026-08-31: --repo-root X:/DoE-claude printed "3
    # claude-klabauter peers", X:/claude-klabauter printed "14 claude-klabauter peers".
    # The COUNTS tracked the flag correctly, so only the label lied. That is the worse
    # half: a Group EM arming for DoE reads a foreign repo name beside a plausible
    # count and the honest conclusion is that the watch is pointed at the wrong repo,
    # so the failure lands as a stand-down rather than an error.
    resolved_root = os.path.abspath(str(repo_root))
    watched_repo = os.path.basename(resolved_root) or str(repo_root)
    # THE RESOLVED PATH GOES ON THE LINE, not just the derived name -- the
    # name alone survives a mangled root and reads healthy anyway; full
    # incident: `repo_root_arg`'s module docstring. `_cli` refuses that root
    # outright; this is the second line of defence for callers that reach
    # `main` without passing through it.
    #
    # Review: overengineering-reviewer (finding #4, minor, accepted) -- this
    # comment used to retell the incident (mangled path, publish-mirror
    # consequence) at full length, the third of four full retellings across
    # this diff. Reduced to a pointer.
    # ROSTER NAME AND STRUCK INSTANT, beside the count. `peer_count` includes
    # this caller (see `_current_agents`'s docstring -- this measurement runs
    # BEFORE the watcher excludes itself), which is the opposite population
    # from the heartbeat's `subscribed_peers` (caller excluded). Naming both
    # here, plus the instant this enumeration was taken, is what lets a reader
    # tell a real fleet change from a gap inferred between two differently-
    # defined lines (module's C5 note).
    armed_struck_epoch = time.time() if now_epoch is None else now_epoch
    # Review: coordinatorcode-reviewer (finding #1) -- external module, use the
    # promoted public name; `_iso` is the private alias `iso_instant` retired.
    armed_struck_at = watch_heartbeat.iso_instant(armed_struck_epoch)
    emit(
        f"ARMED peer_count={peer_count} {watched_repo} peers at {resolved_root}, "
        f"snapshot={snapshot_ms:.1f}ms, interval={interval:.1f}s, "
        f"roster=(peers seen including this caller), as_of={armed_struck_at}"
    )

    prev_parked: dict[str, bool] = {}
    # Held in memory alongside `prev_parked`, for the same reason and with
    # the same lifetime.
    prev_names: dict[str, dict[str, Any]] = {}
    # `None` until the first tick strikes a count -- the honest no-prior
    # answer, same as `prev_parked` starting empty. Held for the loop's
    # life, same lifetime as `prev_parked`/`prev_names` above.
    prev_inbox_open: Optional[int] = None
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        declinations: list = []
        tick_now = datetime.now(timezone.utc)
        try:
            # Review: coordinator:code-reviewer af0c0865daafdd73a -- the loop
            # used to reassign `prev_parked` from this return, so every line
            # below it read the CURRENT map under a name saying previous, and
            # `subscribed_peers` in particular reported a coverage figure whose
            # own variable name argued it was stale. The rebind to `prev_parked`
            # is the last statement of the iteration, where it means what it says.
            cur_parked, declinations, peer_notes, cur_inbox_open = poll_once(
                repo_root,
                caller_session_id,
                prev_parked,
                now=tick_now,
                emit=emit,
                group_em_session_id=group_em_session_id,
                prev_names=prev_names,
                prev_inbox_open=prev_inbox_open,
            )
            # A stamp failure is a missed tick, never a reason to stop
            # watching -- `stamp` returns False rather than raising, and the
            # next tick rewrites the whole record anyway.
            #
            # Review: coordinatorcode-reviewer.a933f243c20654e60 -- `emit`
            # inside `poll_once`, above, runs before `prev_parked`/
            # `prev_names` are rebound below, same deliberate emit-then-
            # persist ordering as `tick_once` (see the matching comment
            # there for the two failure directions worked out in full):
            # kept because a duplicate GONE next tick is strictly cheaper to
            # read than a silently dropped one, and today's persistence path
            # (`watch_heartbeat.write_atomic`) does not raise in practice.
            watch_heartbeat.stamp(
                repo_root,
                holder_session_id=group_em_session_id or "",
                declinations=declinations,
                interval_seconds=interval,
                # THE PEERS THIS TICK ACTUALLY LOOKED AT, never the default 1.
                # A watch subscribed to one peer and a watch covering the whole
                # repo were indistinguishable from every artifact on disk:
                # measured 2026-09-01 by the Group-EM of this repo, whose record
                # read `subscribed_peers: 1` against a live population of 10-18.
                # A coverage figure nobody writes is a coverage figure nobody
                # can question.
                subscribed_peers=len(cur_parked),
                holder_name=holder_name,
                writer_session_id=caller_session_id,
            )
            _prune_spool(repo_root)
            prev_parked = cur_parked
            prev_names = peer_notes
            prev_inbox_open = cur_inbox_open
        except Exception:
            # Review: coordinatorcode-reviewer.a9e1410288878bea9 -- reporting
            # an error must never be able to fail worse than the error itself.
            # A broken stream at the moment a poll raises would otherwise
            # propagate out of `main` uncaught, ending the watch silently --
            # exactly the "indistinguishable from a quiet repo" failure this
            # module's COVERAGE contract exists to prevent.
            #
            # Review: coordinatorcode-reviewer.a933f243c20654e60 (nit) -- this
            # catches `stamp`'s `ValueError` on an invalid `tick_source`
            # identically to a genuine I/O miss, printing both as the same
            # POLL-ERROR line. That collapse is deliberate for now: both call
            # sites pass a fixed, valid literal (`"cron"`/the loop's own
            # constant), so the ValueError branch is dead code today, a
            # caller bug rather than an environmental failure. Revisit if
            # `tick_source` ever becomes caller-controlled.
            try:
                emit(_poll_error_line())
            except Exception:
                pass
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            break
        sleep_fn(interval)


def _cli(argv: "list[str] | None" = None) -> int:
    """Command-line entrypoint, so the watch can actually be ARMED.

    THIS IS NOT OPTIONAL PLUMBING -- it is what makes this module the thing the
    plan says it is. C2 describes a runnable the Group EM arms once with the
    harness `Monitor` tool, and `Monitor` takes a COMMAND. A module exposing only
    an importable `main()` cannot be named in one, so the standing watch shipped
    unarmable: every test green, the mechanism inert. C10's executor found this
    from the other side (its advisory had no launcher to compose a command from)
    and correctly reported it rather than widening its own scope to fix it.

    Same defect class this repo already took once on the sibling plane --
    cross-repo/inbox/2026-08-30-doe-claude-em-workflow-watch-command-is-unrunnable-outside-the-engine.md.
    A watch you cannot spell on a command line is a watch nobody runs.

    Arm it with:
        group-em-watch --repo-root <path>

    That is the settings-home launcher (`coordinator/bin/group-em-watch.py`),
    which resolves the engine wherever it is installed. The `python -m` spelling
    below works only from a cwd whose interpreter can already import
    `coordinator_core` -- which the repos this watch is armed FOR generally
    cannot, and the failure is a `ModuleNotFoundError` the arming agent reports
    as nothing at all:
        python -m coordinator_core.group_em.watch --repo-root <path>

    When a dispatched teammate holds the watch rather than the Group-EM itself,
    the Group-EM's own id goes on too, or its offers stop suppressing lines:
        python -m coordinator_core.group_em.watch --repo-root <path>             --group-em-session-id <the Group-EM's session id>
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m coordinator_core.group_em.watch",
        description=(
            "Standing Group EM watch: emit one line per peer in this repo entering a "
            "parked state, and one per peer that has left the roster, until the session "
            "ends or the Monitor is stopped."
        ),
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        help="Repository root to watch. Taken as an argument, never derived from cwd -- "
             "the watch runs under a harness tool whose working directory is not ours.",
    )
    parser.add_argument(
        "--caller-session-id",
        default=None,
        help="Session arming the watch. Defaults to the harness's own session id; "
             "never guessed from roster shape.",
    )
    # Pre-2026-09-01 spelling; accepted, unadvertised. Rationale + retirement
    # condition: group_em/tests/test_deprecated_crown_flag_alias.py
    # Review: overengineering-reviewer -- collapsed duplicated 9-line rationale
    # to a pointer; full argument lives in the test file (also the delete unit).
    parser.add_argument(
        "--group-em-session-id",
        dest="group_em_session_id",
        default=None,
        help="The Group-EM's session id, when a dispatched teammate holds the watch instead of "
             "the Group-EM itself. Defaults to --caller-session-id. This is the id whose offer "
             "cooldown suppresses lines and whose name goes on the heartbeat record -- pass it "
             "whenever the watching process is not the Group-EM, or the same stopped peer gets "
             "nudged twice.",
    )
    parser.add_argument(
        "--crown-session-id",
        dest="group_em_session_id",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Answer 'is a watch alive for this repo?' in plain words and exit, watching "
             "nothing. Exit 0 alive, 1 not running, 2 unknown (no watch ever armed, or an "
             "unreadable record) -- unknown is never reported as healthy.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run ONE tick against the carried parked map and exit, instead of holding a poll "
             "loop. This is the form a cron floor -- or any session-state wake -- fires: nothing "
             "is held between wakes, so nothing can silently lapse. Stamps the heartbeat with "
             "tick_source=cron.",
    )
    parser.add_argument(
        "--tick-interval-seconds",
        type=float,
        default=_CRON_FLOOR_INTERVAL_SECONDS,
        help="With --once: the CALLER's cadence, which sets the staleness deadline the heartbeat "
             f"promises. Defaults to the group-em entry sequence's own cron floor "
             f"({_CRON_FLOOR_INTERVAL_SECONDS/60:.0f} minutes). Ignored without --once, where the "
             "interval is measured at arm time.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Stop after N poll iterations instead of running until the session ends. "
             "For probes and tests; omit for a real arm.",
    )
    args = parser.parse_args(argv)

    # REFUSE A ROOT THIS PROCESS CANNOT STAND ON, before anything reads or
    # writes through it. An unarmable root used to arm: `peer_count=0` is what a
    # quiet repo and an unreadable one both print, and the run exited 0.
    try:
        args.repo_root = repo_root_arg.resolve_repo_root_arg(args.repo_root)
    except repo_root_arg.RepoRootArgError as exc:
        print(f"group-em-watch: {exc}", file=sys.stderr)
        return 2

    if args.status:
        # STATUS ANSWERS FOR A HUMAN, and takes no lock, no roster read, no
        # poll. The record already distinguishes a quiet live watch from a dead
        # one and from a repo nobody ever armed; before this flag the only
        # reader of that distinction was another program, so a person asking
        # "is my watch alive?" got the harness's `idle` -- which every one of
        # the three states prints. Exit code carries the same three states for
        # a caller that cannot read prose; 2 is UNKNOWN, never a pass.
        liveness = watch_heartbeat.read_liveness(args.repo_root)
        print(watch_heartbeat.human_verdict(liveness))
        if liveness["verdict"] == watch_heartbeat.VERDICT_ARMED:
            return 0
        return 1 if liveness["verdict"] == watch_heartbeat.VERDICT_STALE else 2

    if args.once:
        # A single-shot wake reports its own failure through the exit code --
        # there is no loop to carry on into, and a wake that exits 0 having
        # done nothing is the false-green this mode exists to remove.
        return tick_once(
            args.repo_root,
            caller_session_id=args.caller_session_id,
            group_em_session_id=args.group_em_session_id,
            tick_interval_seconds=args.tick_interval_seconds,
        )

    try:
        main(
            args.repo_root,
            caller_session_id=args.caller_session_id,
            max_iterations=args.max_iterations,
            group_em_session_id=args.group_em_session_id,
        )
    except KeyboardInterrupt:
        # A stopped Monitor is an ordinary end, not a failure -- exit quietly so
        # the run does not read as a crash in whatever armed it.
        return 0
    except WatchAlreadyHeldError as exc:
        # NON-ZERO AND NAMED, never a silent no-op: an arm that quietly does
        # nothing is indistinguishable from an arm that worked, the exact
        # defect class this refusal exists to remove.
        print(f"group-em-watch: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via _cli in tests
    raise SystemExit(_cli())
