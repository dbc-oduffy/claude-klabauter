"""coordinator_core.group_em.watch_spool -- our half of the Group-EM wake
spool (sizing `state/sizings/2026-09-01-the-group-em-wake-gets-the-spool-it-is-m.yaml`,
deliverable `dlv-the-group-em-wake-gets-the-spool-it-is-m-bd7b96`).

PURPOSE. `<repo_root>/state/group-em-watch-spool.jsonl` is a transition spool
a DoE-plane `Stop` guard appends one line to per park -- see
`cross-repo/inbox/2026-09-02-doe-claude-em-group-em-wake-fires-on-stop.md`.
That producer is theirs, is not in this repo, and may not exist on disk yet;
this module is written against the AGREED CONTRACT, never against a live
producer, and this docstring is deliberately silent about how or when the
DoE-side hook fires -- that shape is theirs to own.

RECORD SHAPE, one JSON object per `\\n`-terminated line, four keys:

    {"session_id": "<parking peer's session id>", "state": "PAUSED:<reason>",
     "at": "2026-09-02T10:41:07Z", "writer": "receiver-state-sensor"}

`at` is `%Y-%m-%dT%H:%M:%SZ`, naive UTC -- the SAME format
`watch_heartbeat.iso_instant` writes and the DoE heartbeat reader already
parses. This module reuses `watch_heartbeat`'s own format constant
(`watch_heartbeat._STAMP_FORMAT`) and the SAME `calendar.timegm(time.strptime(...))`
idiom `watch_heartbeat._tick_age_seconds`/`is_fresh_and_foreign` already use --
no second parser, no second format is introduced here.

`state` IS DIAGNOSTIC CONTEXT, NEVER A CLASSIFIER INPUT. It carries OUR OWN
verdict back to us, round-tripped through the producer's hook -- `poll_once`
re-derives parked from the registry exactly as it does today
(`read_pass.classify_peer`). No function in this module reads a record's
`state` field for any decision; `read_records` yields it unread and
`should_suppress_wake` never looks at it. A caller that starts branching on it
would be building the exact second classifier `watch.py`'s own module
docstring already forbids.

UNKNOWN KEYS ARE IGNORED BY DESIGN -- the producer may add keys without a
version bump, and `read_records` yields whatever `json.loads` decoded rather
than validating a closed key set.

A LINE THAT FAILS TO PARSE IS SKIPPED, NEVER FATAL. `read_records` treats a
non-JSON line, a JSON scalar/array (not an object), and an absent file
identically: skip and continue. A torn interleaved append -- two producers'
writes landing byte-interleaved because appends are not themselves locked
against each other -- costs exactly the one record whose bytes were torn, not
the read of the whole spool. This mirrors `load_prev_parked`'s own posture in
`watch.py`: absent, unreadable, and malformed all degrade to "nothing here"
rather than raising.

PRODUCER-SIDE RULES THIS MODULE HOLDS TO, WITHOUT ENFORCING THEM: append-only,
never read/truncate/rotate, create-on-first-append, no-op if `state/` is
missing. This module's own `append` (below) follows the same rules -- it
exists for tests and a local/cron producer, and is NOT how the production
spool gets written; the production producer is the sibling plane's `Stop`
guard.

COMPACTION IS OURS ALONE, AND IS THE ONLY THING THAT EVER SHORTENS THE FILE.
`compact` drops every record at or older than a drain point the caller
supplies (typically the drain's own `now`, or the heartbeat's freshly-stamped
`last_tick_at`), and is safe under exactly one hazard: A CONCURRENT PRODUCER
APPENDING BETWEEN OUR READ AND OUR REPLACE MUST NOT BE LOST.

THE RACE, AND HOW `compact` DEFENDS AGAINST IT. `compact` reads the whole
file once (`content`, length `base_len`) and computes the kept lines from
that snapshot alone -- a producer append landing in the file AFTER this read
is invisible to that computation. To avoid discarding it, `compact` re-opens
the file, seeks to `base_len`, and reads whatever has been appended since
(`tail`) -- then repeats that seek-and-read up to
`_COMPACT_TAIL_STABILIZE_ATTEMPTS` times, stopping as soon as two consecutive
reads agree, i.e. nothing grew between them. The final payload is
`kept_lines + tail`, written via the same temp-file-then-`os.replace` atomicity
`watch_heartbeat.write_atomic` uses (Windows and POSIX alike). This closes the
race down to the residual window between the LAST stabilized tail read and the
`os.replace` call itself -- an append landing in that last sliver is still
lost, exactly the same shape of residual gap `watch_heartbeat.stamp`'s own
docstring accepts ("NO LOCK SPANS READ-DECIDE-WRITE, AND THIS IS A KNOWN,
UNCLOSED GAP") rather than adding a lock across a shared, append-only file on
a hot path under this repo's 500ms brightline. `watch.py`'s own module
docstring already documents "CONCURRENT `--once` WAKES ARE NOT LOCKED" for
the parked-map read-modify-write; this module does not become the thing that
finally requires one.

Compaction permanently drops a malformed line or a well-formed record with an
unparseable `at` -- neither can ever be judged "newer than the drain point"
or read back by `newest_at_epoch`, so keeping them only grows the file for no
future benefit.

THE DEBOUNCE. `should_suppress_wake` answers "does classification need to run
at all", for the PRE-FLIGHT `watch.tick_once` runs before its classify path
(`poll_once`) -- never inside `poll_once` itself, which stays the one-job
classifier. A wake self-suppresses (returns `True`, no roster read, no
`poll_once` call) only when a watch that reads `armed` has already covered
everything the spool knows about -- see `should_suppress_wake` for the three
terms and why each one is asked the way it is.

THE FRESHNESS TERM IS `read_liveness`, NOT `last_tick_at`'s PRESENCE. The
distinction is the whole safety of this debounce: a `stale` or `absent` watch
is exactly the vacant window the cron floor exists to cover, so a debounce
that suppressed against a dead poller would silence the fallback precisely
when it is the only thing still running -- the failure this line was opened
to close, reintroduced one layer down. Only `armed` suppresses.

MEASURED DRAIN COST (this box, `time.process_time()` delta, 200 iterations of
write+`newest_at_epoch`+`compact` over a 200-line spool, averaged): 3.4ms
process time per drain, well under the 200ms one-process-needs-a-fix line and
the 500ms brightline. A small line-oriented file read plus one JSON parse per
line does not approach either budget.

NEGATIVE SPEC -- what this module deliberately does not do:

- No lock, anywhere, on any file this module touches -- see the race note
  above for why, and what residual gap is accepted instead.
- No second `at`/timestamp parser and no second format -- every parse in this
  module goes through `_parse_iso_instant`, which is `watch_heartbeat`'s own
  `_STAMP_FORMAT` and its own `calendar.timegm(time.strptime(...))` idiom.
- No read/branch on a record's `state` field for any decision -- see "STATE
  IS DIAGNOSTIC CONTEXT" above.
- No production producer. `append` is for tests and an optional local/cron
  producer only; the DoE `Stop` guard is the production writer and is not
  built here, imported here, or assumed to exist on disk.
- No truncation, rotation, or read/modify-in-place outside `compact` --
  `read_records`, `newest_at_epoch`, and `append` never shorten or rewrite
  the file; only `compact` does, and only via the race-safe path above.
"""

