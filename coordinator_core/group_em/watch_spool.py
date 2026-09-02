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
parses.

`state` IS DIAGNOSTIC CONTEXT, NEVER A CLASSIFIER INPUT FOR `poll_once`. It
carries OUR OWN verdict back to us, round-tripped through the producer's
hook -- `poll_once` re-derives parked from the registry exactly as it does
today (`read_pass.classify_peer`) and never reads a record's `state` field
for that decision. That is unchanged by the retention update below; only
compaction (this module's own job) now reads `at` -- see "RETENTION IS
AGE-BOUNDED" below.

THE SPOOL IS A DOORBELL TO `poll_once`, AND ALSO A TRIAGE SURFACE TO A
SIBLING-PLANE READER (2026-09-02 update -- premise corrected). This
module's OWN production job remains keeping the file from growing without
bound, and `poll_once` still classifies the whole roster off the live
registry every tick, unconditionally, never off spool content. What is NO
LONGER true is that nothing reads spool content at all: the sibling DoE
plane's `coordinator:fleet-watch` agent reads and triages this file (see
"RETENTION IS AGE-BOUNDED" below) -- that premise held when this module
first shipped and does not hold now, which is why compaction moved from a
blind truncate to an age-bounded prune.

(An earlier revision kept a `compact(repo_root, drain_point_epoch)` that
read every record, parsed its `at`, and retained only those newer than a
supplied drain point -- built to serve a debounce that read the spool to
decide whether to classify at all. That debounce is gone and stays gone
(`state/improvement-queue/2026-09-02-the-wake-spool-s-debounce-optimises-an-a-e480fd8bba2d.yaml`,
findings 2 and 5) and is out of scope here -- `poll_once` never reads spool
content for a classify-or-skip decision, before or after this update. The
blind-truncate `clear` that briefly replaced `compact` is ALSO gone, on the
premise reversal above; `prune` is not a revival of `compact`'s
drain-point-parameter shape, it is a fresh age-window policy -- see
"RETENTION IS AGE-BOUNDED" below.)

PRODUCER-SIDE RULES THIS MODULE HOLDS TO, WITHOUT ENFORCING THEM: append-only,
never read/truncate/rotate, create-on-first-append, no-op if `state/` is
missing. This module writes no records of its own -- there is no local test
or cron producer here (the dead `read_records`/`append` names an earlier
docstring described were retired at `04263bdbef` and are not being revived);
the sibling plane's `Stop` guard is the only writer, `prune` is the only
thing that ever shortens what it wrote, and tests that need a record on disk
write the JSONL line directly.

PRUNING IS OURS ALONE, AND IS THE ONLY THING THAT EVER SHORTENS THE FILE.
`prune` is called from both `watch.py` tick paths (`tick_once` and `main`'s
held loop) once each has classified this tick -- see `watch._prune_spool`.
Neither call site reads what `prune` discards. RIDING BOTH TICK PATHS IS
DELIBERATE, NOT A TIMER: a prune that fired from the tick lives and dies
with the poller, so a repo whose watch has died simply stops appending
nothing to prune AND stops pruning, together. A timer-based drain could
outlive or predecease the producer and leave a spool nothing ever bounds --
the unbounded-growth defect back in a narrower form. `prune` stays a
housekeeping call at the end of a classify, never its own scheduled job.

RETENTION IS AGE-BOUNDED, WITH A LIVE CROSS-PLANE READER (2026-09-02
update). The blind-truncate `clear` this module used to export is gone: the
sibling DoE plane's `coordinator:fleet-watch` agent
(`coordinator/agents/fleet-watch.md`, their commit `935fa171`) instructs the
watcher, verbatim, to read the spool and triage it -- "You read it and you
triage it. You report what is actionable, never the raw lines." -- and that
agent is the one that starts the held poller whose every tick used to
truncate the file. A park appended at t and read at t+interval was gone
under `clear` with nothing erroring, and the watcher reported a quiet
fleet. THIS DOES NOT REVIVE THE DELETED DEBOUNCE
(`state/improvement-queue/2026-09-02-the-wake-spool-s-debounce-optimises-an-a-e480fd8bba2d.yaml`,
findings 2/5): that stays gone, and `poll_once` still re-derives parked
status off the live registry every tick, unconditionally. What returns is
retention on a different axis -- an AGE window, not the tick's own classify
instant -- serving a reader this module does not import and is not built
against, only sized for.

