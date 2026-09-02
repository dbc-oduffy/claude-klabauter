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

`state` IS DIAGNOSTIC CONTEXT, NEVER A CLASSIFIER INPUT. It carries OUR OWN
verdict back to us, round-tripped through the producer's hook -- `poll_once`
re-derives parked from the registry exactly as it does today
(`read_pass.classify_peer`). This module never reads a record's `state`
field for any decision, because it never reads a record's CONTENT at all --
see "THE SPOOL IS A DOORBELL, NOT A DATA SOURCE" below.

THE SPOOL IS A DOORBELL, NOT A DATA SOURCE. This module has exactly one
production job left: keep the file from growing without bound. Nothing in
this codebase, or planned for it, ever derives a decision from what a spool
record SAYS -- `poll_once` classifies the whole roster off the live registry
every tick, unconditionally. So this module holds no reader of record
content: no timestamp scan, no retention-by-drain-point, no debounce. It
knows the path, and it knows how to make the file empty again.

(An earlier revision kept a `compact(repo_root, drain_point_epoch)` that
read every record, parsed its `at`, and retained only those newer than a
supplied drain point -- built to serve a debounce that read the spool to
decide whether to classify at all. Both are gone:
`state/improvement-queue/2026-09-02-the-wake-spool-s-debounce-optimises-an-a-e480fd8bba2d.yaml`,
findings 2 and 5. Retention-by-content survived one extra pass after the
debounce was deleted because a test still asserted it
(`test_tick_once_keeps_a_record_appended_after_its_classify`); that test
existed only to protect the debounce from missing a park it had not yet
classified, so once the debounce was gone the property it pinned had no
consumer left and the test was deleted along with `compact`.)

WHAT `clear` ACCEPTS LOSING, AND WHY THAT IS FREE TODAY. `clear` truncates
the spool to empty. A producer append landing mid-call, or in the gap
between this process's read/decide and its write, is simply gone -- there is
no salvage, no drain-point comparison, no partial retention. That is free
FOR EXACTLY AS LONG AS NOTHING READS A RECORD OUT OF THIS SPOOL: since
`poll_once` always re-derives its answer from the live registry, a record
dropped by `clear` was never going to be consulted by anything. THE LIVE
CANDIDATE THAT WOULD BREAK THIS: a future wake trigger that reads the spool
itself to decide what changed (e.g. a session-state-transition wake reading
its own row rather than merely being woken by one) would turn a lost record
into a missed transition. That check belongs BEFORE such a reader is added,
not after -- the day this module gains its first reader of record content is
the day `clear`'s blind-truncate contract must be re-litigated, not
discovered later as a bug.

PRODUCER-SIDE RULES THIS MODULE HOLDS TO, WITHOUT ENFORCING THEM: append-only,
never read/truncate/rotate, create-on-first-append, no-op if `state/` is
missing. This module writes no records of its own -- there is no local test
or cron producer here (the dead `read_records`/`append` names an earlier
docstring described were retired at `04263bdbef` and are not being revived);
the sibling plane's `Stop` guard is the only writer, `clear` is the only
thing that ever shortens what it wrote, and tests that need a record on disk
write the JSONL line directly.

CLEARING IS OURS ALONE, AND IS THE ONLY THING THAT EVER SHORTENS THE FILE.
`clear` is called from both `watch.py` tick paths (`tick_once` and `main`'s
held loop) once each has classified this tick -- see `watch._compact_spool`.
Neither call site reads what `clear` discards.

NEGATIVE SPEC -- what this module deliberately does not do:

- No lock, anywhere, on any file this module touches -- see "WHAT `clear`
  ACCEPTS LOSING" above for the one hazard a lock would close and why it is
  not worth closing for a spool nothing reads.
- No timestamp parser, no `at` comparison, no retention by drain point --
  `clear` does not open the file to inspect it, only to truncate it.
- No read/branch on a record's `state` field for any decision -- see "STATE
  IS DIAGNOSTIC CONTEXT" above.
- No production producer, and no local/cron producer either -- this module
  writes no records; the DoE `Stop` guard is the production writer and is
  not built here, imported here, or assumed to exist on disk.
- No debounce, no reader of spool content for a classify-or-skip decision --
  see "THE SPOOL IS A DOORBELL" above.
"""

from __future__ import annotations

import os
import tempfile

_SPOOL_RELATIVE_PATH = os.path.join("state", "group-em-watch-spool.jsonl")


def spool_path(repo_root: str) -> str:
    """Absolute path of the transition spool for `repo_root`."""
    return os.path.join(repo_root, _SPOOL_RELATIVE_PATH)


def clear(repo_root: str) -> bool:
    """Truncate the spool to empty. The only thing that ever shortens it.

    NO READ, NO RETENTION -- see module docstring's "WHAT `clear` ACCEPTS
    LOSING" for what this discards and the condition under which that stops
    being free. Returns `True` when there is nothing to clear (absent spool)
    and whenever the truncate lands; `False` only on an I/O failure, in
    which case the spool is left untouched -- same non-raising posture as
    `watch_heartbeat.write_atomic`/`stamp`: a failed clear costs disk space,
    never correctness (the next successful clear still empties it).
    """
    path = spool_path(str(repo_root))
    if not os.path.exists(path):
        return True
    directory = os.path.dirname(path)
    tmp_path = None
    try:
        handle, tmp_path = tempfile.mkstemp(
            prefix=".group-em-watch-spool-", suffix=".tmp", dir=directory
        )
        os.close(handle)
        os.replace(tmp_path, path)
        return True
    except OSError:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False