from __future__ import annotations

import calendar
import json
import os
import tempfile
import time
from typing import Any, Iterator, Optional

from coordinator_core.group_em import watch_heartbeat

_SPOOL_RELATIVE_PATH = os.path.join("state", "group-em-watch-spool.jsonl")

#: How many times `compact` re-reads the post-snapshot tail before giving up
#: and writing whatever it last observed -- see the module docstring's race
#: note. Five attempts is generous for a small append-only file under normal
#: contention; it is a bound on retries, not a timer.
_COMPACT_TAIL_STABILIZE_ATTEMPTS = 5

#: `watch_heartbeat._STAMP_FORMAT`, reused rather than restated -- see module
#: docstring.
_STAMP_FORMAT = watch_heartbeat._STAMP_FORMAT


#: How much of the spool's tail `newest_at_epoch` reads to find the newest
#: record. Sized against the sibling plane's post-landing measurement --
#: `PAUSED:turn-ended` is spooled at every turn end of every session, so the
#: file is thousands of lines rather than the handful "a park is rare" would
#: have predicted. 8 KiB is ~60 records at the shape the producer writes:
#: far more slack than concurrent appenders can reorder, and a fixed cost on
#: the wake path regardless of how long the spool has gone undrained.
_TAIL_WINDOW_BYTES = 8192


def spool_path(repo_root: str) -> str:
    """Absolute path of the transition spool for `repo_root`."""
    return os.path.join(repo_root, _SPOOL_RELATIVE_PATH)


