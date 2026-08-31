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
asked. `holder_name` is always written null: a name is an address that
re-points, and the DoE reader already prefers the live registry row over any
stored copy.

NEVER RAISES, NEVER GATES. A failed stamp is a missed tick -- it must never
end a watch that is otherwise working. `stamp` returns True/False; it does
not decide anything, does not read the clock to choose whether to fire, and
does not act on a stale verdict (acting on staleness is out of scope on both
planes).
"""

from __future__ import annotations

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


def _write_atomic(path: str, payload: dict) -> bool:
    """Temp-file then `os.replace` -- atomic on POSIX and Windows alike.

    Returns False on any I/O failure rather than raising: the caller is a poll
    loop whose product is its stdout lines, and a heartbeat that could not be
    written is a missed tick, not a reason to stop watching.
    """
    directory = os.path.dirname(path)
    tmp_path = None
    try:
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


def stamp(
    repo_root: str,
    holder_session_id: str,
    declinations: list,
    interval_seconds: float,
    subscribed_peers: int = 1,
    now_epoch: Optional[float] = None,
) -> bool:
    """Rewrite the heartbeat for one tick. Whole-file replace, never a fold.

    `declinations` is THIS tick's rows only -- each `{session_id, name, gate,
    reason}` -- never an accumulating history: that is what lets a reader tell
    "looked, nothing to do" apart from "did not look". A tick that emitted and
    declined nothing passes `[]`.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    payload: dict[str, Any] = {
        "holder_session_id": holder_session_id,
        "holder_name": None,
        "last_tick_at": _iso(now_epoch),
        "tick_source": TICK_SOURCE,
        "next_expected_by": next_expected_by(now_epoch, interval_seconds),
        "subscribed_peers": subscribed_peers,
        "declinations": list(declinations or []),
    }
    return _write_atomic(watch_path(repo_root), payload)
