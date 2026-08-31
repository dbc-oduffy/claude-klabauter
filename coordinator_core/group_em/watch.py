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

DO NOT ADD A THIRD TRANSCRIPT-MTIME SITE. `read_pass._transcript_mtime_epoch`
was extracted ahead of this chunk for exactly this reuse; this module calls
it and does not add its own `getmtime`.

NEGATIVE SPEC -- what this module deliberately does not do:

- No re-arming step, no cron, no second entry point. The watcher is armed
  once, by the session, with the harness `Monitor` tool -- nothing in this
  module fires itself, and nothing here tells a future session to remember
  to arm it (that prose instruction is the defect this chunk replaces).
- No CPU-delta leg, no `state`/`waitingFor` read, no shouldn't-be
  adjudication -- same negative spec `read_pass`/`send_pass` already carry.
- No send, no nudge, no write to any peer's state. This module only reads
  the registry, the receiver-state reader, transcript tails, the ledger, and
  the send log's cooldown -- and writes stdout.
- No per-peer entry point beyond `main`'s own loop; `transitions` is a pure
  function over two already-computed boolean maps, never over raw agents.
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, TextIO

from coordinator_core.group_em import obligations
from coordinator_core.group_em import read_pass
from coordinator_core.group_em import send_pass
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

#: `send_pass.build_send_digest`'s own default -- the watch reuses the SAME
#: clock rather than a second cooldown window, per the module docstring.
_COOLDOWN_SECONDS = send_pass.DEFAULT_COOLDOWN_SECONDS


def _measure_snapshot_ms(repo_root: str) -> tuple[float, int]:
    """Time one `fetch_live_agents` call on THIS box; return (ms, peer_count).

    Re-measures rather than trusting the plan's cited 4.6ms cold -- that
    number was measured on a different box, on a different tick, and this
    module's poll interval is derived from ITS OWN measurement so the
    denominator printed on `ARMED` is always this box's own evidence.
    """
    started = time.monotonic()
    agents = read_pass.fetch_live_agents(repo_root)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    return elapsed_ms, len(agents)


def _poll_interval_seconds(snapshot_ms: float) -> float:
    """Derive the poll interval from this tick's measured `snapshot_ms`.

    See the module-level constants' docstring for the derivation. Never a
    chosen round number standing alone -- it is always a function of a
    measurement taken this call.
    """
    derived = (snapshot_ms / 1000.0) * _POLL_INTERVAL_MEASURED_MULTIPLIER
    return max(_POLL_INTERVAL_FLOOR_SECONDS, derived)


def _current_agents(repo_root: str, caller_session_id: Optional[str]) -> list[dict[str, Any]]:
    """This tick's repo-filtered, self-excluded peer set.

    Sourced from `read_pass.fetch_live_agents` (-> `peer_roster.build_roster`,
    already case-folded `cwd` containment -- see that module's `_normalize_path`)
    and `read_pass.enumerate_repo_peers` (self-exclusion by session id, never
    by name). No second enumeration and no second cwd filter is built here.
    """
    agents = read_pass.fetch_live_agents(repo_root)
    return read_pass.enumerate_repo_peers(agents, caller_session_id)


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


def _transcript_idle_seconds(repo_root: str, session_id: str, cwd: Optional[str], now: datetime) -> Optional[float]:
    """Seconds since this peer's transcript last moved, or `None` if unreadable.

    Calls `read_pass._transcript_mtime_epoch` -- the one place a session id
    becomes a transcript mtime, extracted for exactly this reuse (module
    docstring). No second `getmtime` site is added here.
    """
    mtime_epoch = read_pass._transcript_mtime_epoch(session_id, cwd or repo_root)
    if mtime_epoch is None:
        return None
    return now.timestamp() - mtime_epoch


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
) -> str:
    """Compose one PARKED line -- observed evidence, framed as evidence."""
    stamped_age = _stamped_age_seconds(repo_root, session_id, now)
    transcript_idle = _transcript_idle_seconds(repo_root, session_id, cwd, now)
    obligations_summary = _obligation_summary(repo_root, session_id)
    reason = verdict.get("reason")
    return (
        f"PARKED session={session_id} reason={reason} "
        f"stamped_age={_fmt_seconds(stamped_age)} "
        f"transcript_idle={_fmt_seconds(transcript_idle)} "
        f"obligations={obligations_summary} "
        f"(observed, not asserted -- overturn if wrong)"
    )


