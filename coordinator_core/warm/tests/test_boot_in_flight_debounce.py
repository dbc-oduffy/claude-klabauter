"""The boot-in-flight primitive `should_spawn_decision` falls through to when
NO record vouches for a boot at all -- the succession-window hole C4 closes.

Spec backlink: `state/dispatch-briefs/2026-09-01-a-guard-that-cannot-reach-
warmth-still-r/C4.md`. MEASURED premise (not a token N): a natural succession
on 2026-09-01T13:46Z ran FIVE concurrent `supervisor.py` processes, four of
them created inside 1.3 seconds of each other while the discovery record was
absent. `N_CALLERS` below is sized to that floor, never a token 2.

Tests `breadcrumb.try_claim_boot` / `breadcrumb.should_spawn_decision`
directly -- the one shared body every `should_spawn` wrapper in this package
(`breadcrumb`, `supervisor`, `front_door`) now delegates to -- rather than
each wrapper, since the primitive under test is identical no matter which
record shape calls it (Kira finding #4: unify, don't merely share).

Negative-spec: does NOT assert on `SPAWN_DEBOUNCE_SECS` timing or on a live
discovery/breadcrumb record's young-and-alive branch -- those are
`should_spawn_decision`'s OTHER branch, already covered by
`test_breadcrumb.py` / existing `should_spawn` suites. This file owns only
the boot-in-flight fallback: what happens when the record is absent.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from coordinator_core.warm import breadcrumb

# Measured floor (module docstring): four concurrent starters inside 1.3s
# during one real succession window. Kept >= 5 per C4's own instruction that
# the test's N must not be softened to a token 2.
N_CALLERS = 8


def test_n_concurrent_callers_with_absent_record_produce_exactly_one_spawn(tmp_path):
    """`N_CALLERS` threads race `try_claim_boot` on the same lock path with no
    discovery/breadcrumb record backing any of them (`record=None`) -- exactly
    one must be told to proceed; every other must be told a boot is already
    claimed.

    Threads, not processes: `try_claim_boot`'s lock is per OPEN FILE
    DESCRIPTION (flock/`msvcrt.locking`), not per-process -- two fds opened
    from the same process still conflict (see that function's own docstring),
    so a thread pool reproduces the real multi-process race without the
    overhead of spawning `N_CALLERS` real processes.
    """
    lock_path = tmp_path / "warm-http.json.boot.lock"

    results: list[bool] = [False] * N_CALLERS
    barrier = threading.Barrier(N_CALLERS)

    def _call(i: int) -> None:
        barrier.wait()
        results[i] = breadcrumb.should_spawn_decision(None, lock_path=lock_path)

    threads = [threading.Thread(target=_call, args=(i,)) for i in range(N_CALLERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sum(1 for r in results if r) == 1, (
        f"expected exactly one spawn among {N_CALLERS} concurrent callers with "
        f"no vouching record, got {results}"
    )


def test_try_claim_boot_absent_record_matches_should_spawn_decision(tmp_path):
    """Sanity: `should_spawn_decision(None, ...)` is exactly `try_claim_boot`
    on that lock path -- the fallback this chunk added, not a parallel check.

    Uses pytest's own `tmp_path` (lazily swept, never rmtree'd inline) rather
    than `tempfile.TemporaryDirectory`, because `try_claim_boot`'s claimed fd
    is DELIBERATELY leaked for the life of this process (see its own
    docstring) -- an inline `TemporaryDirectory.__exit__` rmtree would hit a
    file still held open on Windows.
    """
    lock_path_a = tmp_path / "a" / "record.json.boot.lock"
    lock_path_b = tmp_path / "b" / "record.json.boot.lock"

    claimed = breadcrumb.try_claim_boot(lock_path_a)
    decided = breadcrumb.should_spawn_decision(None, lock_path=lock_path_b)
    assert claimed is True
    assert decided is True


_HOLDER_SCRIPT = """
import sys
sys.path.insert(0, {repo_root!r})
from pathlib import Path
from coordinator_core.warm import breadcrumb

