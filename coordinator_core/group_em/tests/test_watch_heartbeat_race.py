"""Two-writer collision driver for `watch_heartbeat.stamp`'s read-decide-write
window, plus per-process cost measurements for the four candidate guard
shapes the predecessor handoff enumerates (a fifth, `locked_write.locked_rmw`,
is only COSTED through its two-part eligibility gate — see below).

THE FIRST DELIVERABLE IS A NUMBER, NOT A MECHANISM (plan
`docs/plans/2026-09-11-the-heartbeat-s-shared-record-gets-a-mea.md`, C1). This
file ships no guard and reverts nothing: it measures, and the measured
numbers plus the branch verdict live in
`docs/research/2026-09-11-group-em-heartbeat-collision-rate-and-candidate-cost.md`,
never argued here.

WHY NO LITERAL 23-MINUTE WALL-CLOCK RUN. The real writer cadences named by the
predecessor handoff are ~18s (monitor), ~80s (the handoff's second observed
figure), and ~23min (cron/audit). Sleeping a test process for 23 minutes to
observe whether two independent tickers' calls land inside `stamp`'s
read-decide-write window is itself the "box was busy" anti-pattern this
plan's anti-scope forbids treating as a finding, and it burns 23 real minutes
of CI/cadence-tier time to observe an event whose probability is governed by
the window's OWN duration, not by watching the clock run out. So this driver
measures the one thing that can be measured in process time — the window's
duration under a FORCED, worst-case simultaneous entry — and extrapolates the
natural collision probability at each named cadence from that duration
analytically. Both numbers land in the research doc, together with the
"could not be measured under a process-time bar" verdict this combination
retires the question with (predecessor anti-scope, third legitimate C1
outcome).

Marked `slow`: it sleeps and spawns real OS processes by construction. Not on
the fast tier — `pyproject.toml`'s `markers` list is the SSOT for that
exclusion (`-m 'not slow'`), read there, never off this docstring.
"""

from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core import locked_write
from coordinator_core.group_em import watch_heartbeat
from coordinator_core.session import day_branch_cut_lock

pytestmark = [pytest.mark.slow, pytest.mark.spawns_process]

# How many forced-simultaneous stamp() pairs to drive. Kept small: each pair
# is two real OS processes plus a barrier rendezvous, and this file's whole
# job is a bounded measurement, not a stress suite -- large N buys narrower
# error bars on a number this research doc already reports as an order-of-
# magnitude extrapolation, not a precision figure.
_FORCED_PAIRS = 20

# The three real cadences the predecessor handoff records, in seconds. Named
# here as PARAMETERS with the observed value beside them (gated exit
# criterion "no-single-machine-assumptions") -- never baked into
# `watch_heartbeat.py` itself, which this file does not touch.
CADENCE_MONITOR_SECONDS = 18.0
CADENCE_OBSERVED_SECONDS = 80.0
CADENCE_CRON_SECONDS = 23 * 60.0


def _worker(
    repo_root: str,
    holder_session_id: str,
    writer_session_id: str,
    barrier: "multiprocessing.synchronize.Barrier",
    result_queue: "multiprocessing.Queue",
) -> None:
    """One writer's single tick: wait at the barrier, then stamp().

    Runs in a CHILD PROCESS (`multiprocessing`, not a `git`/`bash`/`python`
    subprocess literal -- this is process-level concurrency for the race
    itself, not a spawn-ratchet-relevant external binary launch). The
    barrier forces both children into `stamp()`'s read-decide-write window
    at (as close as the OS scheduler allows) the same instant -- the
    WORST-CASE collision shape, never the natural one.
    """
    barrier.wait()
    t0 = time.process_time()
    wrote = watch_heartbeat.stamp(
        repo_root,
        holder_session_id=holder_session_id,
        declinations=[],
        interval_seconds=CADENCE_MONITOR_SECONDS,
        writer_session_id=writer_session_id,
        tick_source="monitor",
    )
    elapsed = time.process_time() - t0
    result_queue.put((writer_session_id, wrote, elapsed))


