"""coordinator_core.group_em.watch_heartbeat -- the standing watch's on-disk
presence stamp.

PURPOSE. `<repo_root>/state/group-em-watch.json` is how a session OTHER than
the watcher learns that a watch exists at all. It is read on the DoE plane by
`coordinator/skills/group-em/watch_heartbeat.read_watch`, which feeds the
`GROUP EM WATCH: <verdict>` line on the SessionStart presence hook
(`coordinator/hooks/hooks.json`, the Group EM watch presence registration).
Until this module existed, `group_em.watch` -- the standing `Monitor` runnable
that is supposed to REPLACE hand-ticking -- wrote no such stamp, so a Group-EM
that armed it correctly and stopped hand-stamping read to every other session
in the fleet as a repo with no watcher at all. The file going quiet is that
reader's `stale`/`absent` signal; a correct arm must not produce it.

THE RECORD SHAPE IS A CROSS-PLANE CONTRACT, not this module's preference. The
seven keys below are exactly what the DoE reader reads, in the timestamp
format it parses (`%Y-%m-%dT%H:%M:%SZ`, `calendar.timegm` -- naive UTC). We
write it rather than import it: the reader lives in a repo claude-klabauter does not
own, and file-path importing a sibling's skill module on the watch's poll
path is the coupling this boundary exists to refuse. `tests/test_watch_heartbeat.py`
pins the key set, and names the reader it is pinned against.

`tick_source: "monitor"` is not a new vocabulary word -- it is the third of
the three the DoE writer already declares (`cron` | `monitor` | `entry`) and
the only one no writer produced until now. A reader can therefore tell a
`Monitor`-held watch apart from an entry stamp without any change on its side.

WHO THE HOLDER IS. `holder_session_id` is the GROUP-EM's session id, never the
watching process's, whenever the two differ (a Group-EM that dispatches a
teammate to hold the watch -- see `watch.main`'s `group_em_session_id`). The
holder is the session accountable for the fleet, and the record exists so
handover is legible from outside; naming a teammate that dies with its
dispatch would make the record answer a different question than the one it is
asked. `holder_name` is SELF-DESCRIPTION, never an address: a name re-points,
so every reader that can reach the registry prefers the live row over this
copy (the DoE reader does exactly that). It is written for the reader that
cannot -- another machine, or the record read cold after the fact, which is
precisely when self-description is the only thing left. Resolved once at arm
time off the enumeration the caller already made; a nameless writer carries
the previous tick's name forward rather than blanking it.

NEVER RAISES, NEVER GATES. A failed stamp is a missed tick -- it must never
end a watch that is otherwise working. `stamp` returns True/False; it does
not decide anything, does not read the clock to choose whether to fire, and
does not act on a stale verdict (acting on staleness is out of scope on both
planes).
"""

from __future__ import annotations

import calendar
import json
import os
import tempfile
import time
from typing import Any, Optional

_WATCH_RELATIVE_PATH = os.path.join("state", "group-em-watch.json")

#: How long past this tick a reader should still call the watch armed. The
#: watch's own poll interval is derived at arm time (see
#: `watch._poll_interval_seconds`), so the deadline is a multiple of THAT
#: measurement rather than a fixed window: three missed polls, floored at a
#: minute so a fast interval cannot make the record flicker STALE on one slow
#: tick under fleet load.
_GRACE_TICKS = 3
_GRACE_FLOOR_SECONDS = 60.0

TICK_SOURCE = "monitor"

#: The three words the DoE reader already declares. A tick that cannot name
#: itself one of them is a writer bug, not a record to write: an unknown word
#: reads to that reader as a watch of unknown provenance, which is worse than
#: a loud failure here. This repo's OWN writers produce only two of the
#: three -- `monitor` (`watch.main`) and `cron` (`watch.tick_once`); `entry`
#: is the sibling DoE reader's word to write, never ours. The allow-list is
#: deliberately wider than this repo's own producer set, mirroring the
#: reader's declared vocabulary rather than narrowing to what we emit.
TICK_SOURCES = ("cron", "monitor", "entry")

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def watch_path(repo_root: str) -> str:
    """Absolute path of the heartbeat file for `repo_root`."""
    return os.path.join(repo_root, _WATCH_RELATIVE_PATH)


def _iso(epoch: float) -> str:
    return time.strftime(_STAMP_FORMAT, time.gmtime(epoch))


def next_expected_by(now_epoch: float, interval_seconds: float) -> str:
    """The deadline this tick promises the next one by, as the reader parses it."""
    grace = max(_GRACE_FLOOR_SECONDS, interval_seconds * _GRACE_TICKS)
    return _iso(now_epoch + grace)