lock_path = Path({lock_path!r})
claimed = breadcrumb.try_claim_boot(lock_path)
sys.stdout.write("claimed\\n" if claimed else "denied\\n")
sys.stdout.flush()
import time
time.sleep(60)
"""


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_holder_killed_mid_boot_releases_and_the_next_caller_spawns(tmp_path):
    """A process that claims the boot-in-flight lock and is then killed (not
    given the chance to run any cleanup) must release it -- WAIT_ABANDONED /
    crashed-holder-releases semantics, never a TTL. The next caller must see
    `should_spawn_decision(None, ...)` return True immediately after the kill,
    with no wait for any timeout to elapse.

    This is the load-bearing property C4's chunk body names: a debounce that
    deadlocks on a dead starter converts a bounded burst into a box-wide
    outage until a human intervenes. `try_claim_boot` is built on `flock` /
    `msvcrt.locking`, whose crashed-holder-releases guarantee is already
    proven cross-process by `coordinator_core.locked_write`'s own
    `TestCrashSafety` / `TestMachineRendezvousCrashRelease` -- this test
    exercises that same guarantee through THIS primitive specifically.
    """
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    lock_path = tmp_path / "warm.json.boot.lock"

    script = _HOLDER_SCRIPT.format(repo_root=repo_root, lock_path=str(lock_path))
    script_path = tmp_path / "_holder.py"
    script_path.write_text(script, encoding="utf-8")

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    proc = subprocess.Popen(
        [sys.executable, str(script_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,
    )
    try:
        line = proc.stdout.readline().strip()
        assert line == "claimed", f"holder subprocess did not claim the lock: {line!r}"

        # While the holder is alive, a fresh caller must be denied.
        assert breadcrumb.should_spawn_decision(None, lock_path=lock_path) is False

        # Kill it WITHOUT letting it clean up -- proves release-on-death, not
        # release-on-orderly-exit.
        proc.kill()
        proc.wait(timeout=10)

        deadline = time.monotonic() + 5.0
        released = False
        last = None
        while time.monotonic() < deadline:
            last = breadcrumb.should_spawn_decision(None, lock_path=lock_path)
            if last is True:
                released = True
                break
            time.sleep(0.05)

        assert released, (
            f"the next caller was not told to spawn within 5s of the holder's "
            f"death (last decision: {last}) -- a TTL'd or non-releasing "
            f"primitive here converts a bounded burst into a box-wide outage"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_posix_primitive_is_flock_release_on_process_death():
    """Multi-OS is the live risk (C4's chunk body): assert the POSIX arm of
    `try_claim_boot` is actually `fcntl.flock`, not a Windows-only mutex ported
    thinly. `flock`'s documented semantics (held locks release when the last
    descriptor referring to them closes, including on process death) are what
    the two tests above rely on for the crashed-holder-releases guarantee --
    this test pins that the POSIX branch exists and uses that call, so a
    future edit cannot silently drop the POSIX arm and leave only a
    Windows-only debounce (break-class per this chunk's own instruction).

    RUNS ON EVERY PLATFORM, deliberately. It reads source and calls nothing, so
    it needs no POSIX host -- and this box is the only one the suite runs on
    (C10: POSIX *verification* is `wont_do` here). Skipping it on Windows would
    retire the sole guard against the POSIX arm being deleted, on the only
    machine positioned to notice, which is the `unmet-because-skipped` reading
    this plan's multi-os gated criterion requires stay distinguishable from
    `unmet-because-untestable`. What remains genuinely unverified on this host is
    flock's RUNTIME release-on-death behaviour, which the two behavioural tests
    above cover on Windows only."""
    import inspect

    src = inspect.getsource(breadcrumb.try_claim_boot)
    assert "fcntl" in src and "flock" in src, (
        "try_claim_boot must use fcntl.flock on POSIX -- a Windows-only "
        "primitive here is break-class (C4 chunk body)"
    )


def test_windows_primitive_is_msvcrt_locking():
    """Companion pin to the POSIX assertion above, run on every platform since
    it only inspects source: the Windows branch must exist and use
    `msvcrt.locking`, not a bespoke `CreateMutexW` reimplementation."""
    import inspect

    src = inspect.getsource(breadcrumb.try_claim_boot)
    assert "msvcrt" in src and "locking" in src