def _parse_iso_instant(value: Any) -> Optional[float]:
    """`value` parsed as `_STAMP_FORMAT`, or `None` -- never a second format.

    Same idiom `watch_heartbeat._tick_age_seconds` and `is_fresh_and_foreign`
    already use (`calendar.timegm(time.strptime(...))`); a non-string, or a
    string that does not match, both answer `None` rather than raising.
    """
    if not isinstance(value, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(value, _STAMP_FORMAT)))
    except (ValueError, TypeError):
        return None


def read_records(repo_root: str) -> Iterator[dict]:
    """Yield each well-formed JSON object in the spool, in file order.

    Absent file, unreadable file, a malformed line, and a well-formed-JSON
    line that is not an object all degrade to "skip this one" -- see module
    docstring's "A LINE THAT FAILS TO PARSE IS SKIPPED, NEVER FATAL". This
    function performs no filtering beyond "is it a JSON object" -- unknown
    keys, a missing key, and an unparseable `at` are all still yielded; it is
    the caller's job (`newest_at_epoch`, `should_suppress_wake`) to decide
    what a record without a usable `at` means for them.
    """
    try:
        fh = open(spool_path(repo_root), "r", encoding="utf-8")
    except OSError:
        return
    try:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record
    finally:
        fh.close()


def newest_at_epoch(repo_root: str) -> Optional[float]:
    """The newest parseable `at` in the spool, or `None`.

    READS THE TAIL, NEVER THE WHOLE FILE, and the reason is a measurement the
    sibling plane took after the producer landed: the verdict it spools is
    `PAUSED:turn-ended`, which is the ordinary end of an ordinary turn, not a
    rare event. At ~20 concurrent sessions on this box that is thousands of
    records a day and hundreds of KB between drains. A debounce that read
    every line to find a maximum would put that whole read on the wake path,
    which is the load norm's own definition of a mechanism that occupies a
    busy box.

    The tail is enough because the file is append-only and its records are
    written in the order they occur, so the newest `at` is at the END. It is
    a WINDOW rather than the last line alone because concurrent appenders on
    ~20 sessions can interleave two records written the same second in either
    order; `_TAIL_WINDOW_BYTES` of slack absorbs that without a sort and
    without a lock.

    The first line of a mid-file window is dropped as presumptively partial
    (the window boundary is a byte offset, not a record boundary) unless the
    window covers the whole file, where there is nothing before it to have
    truncated. `None` covers "absent, empty, or nothing in the window carries
    a parseable `at`" -- all of which mean the same thing to
    `should_suppress_wake`: no evidence of a park it has not already seen.
    """
    path = spool_path(repo_root)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            window = min(size, _TAIL_WINDOW_BYTES)
            fh.seek(size - window)
            blob = fh.read(window)
    except OSError:
        return None

    lines = blob.decode("utf-8", errors="replace").splitlines()
    if window < size and lines:
        lines = lines[1:]

    newest: Optional[float] = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        epoch = _parse_iso_instant(record.get("at"))
        if epoch is None:
            continue
        if newest is None or epoch > newest:
            newest = epoch
    return newest


def should_suppress_wake(repo_root: str) -> bool:
    """Should the caller's classify path (`poll_once`) be skipped this wake?

    See the module docstring's "THE DEBOUNCE" section for the general rule.
    Suppression requires ALL THREE of the terms below, and every edge case is
    answered toward doing the work rather than skipping it -- a wrongly
    suppressed wake is a fleet nobody classified, which is the exact window
    this whole line exists to close.

      - THE WATCH MUST READ `armed`, asked of `watch_heartbeat.read_liveness`
        -- never of `last_tick_at`'s mere presence, and never of a constant
        age. `armed` means the previous tick met a deadline IT wrote for
        itself off its own cadence (that reader's "THIS IS NOT AN MTIME LIE"),
        so it is the only evidence that a healthy poller is genuinely covering
        this repo. `stale` and `absent` never suppress: a watch whose holder
        has exited is precisely the vacant window the cron floor is the
        belt-and-suspenders for, and debouncing the floor against a dead
        poller would silence the fallback exactly when it is the only thing
        left running.
      - THE SPOOL FILE ITSELF MUST EXIST. Absent means no producer has ever
        written here -- the sibling DoE-plane `Stop` guard may not be
        installed, or this repo may run on cron alone -- so there is no
        evidence to debounce against. A spool that exists but is EMPTY does
        suppress (under an `armed` watch): its presence is itself evidence a
        producer is wired up and has nothing new to say.
      - NOTHING IN IT MAY BE NEWER than the heartbeat's `last_tick_at`. A
        record at or older than the last tick has already been classified by
        that tick; a record newer than it has not been seen by anybody.
    """
    liveness = watch_heartbeat.read_liveness(repo_root)
    if liveness.get("verdict") != watch_heartbeat.VERDICT_ARMED:
        return False
    last_tick_epoch = _parse_iso_instant(liveness.get("last_tick_at"))
    if last_tick_epoch is None:
        return False
    if not os.path.exists(spool_path(repo_root)):
        return False
    newest = newest_at_epoch(repo_root)
    if newest is None:
        return True
    return newest <= last_tick_epoch