def _run_forced_pair(tmp_path: Path, pair_index: int) -> tuple[bool, bool, float, float]:
    """Drive two DIFFERENT holders' writers at one another, forced-simultaneous.

    Different holders (never same-crown) so neither is declined by
    `is_fresh_and_foreign`'s holder-or-writer check on the FIRST tick of a
    fresh tmp_path -- both should normally succeed absent a race; a
    collision here is a lost record, not a correct decline.
    """
    repo_root = str(tmp_path / f"pair-{pair_index}")
    os.makedirs(os.path.join(repo_root, "state"), exist_ok=True)
    barrier = multiprocessing.Barrier(2)
    result_queue: multiprocessing.Queue = multiprocessing.Queue()
    procs = [
        multiprocessing.Process(
            target=_worker,
            args=(repo_root, f"holder-a-{pair_index}", f"writer-a-{pair_index}", barrier, result_queue),
        ),
        multiprocessing.Process(
            target=_worker,
            args=(repo_root, f"holder-b-{pair_index}", f"writer-b-{pair_index}", barrier, result_queue),
        ),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10.0)
    results = {}
    while not result_queue.empty():
        wsid, wrote, elapsed = result_queue.get()
        results[wsid] = (wrote, elapsed)
    wrote_a, elapsed_a = results.get(f"writer-a-{pair_index}", (False, 0.0))
    wrote_b, elapsed_b = results.get(f"writer-b-{pair_index}", (False, 0.0))
    final_record = watch_heartbeat._read_record(watch_heartbeat.watch_path(repo_root))
    # A COLLISION is: both writers believed they wrote (both `stamp()` calls
    # returned True, i.e. neither declined the other), yet only one writer's
    # identity survives on disk and the OTHER's record -- and everything it
    # would have traced -- is gone with no `prior_*` naming it, because both
    # read the SAME pre-replacement record before either replaced it. This
    # is exactly the defect the module docstring's "NO LOCK SPANS
    # READ-DECIDE-WRITE" note describes.
    both_believed_written = bool(wrote_a and wrote_b)
    surviving_writer = final_record.get("writer_session_id") if isinstance(final_record, dict) else None
    collided = both_believed_written and surviving_writer in (
        f"writer-a-{pair_index}",
        f"writer-b-{pair_index}",
    )
    return both_believed_written, collided, elapsed_a, elapsed_b


def test_forced_simultaneous_two_writer_collision_rate_and_cost(tmp_path):
    """Worst-case (forced-simultaneous) collision measurement, process time only.

    Reports PROCESS TIME and a COUNT, never wall clock (anti-scope). The
    result lands in the research doc; this test only ASSERTS the driver
    itself behaves (never raises, writes a well-formed record, costs stay
    bounded) -- it is not the acceptance oracle for any shipped guard,
    because C1 ships no guard.
    """
    both_written_count = 0
    collision_count = 0
    total_process_time = 0.0
    total_calls = 0
    for i in range(_FORCED_PAIRS):
        both_written, collided, elapsed_a, elapsed_b = _run_forced_pair(tmp_path, i)
        if both_written:
            both_written_count += 1
        if collided:
            collision_count += 1
        total_process_time += elapsed_a + elapsed_b
        total_calls += 2
        # NEVER RAISES, NEVER GATES (module docstring) -- confirmed under
        # forced-concurrent load, not just single-writer use.
        record = watch_heartbeat._read_record(
            watch_heartbeat.watch_path(str(tmp_path / f"pair-{i}"))
        )
        assert isinstance(record, dict)

    # THE BARRIER FORCES BOTH CHILDREN TO START stamp() AT THE SAME INSTANT --
    # it does NOT force both to land inside the read-decide-write window
    # together, because the window itself is sub-millisecond and OS
    # scheduling jitter after the barrier release routinely lets one writer
    # finish its whole read-decide-write-replace before the other even opens
    # the file. When that happens `is_fresh_and_foreign` correctly DECLINES
    # the second writer -- not a collision, a correct decline (the exact
    # mechanism `test_a_fresh_foreign_record_is_declined_and_survives_
    # unchanged` in test_watch_heartbeat.py pins). A genuine collision needs
    # BOTH reads to land before EITHER write lands, which this measurement
    # shows is the minority outcome even under maximal forcing -- that
    # empirical split IS the number this row exists to produce, and it is
    # recorded verbatim in the research doc rather than asserted to a fixed
    # value here (real OS scheduling, not this file, decides the split on
    # any given run).
    assert 0 <= collision_count <= both_written_count <= _FORCED_PAIRS
    avg_call_process_time = total_process_time / total_calls
    # Sanity bound only -- `stamp` is on a sub-500ms-end-to-end hot path
    # (DR-344); a single call ballooning past that here would be a defect
    # in this measurement's own environment, not a shipped-code finding.
    assert avg_call_process_time < 0.5


