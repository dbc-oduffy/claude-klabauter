"""coordinator_core.warm.push_cadence -- the bounded push cadence that
replaces the per-commit detached push.

Spec backlink: docs/plans/2026-08-30-who-pushes-and-when.md § C4

WHAT THIS REPLACES. `git_native`'s commit path used to spawn a fresh
`python.exe` per commit (`hooks/auto_push.py::_detach_and_run`) to push the
commit it just made. That per-commit detach is what C6/C7 delete; this
module is the named, bounded guarantee that makes deleting it safe -- every
commit still reaches the remote, just on a 600s cadence instead of
immediately.

HOST: the warm engine's existing idle watchdog thread
(`warm.server._ServerContext._idle_watchdog_loop`), already polling every
`warm.server._IDLE_WATCHDOG_POLL_SECS` (5.0s) independent of the accept
loop. `on_idle_tick` is a counter on that EXISTING tick -- not a second
thread, not a job queue. `PUSH_CADENCE_INTERVAL_SECS` (600) is strictly
under `warm.idle.DEFAULT_IDLE_MINUTES` (15 * 60 = 900s) so the cadence
always gets at least one shot before the server would otherwise demote.

SYNCHRONOUS, ON PURPOSE (DR-329, docs/decisions/DR-329-push-runs-on-a-
cadence-not-on-every-commit.md). No detached child, no new process, no new
thread: a detach costs +130ms CPU and +7 procs fleet-wide to save ~64ms of
one session's wall, a net loss under DR-344 (process time, never wall
clock) at the 50-70-session load norm. The sweep runs inline on the
watchdog thread and returns before the next tick.

THE REPO SET is derived from what THIS server has actually served -- never
a disk scan, never a hardcoded path. `warm.server._ServerContext` records
each request's envelope-carried repo root (`ipc.resolve_request_repo`) as
it is served; `on_idle_tick`'s `served_repos` callable is that recorded
set. A server that has served zero requests for repo R never sweeps R --
safe only because a server's own final sweep (the exit-path leg wired via
`set_final_sweep_hook`/`warm.lifecycle._run_tail`) fires on every exit,
including superseded-generation retirement, so the predecessor's
unpushed-at-handoff state is swept before the predecessor actually exits.

SWEEP COST BUDGET. Serial over every served repo -- N x push, not one push.
Each repo's own push is bounded by `push_with_retry`'s existing
`CADENCE_PUSH_RETRY_BUDGET_SECS` ladder deadline (its OWN budget, separate
from the interactive `PUSH_RETRY_BUDGET_SECS` -- C5, 2026-08-30, see that
constant's own docstring), reused here unmodified, not duplicated. The
whole sweep additionally REFUSES TO START a repo that cannot finish before
`SWEEP_TOTAL_CEILING_SECS` elapses (`sweep_repos`'s own docstring), so
`SWEEP_TOTAL_CEILING_SECS` is an enforced worst-case bound on the sweep's
own occupancy, not merely a stop-taking-new-repos check that a repo
admitted just under the deadline could still run past -- one wedged repo
cannot make the sweep itself the next unbounded-shutdown-hang class this
plan exists to retire. `EXIT_SWEEP_CEILING_SECS` is tighter than the
idle-tick ceiling: an
unbounded exit-path sweep directly lengthens warm-restart latency
(`lifecycle.begin_shutdown`'s docstring -- a same-token successor cannot
bind until the whole shutdown sequence completes), where the idle-tick
sweep merely delays the NEXT tick by the same amount, against a
900s-default idle deadline it cannot itself extend (`should_demote` is
evaluated before this module ever runs, never after).

THE SWEEP'S UNIT is the served WORKTREE ROOT, not the git COMMON dir --
`push_outstanding` resolves HEAD off the worktree, matching every other
cadence-surface caller. A commit made in a linked worktree this server has
never itself served sits outside the bound with no signal; this is an
accepted exposure (named here, not closed by this module), the same shape
as the pre-existing "uncommitted work" and "~10 minutes of committed work"
accepts this plan already carries.

CONCURRENT SWEEPS ACROSS RESIDENT GENERATIONS. `warm.idle`'s own docstring
records three resident generations off one publish inside one observed
hour; every one of them derives repo R into its own served set and sweeps
it on its own 600s tick, so two sweeps can race `push_outstanding` on one
shared branch. `_acquire_sweep_lock`/`_release_sweep_lock` serialize via a
per-repo lockfile in the git COMMON dir (shared across every worktree and
every resident generation of this engine) with PID-liveness-checked
stale-holder takeover, mirroring the idiom `coordinator_core.session.
day_branch_cut_lock` already uses for an unrelated tree-wide mutex -- NOT
the auto_push pending-record holder claim, which this plan removes the
only writer of (`_write_pending_record` is reachable only from
`_hold_window`, itself reachable only from `auto_push.main()`) and must
not be resurrected as an arbitration mechanism. A second concurrent
sweeper DECLINES (returns without pushing) rather than racing.

FEEDS THE FAILURE DETECTOR. `push_with_retry`/`push_outstanding` never call
`auto_push.log_failure` -- that file's only two writers sit on the path
C6/C7 delete, and the Stop-time push-failure detector
(`runtime-tripwire-em-check.py::_check_push_failures`, DoE-claude) reads
`.git/push-failures.log` written only by `log_failure`. A declined/failed
sweep push records a row through `log_failure` directly so the detector
does not go quiet on exactly the failures the cadence now owns.

DOES NOT DRAIN. This module used to call `drain_pending_push(repo_root)`
ahead of `push_outstanding` on every swept repo, "for free" call site
reasoning that no longer holds: `drain_pending_push`'s only production
writer, `_write_pending_record`, is reachable only from `_hold_window`,
which C8 gravestoned -- so the record it would drain is never written on
any surviving path (review: overengineering-reviewer, Finding 3,
2026-08-30). `push_outstanding`'s own outstanding-work decision does not
depend on the drain either way (it compares HEAD to the upstream ref
directly), so removing the call changes nothing this module's own bound
relies on. The pending-record subsystem itself -- `_write_pending_record`,
`drain_pending_push`, and the `workday.drain_pending_push` op -- was
gravestoned in this same follow-on pass (docs/plans/2026-08-30-who-pushes-
and-when.md C2/C8); only the read primitives (`_read_pending_record`,
`_pending_record_path`, `_record_is_stale`) survive, restored for
`orientation/regenerate_cache.py::emit_auto_push_health`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Union

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_state import head_branch
from coordinator_core.hooks.auto_push import log_failure
from coordinator_core.ops.ceremony.push import CADENCE_PUSH_RETRY_BUDGET_SECS
from coordinator_core.ops.push_outstanding import push_outstanding
from coordinator_core.session.day_branch_cut_lock import holder_alive

__all__ = [
    "PUSH_CADENCE_INTERVAL_SECS",
    "SWEEP_TOTAL_CEILING_SECS",
    "EXIT_SWEEP_CEILING_SECS",
    "ServedReposFn",
    "on_idle_tick",
    "sweep_repos",
    "reset_cadence_for_test",
]

#: Strictly under `warm.idle.DEFAULT_IDLE_MINUTES * 60` (900s) -- see module
#: docstring's HOST section for why that ordering matters.
PUSH_CADENCE_INTERVAL_SECS = 600.0

#: The idle-tick sweep's own ceiling (DR-401, 2026-09-01, re-derived a
#: SECOND time same-day after an EM ruling reversed the first re-derivation
#: below -- see DR-401's amended "Ripple" section for the full history).
#: Sized for exactly ONE slow (has-outstanding-work, needs-the-ladder) repo
#: per idle tick, not two: `CADENCE_PUSH_RETRY_BUDGET_SECS` (16.0) is the
#: floor a single repo needs to clear reliably (`sweep_repos` refuses to
#: START a repo it cannot finish inside this deadline, so anything at or
#: below 16.0 would refuse every repo -- see that function's own
#: docstring); this adds 2.0s on top, covering the sweep's own per-repo
#: overhead beyond the push itself -- one extra git spawn for
#: `head_branch` inside `_feed_failure_detector` (a `git rev-parse`-class
#: call, DR-344's own `git --version` benchmark puts a bare spawn at
#: ~25ms, so 2.0s is generous headroom, not a tight fit) plus the sweep
#: lock's file I/O (sub-millisecond). A second slow repo in the same tick
#: is deliberately NOT budgeted for: it waits for the unconditional retry
#: at `PUSH_CADENCE_INTERVAL_SECS` (600s) instead -- correctness never
#: depends on any one tick succeeding (DR-401's own "Why 16.0, not
#: floor-plus-margin" reasoning). This keeps worst-case idle-tick occupancy
#: near C5's original 14.0s magnitude rather than C5's ratio times the new
#: floor (34.0), honoring the load norm's "an op occupying the box for
#: seconds is real load for ~50-70 queued peers"
#: (`docs/wiki/machine-load-norm.md`). A repo with nothing outstanding
#: costs ~0 via `push_outstanding`'s zero-spawn arm and is unaffected by
#: this number either way, so the two-slow-repo case this sizing declines
#: to cover needs two repos to BOTH have outstanding work AND both be slow
#: in the same tick -- the rare case, not the norm. No multi-repo-same-tick
#: freshness requirement is named anywhere in DR-401 or this module; if one
#: is ever actually named, this number is the one to revisit.
SWEEP_TOTAL_CEILING_SECS = 18.0

#: The exit-path sweep's ceiling is tighter than the idle-tick one -- an
#: unbounded exit sweep directly lengthens warm-restart latency (module
#: docstring's SWEEP COST BUDGET section: a same-token successor cannot
#: bind until the whole shutdown sequence completes). Re-derived (DR-401,
#: 2026-09-01, second re-derivation -- see `SWEEP_TOTAL_CEILING_SECS`'s
#: docstring for the history) for the SAME one-repo-per-tick sizing:
#: `CADENCE_PUSH_RETRY_BUDGET_SECS` (16.0) plus 1.0s -- half the overhead
#: margin `SWEEP_TOTAL_CEILING_SECS` carries, kept tighter on purpose
#: (exit-path latency is the more sensitive of the two per this module's
#: own SWEEP COST BUDGET reasoning) while still leaving enough room to
#: admit the one repo the exit sweep exists to serve (the predecessor's
#: unpushed-at-handoff state, module docstring's THE REPO SET section) --
#: a ceiling at or below 16.0 would refuse it outright, the same floor
#: violation this whole record exists to fix. Strictly under
#: `SWEEP_TOTAL_CEILING_SECS` (18.0).
EXIT_SWEEP_CEILING_SECS = 17.0

#: A zero-arg callable returning the repos (worktree roots) this server has
#: actually served, in the order first served. `on_idle_tick`'s caller
#: (`warm.server._ServerContext`) binds this to a live read of its own
#: recorded set, never a snapshot taken at boot.
ServedReposFn = Callable[[], Iterable[Union[str, Path]]]

_cadence_lock = threading.Lock()
_last_sweep_monotonic: Optional[float] = None

_SWEEP_LOCK_NAME = "coordinator-push-cadence-sweep.json"
#: Generous headroom over one repo's own `push_with_retry` ladder deadline
#: -- a live sweep should never be mistaken for stale mid-push. Keyed to
#: `CADENCE_PUSH_RETRY_BUDGET_SECS` (this module's own cadence-path budget,
#: C5, 2026-08-30), not the interactive `PUSH_RETRY_BUDGET_SECS` this sweep
#: never uses -- that stale coupling held the lock ~4x the work it bounds
#: (overengineering-reviewer finding 5).
_SWEEP_LOCK_HOLD_SECS = CADENCE_PUSH_RETRY_BUDGET_SECS + 10.0
#: Grace past `hold_until` before a peer calls a still-recorded holder
#: stale, mirroring `coordinator_core.session.day_branch_cut_lock`'s own
#: constant of the same name.
_SWEEP_LOCK_STALE_GRACE_SECS = 60.0


def reset_cadence_for_test() -> None:
    """Test-only: clear the module-level cadence clock. Never called by
    production code -- a real server ticks continuously for its whole life
    and never wants to "forget" the last sweep time.
    """
    global _last_sweep_monotonic
    with _cadence_lock:
        _last_sweep_monotonic = None


def _sweep_due(*, clock: Callable[[], float], interval_secs: float) -> bool:
    """True once every `interval_secs` of ticks, false otherwise -- the
    counter-on-an-existing-tick this module's docstring names. The FIRST
    tick after boot or a test reset primes the clock and does not itself
    sweep (there is nothing to have accumulated yet in the time between
    server boot and the first tick); every tick after that fires once
    `interval_secs` has actually elapsed since the last sweep (or since
    priming), never before.
    """
    global _last_sweep_monotonic
    now = clock()
    with _cadence_lock:
        last = _last_sweep_monotonic
        if last is None:
            _last_sweep_monotonic = now
            return False
        if now - last < interval_secs:
            return False
        _last_sweep_monotonic = now
        return True


# ---------------------------------------------------------------------------
# Per-repo sweep lock -- serializes concurrent sweeps across resident
# generations sharing one git common dir. See module docstring's
# CONCURRENT SWEEPS section.
# ---------------------------------------------------------------------------


def _sweep_lock_path(repo_root: Union[str, Path]) -> Path:
    return resolve_git_common_dir(repo_root) / _SWEEP_LOCK_NAME


def _sweep_lock_is_stale(record: dict, now: float) -> bool:
    if holder_alive(record.get("holder_pid")) is False:
        return True
    hold_until = record.get("hold_until")
    return isinstance(hold_until, (int, float)) and now > hold_until + _SWEEP_LOCK_STALE_GRACE_SECS


def _try_create_sweep_lock(path: Path, payload: dict) -> bool:
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False
    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(fd)
    return True


def _read_sweep_lock(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _acquire_sweep_lock(
    repo_root: Union[str, Path], *, now: Optional[float] = None, pid: Optional[int] = None
) -> bool:
    """True iff this process now owns the sweep lock for `repo_root` -- a
    second concurrent sweeper (any resident generation) sees an unexpired
    record here and declines (returns False) rather than racing
    `push_outstanding` on the same branch.
    """
    now = time.time() if now is None else now
    pid = os.getpid() if pid is None else pid
    path = _sweep_lock_path(repo_root)
    payload = {"holder_pid": pid, "hold_until": now + _SWEEP_LOCK_HOLD_SECS}

    if _try_create_sweep_lock(path, payload):
        return True

    record = _read_sweep_lock(path)
    if record is None or _sweep_lock_is_stale(record, now):
        try:
            path.unlink()
        except OSError:
            pass
        return _try_create_sweep_lock(path, payload)
    return False


def _release_sweep_lock(repo_root: Union[str, Path], *, pid: Optional[int] = None) -> None:
    """Best-effort release -- never raises, and never releases a foreign
    holder's record (a stale-but-foreign record is left for the next
    acquirer's own takeover check, not unlinked here).

    Review: code-reviewer P3, 2026-08-30 -- reading the record and then
    unlinking BY PATH is check-then-act: if this holder's own hold window
    has already run past `_SWEEP_LOCK_HOLD_SECS` + `_SWEEP_LOCK_STALE_GRACE_SECS`
    (this process overran its own generous budget) a peer can have already
    declared this record stale, `unlink()`ed it, and recreated it as its
    own live lock between the read below and this function's `unlink()` --
    which would then delete the PEER's live lock by path, not by identity.
    `os.stat`+`st_ino`/`st_dev` on the path immediately before unlinking
    closes that window down to the syscall gap between the two calls
    (irreducible without a platform-level atomic compare-and-delete):
    a peer's takeover always creates a NEW inode, so a mismatch here means
    "someone else already owns this path" and is treated exactly like a
    foreign holder_pid -- leave it alone.
    """
    pid = os.getpid() if pid is None else pid
    path = _sweep_lock_path(repo_root)
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        st = os.fstat(fd)
        data = os.read(fd, 65536)
    finally:
        os.close(fd)
    try:
        record = json.loads(data.decode("utf-8"))
    except ValueError:
        return
    if not isinstance(record, dict) or record.get("holder_pid") != pid:
        return
    try:
        cur_st = os.stat(path)
    except OSError:
        return
    if cur_st.st_ino != st.st_ino or cur_st.st_dev != st.st_dev:
        return
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Failure-detector feed -- see module docstring's FEEDS THE FAILURE DETECTOR.
# ---------------------------------------------------------------------------


def _feed_failure_detector(repo_root: Union[str, Path], outcome) -> None:
    branch = head_branch(Path(repo_root)) or "<unknown>"
    # `is_unconfirmed` and `err_class` are the SAME decision, so they are
    # taken together here rather than re-derived downstream from the
    # `err_class` string. The log row used to headline `PUSH FAILED` on both
    # legs while carrying `sweep-unconfirmed` in its class field -- the row
    # contradicted itself, and the readers key on the headline.
    if outcome.failed:
        first_err = "; ".join(outcome.failed)
        err_class = "sweep-failed"
        is_unconfirmed = False
    else:
        first_err = "; ".join(outcome.unconfirmed)
        err_class = "sweep-unconfirmed"
        is_unconfirmed = True
    # `outcome.attempts`, never a literal: this feed passed `1` for three
    # months, and `log_failure` writes that number into `.git/push-failures.log`
    # as `after <N>` -- which every reader takes as the ladder depth actually
    # run. Example-retrieval-repo-em read `cadence-sweep/... after 1` beside
    # `direct push/... after 3` and inferred an asymmetric one-attempt ladder on
    # this leg (memo 2026-08-30). There is no such asymmetry: this leg reaches
    # `push_with_retry` through `push_outstanding` with the same
    # `_PUSH_MAX_RETRIES` every other caller gets. `None` renders `after ?`.
    try:
        log_failure(
            str(repo_root),
            branch,
            "cadence-sweep",
            err_class,
            outcome.attempts,
            first_err,
            "",
            unconfirmed=is_unconfirmed,
        )
    except Exception:  # noqa: BLE001 -- feeding the detector must never raise
        pass


# Review: overengineering-reviewer Finding 1 -- `per_repo_deadline` existed
# only to be `del`eted on entry; doctrine forbids a signature carrying a
# parameter no caller needs and no callee uses.
# Review: overengineering-reviewer Finding 3 -- no `drain_pending_push` call
# here; see module docstring's DOES NOT DRAIN section for why.
def _sweep_one(repo_root: Union[str, Path]) -> None:
    """Push exactly one repo -- declining outright if another sweeper
    already holds this repo's lock. The per-repo bound is enforced by
    `push_with_retry`'s own `CADENCE_PUSH_RETRY_BUDGET_SECS`-keyed ladder
    deadline inside `push_outstanding` itself -- see the module docstring's
    SWEEP COST BUDGET section, not re-implemented here. Passes the
    cadence's OWN, smaller budget (C5, 2026-08-30) -- never the interactive
    `PUSH_RETRY_BUDGET_SECS` `push_outstanding` defaults to for every other
    caller.
    """
    root = Path(repo_root)
    if not _acquire_sweep_lock(root):
        return
    try:
        try:
            outcome = push_outstanding(root, budget_secs=CADENCE_PUSH_RETRY_BUDGET_SECS)
        except Exception:  # noqa: BLE001 -- a sweep push must never raise
            return
        if outcome.failed or outcome.unconfirmed:
            _feed_failure_detector(root, outcome)
    finally:
        _release_sweep_lock(root)


def sweep_repos(
    repos: Iterable[Union[str, Path]],
    *,
    total_ceiling_secs: float = SWEEP_TOTAL_CEILING_SECS,
    per_repo_budget_secs: float = CADENCE_PUSH_RETRY_BUDGET_SECS,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Sweep every repo in `repos`, serially, stopping (without pushing to
    any remaining repo) once `total_ceiling_secs` has elapsed since this
    call started -- see module docstring's SWEEP COST BUDGET. Never raises:
    every per-repo step is already caught inside `_sweep_one`.

    REFUSES TO START a repo that cannot finish inside the deadline (C5,
    2026-08-30): the old check (`clock() >= deadline` at the TOP of each
    iteration, then run a whole repo) let a repo entered just under the
    deadline spend its full budget anyway, so the true worst case was
    `total_ceiling_secs + per_repo_budget_secs` -- 72s at the pre-C5
    60.0/12.0 pairing, still over the criterion's 15s bound even after
    lowering just the ceiling. Checking `now + per_repo_budget_secs >
    deadline` makes `total_ceiling_secs` the real bound: no repo this call
    ever touches can push it past that ceiling -- for any positive budget
    this single check subsumes the plain `now >= deadline` case, so that
    clause is dropped rather than kept as unreachable dead weight
    (overengineering-reviewer finding 3). `per_repo_budget_secs` defaults to
    the cadence's own
    `CADENCE_PUSH_RETRY_BUDGET_SECS` -- the same budget `_sweep_one` hands
    `push_outstanding` -- so the admission guard and the actual per-repo
    spend agree without a second number to keep in sync.
    """
    deadline = clock() + total_ceiling_secs
    seen: List[Path] = []
    for repo in repos:
        now = clock()
        if now + per_repo_budget_secs > deadline:
            break
        root = Path(repo)
        if root in seen:
            continue
        seen.append(root)
        _sweep_one(root)


def on_idle_tick(
    *,
    served_repos: ServedReposFn,
    clock: Callable[[], float] = time.monotonic,
    interval_secs: float = PUSH_CADENCE_INTERVAL_SECS,
    total_ceiling_secs: float = SWEEP_TOTAL_CEILING_SECS,
    sweep_fn: Callable[..., None] = sweep_repos,
) -> bool:
    """Call on every idle-watchdog tick (`warm.server._ServerContext.
    _idle_tick`). Runs a sweep, synchronously, on THIS thread, iff at least
    `interval_secs` have elapsed since the last sweep (or since the first
    tick this process ever saw) -- never before, never on a second thread.

    Returns True iff a sweep ran this tick, so a caller/test can observe
    cadence timing without depending on `sweep_fn`'s own side effects.
    """
    if not _sweep_due(clock=clock, interval_secs=interval_secs):
        return False
    sweep_fn(served_repos(), total_ceiling_secs=total_ceiling_secs, clock=clock)
    return True