def append(
    repo_root: str,
    session_id: str,
    state: str,
    writer: str = "receiver-state-sensor",
    at_epoch: Optional[float] = None,
) -> bool:
    """Append one transition record. FOR TESTS AND AN OPTIONAL LOCAL/CRON
    PRODUCER ONLY -- the production producer is the sibling DoE-plane `Stop`
    guard (module docstring); this function is not that guard and does not
    claim to be.

    No-ops (returns `False`, never raises) when `repo_root/state` does not
    exist -- the same posture the module docstring names as a producer-side
    rule this module holds to. Creates the spool file on first append via
    ordinary append-mode `open`; never truncates or rewrites existing
    content, matching "append-only, never read/truncate/rotate".
    """
    state_dir = os.path.join(str(repo_root), "state")
    if not os.path.isdir(state_dir):
        return False
    at_epoch = time.time() if at_epoch is None else at_epoch
    record = {
        "session_id": session_id,
        "state": state,
        "at": watch_heartbeat.iso_instant(at_epoch),
        "writer": writer,
    }
    try:
        with open(spool_path(repo_root), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True))
            fh.write("\n")
        return True
    except OSError:
        return False


def _write_bytes_atomic(path: str, payload: bytes) -> bool:
    """Temp-file then `os.replace`, for the raw bytes `compact` writes.

    Same atomicity shape as `watch_heartbeat.write_atomic` (Windows and
    POSIX alike) but over bytes rather than a JSON payload -- `compact`'s
    result is a mix of retained JSON-line bytes and a raw tail read, not a
    single object to `json.dump`, so the JSON-shaped writer does not fit.
    Never mints the repo directory -- same refusal `write_atomic` documents
    (`group_em.repo_root_arg`'s incident): only the `state/` leaf is ours to
    create, and only when its own parent already exists.
    """
    directory = os.path.dirname(path)
    tmp_path = None
    try:
        parent = os.path.dirname(directory)
        if parent and not os.path.isdir(parent):
            return False
        os.makedirs(directory, exist_ok=True)
        handle, tmp_path = tempfile.mkstemp(
            prefix=".group-em-watch-spool-", suffix=".tmp", dir=directory
        )
        with os.fdopen(handle, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)
        return True
    except OSError:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def compact(repo_root: str, drain_point_epoch: float) -> bool:
    """Drop every record at or older than `drain_point_epoch`; keep the rest.

    Race-safe under a concurrent producer append -- see module docstring's
    "COMPACTION IS OURS ALONE" section for the full defence. Returns `True`
    when there is nothing to compact (absent spool) and whenever the write
    lands; `False` only on an I/O failure writing the replacement, in which
    case the spool is left untouched -- same non-raising posture as
    `watch_heartbeat.write_atomic`/`stamp`: a failed compaction costs disk
    space, never correctness (the next successful compaction still drops
    everything up to ITS drain point).
    """
    path = spool_path(repo_root)
    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except OSError:
        return True

    base_len = len(content)
    kept_lines: list[bytes] = []
    for raw_line in content.split(b"\n"):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        at_epoch = _parse_iso_instant(record.get("at"))
        if at_epoch is None:
            continue
        if at_epoch > drain_point_epoch:
            kept_lines.append(raw_line.strip())

    tail = b""
    for _ in range(_COMPACT_TAIL_STABILIZE_ATTEMPTS):
        try:
            with open(path, "rb") as fh:
                fh.seek(base_len)
                observed = fh.read()
        except OSError:
            observed = b""
        if observed == tail:
            break
        tail = observed

    body = b"\n".join(kept_lines)
    if kept_lines:
        body += b"\n"
    return _write_bytes_atomic(path, body + tail)
