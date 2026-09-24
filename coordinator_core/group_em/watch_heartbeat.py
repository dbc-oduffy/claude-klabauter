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
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core import timestamps
from coordinator_core.session import core as session_core
from coordinator_core.session import day_branch_cut_lock

_WATCH_RELATIVE_PATH = os.path.join("state", "group-em-watch.json")

#: Generator-provenance declaration (generator_provenance.py). `write_atomic`
#: rewrites `<repo_root>/state/group-em-watch.json` on every tick, but the
#: destination has no genuine GENERATES contract: `repo_root` is a required
#: parameter with no in-module default (`stamp`'s only caller-facing knob),
#: so this module never anchors the write to its own tree the way e.g.
#: `orientation/regenerate_cache.py` does; and the record's freshness is
#: elapsed-time-since-`last_tick_at` (`is_fresh_and_foreign`, `VERDICT_STALE`
#: below), never staleness-relative-to-a-source-file the way GENERATES'
#: `sources`/`stamp_key` contract expects -- there is no source set whose
#: mtime this heartbeat is regenerated from. A MUTATES glob is the wrong
#: tool too: the target is one concrete filename, which this module's own
#: validator (`_valid_mutates_shape` in generator_provenance.py) reserves
#: for GENERATES. Declared empty rather than inventing either.
GENERATES = []

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


def iso_instant(epoch: float) -> str:
    """The `_STAMP_FORMAT` instant string -- the one seam every `as_of`/
    `taken_at`/`counts_struck_at` caller composes against.

    // Review: overengineering-reviewer finding 3 -- promoted from private
    // `_iso` after `baseline.py` was found reaching across the module
    // boundary for the private name; three other call sites (idle_report,
    // send_pass, group_em_enter) hand-spelled the same expression rather
    // than importing it at all.
    """
    return time.strftime(_STAMP_FORMAT, time.gmtime(epoch))


#: Back-compat alias for the private spelling; nothing outside this module
#: should import it, but code inside the module keeps the short name.
_iso = iso_instant


def next_expected_by(
    now_epoch: float,
    interval_seconds: float,
    observed_interval_seconds: Optional[float] = None,
) -> str:
    """The deadline this tick promises the next one by, as the reader parses it.

    BASIS IS THE MEASURED CADENCE, NOT THE CALLER'S DECLARATION, WHEN ONE IS
    AVAILABLE. A ~3.4x mismatch between `interval_seconds` (what `watch.main`
    or `tick_once` declares) and the sensor's actual inter-tick delta was
    measured, and every downstream freshness verdict (`is_fresh_and_foreign`,
    which reads `next_expected_by` as its sole freshness input) is wrong by
    that factor for as long as the deadline is sized off the declared value
    alone. `observed_interval_seconds` is `None` on the first tick -- the
    only tick with no prior to measure from -- and the declared interval is
    the whole basis there, same as before this function grew the parameter.

    CAPPED AT THE DECLARED INTERVAL. An observed cadence slower than declared
    must never widen the deadline past what the declared interval alone
    would have produced: the dangerous direction of the mismatch is a
    monitor that ticks slower than it claims stamping a deadline further out
    than it should, lengthening every peer's lockout window by the same
    factor. The cap makes the observed basis strictly tighten or match the
    declared one, never loosen it.
    """
    effective_interval = interval_seconds
    if observed_interval_seconds is not None and observed_interval_seconds > 0:
        effective_interval = min(observed_interval_seconds, interval_seconds)
    grace = max(_GRACE_FLOOR_SECONDS, effective_interval * _GRACE_TICKS)
    return _iso(now_epoch + grace)


def _ensure_state_dir(directory: str) -> bool:
    """Create `directory` iff its PARENT already exists. True on success.

    NEVER MINT A REPO. `makedirs` used to create the WHOLE chain, so a
    caller handed a mangled root created a repo-shaped tree wherever that
    path landed -- once inside a publish mirror, where it blocked the
    round for the whole fleet. Full incident: `group_em.repo_root_arg`'s
    module docstring.

    THIS IS NOT THE SAME FIX as that module's arm-time refusal, which
    only covers callers that came through a CLI. A writer able to conjure
    a repo directory is doing something no correct caller needs, so the
    root must already exist and only the `state/` leaf under it is ours
    to create.

    Shared by `write_atomic` and the P103-C2 guard -- the guard's sidecar
    lock lands in the same directory the record itself does, so it needs
    the identical safety check before either can create anything there.
    """
    parent = os.path.dirname(directory)
    if parent and not os.path.isdir(parent):
        return False
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return False
    return True


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
        if not _ensure_state_dir(directory):
            return False
        handle, tmp_path = tempfile.mkstemp(
            prefix=".group-em-watch-", suffix=".tmp", dir=directory
        )
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
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