`RETAIN_SECONDS` IS A FLOOR, NEVER A TARGET, AND OVERSHOOT IS FREE. The
sibling reader's own doctrine states "at least the last 30 minutes of
parks" -- their PM's words describe the spool as a pulse check with no
historical value, so a record surviving past 30 minutes costs nothing and
no consumer notices. What is NOT free is undershooting, ever: a spool that
can read shorter than 30 minutes to that reader breaks a consumer in a repo
this one does not own, on every single poll it happens to land on.

LAZY, WITH HYSTERESIS -- NOT A PRUNE ON EVERY TICK. A poll fires every
5-300s (`watch._POLL_INTERVAL_FLOOR_SECONDS`/`_CEILING_SECONDS`) to enforce
a bound measured in tens of minutes; rewriting the file on every single
tick to police a window that moves ~360x slower is waste on a box running
dozens of concurrent sessions. A naive fix -- prune only when the oldest
record has JUST left the window -- is no better: in steady state the file
spans exactly `RETAIN_SECONDS`, so the head is always just outside it and
every tick rewrites again. Hysteresis is what actually makes this lazy: a
SEPARATE, LARGER trigger. `prune` only rewrites once the OLDEST record is
older than `PRUNE_TRIGGER_SECONDS` (45 minutes), and when it does, it trims
back to `RETAIN_SECONDS` (30 minutes) rather than to the trigger -- so the
next rewrite is not due again until another `PRUNE_TRIGGER_SECONDS -
RETAIN_SECONDS` (15 minutes) has passed. A prune therefore rewrites roughly
every 15 minutes of wall time in steady state, independent of how fast the
tick loop spins.

THE CHEAP CHECK THAT MAKES THIS LAZY. The spool is append-only and
time-ordered, so the OLDEST record is always the FIRST non-blank line.
`prune` reads only that one line -- never the whole file -- to decide
whether an age-triggered rewrite is due; the count cap is checked with an
equally cheap line count, no per-line JSON parse. Only once one of the two
triggers fires does `prune` read, filter, and rewrite the whole file.

A RECORD WHOSE `at` CANNOT BE JUDGED IS DROPPED, NOT KEPT, ONCE A REWRITE
HAPPENS. An absent or unparseable `at` can never be placed in or out of the
retain window, and keeping it only grows the file for no retention
benefit -- so a rewrite treats it the same as an expired record. An
unparseable OLDEST line is itself treated as trigger-worthy (it cannot be
judged fresh, so the cheap check cannot certify "nothing to do" and falls
through to a real rewrite, which then drops it).

THE RECORD-COUNT CAP IS A SAFETY STOP, NOT THE INTENDED MECHANISM, and
triggers a rewrite ON ITS OWN, independent of age -- a pathological burst
well inside `PRUNE_TRIGGER_SECONDS` must not be allowed to grow unbounded
just because nothing has aged out yet. Age is still the retention policy;
`MAX_RECORDS` only bounds a burst that lands faster than the window drains.
Whichever trigger fires, the SAME rewrite runs (filter to `RETAIN_SECONDS`,
then cap-trim); when the cap alone is what trims, the most RECENT
`MAX_RECORDS` in-window records are kept (append order in the file is
chronological, so this is a tail-keep) -- the cap trims volume, it does not
change which age band survives.

