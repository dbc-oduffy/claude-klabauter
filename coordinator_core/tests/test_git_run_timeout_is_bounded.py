"""A git spawn that times out must RETURN, on Windows as on POSIX.

Purpose: `run_git`'s bound used to be `subprocess.run(timeout=)`, which does
not bound the call on Windows. CPython's `run()` catches `TimeoutExpired`,
kills the child, and then — Windows-only, `Lib/subprocess.py:556` under
`if _mswindows:` — calls `process.communicate()` a SECOND time with NO
timeout, to drain the reader threads by joining them. If a reader never
reaches EOF the join never returns, and `kill()` does not close a pipe whose
write end a grandchild inherited. The bound became advisory on the platform
this repo calls first-class, on the PreToolUse(Bash) chain.

MEASURED, not reasoned: caught live 2026-08-31 with
`faulthandler.dump_traceback_later(60)`, main thread parked at
`subprocess.py:556` beneath `subagent_sandbox.engine ::
_resolve_git_root_uncached` (`timeout=2.0`). An external `timeout 600` could
not reap it either — SIGTERM cannot land while the main thread sits in
`Thread.join()`. Record:
`state/bug-backlog/2026-08-31-subprocess-run-s-timeout-does-not-bound-466bceff0ba5.yaml`.

WHY THE DOUBLE LOOKS LIKE THIS. The defect is a HANG, so a test that
reproduced it faithfully would hang the suite — the one thing this file must
never do. The double therefore models the hazard rather than staging it: its
`communicate()` raises `TimeoutExpired` on the bounded first call, and a
SECOND call raises `AssertionError` naming the defect. The second call is
where the real thing hangs; raising instead is what keeps this suite finite
while still failing loudly on the exact re-entry.

What a regression looks like from here, stated precisely rather than
dramatically: revert `run_git` to `subprocess.run` and these tests fail on
`timed_out`, not on the re-entry assertion — the double patches `Popen`, so a
reverted seam bypasses it entirely, runs real `git`, and returns success.
That is still a genuine red, and it is fast. The re-entry assertion covers the
other regression: a future edit that keeps `Popen` but re-adds a drain after
the kill.

Non-spawning by construction: no real `git` process is created, so this
neither costs the shared box a spawn nor lands on
`test_no_new_spawning_tests.py`'s ratchet.
"""

from __future__ import annotations

import subprocess

from coordinator_core.git import run as git_run


class _NeverDrainingProc:
    """A child whose reader threads never reach EOF — CPython's hazard, staged.

    `communicate(timeout=...)` raises on the first (bounded) call, exactly as
    the real one does. A SECOND call — the unbounded re-drain — blocks
    forever, which is the behaviour that makes the real thing hang.
    """

    returncode = -9

    def __init__(self) -> None:
        self.kill_calls = 0
        self.wait_calls = []
        self.communicate_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(cmd=["git"], timeout=timeout or 0)
        raise AssertionError(
            "run_git re-entered communicate() after the kill -- that is the "
            "unbounded Windows re-drain, and in a real process it does not "
            "raise, it hangs forever"
        )

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode


def _install(monkeypatch) -> _NeverDrainingProc:
    proc = _NeverDrainingProc()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    return proc


def test_a_timed_out_git_call_returns_instead_of_re_draining(monkeypatch) -> None:
    """The whole point. The call comes back; it does not re-enter the drain."""
    proc = _install(monkeypatch)

    result = git_run.run_git(["rev-parse", "--show-toplevel"], cwd=".")

    assert result.timed_out is True
    assert result.returncode == -1
    assert proc.communicate_calls == 1, "exactly one drain attempt, never a second"


def test_the_timed_out_child_is_killed_and_reaped_under_a_bound(monkeypatch) -> None:
    """Kill then a BOUNDED wait. `wait` only reaps the process — it does not
    join the reader threads — so an inherited handle cannot park us there,
    and the bound means even a kill that did not take releases the caller."""
    proc = _install(monkeypatch)

    git_run.run_git(["status", "--porcelain"], cwd=".")

    assert proc.kill_calls == 1
    assert proc.wait_calls, "the killed child must be reaped, not abandoned"
    assert all(t is not None for t in proc.wait_calls), "every reap is bounded"


def test_the_pipes_are_closed_on_the_timeout_path(monkeypatch) -> None:
    """The hazard the superseded comment correctly named: a wrapper that
    reimplements the timeout around a raw `Popen` is how a fed stdin leaks a
    pipe on the failure path. Answered by using `Popen` as a context manager,
    so `__exit__` runs on the timeout path too."""
    proc = _install(monkeypatch)

    git_run.run_git(["rev-parse", "HEAD"], cwd=".")

    assert proc.closed is True


def test_a_timed_out_call_yields_no_bytes(monkeypatch) -> None:
    """A timed-out git call discards whatever the dead child wrote. Callers
    read `timed_out`, never partial output, and a partial capture from a
    killed process is not a value any branch should be tempted by."""
    _install(monkeypatch)

    result = git_run.run_git(["log", "--oneline"], cwd=".")

    assert result.stdout == ""
    assert result.stderr == ""


def test_a_reaping_wait_that_itself_times_out_still_returns(monkeypatch) -> None:
    """Belt and braces: if even the bounded reap expires, the caller is still
    released with a truthful timed-out result rather than an exception
    escaping the seam."""
    proc = _install(monkeypatch)

    def _wait(timeout=None):
        proc.wait_calls.append(timeout)
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=timeout or 0)

    monkeypatch.setattr(proc, "wait", _wait)

    result = git_run.run_git(["fsck"], cwd=".")

    assert result.timed_out is True
    assert result.returncode == -1