def write_atomic(path: str, payload: dict) -> bool:
    """Temp-file then `os.replace` -- atomic on POSIX and Windows alike.

    Returns False on any I/O failure rather than raising: the caller is a poll
    loop whose product is its stdout lines, and a heartbeat that could not be
    written is a missed tick, not a reason to stop watching.

    Public because `group_em.watch` persists a second small JSON record next to
    this one (the carried parked map a single-tick wake diffs against) and one
    atomic writer serving both is a copy fewer, not a coupling: the failure
    posture -- False, never raise, never gate -- is the same for both records.
    """
    directory = os.path.dirname(path)
    tmp_path = None
    try:
        # NEVER MINT A REPO. `makedirs` used to create the WHOLE chain, so a
        # caller handed a mangled root created a repo-shaped tree wherever that
        # path landed -- once inside a publish mirror, where it blocked the
        # round for the whole fleet. Full incident: `group_em.repo_root_arg`'s
        # module docstring.
        #
        # THIS IS NOT THE SAME FIX as that module's arm-time refusal, which
        # only covers callers that came through a CLI. A writer able to conjure
        # a repo directory is doing something no correct caller needs, so the
        # root must already exist and only the `state/` leaf under it is ours
        # to create.
        #
        # Review: overengineering-reviewer (finding #4, minor, accepted) --
        # the fourth full retelling of one incident across this diff, reduced
        # to a pointer plus the part that is this site's own reasoning.
        parent = os.path.dirname(directory)
        if parent and not os.path.isdir(parent):
            return False
        os.makedirs(directory, exist_ok=True)
        handle, tmp_path = tempfile.mkstemp(
            prefix=".group-em-watch-", suffix=".tmp", dir=directory
        )
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, path)
        return True
    except (OSError, ValueError, TypeError):
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def _carried_holder_name(repo_root: str, holder_session_id: str) -> Optional[str]:
    """The name the previous tick recorded, when it was for the SAME holder.

    A different holder's name is not carried: the record is whole-file replace
    and a stale name beside a new id is worse than no name at all.
    """
    record = _read_record(watch_path(repo_root))
    if not isinstance(record, dict):
        return None
    if record.get("holder_session_id") != holder_session_id:
        return None
    carried = record.get("holder_name")
    return carried if isinstance(carried, str) and carried else None


def stamp(
    repo_root: str,
    holder_session_id: str,
    declinations: list,
    interval_seconds: float,
    subscribed_peers: int = 1,
    now_epoch: Optional[float] = None,
    tick_source: str = TICK_SOURCE,
    holder_name: Optional[str] = None,
    writer_session_id: Optional[str] = None,
) -> bool:
    """Rewrite the heartbeat for one tick. Whole-file replace, never a fold.

    `declinations` is THIS tick's rows only -- each `{session_id, name, gate,
    reason}` -- never an accumulating history: that is what lets a reader tell
    "looked, nothing to do" apart from "did not look". A tick that emitted and
    declined nothing passes `[]`.
    """
    if tick_source not in TICK_SOURCES:
        raise ValueError(
            f"tick_source {tick_source!r} is not one of the reader's words {TICK_SOURCES}"
        )
    now_epoch = time.time() if now_epoch is None else now_epoch
    if holder_name is None:
        holder_name = _carried_holder_name(repo_root, holder_session_id)
    payload: dict[str, Any] = {
        "holder_session_id": holder_session_id,
        "holder_name": holder_name,
        "last_tick_at": _iso(now_epoch),
        "tick_source": tick_source,
        "next_expected_by": next_expected_by(now_epoch, interval_seconds),
        "subscribed_peers": subscribed_peers,
        "declinations": list(declinations or []),
        "writer_session_id": writer_session_id,
    }
    return write_atomic(watch_path(repo_root), payload)


VERDICT_ABSENT = "absent"
VERDICT_STALE = "stale"
VERDICT_ARMED = "armed"

#: Why an `absent` verdict is absent. Never a fourth verdict -- see the branch
#: in `read_liveness` that sets it.
ABSENT_NEVER_ARMED = "never-armed"
ABSENT_UNREADABLE = "unreadable-record"

#: The re-arm command every non-`armed` verdict carries. A liveness report that
#: says the watch is dead and leaves the reader to reconstruct the invocation is
#: half a report: the launcher name is the whole point (the `python -m` spelling
#: resolves only from a cwd that can already import the engine, which the repos
#: this watch is armed FOR generally cannot -- 2026-09-01, example-game-workbench-repo).
REARM_COMMAND = (
    # Review: coordinator:code-reviewer (a6cd5e5f3f553af13) -- trailing
    # whitespace before the parenthetical was a stray formatting artifact.
    "group-em-watch --repo-root <root> --group-em-session-id <your sid> "
    "(hold it with Monitor, persistent: true; or fire "
    "`group-em-watch --repo-root <root> --group-em-session-id <sid> --once` on a cron floor)"
)