NO TAIL-STABILIZATION RETRY LOOP. Whichever trigger fires, `prune` is one
read, one filter, one `os.replace` -- same shape `clear` had. An append
landing mid-call, or in the gap between this process's read and its write,
is simply gone from this pass; the producer's next append is unaffected and
the record is not lost forever, only from this one prune's view. That is
acceptably lost, same posture as the module always had for a race on this
file.

NEGATIVE SPEC -- what this module deliberately does not do:

- No lock, anywhere, on any file this module touches -- see "NO
  TAIL-STABILIZATION RETRY LOOP" above for the one hazard a lock would
  close and why it is not worth closing for a spool with exactly one
  classifying reader in this repo (`poll_once`, off the live registry,
  never off spool content).
- No second timestamp parser and no second format -- `prune` parses `at`
  with `watch_heartbeat`'s own `_STAMP_FORMAT` constant and its
  `calendar.timegm(time.strptime(...))` idiom, the same one
  `watch_heartbeat.stamp`/`read_liveness` already use for the identical
  `%Y-%m-%dT%H:%M:%SZ` shape.
- No read/branch on a record's `state` field for any decision -- see "STATE
  IS DIAGNOSTIC CONTEXT" above; retention here keys on `at` alone.
- No production producer, and no local/cron producer either -- this module
  writes no records; the DoE `Stop` guard is the production writer and is
  not built here, imported here, or assumed to exist on disk.
- No debounce, no reader of spool content for a classify-or-skip decision --
  `poll_once` never consults this module's content, only its path via
  `prune`'s housekeeping call. See "THE SPOOL IS A DOORBELL" above.
- No tail-stabilization retry loop -- see above.
- No prune scheduled off a timer -- see "RIDING BOTH TICK PATHS IS
  DELIBERATE" above; only the two tick paths call it.
"""

from __future__ import annotations

import calendar
import json
import os
import tempfile
import time

from coordinator_core.group_em.watch_heartbeat import _STAMP_FORMAT

_SPOOL_RELATIVE_PATH = os.path.join("state", "group-em-watch-spool.jsonl")

#: THE GUARANTEE. What `prune` keeps, at minimum, once it rewrites -- a
#: floor, never a target (module docstring, "IS A FLOOR, NEVER A TARGET").
#: Must comfortably exceed the slowest consumer cadence: the sibling plane's
#: `coordinator:fleet-watch` polls every 5-300s (this module's own analogue
#: of `watch._POLL_INTERVAL_FLOOR_SECONDS`/`_CEILING_SECONDS`), and the
#: Group-EM's own cron floor is ~23 minutes (`watch._CRON_FLOOR_INTERVAL_SECONDS`).
#: 30 minutes clears both with margin -- a triage surface that can empty
#: between a reader's polls is not a triage surface.
RETAIN_SECONDS = 30 * 60

#: THE ACTION THRESHOLD. `prune` does not rewrite at all until the OLDEST
#: record is older than this -- the hysteresis gap (`PRUNE_TRIGGER_SECONDS
#: - RETAIN_SECONDS` = 15 minutes) is what makes this lazy in steady state
#: rather than rewriting on every tick (module docstring, "LAZY, WITH
#: HYSTERESIS").
PRUNE_TRIGGER_SECONDS = 45 * 60

#: SAFETY STOP, NOT THE INTENDED MECHANISM -- see module docstring "THE
#: RECORD-COUNT CAP". Age is retention; this only bounds a burst that lands
#: faster than the window drains, and triggers a rewrite on its own.
MAX_RECORDS = 2000


def spool_path(repo_root: str) -> str:
    """Absolute path of the transition spool for `repo_root`."""
    return os.path.join(repo_root, _SPOOL_RELATIVE_PATH)


def _record_at_epoch(line: str) -> "float | None":
    """This line's `at`, in epoch seconds, or `None` if it cannot be judged.

    Covers every reason a line cannot be placed in the retain window: not
    JSON, not an object, no `at`, or an `at` that does not parse against
    `watch_heartbeat`'s own `_STAMP_FORMAT` -- all collapse to `None`, and
    a rewrite drops a line it cannot judge rather than keeping it (module
    docstring, "A RECORD WHOSE `at` CANNOT BE JUDGED IS DROPPED").
    """
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    at = record.get("at")
    if not isinstance(at, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(at, _STAMP_FORMAT)))
    except (ValueError, TypeError):
        return None


def _oldest_at_epoch(path: str) -> "float | None":
    """The FIRST non-blank line's `at` epoch, or `None` if there is none or
    it cannot be judged -- the cheap check `prune` uses to decide whether an
    age-triggered rewrite is due, without reading the rest of the file
    (module docstring, "THE CHEAP CHECK THAT MAKES THIS LAZY").
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                return _record_at_epoch(stripped)
    except OSError:
        return None
    return None


