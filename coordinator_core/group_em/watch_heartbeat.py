"""coordinator_core.group_em.watch_heartbeat -- the standing watch's on-disk
presence stamp.

PURPOSE. `<repo_root>/state/group-em-watch.json` is how a session OTHER than
the watcher learns that a watch exists at all. It is read on the DoE plane by
`coordinator/skills/group-em/watch_heartbeat.read_watch`, which feeds the
`GROUP EM WATCH: <verdict>` line on the SessionStart presence hook
(`coordinator/hooks/hooks.json`, the Group EM watch presence registration).
Until this module existed, `group_em.watch` -- the standing `Monitor` runnable
that is supposed to REPLACE hand-ticking -- wrote no such stamp, so a crown
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

WHO THE HOLDER IS. `holder_session_id` is the CROWN's session id, never the
watching process's, whenever the two differ (a crown that dispatches a
teammate to hold the watch -- see `watch.main`'s `crown_session_id`). The
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
#: a loud failure here.
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
        # caller handed a mangled root created a repo-shaped directory wherever
        # that path landed: measured 2026-09-01, a drive-relative
        # `X:example-game-workbench-repo` resolved against the writer's cwd and this
        # function created `<a sibling checkout>/example-game-workbench-repo/state/`
        # and wrote the heartbeat into it. The stray tree was inside a publish
        # mirror, where it failed a publish row's content check and blocked the
        # round for the whole fleet (claude-klabauter-c0).
        #
        # An arm-time check on `--repo-root` is the other half of that fix and
        # is not this half: it only covers callers that came through a CLI. A
        # writer that can conjure a repo directory is doing something no correct
        # caller ever needs, so the refusal belongs HERE too -- the repo root
        # must already exist, and only the `state/` leaf under it is ours to
        # create.
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
VERDICT_VACANT = "vacant"
VERDICT_STALE = "stale"
VERDICT_ARMED = "armed"

#: The re-arm command every non-`armed` verdict carries. A liveness report that
#: says the watch is dead and leaves the reader to reconstruct the invocation is
#: half a report: the launcher name is the whole point (the `python -m` spelling
#: resolves only from a cwd that can already import the engine, which the repos
#: this watch is armed FOR generally cannot -- 2026-09-01, example-game-workbench-repo).
REARM_COMMAND = (
    "group-em-watch --repo-root <root> --crown-session-id <your sid>   "
    "(hold it with Monitor, persistent: true; or fire "
    "`group-em-watch --repo-root <root> --crown-session-id <sid> --once` on a cron floor)"
)


def _read_record(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def read_liveness(
    repo_root: str,
    agents: Optional[list] = None,
    now_epoch: Optional[float] = None,
) -> dict:
    """Is anything actually watching this repo? `absent` | `vacant` | `stale` | `armed`.

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

    `agents`, when given, is the already-fetched registry -- one enumeration,
    two consumers, never a second spawn at this layer. Omitted, the holder-
    liveness join is skipped entirely and the verdict is freshness-only: a
    registry nobody read cannot retire a holder, and reporting `vacant` on no
    evidence would send a crown to re-arm over a watcher that is fine.

    Every verdict but `armed` carries `remedy`. A liveness leg that reports a
    dead watch and no way to restart it just moves the prose one file over.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    record = _read_record(watch_path(repo_root))
    if record is None:
        return {
            "verdict": VERDICT_ABSENT,
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

    if agents is not None:
        listed = any(
            isinstance(a, dict) and a.get("sessionId") == holder_session_id for a in agents
        )
        if not listed:
            # `absent` and `vacant` never collapse, matching the sibling reader:
            # no file is "nobody ever entered"; a file naming a session that is
            # gone is "the watcher exited".
            return {"verdict": VERDICT_VACANT, "seconds_overdue": None,
                    "remedy": REARM_COMMAND, **base}

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
