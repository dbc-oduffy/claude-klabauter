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
    its first sighting => emit nothing. `_run_baseline` already reports
    spawned/exited/changed on every tick elsewhere in this plan; a second
    reporter of the same fact is duplication `transitions` deliberately
    declines by requiring membership in BOTH snapshots.
  - a peer that exits (present prior tick, absent now) => emit nothing, for
    the same reason.

THE WATCH MUST NOT RE-FLAG AN ANSWERED PEER. Before emitting, this module
checks `send_pass`'s own per-peer offer cooldown (`read_send_log` +
`_cooldown_remaining`, the SAME clock `build_send_digest` arms on every
offer) and stays silent while it is armed -- not a second mechanism, not an
operator-maintained mute list (which would drift the moment a peer's
situation changed): a peer answered on either path is answered on both, and
the cooldown expires on its own.

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
- No per-peer entry point beyond `main`'s own loop; `transitions` is a pure
  function over two already-computed boolean maps, never over raw agents.
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
    """
    agents = read_pass.fetch_live_agents(repo_root)
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
    caller_session_id: str,
    session_id: str,
    verdict: dict[str, Any],
    cwd: Optional[str],
    now: datetime,
    name: Optional[str] = None,
) -> str:
    """Compose one PARKED line -- observed evidence, framed as evidence.

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
) -> tuple[dict[str, bool], list[dict[str, Any]]]:
    """One poll: classify, diff against `prev_parked`, emit any PARKED lines.

    Returns `(parked_map, declinations)`: this tick's
    `{session_id: parked_bool}` -- the caller's new `prev_parked` -- and this
    tick's declination rows. Never raises: `main`'s loop is the
    coverage boundary (module docstring's POLL-ERROR contract), but this
    function is also exercised directly by tests, so it stays a plain
    computation the caller can drive without a live registry.

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
            group_em_session_id,
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

    return cur_parked, declinations


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
    try:
        with open(parked_state_path(repo_root), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    parked = payload.get("parked") if isinstance(payload, dict) else None
    if not isinstance(parked, dict):
        return {}
    return {
        str(sid): bool(value)
        for sid, value in parked.items()
        if isinstance(sid, str)
    }


def save_prev_parked(repo_root: str, parked: dict[str, bool]) -> bool:
    """Write this tick's parked map for the next wake to diff against.

    Same posture as the heartbeat stamp it sits beside: returns False on I/O
    failure, never raises. A lost map costs one tick's transitions, never the
    watch.
    """
    return watch_heartbeat.write_atomic(
        parked_state_path(repo_root), {"parked": dict(parked)}
    )


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
    it sets the staleness deadline the heartbeat promises, so a caller on a
    slower clock must say so or the record reads STALE between correct wakes.

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

    try:
        cur_parked, declinations = poll_once(
            repo_root,
            caller_session_id,
            load_prev_parked(repo_root),
            now=now,
            emit=emit,
            group_em_session_id=group_em_session_id,
        )
    except Exception:
        try:
            emit(_poll_error_line())
        except Exception:
            pass
        return 1

    save_prev_parked(repo_root, cur_parked)
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
    return 0


def main(
    repo_root: str,
    caller_session_id: Optional[str] = None,
    stream: Optional[TextIO] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_iterations: Optional[int] = None,
    group_em_session_id: Optional[str] = None,
) -> None:
    """Arm the watch: print `ARMED`, then poll forever (or `max_iterations`
    times, for tests), emitting one line per PARKED transition.

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
    emit(
        f"ARMED peer_count={peer_count} {watched_repo} peers at {resolved_root}, "
        f"snapshot={snapshot_ms:.1f}ms, interval={interval:.1f}s"
    )

    prev_parked: dict[str, bool] = {}
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        declinations: list = []
        try:
            prev_parked, declinations = poll_once(
                repo_root,
                caller_session_id,
                prev_parked,
                emit=emit,
                group_em_session_id=group_em_session_id,
            )
            # A stamp failure is a missed tick, never a reason to stop
            # watching -- `stamp` returns False rather than raising, and the
            # next tick rewrites the whole record anyway.
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
                subscribed_peers=len(prev_parked),
                holder_name=holder_name,
                writer_session_id=caller_session_id,
            )
        except Exception:
            # Review: coordinatorcode-reviewer.a9e1410288878bea9 -- reporting
            # an error must never be able to fail worse than the error itself.
            # A broken stream at the moment a poll raises would otherwise
            # propagate out of `main` uncaught, ending the watch silently --
            # exactly the "indistinguishable from a quiet repo" failure this
            # module's COVERAGE contract exists to prevent.
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
            "Standing Group EM watch: emit one line per claude-klabauter peer entering a "
            "parked state, until the session ends or the Monitor is stopped."
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
        # `--crown-session-id` is the pre-2026-09-01 spelling, retained as an
        # accepted-but-unadvertised alias (DR-084's `_DEPRECATED_ALIASES` shape).
        # It is NOT cosmetic back-compat: DoE-claude's fleet-watch agent definition
        # and group-em skill both instruct agents to pass the old spelling, argparse
        # hard-errors on an unknown flag, and those agents are dispatched and running.
        # Dropping it strands every live watcher the moment this lands. Retire it once
        # the sibling's text has moved -- see the memo
        # cross-repo/archive/...-crown-nomenclature-retired.md. `help=argparse.SUPPRESS`
        # keeps it out of --help so the canonical spelling is the only one advertised.
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
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via _cli in tests
    raise SystemExit(_cli())
