"""C7 — the tree-keyed cut lock.

The N-racer test uses REAL PROCESSES, not threads: the guarantee this module
rests on is filesystem atomicity (``O_CREAT | O_EXCL``), and threads in one
interpreter do not exercise it.

Spec backlink: DoE-claude
``docs/plans/2026-08-18-enforce-day-branch-cut-tree-invariant.md`` chunk C7.

Spawn ratchet C2 disposition: TIER -- multi-process IS the property
(``TestRealProcessRace``); a single interpreter cannot exercise filesystem
mutex atomicity across real racing processes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.session import day_branch_cut_lock as lock
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

# The racer HOLDS after acquiring. A winner that exits immediately would be a
# CONFIRMED-DEAD holder, and the next racer would correctly take the lock
# over -- which is the crash-recovery path, not the race the guarantee is
# about. Every racer must be alive at once for this to test atomicity.
_RACER = textwrap.dedent(
    """
    import json, sys, time
    sys.path.insert(0, sys.argv[2])
    from coordinator_core.session import day_branch_cut_lock as lock
    v = lock.acquire(sys.argv[1], session_id=sys.argv[3])
    print(json.dumps({"acquired": v.acquired, "sid": sys.argv[3]}), flush=True)
    time.sleep(3)
    """
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _repo_root_of_this_package() -> str:
    # coordinator_core/session/tests/ -> repo root
    return str(Path(__file__).resolve().parents[3])


class TestKeying:
    def test_lock_lives_in_the_git_common_dir(self, repo):
        """Keyed on resolve_git_common_dir's output, NOT a hashed path string.

        A hashed path fails OPEN under the routes this repo actually sees
        (`X:\\repo` vs `X:/repo` vs a substituted drive vs UNC): two sessions
        take two different locks, both win, both cut.
        """
        assert lock.lock_path(repo) == repo / ".git" / "coordinator-day-branch-cut.json"

    def test_path_variants_of_one_tree_resolve_to_one_lock(self, repo):
        forward = str(repo).replace("\\", "/")
        backward = str(repo).replace("/", "\\")
        assert lock.lock_path(forward) == lock.lock_path(backward)


class TestAcquireRelease:
    def test_first_acquire_wins_second_loses(self, repo):
        # A LIVE holder pid: this process's own. A fabricated pid reads as
        # CONFIRMED-DEAD and is correctly taken over, which would test the
        # crash path instead of the mutex.
        live = os.getpid()
        first = lock.acquire(repo, session_id="a", pid=live)
        assert first.acquired
        second = lock.acquire(repo, session_id="b", pid=live + 1)
        assert not second.acquired
        assert second.holder_pid == live
        assert second.holder_sid == "a"

    def test_release_only_by_holder(self, repo):
        live = os.getpid()
        lock.acquire(repo, session_id="a", pid=live)
        assert lock.release(repo, pid=live + 1) is False
        assert lock.lock_path(repo).exists()
        assert lock.release(repo, pid=live) is True
        assert not lock.lock_path(repo).exists()

    def test_dead_holder_is_taken_over_immediately(self, repo):
        """PID-liveness, not age: a peer must not poll a corpse for the full
        grace window before the invariant can proceed."""
        payload = {
            "holder_pid": 999_999_999,  # never live
            "holder_sid": "crashed",
            "hold_until": time.time() + 10_000,  # age alone would NOT free it
        }
        lock.lock_path(repo).write_text(json.dumps(payload), encoding="utf-8")
        v = lock.acquire(repo, session_id="takeover", pid=2222)
        assert v.acquired

    def test_age_is_the_ceiling_when_pid_is_unprobeable(self, repo):
        payload = {"holder_pid": None, "holder_sid": "x", "hold_until": time.time() - 10}
        assert lock.record_is_stale(payload) is False
        payload["hold_until"] = time.time() - (lock._STALE_GRACE_SECONDS + 10)
        assert lock.record_is_stale(payload) is True

    def test_corrupt_record_is_taken_over_not_raised_on(self, repo):
        lock.lock_path(repo).write_text("{not json", encoding="utf-8")
        assert lock.read_record(repo) is None
        assert lock.acquire(repo, session_id="a", pid=os.getpid()).acquired

    def test_windows_handle_is_not_held_open(self, repo):
        """Open-then-close: a held handle turns a takeover's unlink into a
        sharing violation on Windows."""
        lock.acquire(repo, session_id="a", pid=os.getpid())
        lock.lock_path(repo).unlink()  # would raise PermissionError if held


class TestHolderAlive:
    """The promoted, shared holder-liveness CHECK: unknown is ``None`` and
    never a verdict; a non-``int`` pid is unknown; a probe that raises is
    unknown. ``warm.push_cadence`` imports this same function rather than
    keeping its own copy."""

    def test_live_pid_is_true(self):
        assert lock.holder_alive(os.getpid()) is True

    def test_confirmed_dead_pid_is_false(self):
        assert lock.holder_alive(999_999_999) is False

    def test_non_int_pid_is_unknown(self):
        assert lock.holder_alive("not-a-pid") is None
        assert lock.holder_alive(None) is None

    def test_probe_raising_is_unknown_not_a_verdict(self, monkeypatch):
        def _boom(pid):
            raise RuntimeError("probe blew up")

        monkeypatch.setattr(lock.session_core, "pid_alive", _boom)
        assert lock.holder_alive(os.getpid()) is None


class TestRealProcessRace:
    def test_exactly_one_of_n_processes_acquires(self, repo):
        """Real processes, not threads — filesystem atomicity is the guarantee."""
        n = 6
        root = _repo_root_of_this_package()

        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _RACER, str(repo), root, f"sid-{i}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, **no_console_creationflags(),
            )
            for i in range(n)
        ]
        try:
            results = []
            for proc in procs:
                line = proc.stdout.readline()
                assert line, proc.stderr.read()
                results.append(json.loads(line))
        finally:
            for proc in procs:
                proc.kill()
                proc.wait()

        winners = [r for r in results if r["acquired"]]
        assert len(winners) == 1, f"expected exactly one winner, got {results}"
        assert len(results) - len(winners) == n - 1