def _writer_identity(record: dict) -> tuple:
    """The three fields that together name WHICH INSTRUMENT wrote a record.

    Holder alone is not the identity, and that is the whole point of this
    helper. Measured on this repo 2026-09-01: a cron audit tick stamped five
    declinations at 19:31:16Z and the fleet-watch monitor replaced the record
    fifty seconds later at 19:32:06Z, under the SAME `holder_session_id` and
    the SAME `writer_session_id`. Two crown instruments, one record, and the
    later arrival won by arriving later. A discriminator keyed on holder would
    have seen no difference at all.
    """
    return (
        record.get("holder_session_id"),
        record.get("writer_session_id"),
        record.get("tick_source"),
    )


def is_fresh_and_foreign(
    record: Optional[dict],
    now_epoch: float,
    holder_session_id: str,
    writer_session_id: Optional[str] = None,
) -> bool:
    # No `tick_source` parameter, and its absence is the contract: this
    # predicate is holder-or-writer by design (see below), so accepting a
    # source it cannot consult would invite a caller to believe it was
    # consulted. `_writer_identity` is where `tick_source` belongs.
    """Is `record` still live by its OWN deadline, and held by ANOTHER PARTY?

    FRESH is asked of the record's own `next_expected_by`, never a constant
    invented here: the writer that stamped it is the only party that knew its
    own cadence, and a threshold picked at the reading end would be a second
    opinion about someone else's clock. An unparseable or absent deadline is
    NOT fresh -- a record that cannot say when it expects to be replaced has no
    claim to be left alone.

    FOREIGN IS HOLDER-OR-WRITER, DELIBERATELY NOT `tick_source`, AND THE
    ASYMMETRY WITH `_writer_identity` IS THE POINT. This predicate and the
    prior-holder trace ask two different questions and must not share one
    answer:

      - the TRACE asks "whose record did I just replace" and keys on all three
        fields, because a same-crown cron-vs-monitor replacement is still one
        instrument overwriting another's rows and a reader deserves to see it;
      - this predicate asks "am I about to step on a LIVE PEER CROWN", and a
        different `tick_source` under the same holder is not a peer crown. It
        is the same crown's other instrument, doing its job.

    Collapsing the two deadlocks the fleet, and this is measured rather than
    argued. A cron audit tick declares `interval_seconds=23*60`, so its
    `next_expected_by` sits ~69 minutes ahead. Under a three-field foreignness
    the monitor -- cadence ~80s, the SAME holder and writer -- is declined on
    every single poll until that deadline passes: the standing watch stops
    stamping for over an hour after each audit tick, and the record it cannot
    refresh reads STALE to every other session in the fleet. That is a worse
    failure than the clobber this chunk exists to fix, and it is the
    two-watchers-declining-each-other shape the arm-time chunk names as the
    thing to avoid. Verified before the fix: `stamped: False`, monitor locked
    out for 68.2 minutes.

    Shared deliberately with the arm-time refusal (`group_em.watch`): one
    predicate, two call sites. Arming and stamping ask the identical question
    about the identical record, and two spellings of it would drift.
    """
    if not isinstance(record, dict):
        return False
    deadline = record.get("next_expected_by")
    if not isinstance(deadline, str):
        return False
    try:
        deadline_epoch = calendar.timegm(time.strptime(deadline, _STAMP_FORMAT))
    except (ValueError, TypeError):
        return False
    if now_epoch >= deadline_epoch:
        return False
    if "writer_session_id" not in record:
        # A record from a producer that predates this field carries no opinion
        # about its writer, and absence is writer-UNKNOWN rather than a second
        # party. Reading it as one refuses the entering crown its own watch for
        # a whole `next_expected_by` interval -- the self-lockout this refusal
        # exists to prevent, arrived at from the other side. Measured: the
        # doctrine plane's `coordinator/skills/group-em/watch_heartbeat.py`
        # still ships the pre-`writer_session_id` `stamp` signature, so every
        # `groupem.enter` stamp lands here keyless and locked the entering
        # session out of arming for 23 minutes.
        return record.get("holder_session_id") != holder_session_id
    return (record.get("holder_session_id"), record.get("writer_session_id")) != (
        holder_session_id,
        writer_session_id,
    )


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