def poll_once(
    repo_root: str,
    caller_session_id: str,
    prev_parked: dict[str, bool],
    now: Optional[datetime] = None,
    emit: Callable[[str], None] = print,
) -> dict[str, bool]:
    """One poll: classify, diff against `prev_parked`, emit any PARKED lines.

    Returns this tick's `{session_id: parked_bool}` map -- the caller's new
    `prev_parked` for the next call. Never raises: `main`'s loop is the
    coverage boundary (module docstring's POLL-ERROR contract), but this
    function is also exercised directly by tests, so it stays a plain
    computation the caller can drive without a live registry.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    now_epoch = now.timestamp()

    agents = _current_agents(repo_root, caller_session_id)
    agents_by_id = {
        a.get("sessionId"): a for a in agents if isinstance(a.get("sessionId"), str)
    }
    verdicts = _classify_all(repo_root, agents, now=now)
    cur_parked = {sid: bool(v.get("candidate")) for sid, v in verdicts.items()}

    for session_id in transitions(prev_parked, cur_parked):
        if _cooldown_active(repo_root, caller_session_id, session_id, now_epoch):
            continue
        peer = agents_by_id.get(session_id, {})
        line = _parked_line(
            repo_root, caller_session_id, session_id, verdicts[session_id], peer.get("cwd"), now
        )
        emit(line)

    return cur_parked


def main(
    repo_root: str,
    caller_session_id: Optional[str] = None,
    stream: Optional[TextIO] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_iterations: Optional[int] = None,
) -> None:
    """Arm the watch: print `ARMED`, then poll forever (or `max_iterations`
    times, for tests), emitting one line per PARKED transition.

    `caller_session_id` defaults to `read_pass.caller_session_id()` (the
    harness's own `CLAUDE_CODE_SESSION_ID`) -- never guessed from roster
    shape. Coverage: any exception raised inside one poll iteration is
    caught here, reported as a `POLL-ERROR` line, and the loop continues --
    a poll that raises must never silently end the watch (module docstring).
    """
    if caller_session_id is None:
        caller_session_id = read_pass.caller_session_id()

    # LATE-BOUND, deliberately. `stream: TextIO = sys.stdout` freezes whatever
    # stdout was at IMPORT time, so anything that replaces it afterwards -- a
    # harness wrapping the stream, a test capturing it -- is written past rather
    # than to. For a process whose entire product is its stdout lines, a stream
    # captured before the caller existed is the wrong one by default.
    out = sys.stdout if stream is None else stream

    def emit(line: str) -> None:
        print(line, file=out, flush=True)

    snapshot_ms, denominator = _measure_snapshot_ms(repo_root)
    interval = _poll_interval_seconds(snapshot_ms)
    emit(f"ARMED denominator={denominator} claude-klabauter peers, snapshot={snapshot_ms:.1f}ms, interval={interval:.1f}s")

    prev_parked: dict[str, bool] = {}
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        try:
            prev_parked = poll_once(repo_root, caller_session_id, prev_parked, emit=emit)
        except Exception:
            emit("POLL-ERROR " + traceback.format_exc(limit=1).strip().replace("\n", " | "))
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
        python -m coordinator_core.group_em.watch --repo-root <path>
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
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Stop after N poll iterations instead of running until the session ends. "
             "For probes and tests; omit for a real arm.",
    )
    args = parser.parse_args(argv)

    try:
        main(
            args.repo_root,
            caller_session_id=args.caller_session_id,
            max_iterations=args.max_iterations,
        )
    except KeyboardInterrupt:
        # A stopped Monitor is an ordinary end, not a failure -- exit quietly so
        # the run does not read as a crash in whatever armed it.
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via _cli in tests
    raise SystemExit(_cli())
