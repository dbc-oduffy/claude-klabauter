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
cadence-not-on-every-commit.md). No detached child, no child interpreter:
a detach costs +130ms CPU and +7 procs fleet-wide to save ~64ms of one
session's wall, a net loss under DR-344 (process time, never wall clock) at
the 50-70-session load norm. The sweep runs inline on the watchdog thread;
its per-repo pushes run on worker threads that `sweep_repos` itself starts
and joins before it returns, so nothing outlives the call.

THE REPO SET is derived from what THIS server has actually served -- never
a disk scan, never a hardcoded path. `warm.server._ServerContext` records
each request's envelope-carried repo root (`ipc.resolve_request_repo`) as
it is served; `on_idle_tick`'s `served_repos` callable is that recorded
set. A server that has served zero requests for repo R never sweeps R --
safe only because a server's own final sweep (the exit-path leg wired via
`set_final_sweep_hook`/`warm.lifecycle._run_tail`) fires on every exit,
including superseded-generation retirement, so the predecessor's
unpushed-at-handoff state is swept before the predecessor actually exits.

THE FOREIGN-DELIVERY SET is the second repo set: `(root, branch)` pairs
registered by `note_foreign_delivery` (a `memo.send` delivery landed on a
receiver's day branch). `sweep_repos` pushes each named branch after the served
set, within the same deadline, through `_push_foreign` -- never HEAD's branch,
never `main` (`branch_gate`). A pair stays until its remote-tracking ref equals
the local sha. Named residual: a `memo.send` served cold registers in a process
with no idle tick, so its push waits for a later send from a warm server -- the
same accepted-exposure class as the linked-worktree note below.

SWEEP COST BUDGET. Every ahead repo per tick, concurrently. All admitted
pushes start together, so the sweep's occupancy is the LARGEST admitted
ceiling, never the sum, and spawn count and process time equal the serial
design pushing the same repos. Each repo's own push is bounded by
`push_with_retry`'s ladder deadline, which is that repo's resolved ceiling
(`push_ceiling.resolve_push_ceiling`, default `CADENCE_PUSH_RETRY_BUDGET_SECS`,
at most `PUSH_CEILING_MAX_SECS`). At most one repo whose ceiling exceeds the
default is admitted per idle tick (the exit sweep declines it); the rest wait
for the next tick, where longest-since-pushed ordering puts them first. A repo
with nothing outstanding costs ~0 and is never admitted. Because a default
ceiling is at most `SWEEP_TOTAL_CEILING_SECS`, that constant remains the
enforced worst-case bound on the sweep's own occupancy -- one wedged repo
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
(`runtime-tripwire-em-check.py::_check_push_failures`, coordinator-content-repo) reads
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

import concurrent.futures
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_state import head_branch, head_sha
from coordinator_core.git.git_objects import cas_ref
from coordinator_core.git.git_objects import read_ref_loose_or_packed as _ref_sha
from coordinator_core.hooks.auto_push import branch_gate, log_failure
from coordinator_core.ops.ceremony.git_native import push_refspec
from coordinator_core.ops.ceremony.push import CADENCE_PUSH_RETRY_BUDGET_SECS
from coordinator_core.ops.ceremony.push_ceiling import (
    PUSH_CEILING_MAX_SECS,
    resolve_push_ceiling,
)
from coordinator_core.ops.fleet._memo_resolver import RegistryReadError, read_registry_repos
from coordinator_core.ops.push_outstanding import push_outstanding, resolve_push_remote
from coordinator_core.session.day_branch_cut_lock import record_is_stale
from coordinator_core.warm import telemetry
from coordinator_core.warm.push_sweep_order import (
    is_ahead,
    no_upstream_and_no_new_commits,
    since_pushed_key,
)

try:
    from coordinator_core.machine_profile import machine_profile
except ImportError:  # pragma: no cover
    def machine_profile() -> str:
        return "author"

__all__ = [
    "PUSH_CADENCE_INTERVAL_SECS",
    "SWEEP_TOTAL_CEILING_SECS",
    "EXIT_SWEEP_CEILING_SECS",
    "ServedReposFn",
    "on_idle_tick",
    "sweep_repos",
    "registry_sweep_repos",
    "note_foreign_delivery",
    "foreign_deliveries",
    "note_commit",
    "reset_cadence_for_test",
]

#: Strictly under `warm.idle.DEFAULT_IDLE_MINUTES * 60` (900s) -- see module
#: docstring's HOST section for why that ordering matters.
PUSH_CADENCE_INTERVAL_SECS = 600.0

#: The idle-tick sweep's own ceiling (DR-401, 2026-09-01, re-derived a
#: SECOND time same-day after an EM ruling reversed the first re-derivation
#: below -- see DR-401's amended "Ripple" section for the full history).
#: `CADENCE_PUSH_RETRY_BUDGET_SECS` (16.0) is the floor a single push needs
#: to clear reliably; this adds 2.0s on top, covering the sweep's own
#: per-repo overhead beyond the push itself -- one extra git spawn for
#: `head_branch` inside `_feed_failure_detector` (DR-344's `git --version`
#: benchmark puts a bare spawn at ~25ms, so 2.0s is generous headroom) plus
#: the sweep lock's file I/O. Every ahead repo is pushed in the same tick,
#: concurrently, so this number bounds the sweep's occupancy (the largest
#: admitted ceiling, never the sum) and stays at 18.0 -- raising it to
#: N x 16.0 would multiply the watchdog's occupancy, the 34.0s shape DR-401
#: refused. A repo with nothing outstanding costs ~0 and is never admitted.
SWEEP_TOTAL_CEILING_SECS = 18.0

#: The exit-path sweep's ceiling is tighter than the idle-tick one -- an
#: unbounded exit sweep directly lengthens warm-restart latency (module
#: docstring's SWEEP COST BUDGET section: a same-token successor cannot
#: bind until the whole shutdown sequence completes). Re-derived (DR-401,
#: 2026-09-01, second re-derivation) for the same occupancy bound:
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
#: `(root, branch)` pairs `note_foreign_delivery` registered, insertion-ordered,
#: each mapped to its consecutive rejected-push count. Guarded by `_cadence_lock`.
_foreign_deliveries: "dict[tuple[Path, str], int]" = {}

#: Consecutive rejected pushes after which a pair is dropped. A rejection that
#: will not self-heal (non-fast-forward, auth) must not cost a push every tick
#: forever; every rejection is still logged for the push-failure detector.
FOREIGN_PUSH_MAX_REJECTS = 3

#: Repos `note_commit` flagged since their last sweep visit; the visit lifts
#: their failure backoff and clears the mark. Marks only -- the push still
#: happens on the cadence (DR-329). Guarded by `_cadence_lock`.
_due_repos: "set[Path]" = set()

#: Per-repo push-failure backoff: `[consecutive_failed_sweeps, sweeps_left_to_skip]`.
#: Without it a repo that fails every tick is re-pushed every tick and logs a
#: `push-failures.log` row each time. Guarded by `_cadence_lock`.
_backoff: "dict[Path, list[int]]" = {}

#: A repo that failed N consecutive sweeps is skipped for `2**(N-1)` sweeps
#: (1, 2, 4, 8), capped here -- 8 ticks is ~80 minutes at the 600s cadence.
BACKOFF_MAX_SKIP_SWEEPS = 8

_SWEEP_LOCK_NAME = "coordinator-push-cadence-sweep.json"
#: Headroom over the longest per-repo ladder deadline any repo can resolve to
#: (`PUSH_CEILING_MAX_SECS`) -- a live long push is never mistaken for stale.
_SWEEP_LOCK_HOLD_SECS = PUSH_CEILING_MAX_SECS + 10.0


def reset_cadence_for_test() -> None:
    """Test-only: clear the module-level cadence clock. Never called by
    production code -- a real server ticks continuously for its whole life
    and never wants to "forget" the last sweep time.
    """
    global _last_sweep_monotonic
    with _cadence_lock:
        _last_sweep_monotonic = None
        _foreign_deliveries.clear()
        _due_repos.clear()
        _backoff.clear()


def _sweep_due(*, clock: Callable[[], float], interval_secs: float) -> bool:
    """True once every `interval_secs` -- the counter-on-an-existing-tick this
    module's docstring names. The first tick after boot or a test reset only
    seeds the clock and returns False, so an idle engine spawns nothing until
    one full interval has passed. Trade: work a predecessor's exit sweep
    could not reach waits up to one interval; it is publish-class work
    outside the process bar, and a generation shorter than one interval
    relies on its exit sweep alone.
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
    if record is None or record_is_stale(record, now):
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

    Reading the record and then
    unlinking BY PATH is check-then-act: if this holder's own hold window
    has already run past `_SWEEP_LOCK_HOLD_SECS` + the stale grace
    `day_branch_cut_lock.record_is_stale` applies
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


_REF_LOCK_PATH_RE = re.compile(r"Unable to create '([^']+\.lock)'")


def _reap_named_ref_lock(root: Path, failures: Iterable[str]) -> bool:
    """True when a failure names a local `<path>.lock` and the reaper removed it.

    Only the lock path git named is offered to `reap_ref_lock`, which owns the
    age and stability judgement; a fresh lock stays and no retry follows.
    """
    from coordinator_core.ops.reap_stale_locks import reap_ref_lock

    for text in failures:
        m = _REF_LOCK_PATH_RE.search(text or "")
        if m and reap_ref_lock(root, m.group(1)):
            return True
    return False


# No `drain_pending_push` call in this module; see its docstring's DOES NOT
# DRAIN section for why.
def _sweep_one(repo_root: Union[str, Path]) -> str:
    """Returns "pushed", "skipped" or "failed" for the sweep telemetry row.

    Push exactly one repo -- declining outright if another sweeper
    already holds this repo's lock. The per-repo bound is enforced by
    `push_with_retry`'s own ladder deadline inside `push_outstanding` itself,
    keyed to the repo's resolved ceiling -- see the module docstring's
    SWEEP COST BUDGET section, not re-implemented here. Passes the
    cadence's OWN budget as the resolver default -- never the interactive
    `PUSH_RETRY_BUDGET_SECS` `push_outstanding` defaults to for every other
    caller. Passes `use_streamed_push=True` (P052-C3, 2026-09-10): the
    cadence sweep is the one sanctioned consumer of `push_with_retry`'s
    progress-watched, silence-stall-detecting push leg -- see that
    parameter's own docstring in `ops.ceremony.push`.
    """
    root = Path(repo_root)
    if machine_profile() != "author":
        return "skipped"
    branch = head_branch(root)
    sha = head_sha(root) if branch is not None else None
    if branch is not None and sha is not None and no_upstream_and_no_new_commits(root, branch, sha):
        return "skipped"
    if not _acquire_sweep_lock(root):
        return "skipped"
    try:
        def _push():
            return push_outstanding(
                root,
                budget_secs=resolve_push_ceiling(
                    root, default_secs=CADENCE_PUSH_RETRY_BUDGET_SECS
                ),
                use_streamed_push=True,
            )

        try:
            outcome = _push()
            if outcome.failed and _reap_named_ref_lock(root, outcome.failed):
                outcome = _push()
        except Exception:  # noqa: BLE001 -- a sweep push must never raise
            return "failed"
        if outcome.failed or outcome.unconfirmed:
            _feed_failure_detector(root, outcome)
            return "failed"
        return "pushed" if "push" in outcome.acted else "skipped"
    finally:
        _release_sweep_lock(root)


def note_commit(repo_root: Union[str, Path]) -> None:
    """Flag `repo_root` as holding a fresh commit: a failure backoff on it is
    lifted for the next sweep visit. Mark only -- never pushes, never reorders
    (DR-329). Idempotent; cleared when the sweep visits the repo.
    """
    with _cadence_lock:
        _due_repos.add(Path(repo_root))
        entry = _backoff.get(Path(repo_root))
        if entry is not None:
            entry[1] = 0


def _backed_off_now(root: Path) -> bool:
    """True (consuming one skip) while `root` is inside its failure backoff."""
    with _cadence_lock:
        entry = _backoff.get(root)
        if entry is None or entry[1] <= 0:
            return False
        entry[1] -= 1
        return True


def _note_sweep_result(root: Path, result: Optional[str]) -> None:
    with _cadence_lock:
        if result == "failed":
            entry = _backoff.setdefault(root, [0, 0])
            entry[0] += 1
            entry[1] = min(2 ** (entry[0] - 1), BACKOFF_MAX_SKIP_SWEEPS)
        else:
            _backoff.pop(root, None)


def note_foreign_delivery(repo_root: Union[str, Path], branch: str) -> None:
    """Register `(repo_root, branch)` for the named-branch push arm. Idempotent;
    a pair leaves the registry only once its remote-tracking ref equals the
    local sha (or the pair is unpushable).
    """
    with _cadence_lock:
        _foreign_deliveries[(Path(repo_root), branch)] = 0


def foreign_deliveries() -> list:
    with _cadence_lock:
        return list(_foreign_deliveries)


def _forget_foreign(pair: tuple) -> None:
    with _cadence_lock:
        _foreign_deliveries.pop(pair, None)


def _note_foreign_reject(pair: tuple) -> None:
    with _cadence_lock:
        if pair not in _foreign_deliveries:
            return
        _foreign_deliveries[pair] += 1
        if _foreign_deliveries[pair] >= FOREIGN_PUSH_MAX_REJECTS:
            _foreign_deliveries.pop(pair, None)


#: stderr fragments (lowercased) of a remote that answered and refused. A
#: timeout or transport failure carries none of them and retries next sweep.
_DEFINITIVE_REJECT_MARKERS = (
    "[rejected]",
    "[remote rejected]",
    "non-fast-forward",
    "protected branch",
    "permission denied",
    "authentication failed",
    "access denied",
    "denied to",
    "pre-receive hook declined",
)


def _is_definitive_reject(stderr: str) -> bool:
    text = (stderr or "").lower()
    return any(marker in text for marker in _DEFINITIVE_REJECT_MARKERS)


def _push_foreign(pair: tuple, *, ceiling_secs: float) -> None:
    """Push one registered named branch; never raises, never touches HEAD's branch."""
    root, branch = pair
    allowed, _reason = branch_gate(branch)
    if not allowed:
        _forget_foreign(pair)
        return
    common_dir = resolve_git_common_dir(root)
    local = _ref_sha(common_dir, f"refs/heads/{branch}")
    if local is None:
        _forget_foreign(pair)
        return
    remote = resolve_push_remote(root, branch)
    if remote is None:
        _forget_foreign(pair)
        return
    tracking = f"refs/remotes/{remote}/{branch}"
    seen = _ref_sha(common_dir, tracking)
    if seen == local:
        _forget_foreign(pair)
        return
    if not _acquire_sweep_lock(root):
        return
    try:
        result = push_refspec(
            root, remote, f"refs/heads/{branch}", f"refs/heads/{branch}",
            timeout=ceiling_secs,
        )
        if not result.ok:
            try:
                log_failure(
                    str(root), branch, "cadence-sweep", "sweep-failed", 1,
                    (result.stderr or "").strip()[:500], "", unconfirmed=False,
                )
            except Exception:  # noqa: BLE001 -- feeding the detector must never raise
                pass
            if _is_definitive_reject(result.stderr):
                _note_foreign_reject(pair)
            return
        _forget_foreign(pair)
        if _ref_sha(common_dir, tracking) != local:
            cas_ref(common_dir, tracking, _ref_sha(common_dir, tracking), local)
    except Exception:  # noqa: BLE001 -- a sweep push must never raise
        return
    finally:
        _release_sweep_lock(root)


def _sweep_foreign(*, deadline: float, clock: Callable[[], float]) -> None:
    if machine_profile() != "author":
        return
    for pair in foreign_deliveries():
        try:
            ceiling = resolve_push_ceiling(pair[0], default_secs=CADENCE_PUSH_RETRY_BUDGET_SECS)
            if clock() + ceiling > deadline:
                return
            _push_foreign(pair, ceiling_secs=ceiling)
        except Exception:  # noqa: BLE001 -- sweep_repos promises never to raise
            continue


def sweep_repos(
    repos: Iterable[Union[str, Path]],
    *,
    total_ceiling_secs: float = SWEEP_TOTAL_CEILING_SECS,
    per_repo_budget_secs: Optional[float] = None,
    allow_extended_first: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Push every ahead repo in `repos` inside one call, concurrently, then
    the named-branch arm. Never raises: every per-repo step is caught inside
    `_sweep_one`, and a worker exception counts as "failed".

    CLASSIFY (zero spawns). Backed-off repos and repos with nothing
    outstanding (`push_sweep_order.is_ahead` False) are skipped without
    spending anything. The ahead repos are ordered by
    `push_sweep_order.since_pushed_key`, longest-since-pushed first, so a repo
    that was not admitted keeps its old key and leads the next tick.

    ADMIT. Each ahead repo's ceiling is resolved once (`resolve_push_ceiling`;
    `per_repo_budget_secs`, when given, is a test seam replacing the
    resolver). Every default-ceiling repo is admitted: all pushes start
    together, so the sweep's occupancy is the largest admitted ceiling, never
    the sum, and `total_ceiling_secs` needs no per-repo reservation. At most
    one repo whose ceiling exceeds `CADENCE_PUSH_RETRY_BUDGET_SECS` is
    admitted, only with `allow_extended_first=True` (`on_idle_tick` passes
    it; the exit sweep does not). Any other extended repo is `not_reached`.

    RUN. The admitted pushes run on a `ThreadPoolExecutor` created and joined
    inside this call, one worker per admitted repo -- the width is the ahead
    count, never a constant. A push is never clamped below its own ceiling:
    a push killed mid-flight is `unconfirmed`. Results are recorded on the
    calling thread after the join.

    ARM B (P052-C5): the silence watchdog `git_native.push_streamed` adds is
    layered under the per-repo ladder deadline; every ceiling is capped at
    `PUSH_CEILING_MAX_SECS`, so no push runs unbounded.
    """
    start = clock()
    deadline = start + total_ceiling_secs
    ordered = list(dict.fromkeys(Path(repo) for repo in repos))
    pushed: list = []
    skipped: list = []
    failed: list = []
    backed_off: list = []
    ahead: list = []
    for root in ordered:
        if _backed_off_now(root):
            skipped.append(root)
            backed_off.append(root)
        elif is_ahead(root):
            ahead.append(root)
        else:
            skipped.append(root)
    ahead.sort(key=since_pushed_key)
    admitted: list = []
    not_reached: list = []
    extended_taken = False
    for root in ahead:
        ceiling = (
            per_repo_budget_secs
            if per_repo_budget_secs is not None
            else resolve_push_ceiling(root, default_secs=CADENCE_PUSH_RETRY_BUDGET_SECS)
        )
        if ceiling > CADENCE_PUSH_RETRY_BUDGET_SECS:
            if not allow_extended_first or extended_taken:
                not_reached.append(root)
                continue
            extended_taken = True
        admitted.append(root)
    results: dict = {}
    if admitted:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(admitted), thread_name_prefix="push-sweep"
        ) as pool:
            futures = {root: pool.submit(_sweep_one, root) for root in admitted}
        for root, future in futures.items():
            try:
                results[root] = future.result()
            except Exception:  # noqa: BLE001 -- a sweep push must never raise
                results[root] = "failed"
    for root in admitted:
        result = results[root]
        with _cadence_lock:
            _due_repos.discard(root)
        _note_sweep_result(root, result)
        {"pushed": pushed, "failed": failed}.get(result, skipped).append(root)
    _sweep_foreign(deadline=deadline, clock=clock)
    with _cadence_lock:
        streaks = {root: _backoff[root][0] for root in backed_off if root in _backoff}
    telemetry.record_sweep(
        pushed,
        skipped,
        failed,
        not_reached=not_reached,
        backoff=streaks,
        ahead=ahead,
        elapsed_secs=clock() - start,
    )


def registry_sweep_repos(served: Iterable[Union[str, Path]]) -> list:
    """Served repos first, then the machine registry's repos: existing
    directories only, deduplicated by resolved path. An unreadable registry
    leaves the served set alone."""
    seen: "dict[Path, Path]" = {}
    try:
        registry = list(read_registry_repos().values())
    except RegistryReadError:
        registry = []
    for raw in (*served, *registry):
        path = Path(raw)
        try:
            if path.is_dir():
                seen.setdefault(path.resolve(), path)
        except OSError:
            continue
    return list(seen.values())


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
    sweep_fn(
        served_repos(),
        total_ceiling_secs=total_ceiling_secs,
        allow_extended_first=True,
        clock=clock,
    )
    return True