def _self_process_identity() -> tuple[int, Optional[int]]:
    """`(os.getpid(), create_time_epoch)` for THIS process, best-effort.

    `create_time_epoch` is `None` when `psutil` is unavailable or its query
    raises -- never invented. Reused verbatim as `stable_pid_alive`'s witness
    at read time (`process_confirmed_alive`, item 2), the same primitive
    `session.core` already uses for every other process-identity compare in
    this repo (`core.stable_pid_alive`'s own docstring) -- no second
    liveness mechanism invented here. In-process only: no subprocess spawn,
    no `ps`/`kill -0` shell-out (DR-344).
    """
    pid = os.getpid()
    psutil_mod = session_core._psutil()
    if psutil_mod is None:
        return pid, None
    try:
        return pid, int(psutil_mod.Process(pid).create_time())
    except Exception:
        return pid, None


def process_confirmed_alive(liveness: dict) -> Optional[bool]:
    """Is the process that wrote this record's last tick CONFIRMED running?

    Single-machine process liveness, `stamp`'s own writer describing itself
    (`pid`/`pid_start_epoch`, self-captured every tick by
    `_self_process_identity`) -- NOT the cross-machine holder-identity
    question item 1's `is_fresh_and_foreign`/displacement teardown answers
    (that predicate is deliberately never PID-keyed; see its own docstring
    and the rejected-alternatives note in the memo this function answers).
    A PID is a fair liveness input on the SAME box a `--status` caller runs
    on, which is exactly this question -- never used to decide whose
    record wins a write.

    Returns:
      - `True`  -- the recorded pid is confirmed alive (and, when a birth
        epoch was captured, still the SAME process -- `stable_pid_alive`'s
        own recycled-pid guard).
      - `False` -- the recorded pid is confirmed gone.
      - `None`  -- cannot be confirmed either way (no `pid` in this record --
        e.g. a pre-this-fix stamp, or `psutil` raising something other than
        a clean dead/alive answer). `None` is never promoted to `True`: a
        `--status` caller that cannot confirm the process must not report
        ALIVE on staleness arithmetic alone (the defect this function
        exists to close).

    No subprocess spawn -- `core.stable_pid_alive` is `psutil`-only,
    in-process (DR-344).
    """
    pid = liveness.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    start_epoch = liveness.get("pid_start_epoch")
    if not isinstance(start_epoch, (int, float)):
        # NO BIRTH-INSTANT WITNESS, NO CONFIRMATION -- `stable_pid_alive`
        # with an empty `stored_start_epoch` AND an empty `stored_lstart`
        # returns `False` unconditionally (its own "legacy fallback" arm,
        # `if not stored_lstart: return False`), which would read a
        # genuinely alive process as confirmed-dead. A bare pid with no
        # epoch is therefore NOT a fair input here -- degrade to "cannot
        # confirm" rather than call through to a predicate that would lie.
        return None
    try:
        return session_core.stable_pid_alive(pid, "", str(int(start_epoch)))
    except Exception:
        return None


#: Sidecar lock suffix, placed BESIDE `state/group-em-watch.json` itself --
#: never under a foreign watched repo's `.git` common dir. `stamp`'s
#: `repo_root` is an arbitrary watched repo, and P103-C1's two-part gate
#: found writing a lock sidecar under such a repo's git common dir a NO
#: under `CLAUDE.md`'s boundary doctrine; the heartbeat file's own directory
#: is already this module's write target (`write_atomic`), so no new write
#: surface is opened by anchoring the guard there instead.
_GUARD_LOCK_SUFFIX = ".lock"

#: Sub-budget hold window and stale grace for the read-decide-write guard --
#: explicit, and far below `day_branch_cut_lock`'s own 10.0s/60.0s ceilings
#: (those size a ~30ms `git checkout -b`; this guards one JSON
#: read-decide-write, measured at ~12ms per critical section, P103-C1
#: research doc). A lock still held past this window names a crashed or
#: stuck writer, not a slow one.
_GUARD_HOLD_SECONDS = 1.0
_GUARD_STALE_GRACE_SECONDS = 2.0


def _guard_lock_path(watch_file: str) -> Path:
    return Path(watch_file + _GUARD_LOCK_SUFFIX)


def _guard_record_is_stale(record: dict, now_epoch: float) -> bool:
    """`day_branch_cut_lock.record_is_stale`'s SHAPE (PID-liveness checked
    first, then hold_until-plus-grace) at this guard's own sub-budget window
    -- reused function, `day_branch_cut_lock.holder_alive`, never a second
    liveness mechanism invented here.
    """
    if day_branch_cut_lock.holder_alive(record.get("holder_pid")) is False:
        return True
    hold_until = record.get("hold_until")
    return isinstance(hold_until, (int, float)) and now_epoch > (
        hold_until + _GUARD_STALE_GRACE_SECONDS
    )


