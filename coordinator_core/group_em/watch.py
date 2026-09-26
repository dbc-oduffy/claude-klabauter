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

FOLDED FROM DoE-claude, NOT PORTED AS A SEPARATE MODULE (W2-C1,
`docs/plans/2026-09-18-doe-holds-no-scripts.md`): `coordinator/hooks/scripts/_watch_module.py`
resolved `watch_heartbeat` by `sys.path`-inserted file path (its own docstring: "not an importable
package name ... resolved by file path") because the DoE-plane skills directory it lived beside
carries a hyphen. That whole resolution workaround has no reason to exist here -- `watch_heartbeat`
is already an ordinary importable sibling in this package. Its verdict-rendering surface
(`resolve_watch_module`, `vacant_verdict`, `age_phrase`, `render_verdict_line`, `render_watch_line`)
duplicates, wholesale, what `watch_heartbeat.read_liveness` + `watch_heartbeat.human_verdict`
already compute in-process in this repo -- including the four-verdict ladder (armed/stale/absent,
plus `vacant`'s DoE-only case, which this repo's `read_liveness` deliberately dropped for lack of a
caller; see that function's own docstring) and the age-phrase rendering (`timestamps.age_phrase`).
Porting a second renderer that reimplements an already-present one is the duplication this module's
own fold instruction names, not a gap to fill: there is nothing left in `_watch_module.py` that
this repo does not already have, so no `_watch_module.py` file is created here. A future caller
needing the DoE-plane hook's exact fail-open `render_watch_line(repo_root) -> Optional[str]` shape
should call `watch_heartbeat.read_liveness` + `watch_heartbeat.human_verdict` directly rather than
reintroducing the by-path resolver this note retires.
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
from coordinator_core import memo_corpus
from coordinator_core.group_em import read_pass
from coordinator_core.group_em import repo_root_arg
from coordinator_core.group_em import send_pass
from coordinator_core.group_em import watch_heartbeat
from coordinator_core.group_em import watch_spool
from coordinator_core.session.receiver_state import read_receiver_state

#: a-fix line: the poll interval is 1000x the MEASURED `snapshot()` cost
_POLL_INTERVAL_FLOOR_SECONDS = 5.0
_POLL_INTERVAL_MEASURED_MULTIPLIER = 1000.0

_POLL_INTERVAL_CEILING_SECONDS = 300.0

_COOLDOWN_SECONDS = send_pass.DEFAULT_COOLDOWN_SECONDS

_INBOX_FRONTMATTER_HEAD_LINES = 14

_INBOX_OPEN_STATUS = "open"


def _inbox_frontmatter_status(path: str) -> Optional[str]:
    """The `status:` value from a memo's frontmatter head, or `None`.

    Reads only the first `_INBOX_FRONTMATTER_HEAD_LINES` lines -- a full
    parse is not needed for one scalar key, and this repo's own memos never
    put `status:` past line 5. `None` on any read failure or absent key:
    an unreadable memo is not an open one, but it is also not silently
    dropped from `total_count` -- the caller counts the file either way.
    """
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
    taken_at_epoch = time.time()
    corpus_root = memo_corpus.memo_corpus_root(str(repo_root))
    inbox_dir = os.path.join(corpus_root, "inbox")
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
    population = f"inbox memos open, of {total_count} total"
    return (
        f"INBOX {open_count} ({population}) "
        f"counts_struck_at={watch_heartbeat.iso_instant(taken_at_epoch)}"
    )


_PARKED_STATE_RELATIVE_PATH = os.path.join("state", "group-em-watch-parked.json")

_CRON_FLOOR_INTERVAL_SECONDS = 23 * 60.0

#: `watch_heartbeat._STAMP_FORMAT`, matched deliberately: a reader comparing a
_GONE_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

def _measure_snapshot_ms(repo_root: str) -> tuple[float, list]:
    started = time.monotonic()
    agents = read_pass.fetch_live_agents(repo_root)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    return elapsed_ms, list(agents)


def _holder_name(agents: list, session_id: Optional[str]) -> Optional[str]:
    if not session_id:
        return None
    for agent in agents:
        if isinstance(agent, dict) and agent.get("sessionId") == session_id:
            name = agent.get("name")
            return name if isinstance(name, str) and name else None
    return None


def _poll_interval_seconds(snapshot_ms: float) -> float:
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
    verdicts: dict[str, dict[str, Any]] = {}
    for peer in agents:
        session_id = peer.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            continue
        verdicts[session_id] = read_pass.classify_peer(repo_root, peer, now=now)
    return verdicts


def transitions(prev: dict[str, bool], cur: dict[str, bool]) -> list[str]:
    return sorted(
        session_id
        for session_id, parked_now in cur.items()
        if parked_now and session_id in prev and not prev[session_id]
    )


def gone(prev: dict[str, bool], cur: dict[str, bool]) -> list[str]:
    return sorted(session_id for session_id in prev if session_id not in cur)


def _obligation_summary(repo_root: str, session_id: str) -> str:
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

    `caller_session_id` was accepted but never
    read in this body (Pyright: reportUnusedVariable-adjacent, unused param);
    the caller never needed the callee to see its own id here. Removed rather
    than kept for signature parity nobody was relying on.

    `name` IS PROVENANCE, NOT AN ADDRESS, and the line says so. A reader
    cannot act on a session uuid -- `SendMessage` takes a name -- so a line
    carrying only the uuid asks the Group-EM to go resolve one, and the resolve
    is a second read of a registry this tick already held. The name goes on.

    IDENTIFY-BY-SID, ADDRESS-BY-RESOLVED-NAME (the same convention
    `coordinator-safe-commit.py`'s holder refusal states, per DoE ruling 6 on
    `2026-09-11-doe-claude-em-rulings-owed-bundle.md`): the sid is the stable
    identifier this line is ABOUT, and the name is the address resolved from
    the registry at the moment this line was composed, not a permanently
    stable claim either way -- a session can be renamed, and a sid this line
    printed can stop resolving by the time it is read. So the line must NOT
    tell the reader to re-resolve from the printed sid as though that were a
    check that always succeeds: by the time it would matter -- the peer has
    gone or been re-pointed -- the sid is exactly as likely to have stopped
    resolving as the name is to be stale. `verify before sending` is the
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


def _displacement_line(record: dict, now: datetime) -> str:
    """One DISPLACED line: a successor took this repo's watch record.

    ITEM 1, the memo's gated ask. `main`'s held loop emits this and exits
    the moment its own `stamp()` is declined by a record whose HOLDER is no
    longer this watch's own (`watch_heartbeat.displacement_record`) --
    never on a same-holder writer or `tick_source` mismatch, which is the
    same crown's other instrument declining, not a displacement (see that
    function's own docstring and `is_fresh_and_foreign`'s HOLDER-OR-WRITER
    note). Before this, the displaced watch's `stamp()` kept declining
    silently and the loop polled on forever: E2 in the memo's reproduction
    (`A/A/monitor/4 prior=B`, `B arm+90s REFUSED`) -- the orphan retakes the
    record once the successor's own lease lapses and refuses the live
    successor from then on.
    """
    holder_name = record.get("holder_name")
    holder_session_id = record.get("holder_session_id")
    if holder_name and holder_session_id:
        who = f"{holder_name} [{holder_session_id}]"
    else:
        who = str(holder_session_id or holder_name or "an unknown holder")
    return (
        f"DISPLACED now_held_by={who} as_of={watch_heartbeat.iso_instant(now.timestamp())} "
        "(this watch's record now names a different holder -- a successor entered and "
        "took over the crown; exiting rather than polling on toward retaking an expired "
        "lease and refusing the live successor)"
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

    These were an out-parameter
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

    watched_repo = os.path.basename(os.path.abspath(str(repo_root))) or str(repo_root)
    for session_id in gone(prev_parked, cur_parked):
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
    return os.path.join(repo_root, _PARKED_STATE_RELATIVE_PATH)


def load_prev_parked(repo_root: str) -> dict[str, bool]:
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
    payload: dict[str, Any] = {"parked": dict(parked)}
    if peers is not None:
        payload["peers"] = dict(peers)
    return watch_heartbeat.write_atomic(parked_state_path(repo_root), payload)


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

    # docstring, "LAZY, WITH HYSTERESIS").
    tick_now = now if now is not None else datetime.now(timezone.utc)

    try:
        # docstring's CONCURRENT WAKES note) and there is no on-disk carry
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

    # it again (a DUPLICATE line); the reviewer's suggested reorder
    # the departure SILENTLY -- the exact "fleet went quiet and nobody said
    save_prev_parked(repo_root, cur_parked, peers=peer_notes)
    watch_heartbeat.stamp(
        repo_root,
        holder_session_id=group_em_session_id or "",
        declinations=declinations,
        interval_seconds=tick_interval_seconds,
        tick_source="cron",
        subscribed_peers=len(cur_parked),
        writer_session_id=caller_session_id,
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

    TEARS ITSELF DOWN ON DISPLACEMENT (item 1, the memo's gated ask). When a
    tick's own `stamp()` is declined because a SUCCESSOR now holds this
    repo's record (`watch_heartbeat.displacement_record`), this loop emits
    one `DISPLACED` line and returns -- it does not keep polling toward
    retaking the record once the successor's own lease lapses, which is the
    E2 defect the memo reproduces (an orphan stamping every tick while the
    live successor reads `REFUSED`). A same-holder writer or `tick_source`
    mismatch is NOT a displacement and stays the pre-existing quiet decline
    (see `displacement_record`'s own docstring).
    """
    caller_session_id, group_em_session_id = _resolve_caller_and_gem_ids(
        caller_session_id, group_em_session_id
    )
    _refuse_if_already_armed(
        repo_root, group_em_session_id, caller_session_id, now_epoch=now_epoch
    )

    # LATE-BOUND, deliberately. `stream: TextIO = sys.stdout` freezes whatever
    out = sys.stdout if stream is None else stream
    emit = _emit_for(out)

    snapshot_ms, agents = _measure_snapshot_ms(repo_root)
    peer_count = len(agents)
    # SAME EXCLUSION AS `_current_agents`, applied to the enumeration already in
    excluding_caller = read_pass.enumerate_repo_peers(agents, caller_session_id)
    if group_em_session_id is not None and group_em_session_id != caller_session_id:
        excluding_caller = read_pass.enumerate_repo_peers(
            excluding_caller, group_em_session_id
        )
    peer_count_excluding_caller = len(excluding_caller)
    holder_name = _holder_name(agents, group_em_session_id)
    interval = _poll_interval_seconds(snapshot_ms)
    resolved_root = os.path.abspath(str(repo_root))
    watched_repo = os.path.basename(resolved_root) or str(repo_root)
    # THE RESOLVED PATH GOES ON THE LINE, not just the derived name -- the
    armed_struck_epoch = time.time() if now_epoch is None else now_epoch
    armed_struck_at = watch_heartbeat.iso_instant(armed_struck_epoch)
    emit(
        f"ARMED peer_count={peer_count} {watched_repo} peers at {resolved_root}, "
        f"snapshot={snapshot_ms:.1f}ms, interval={interval:.1f}s, "
        f"roster=(peers seen including this caller), "
        f"peer_count_excluding_caller={peer_count_excluding_caller} "
        f"(matches subscribed_peers below), as_of={armed_struck_at}"
    )

    prev_parked: dict[str, bool] = {}
    prev_names: dict[str, dict[str, Any]] = {}
    prev_inbox_open: Optional[int] = None
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        declinations: list = []
        tick_now = datetime.now(timezone.utc)
        try:
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
            stamped = watch_heartbeat.stamp(
                repo_root,
                holder_session_id=group_em_session_id or "",
                declinations=declinations,
                interval_seconds=interval,
                # THE PEERS THIS TICK ACTUALLY LOOKED AT, never the default 1.
                subscribed_peers=len(cur_parked),
                holder_name=holder_name,
                writer_session_id=caller_session_id,
            )
            if not stamped:
                # ITEM 1 (the memo's gated ask): DISPLACED-WATCH TEARDOWN.
                displaced_by = watch_heartbeat.displacement_record(
                    repo_root, group_em_session_id or "", caller_session_id
                )
                if displaced_by is not None:
                    emit(_displacement_line(displaced_by, tick_now))
                    return
            _prune_spool(repo_root)
            prev_parked = cur_parked
            prev_names = peer_notes
            prev_inbox_open = cur_inbox_open
        except Exception:
            # module's COVERAGE contract exists to prevent.
            # POLL-ERROR line. That collapse is deliberate for now: both call
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
             "nothing. Exit 0 alive, 1 not running, 2 unknown (no watch ever armed, an "
             "unreadable record, or a fresh record whose writing process cannot be "
             "confirmed running) -- unknown is never reported as healthy.",
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

    try:
        args.repo_root = repo_root_arg.resolve_repo_root_arg(args.repo_root)
    except repo_root_arg.RepoRootArgError as exc:
        print(f"group-em-watch: {exc}", file=sys.stderr)
        return 2

    if args.status:
        liveness = watch_heartbeat.read_liveness(args.repo_root)
        print(watch_heartbeat.human_verdict(liveness))
        if liveness["verdict"] == watch_heartbeat.VERDICT_ARMED:
            # deadline is STALENESS evidence only -- it says the record's
            if watch_heartbeat.process_confirmed_alive(liveness) is not True:
                print(
                    "  (the record is fresh, but the process that wrote it "
                    "cannot be confirmed running -- reporting UNKNOWN rather "
                    "than ALIVE)"
                )
                return 2
            return 0
        return 1 if liveness["verdict"] == watch_heartbeat.VERDICT_STALE else 2

    if args.once:
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
        return 0
    except WatchAlreadyHeldError as exc:
        # NON-ZERO AND NAMED, never a silent no-op: an arm that quietly does
        print(f"group-em-watch: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via _cli in tests
    raise SystemExit(_cli())