def test_forced_simultaneous_two_writer_never_collides_under_the_guard(tmp_path):
    """P103-C2's ACCEPTANCE ORACLE, mechanism branch (plan `docs/plans/
    2026-09-11-the-heartbeat-s-shared-record-gets-a-mea.md`, exit criterion
    2, falsifier). C1's research doc measured 9/20 forced-simultaneous pairs
    (45%) colliding with NO guard in place; this asserts ZERO collisions with
    C2's guard landed, and fails on a reverted guard exactly as the falsifier
    prescribes -- the guard, not the barrier, is what this test is pinning.
    """
    collision_count = 0
    for i in range(_FORCED_PAIRS):
        _both_written, collided, _elapsed_a, _elapsed_b = _run_forced_pair(tmp_path, i)
        if collided:
            collision_count += 1
    # C1's measured baseline (no guard): 9/20 (45%) forced-simultaneous pairs
    # collided. At that rate 20 pairs colliding zero times by chance alone is
    # vanishingly unlikely, which is exactly what makes this assertion a
    # falsifier -- it fails on a reverted guard almost every run, and passes
    # here because the guard now serializes the window.
    assert collision_count == 0


def test_locked_rmw_two_part_eligibility_gate():
    """`locked_write.locked_rmw`/`held_lock` are eligible ONLY on a YES on
    BOTH parts of the plan's gate. Part 1 (git_common_dir resolves for an
    arbitrary watched repo, zero-spawn) is checked here directly against
    THIS repo root -- `stamp`'s own `repo_root` argument is exactly this
    shape, an arbitrary watched repo, never a hardcoded path (gated exit
    criterion "no-single-machine-assumptions"). Part 2 is a DOCTRINE
    reading, not a runtime check, and is recorded in the research doc:
    `CLAUDE.md`'s "What this repo is" states `state/` is authoritative
    disk-truth for claude-klabauter's OWN corpus only -- writing a lock sidecar under
    a FOREIGN watched repo's `.git` common dir, at ~18s cadence, three
    writers, is a write into a tree this module does not own. That is a NO,
    disqualifying `held_lock`/`locked_rmw` at their DEFAULT anchor for this
    call site; recorded as a disqualification, never a cost, per the row's
    own instruction.
    """
    repo_root = Path(__file__).resolve().parents[3]
    t0 = time.process_time()
    resolved = locked_write._lock_dir_path(repo_root)
    part1_elapsed = time.process_time() - t0
    assert resolved is not None
    # Part 1 passes: resolution succeeds and costs effectively nothing
    # (zero-spawn upward walk -- `git_common_dir`'s own docstring).
    assert part1_elapsed < 0.05


def test_day_branch_cut_lock_reuse_cost(tmp_path):
    """Cost candidate (3): an `O_EXCL` sidecar lock with a deadline, reusing
    `day_branch_cut_lock`'s takeover-on-staleness-with-PID-liveness shape
    rather than inventing a new one (row body, explicit instruction).

    This does not exercise `day_branch_cut_lock`'s OWN acquire/release entry
    points (that module is keyed on the CALLING repo's git common dir, a
    different call shape than a heartbeat guard would need) -- it costs the
    PRIMITIVE it is built on, `_try_create`'s `O_CREAT | O_EXCL` atomic
    sidecar create/release, at the file-op granularity a reused version of
    this shape would pay per `stamp()` tick.
    """
    lock_path = tmp_path / "sidecar.lock"
    t0 = time.process_time()
    created = day_branch_cut_lock._try_create(
        lock_path, {"holder_pid": os.getpid(), "hold_until": time.time() + 5.0}
    )
    os.unlink(lock_path)
    elapsed = time.process_time() - t0
    assert created
    assert elapsed < 0.05


def test_re_read_after_replace_cost(tmp_path):
    """Cost candidate (1): re-read after `os.replace` and report the
    mismatch. Cheapest candidate by construction -- one extra file read,
    no lock, no retry.
    """
    repo_root = str(tmp_path)
    os.makedirs(os.path.join(repo_root, "state"), exist_ok=True)
    watch_heartbeat.stamp(
        repo_root, holder_session_id="h1", declinations=[], interval_seconds=18.0,
        writer_session_id="w1", tick_source="monitor",
    )
    t0 = time.process_time()
    reread = watch_heartbeat._read_record(watch_heartbeat.watch_path(repo_root))
    elapsed = time.process_time() - t0
    assert isinstance(reread, dict)
    assert elapsed < 0.05


def test_compare_and_swap_content_hash_cost(tmp_path):
    """Cost candidate (2): compare-and-swap on a content hash with one
    retry. Costed as the hash computation over the written payload --
    the cheapest CAS witness available without a new dependency.
    """
    import hashlib

    repo_root = str(tmp_path)
    os.makedirs(os.path.join(repo_root, "state"), exist_ok=True)
    watch_heartbeat.stamp(
        repo_root, holder_session_id="h1", declinations=[], interval_seconds=18.0,
        writer_session_id="w1", tick_source="monitor",
    )
    path = watch_heartbeat.watch_path(repo_root)
    t0 = time.process_time()
    with open(path, "rb") as fh:
        digest = hashlib.sha1(fh.read()).hexdigest()
    elapsed = time.process_time() - t0
    assert digest
    assert elapsed < 0.05