def _line_count(path: str) -> int:
    """Non-blank line count, with no JSON parse -- the equally cheap check
    behind the count-cap trigger (module docstring, "THE CHEAP CHECK THAT
    MAKES THIS LAZY")."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def prune(repo_root: str, now_epoch: "float | None" = None) -> bool:
    """Lazily bound the spool: rewrite only once one of two triggers fires,
    and when it does, keep every record within `RETAIN_SECONDS` of
    `now_epoch` (dropping the rest), then trim to `MAX_RECORDS` if still
    over.

    THE TWO TRIGGERS (module docstring, "LAZY, WITH HYSTERESIS" / "THE
    RECORD-COUNT CAP"): the OLDEST record is older than
    `PRUNE_TRIGGER_SECONDS`, or there are more than `MAX_RECORDS` non-blank
    lines. Neither triggering means NO rewrite happens at all -- the file is
    left byte-for-byte untouched, which is the laziness this function
    exists to provide.

    ONE READ, ONE FILTER, ONE REPLACE once a trigger fires -- see module
    docstring, "NO TAIL-STABILIZATION RETRY LOOP". An append landing
    mid-call is simply lost from this pass, same posture the module always
    had.

    Returns `True` when there is nothing to prune (absent spool), when
    neither trigger fires (no rewrite needed), and whenever a triggered
    rewrite lands; `False` only on an I/O failure during a triggered
    rewrite, in which case the spool is left untouched -- same non-raising
    posture as `watch_heartbeat.write_atomic`/`stamp`: a failed prune costs
    disk space (or stale records surviving one extra tick), never
    correctness -- the next successful prune still catches up.

    MEASURED (this box): the lazy no-op path (read one line, one line
    count) is ~0.4ms; a triggered rewrite of a `MAX_RECORDS`-sized (2000
    record) file is ~12ms -- both well under the 200ms process-time line.
    """
    path = spool_path(str(repo_root))
    if not os.path.exists(path):
        return True
    now_epoch = time.time() if now_epoch is None else now_epoch

    oldest_epoch = _oldest_at_epoch(path)
    age_triggered = oldest_epoch is None or (now_epoch - oldest_epoch) > PRUNE_TRIGGER_SECONDS
    cap_triggered = _line_count(path) > MAX_RECORDS
    if not age_triggered and not cap_triggered:
        return True

    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False

    kept = []
    for line in lines:
        if not line.strip():
            continue
        at_epoch = _record_at_epoch(line)
        if at_epoch is None:
            continue
        if now_epoch - at_epoch > RETAIN_SECONDS:
            continue
        kept.append(line)
    if len(kept) > MAX_RECORDS:
        kept = kept[-MAX_RECORDS:]

    directory = os.path.dirname(path)
    tmp_path = None
    try:
        handle, tmp_path = tempfile.mkstemp(
            prefix=".group-em-watch-spool-", suffix=".tmp", dir=directory
        )
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            for line in kept:
                fh.write(line)
                fh.write("\n")
        os.replace(tmp_path, path)
        return True
    except OSError:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False