def _read_guard_record(lock_path: Path) -> Optional[dict]:
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _guard_mtime_stale(lock_path: Path, now_epoch: float) -> bool:
    """Fallback staleness signal for an UNREADABLE lock file (corrupt, or
    caught empty mid-write) -- the file's own mtime, past the guard's hold
    window PLUS its grace, same as `_guard_record_is_stale`'s hold_until
    arm but read off the filesystem when the JSON content cannot be. A
    file that has genuinely vanished by the time this is checked (`OSError`
    from `stat`) is treated as stale too -- there is nothing left to wait
    out.
    """
    try:
        mtime = lock_path.stat().st_mtime
    except OSError:
        return True
    return now_epoch > mtime + _GUARD_HOLD_SECONDS + _GUARD_STALE_GRACE_SECONDS


def _acquire_guard(watch_file: str, now_epoch: float, guard_pid: int) -> bool:
    """One racer wins this tick's read-decide-write window; the loser NEVER
    blocks or retries -- `stamp` never gates (module docstring) -- it
    declines the tick instead. Reuses `day_branch_cut_lock._try_create`, the
    `O_CREAT | O_EXCL` atomic-create-or-fail primitive this whole guarantee
    rests on, rather than inventing a second one.
    """
    lock_path = _guard_lock_path(watch_file)
    payload = {"holder_pid": guard_pid, "hold_until": now_epoch + _GUARD_HOLD_SECONDS}
    if day_branch_cut_lock._try_create(lock_path, payload):
        return True
    record = _read_guard_record(lock_path)
    if isinstance(record, dict) and _guard_record_is_stale(record, now_epoch):
        # POSITIVELY CONFIRMED stale (dead holder, or grace elapsed) --
        # unlink then re-create; exactly one racer's unlink-plus-create wins.
        try:
            lock_path.unlink()
        except OSError:
            pass
        return day_branch_cut_lock._try_create(lock_path, payload)
    if record is None:
        # UNREADABLE IS NOT PROOF OF ABSENCE, and unlinking on that belief
        # alone is the exact bug this note exists to name: `_try_create`'s
        # own `O_CREAT` then `os.write` is two syscalls, not one, so a
        # concurrent reader can catch the file freshly created but still
        # EMPTY by its legitimate holder -- that reads back as an
        # unparseable record, identically to "the file is gone". Measured:
        # unconditionally unlinking here let a second racer take over the
        # first racer's still-forming lock and both proceed to write -- the
        # exact collision this guard exists to prevent, reintroduced one
        # level down. So the file's own mtime is the fallback staleness
        # witness when content cannot be: within the hold-plus-grace
        # window, decline without touching it (the ordinary transient
        # case, resolved by NOT unlinking); past it, treat it the same as a
        # confirmed-stale record and take over -- otherwise a genuinely
        # corrupt leftover (e.g. a crash mid-write) would wedge this guard
        # forever, which is worse than the clobber it exists to prevent.
        if _guard_mtime_stale(lock_path, now_epoch):
            try:
                lock_path.unlink()
            except OSError:
                pass
        return day_branch_cut_lock._try_create(lock_path, payload)
    return False


def _release_guard(watch_file: str, guard_pid: int) -> None:
    """Drop the guard iff this process still holds it. Never raises -- a
    lock that outlives its own process is exactly what `_guard_record_is_stale`
    exists to take over, not a reason to crash the tick that just wrote.
    """
    lock_path = _guard_lock_path(watch_file)
    record = _read_guard_record(lock_path)
    if record is not None and record.get("holder_pid") != guard_pid:
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