def _read_record(path: str) -> Optional[dict]:
    """Best-effort record read; `None` for both "no file" and "unreadable".

    Deliberate collapse at the VERDICT level, and only there: the cross-plane
    reader this record is written for
    (`X:/DoE-claude/coordinator/skills/group-em/watch_heartbeat.py::read_watch`,
    via its own `_read_existing`) makes the identical collapse -- a missing
    file and a present-but-unparseable one both fall through to its `None`
    branch and read `absent`. A fourth verdict word here would make this
    module's vocabulary answer a question that reader cannot ask back.

    The distinction itself is NOT lost, and an earlier revision of this note
    said it was: `read_liveness` carries `absent_reason` beside the unchanged
    verdict (`ABSENT_NEVER_ARMED` | `ABSENT_UNREADABLE`), because the two mean
    the same thing to a program -- nothing is watching -- and different things
    to the person deciding what to do about it. Arming a watch fixes one of
    them and not the other. That is a detail on our own reader, not a word in
    the foreign one's vocabulary, which is why it costs that reader nothing.
    (Review: review-integrator, carried tradeoff from a sibling integrator's
    escalation, resolved rather than carried once a human-facing reader
    existed to need it.)
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def read_liveness(
    repo_root: str,
    now_epoch: Optional[float] = None,
) -> dict:
    """Is anything actually watching this repo? `absent` | `stale` | `armed`.

    WHY A SECOND READER EXISTS AT ALL. `group_em.teammates.presence` answers
    "did this session dispatch a watcher", on a dispatch record, and refuses a
    clock on purpose. That is the right evidence for that question and the
    wrong evidence for this one: a watcher that WAS dispatched and whose
    subprocess never started -- the `ModuleNotFoundError` reproduced from
    example-game-workbench-repo on 2026-09-01 -- has a perfectly good dispatch
    record and is watching nothing. The agent presented `idle`, and an idle
    watcher is indistinguishable from a quiet fleet from outside. So the two
    legs answer different questions and neither replaces the other.

    THIS IS NOT AN MTIME LIE. The freshness term here is not "a file was
    touched recently": `next_expected_by` is a deadline the previous tick
    WROTE for itself, off its own cadence. Missing a deadline you set is
    evidence; a file being old is not. That distinction is why the record
    carries the deadline at all.

    FRESHNESS ONLY, deliberately no holder-liveness join. A `vacant` verdict
    (holder session no longer in the registry) previously existed here behind
    an `agents` parameter; it had no production caller -- the sole in-repo
    reader (`ops/group_em_enter.py::_run_watch_liveness`) never passed it, and
    argued in its own docstring why passing it would be wrong from that
    caller (the registry join there could only answer "does the caller exist"
    or false-negative on an enumeration that omits self). A watcher that
    exited stops stamping and reads `stale` on the next tick anyway, which is
    the same finding by better evidence.
    (Review: overengineering-reviewer, finding #1, major, accepted -- dropped
    rather than kept for a hypothetical different-session reader; add it back
    only once a real one is named.)

    Every verdict but `armed` carries `remedy`. A liveness leg that reports a
    dead watch and no way to restart it just moves the prose one file over.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    path = watch_path(repo_root)
    record = _read_record(path)
    if record is None:
        # `absent` stays ONE verdict -- the sibling reader's vocabulary is not
        # ours to widen (see `_read_record`). `absent_reason` is a detail beside
        # it, for the reader that has to tell a human WHY: "nobody ever armed a
        # watch here" and "a record exists and will not read" both mean nothing
        # is watching, but only one of them is fixed by arming a watch.
        return {
            "verdict": VERDICT_ABSENT,
            "absent_reason": ABSENT_UNREADABLE if os.path.exists(path) else ABSENT_NEVER_ARMED,
            "holder_session_id": None,
            "holder_name": None,
            "last_tick_at": None,
            "tick_source": None,
            "seconds_overdue": None,
            "remedy": REARM_COMMAND,
        }

    holder_session_id = record.get("holder_session_id")
    # `holder_name` is carried as PROVENANCE -- how the holder was known when
    # the tick was written -- never as an address to send to and never as an
    # instruction to re-resolve from the sid beside it. In the case that
    # matters (the holder re-pointed or exited) that sid is exactly the one
    # that no longer resolves, so "re-resolve from it" reads like a check and
    # performs like a ritual, failing where a reader needs it most.
    base = {
        "holder_session_id": holder_session_id,
        "holder_name": record.get("holder_name"),
        "last_tick_at": record.get("last_tick_at"),
        "tick_source": record.get("tick_source"),
    }

    deadline = record.get("next_expected_by")
    if not isinstance(deadline, str):
        return {"verdict": VERDICT_STALE, "seconds_overdue": None,
                "remedy": REARM_COMMAND, **base}
    try:
        deadline_epoch = calendar.timegm(time.strptime(deadline, _STAMP_FORMAT))
    except ValueError:
        return {"verdict": VERDICT_STALE, "seconds_overdue": None,
                "remedy": REARM_COMMAND, **base}

    overdue = now_epoch - deadline_epoch
    if overdue > 0:
        return {"verdict": VERDICT_STALE, "seconds_overdue": round(overdue, 1),
                "remedy": REARM_COMMAND, **base}
    return {"verdict": VERDICT_ARMED, "seconds_overdue": None, **base}