def stamp(
    repo_root: str,
    holder_session_id: str,
    declinations: Optional[list],
    interval_seconds: float,
    subscribed_peers: Optional[int] = None,
    now_epoch: Optional[float] = None,
    tick_source: str = TICK_SOURCE,
    holder_name: Optional[str] = None,
    writer_session_id: Optional[str] = None,
    pid: Optional[int] = None,
    pid_start_epoch: Optional[int] = None,
) -> bool:
    """Rewrite the heartbeat for one tick. Whole-file replace, never a fold.

    `declinations` is THIS tick's rows only -- each `{session_id, name, gate,
    reason}` -- never an accumulating history: that is what lets a reader tell
    "looked, nothing to do" apart from "did not look". A tick that emitted and
    declined nothing passes `[]`.

    UNCOMPUTED-ZERO VS COMPUTED-ZERO. `subscribed_peers` and `declinations`
    each default/accept `None`, and `None` is carried into the payload
    VERBATIM -- never coerced to `0`/`[]`. `None` means the caller never
    computed the figure this tick; `0`/`[]` means it did, and the answer was
    zero. Collapsing those (the old `subscribed_peers: int = 1` default, and
    `list(declinations or [])`) made "a watch that covered nobody this tick"
    indistinguishable from "a watch that never measured coverage at all" --
    exactly the ambiguity a reader of `stamp()`'s payload cannot resolve from
    the record alone. A caller that HAS computed a real zero passes `0`/`[]`
    explicitly, same as before; only an omitted argument now reads back as
    `None` instead of a fabricated `1`.

    THE READ-DECIDE-WRITE WINDOW IS NOW GUARDED (`_acquire_guard`/
    `_release_guard`, landed e45c43a772) -- THIS IS NO LONGER AN OPEN GAP.
    `write_atomic` makes the WRITE atomic; on its own it does not make the
    sequence "read `prior_record`, decide via `is_fresh_and_foreign`, write"
    atomic, and two crowns racing inside that window would both read the
    same pre-replacement record, both pass the decline, and the later
    `os.replace` would win with no trace naming the destroyed racer.
    `send_pass`'s `build_send_digest` documents an analogous race and bounds
    it because that record is caller-scoped (one path, one writer at a time
    in practice); this record is repo-scoped and SHARED across every crown
    in the fleet, so that bound did not apply here. MEASURED RATE
    (`docs/research/2026-09-11-group-em-heartbeat-collision-rate-and-candidate-cost.md`):
    the natural cadence could not be measured under a process-time bar, so
    the window's forced-simultaneous duration was measured instead
    (≈12.0ms average critical-section process time) and extrapolated to the
    real cadences -- ≈6.7x10⁻⁴ per monitor x monitor tick-pair (18s), which
    accumulates to an expected ≈3.2 lost records/day for a repo watched by
    two concurrent monitor crowns, consistent with DoE-claude's reported
    hits. That rate is what justified a guard rather than accept-and-document
    (the anti-scope's forbidden shortcut is arguing collisions "seem
    unlikely"; this is a cited number showing the opposite). The guard reuses
    `day_branch_cut_lock`'s `O_CREAT | O_EXCL` + PID-liveness-takeover
    primitive (measured at 0.10ms create+release, well under the 200ms
    per-process bar) rather than a new lock or `locked_write.locked_rmw`/
    `held_lock` (disqualified for this call site: `_lock_dir_path` anchors
    under the WATCHED repo's own git-common-dir, a foreign tree this module
    does not own, and its default wait ceilings are 20x-360x over budget --
    see the research doc's two-part gate). The guard never blocks or
    retries: the losing racer declines the tick and this module's caller
    sees a `POLL-ERROR watch_heartbeat.stamp` line naming the loss, never a
    silent clobber; `stamp` still returns `False` rather than raising on
    every guard failure path (module contract, unchanged).

    WHAT THE EXTENDED TRACE DOES AND DOES NOT DO (DEFECT 1). `prior_*`
    (including `prior_subscribed_peers` and `prior_declination_count`, added
    alongside the identity fields) makes a destroyed record's identity AND
    its rough shape legible and attributable after the fact. It does not
    make the destroyed record RECOVERABLE -- the actual `declinations` rows
    and any other prior content are gone the moment `os.replace` lands, trace
    or no trace. A reader who wants "what changed" from this file across
    ticks needs the un-added history this record deliberately does not keep
    (see "never an accumulating history", above); this trace answers "what
    was destroyed", never "what changed".

    `writer_session_id` IS REQUIRED DESPITE THE `Optional[str] = None` DEFAULT --
    the body below raises `ValueError` if it is falsy. The signature documents
    itself as optional; the contract actually enforced is mandatory. Left
    standing rather than resolved here: whether the doc-only mismatch is
    deliberate, or the kwarg should become positional/required, is an open
    question for whoever next owns this module's call sites.
    """
    if tick_source not in TICK_SOURCES:
        raise ValueError(
            f"tick_source {tick_source!r} is not one of the reader's words {TICK_SOURCES}"
        )
    if not writer_session_id:
        raise ValueError(
            "writer_session_id is required -- an omitting call is how a crown reads its "
            "own write back as independent confirmation (see this module's C1 note)"
        )
    now_epoch = time.time() if now_epoch is None else now_epoch
    if holder_name is None:
        holder_name = _carried_holder_name(repo_root, holder_session_id)
    if pid is None and pid_start_epoch is None:
        # SELF-DESCRIPTION, captured every tick -- item 2's `--status` process
        # check reads this back via `process_confirmed_alive`. Never invented
        # for a caller that passed its own `pid` explicitly (a future writer
        # stamping on behalf of a teammate process, should one exist).
        pid, pid_start_epoch = _self_process_identity()

    watch_file = watch_path(repo_root)

    # The guard's sidecar lock lands beside `watch_file`, so the directory
    # must exist before the FIRST file operation, not only before
    # `write_atomic`'s own write -- same "never mint a repo" safety check
    # either way (`_ensure_state_dir`). A directory that cannot be ensured is
    # a missed tick, never a raise.
    if not _ensure_state_dir(os.path.dirname(watch_file)):
        return False

    # GUARD THE READ-DECIDE-WRITE WINDOW (P103-C2). `write_atomic` makes the
    # WRITE atomic; this makes the SEQUENCE atomic across concurrent
    # `stamp()` callers on the same `watch_file`, closing the gap the module
    # docstring's predecessor text described. The loser NEVER waits for the
    # winner -- `stamp` never gates (module docstring) -- it declines this
    # tick and REPORTS the contention on stderr; the next tick (~18s away at
    # the monitor cadence) tries again. This is "prevented", not "detected
    # after the fact": the second racer never reads a record the first is
    # about to replace.
    guard_pid = os.getpid()
    if not _acquire_guard(watch_file, now_epoch, guard_pid):
        print(
            "POLL-ERROR watch_heartbeat.stamp declined a concurrent write: "
            f"the read-decide-write window for {watch_file!r} was already "
            f"held (holder_session_id={holder_session_id!r}, "
            f"writer_session_id={writer_session_id!r}); this tick is "
            "skipped, not lost silently.",
            file=sys.stderr,
            flush=True,
        )
        return False
    try:
        prior_record = _read_record(watch_file)

        # DECLINE ON FRESH-AND-FOREIGN, checked BEFORE the trace/write. Shares
        # `is_fresh_and_foreign` with the arm-time refusal in `group_em.watch`
        # -- one predicate, two call sites. Decline is falsey-return only,
        # never a raise: a writer that raises where it used to write turns a
        # reporting defect into a tick that dies (see module docstring, NEVER
        # RAISES, NEVER GATES). The record on disk is left untouched.
        if is_fresh_and_foreign(
            prior_record, now_epoch, holder_session_id, writer_session_id
        ):
            return False

        # OBSERVED INTER-TICK DELTA. Measured off the record ABOUT TO BE
        # REPLACED, whichever instrument wrote it -- what a reader actually
        # experiences as "how often does this file change" is exactly this
        # gap, regardless of which crown/tick_source produced the previous
        # write. `None` when there is no prior tick to measure from (first
        # stamp) or its timestamp cannot be parsed; `next_expected_by` falls
        # back to the declared `interval_seconds` in both cases.
        observed_interval_seconds = None
        if isinstance(prior_record, dict):
            prior_last_tick_at = prior_record.get("last_tick_at")
            if isinstance(prior_last_tick_at, str):
                try:
                    prior_epoch = calendar.timegm(
                        time.strptime(prior_last_tick_at, _STAMP_FORMAT)
                    )
                    delta = now_epoch - prior_epoch
                    if delta > 0:
                        observed_interval_seconds = delta
                except (ValueError, TypeError):
                    pass

        payload: dict[str, Any] = {
            "holder_session_id": holder_session_id,
            "holder_name": holder_name,
            "last_tick_at": _iso(now_epoch),
            "tick_source": tick_source,
            "next_expected_by": next_expected_by(
                now_epoch, interval_seconds, observed_interval_seconds
            ),
            "subscribed_peers": subscribed_peers,
            "declinations": list(declinations) if declinations is not None else None,
            "writer_session_id": writer_session_id,
            # ITEM 2 (`--status` false-alive with no process check). Additive
            # keys the DoE reader never asked for and ignores by name -- see
            # this module's own `_READER_KEYS` pin docstring on the reader's
            # by-name-not-by-shape contract. `pid_start_epoch` is `None` when
            # `psutil` could not be consulted -- `process_confirmed_alive`
            # reads a bare `pid` with no epoch as UNCONFIRMED, never as a
            # weaker-but-usable witness (see that function's own docstring for
            # why: `stable_pid_alive` reads a missing epoch AND lstart as
            # unconditionally dead).
            "pid": pid,
            "pid_start_epoch": pid_start_epoch,
        }

        # PRIOR-HOLDER TRACE (C1). Whenever the record about to be replaced
        # was written by a DIFFERENT instrument -- any of holder, writer, or
        # tick_source differing, one disjunction with tick_source a
        # first-class arm -- carry that instrument's identity forward as
        # `prior_*` keys. Additive only: never reorders, renames, or drops an
        # existing key, and never changes the timestamp format (the record
        # shape is a cross-plane contract -- see module docstring). Never
        # refuses, never raises. A record with no prior write (first stamp)
        # carries no `prior_*` keys at all -- absent, not null.
        if isinstance(prior_record, dict) and _writer_identity(prior_record) != (
            holder_session_id,
            writer_session_id,
            tick_source,
        ):
            payload["prior_holder_session_id"] = prior_record.get("holder_session_id")
            payload["prior_holder_name"] = prior_record.get("holder_name")
            payload["prior_tick_source"] = prior_record.get("tick_source")
            payload["prior_last_tick_at"] = prior_record.get("last_tick_at")
            # DEFECT 1 FIX. The trace above names WHO wrote the destroyed
            # record; these two scalars name WHAT it counted.
            # `subscribed_peers` and `declinations` are the substantive
            # product of a tick -- without these, the trace answers "whose
            # record did I replace" but not "what did I destroy".
            # `declinations` itself is not carried (a list, and this trace is
            # deliberately additive scalars only -- see the module
            # docstring's "prior_* keys" note); its length is. An
            # older-format prior record that lacks either key (pre-this-fix)
            # reads back `None` via `.get`, not a crash -- the trace degrades
            # to "unknown" rather than inventing a count that was never
            # written.
            prior_declinations = prior_record.get("declinations")
            payload["prior_subscribed_peers"] = prior_record.get("subscribed_peers")
            payload["prior_declination_count"] = (
                len(prior_declinations) if isinstance(prior_declinations, list) else None
            )

        return write_atomic(watch_file, payload)
    finally:
        # RELEASE UNCONDITIONALLY, on every return path above (decline or
        # write) -- a guard left held by a process that already finished its
        # tick would starve every peer for `_GUARD_HOLD_SECONDS` +
        # `_GUARD_STALE_GRACE_SECONDS` for nothing.
        _release_guard(watch_file, guard_pid)