def _age_phrase(seconds: float) -> str:
    """`41 minutes`, `3 seconds` -- a duration a human reads without a unit key."""
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


def _tick_age_seconds(last_tick_at: Any, now_epoch: float) -> Optional[float]:
    """Age of the last tick, or `None` when the record cannot say.

    Both sides are epoch seconds: the stamp is parsed as the UTC it declares
    (`calendar.timegm`, never `time.mktime`), and `now_epoch` is `time.time()`.
    A local-clock reading of a `Z` stamp is how this fleet has previously
    invented an hour of staleness that did not exist.
    """
    if not isinstance(last_tick_at, str):
        return None
    try:
        return now_epoch - calendar.timegm(time.strptime(last_tick_at, _STAMP_FORMAT))
    except ValueError:
        return None


def human_verdict(liveness: dict, now_epoch: Optional[float] = None) -> str:
    """`read_liveness` rendered for a person who has never heard of a heartbeat.

    WHY A RENDERER, AND NOT A FIELD ON THE RECORD. The three states this repo
    keeps collapsing -- watcher alive and correctly quiet, watcher dead or
    never started, no watcher ever armed -- are already distinct in
    `read_liveness`. The defect they cost us on 2026-09-01 was not that the
    engine could not tell them apart: it was that the only surface a human had
    said `idle` for all three, and the instrument that knew better answered
    only to something able to read a JSON file. So this function adds no
    signal; it moves the signal that exists into the vocabulary of the question
    actually being asked, which is "is my watch alive?".

    NEVER GREEN ON NO EVIDENCE. `absent` renders as UNKNOWN, never as healthy
    and never as an all-clear. "Nothing owed" and "nothing looked" rendering
    the same is the specific failure that produced this function; a reader who
    later shortens the absent branch to something reassuring reintroduces it.

    Prose, not a code: the caller decides the exit code (see `watch._cli`'s
    `--status`), and no consumer should parse these words back into a verdict.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    verdict = liveness.get("verdict")
    holder = liveness.get("holder_name") or liveness.get("holder_session_id") or "unknown holder"
    remedy = liveness.get("remedy") or REARM_COMMAND
    age = _tick_age_seconds(liveness.get("last_tick_at"), now_epoch)

    if verdict == VERDICT_ARMED:
        when = f"{_age_phrase(age)} ago" if age is not None else f"at {liveness.get('last_tick_at')}"
        lines = [
            f"ALIVE - a watch is running and checked the fleet {when}.",
            f"  Held by: {holder}",
            "  Quiet is the normal state between checks; it is not a fault.",
        ]
    elif verdict == VERDICT_STALE:
        overdue = liveness.get("seconds_overdue")
        late = (
            f" and is {_age_phrase(overdue)} past the deadline it set itself"
            if isinstance(overdue, (int, float))
            else " and has missed the deadline it set itself"
        )
        when = f"{_age_phrase(age)} ago" if age is not None else "at an unreadable time"
        lines = [
            f"NOT RUNNING - the watch last checked {when}{late}.",
            f"  Last held by: {holder}",
            "  Nothing is watching this repo now. Restart it:",
            f"    {remedy}",
        ]
    else:
        detail = (
            "a watch record exists here but cannot be read"
            if liveness.get("absent_reason") == ABSENT_UNREADABLE
            else "no watch has ever reported for this repo"
        )
        lines = [
            f"UNKNOWN - {detail}.",
            "  This is NOT an all-clear: nothing has looked. Arm a watch:",
            f"    {remedy}",
        ]
    return chr(10).join(lines)