def displacement_record(
    repo_root: str,
    holder_session_id: str,
    writer_session_id: Optional[str] = None,
    now_epoch: Optional[float] = None,
) -> Optional[dict]:
    """The foreign record that just declined a `stamp()` call for these
    identifiers, IFF that decline was a HOLDER mismatch -- the signal
    `group_em.watch`'s held loop tears itself down on (item 1, the memo's
    gated ask). `None` for every other shape: no decline at all (the record
    is not fresh-and-foreign), or a decline whose holder is unchanged -- a
    same-holder writer or `tick_source` mismatch is the same crown's OTHER
    instrument declining (see `is_fresh_and_foreign`'s own HOLDER-OR-WRITER
    note), never a displacement, and must stay a quiet decline exactly as
    before this function existed.

    CALLER CONTRACT: call this ONLY immediately after `stamp()` has already
    returned `False` for the SAME `(repo_root, holder_session_id,
    writer_session_id)`. A decline never mutates the record on disk
    (`test_a_fresh_foreign_record_is_declined_and_survives_unchanged`), so
    this re-read sees what `stamp()` just saw, absent a THIRD writer racing
    in between the two reads -- an accepted, documented residual, the same
    class `stamp`'s own "NO LOCK SPANS READ-DECIDE-WRITE" note already
    carries for the write side. Never called from `stamp()` itself: two
    reads of one record for one decline is a cost only the caller that
    needs the extra fact should pay.

    Shares `is_fresh_and_foreign` with `stamp`'s own decline test and the
    arm-time refusal (`group_em.watch._refuse_if_already_armed`) -- one
    predicate, three call sites now, never a second opinion invented here
    about whose record wins.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    record = _read_record(watch_path(repo_root))
    if not is_fresh_and_foreign(
        record, now_epoch, holder_session_id, writer_session_id
    ):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("holder_session_id") == holder_session_id:
        return None
    return record


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
    # Trailing
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
            "next_expected_by": None,
            "seconds_overdue": None,
            "pid": None,
            "pid_start_epoch": None,
            "remedy": REARM_COMMAND,
        }

    holder_session_id = record.get("holder_session_id")
    # `holder_name` is carried as PROVENANCE -- how the holder was known when
    # the tick was written -- never as an address to send to and never as an
    # instruction to re-resolve from the sid beside it. In the case that
    # matters (the holder re-pointed or exited) that sid is exactly the one
    # that no longer resolves, so "re-resolve from it" reads like a check and
    # performs like a ritual, failing where a reader needs it most.
    #
    # ITEM 4 (`read_liveness` drops `next_expected_by`/`pid`* from its
    # payload). `next_expected_by` is the raw deadline string this tick
    # promised the next one by -- carried here EVEN WHEN ARMED, not only on
    # the STALE branches below that already compute `seconds_overdue`
    # against it. `ops/group_em_enter.py::_run_watch_liveness` forwards this
    # dict verbatim into the entry sequence's `watch_liveness` leg; before
    # this fix an ARMED tick's deadline was computed internally and then
    # discarded, so that leg read green with no deadline a caller could act
    # on. `pid`/`pid_start_epoch` are carried for the same reason -- they
    # are item 2's own inputs (`process_confirmed_alive`), and a caller of
    # `read_liveness` other than `--status` (the entry leg above) gets the
    # same evidence rather than having to re-open the record itself.
    base = {
        "holder_session_id": holder_session_id,
        "holder_name": record.get("holder_name"),
        "last_tick_at": record.get("last_tick_at"),
        "tick_source": record.get("tick_source"),
        "next_expected_by": record.get("next_expected_by"),
        "subscribed_peers": record.get("subscribed_peers"),
        "declinations": record.get("declinations"),
        "pid": record.get("pid"),
        "pid_start_epoch": record.get("pid_start_epoch"),
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
    The `armed` branch carries the identical hazard: a record clobbered to
    zero population (no subscribed peers, no declinations) is still
    structurally ARMED, so the reassurance line is suppressed there too and
    the zero is named instead -- "running but reported on nobody" is a
    different fact from a healthy quiet fleet, and rendering them the same
    is this same failure one branch over.

    Prose, not a code: the caller decides the exit code (see `watch._cli`'s
    `--status`), and no consumer should parse these words back into a verdict.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    verdict = liveness.get("verdict")
    # ITEM 3 (`human_verdict` prints the holder's bare name). A bare name is
    # ambiguous the moment two live sessions share one -- this repo's own
    # display names are operator-chosen, not unique by construction. The
    # session id is the one field that IS unique, so it always goes on
    # alongside the name when both are known; the id alone (name absent, an
    # older or nameless record) is still unambiguous on its own.
    holder_name = liveness.get("holder_name")
    holder_session_id = liveness.get("holder_session_id")
    if holder_name and holder_session_id:
        holder = f"{holder_name} [{holder_session_id}]"
    elif holder_name:
        holder = holder_name
    elif holder_session_id:
        holder = holder_session_id
    else:
        holder = "unknown holder"
    remedy = liveness.get("remedy") or REARM_COMMAND
    age = timestamps.age_seconds(liveness.get("last_tick_at"), now_epoch)

    if verdict == VERDICT_ARMED:
        when = (
            f"{timestamps.age_phrase(age)} ago"
            if age is not None
            else f"at {timestamps.with_age(liveness.get('last_tick_at'))}"
        )
        # WHAT ACTUALLY LOOKED. An `entry` stamp never polled the fleet -- it
        # is a stand-in tick a session writes on its own way in, not a
        # sensor read -- so the headline must not claim a check that did not
        # happen just because every OTHER tick_source performs one.
        if liveness.get("tick_source") == "entry":
            headline = f"ALIVE - a watch entry was stamped {when} (an entry tick does not check the fleet)."
        else:
            headline = f"ALIVE - a watch is running and checked the fleet {when}."
        lines = [headline, f"  Held by: {holder}"]
        # GATE ON THE POPULATION ACTUALLY WATCHED, NOT ON DECLINATIONS ALONE.
        # `declinations` are peers this tick chose not to nudge -- they are
        # not coverage, and an entry tick's declinations by themselves used
        # to vouch for a watch that had subscribed to nobody.
        declinations = liveness.get("declinations")
        declination_count = len(declinations) if isinstance(declinations, list) else 0
        if liveness.get("subscribed_peers"):
            lines.append("  Quiet is the normal state between checks; it is not a fault.")
        else:
            lines.append(
                f"  Reported on nobody: 0 subscribed peers and {declination_count} "
                "declinations this tick."
            )
            lines.append(
                "  This is NOT a healthy quiet fleet -- nothing was watched, not nothing found."
            )
    elif verdict == VERDICT_STALE:
        overdue = liveness.get("seconds_overdue")
        late = (
            f" and is {timestamps.age_phrase(overdue)} past the deadline it set itself"
            if isinstance(overdue, (int, float))
            else " and has missed the deadline it set itself"
        )
        when = f"{timestamps.age_phrase(age)} ago" if age is not None else "at an unreadable time"
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
