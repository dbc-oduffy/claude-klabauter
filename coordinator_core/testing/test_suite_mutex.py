"""
coordinator_core.testing.test_suite_mutex — scoped tests for the machine-wide
test-suite mutex (`suite_mutex.py`).

Purpose: exercises the acquire/release round-trip, cross-owner exclusion, both
staleness signals (dead PID and TTL expiry), re-entrancy, non-owner release
safety, and the never-raises contract on `holder()`.

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-test-suite-invocation-guards.md § ask 4
Spec backlink: DoE coordinator/docs/wiki/test-environment-discipline.md §5, §6

Negative-spec:
    - Does NOT touch the real machine-wide lock. Every test repoints
      `COORDINATOR_SETTINGS_HOME` at a `tmp_path` via an autouse fixture, so a
      test run can never contend with — or reclaim — a live suite run's lock.
    - Does NOT assert on wall-clock sleep durations from the backoff poll
      (flaky); the blocking path is asserted on its return value only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.testing import suite_mutex

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def sandboxed_settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    return home


def _write_meta(pid: int, owner: str, started_at: str, cmd: str = "pytest") -> Path:
    path = suite_mutex.lock_path()
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(
        json.dumps({"pid": pid, "owner": owner, "started_at": started_at, "cmd": cmd}),
        encoding="utf-8",
    )
    return path


def _iso_ago(seconds: float) -> str:
    """Return an ISO-8601 UTC stamp `seconds` in the past."""
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


_DEAD_PID = 2_000_000_011
_FAKE_STATE: dict = {}
_FAKE_CREATE: dict = {}


@pytest.fixture(autouse=True)
def fake_pid_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Liveness and create time come from fakes: `_DEAD_PID` reads dead, every
    other pid reads as the real probe says, and no create time is known unless
    a test sets one (the test process itself post-dates the back-dated locks)."""
    from coordinator_core.ask_spool import record

    real_state = record.pid_state
    _FAKE_STATE.clear()
    _FAKE_CREATE.clear()
    _FAKE_STATE[_DEAD_PID] = False
    monkeypatch.setattr(record, "pid_state", lambda pid: _FAKE_STATE[pid] if pid in _FAKE_STATE else real_state(pid))
    monkeypatch.setattr(record, "pid_create_time", lambda pid: _FAKE_CREATE.get(pid))


def _dead_pid() -> int:
    return _DEAD_PID


def _heartbeat_meta(pid: int, heartbeat_ago: float, started_ago: float = 60.0) -> Path:
    path = _write_meta(pid=pid, owner="hb-session", started_at=_iso_ago(started_ago))
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    meta["heartbeat_at"] = _iso_ago(heartbeat_ago)
    (path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


def test_lock_path_is_under_settings_home() -> None:
    path = suite_mutex.lock_path()
    assert path.parent == Path(os.environ["COORDINATOR_SETTINGS_HOME"])
    assert path.name == "test-suite-mutex.lock"
    assert os.environ["COORDINATOR_SETTINGS_HOME"] in str(path)


def test_holder_on_free_lock_returns_none() -> None:
    assert suite_mutex.holder() is None


def test_acquire_release_round_trip() -> None:
    assert suite_mutex.acquire("session-a", "python3 -m pytest") is True

    current = suite_mutex.holder()
    assert current is not None
    assert current["owner"] == "session-a"
    assert current["pid"] == os.getpid()
    assert current["cmd"] == "python3 -m pytest"
    assert current["started_at"].endswith("Z")

    suite_mutex.release("session-a")
    assert suite_mutex.holder() is None
    assert not suite_mutex.lock_path().exists()


def test_second_owner_is_blocked_while_first_holds() -> None:
    assert suite_mutex.acquire("session-a", "pytest") is True
    assert suite_mutex.acquire("session-b", "pytest") is False
    assert suite_mutex.holder()["owner"] == "session-a"


def test_second_owner_blocked_with_timeout_still_returns_false() -> None:
    assert suite_mutex.acquire("session-a", "pytest") is True
    assert suite_mutex.acquire("session-b", "pytest", timeout=0.3) is False
    assert suite_mutex.holder()["owner"] == "session-a"


def test_reentrant_same_owner_is_noop_success() -> None:
    assert suite_mutex.acquire("session-a", "pytest") is True
    assert suite_mutex.acquire("session-a", "pytest") is True
    assert suite_mutex.holder()["owner"] == "session-a"

    suite_mutex.release("session-a")
    assert suite_mutex.holder() is None


def test_same_owner_other_live_process_is_not_admitted() -> None:
    import subprocess
    import sys

    from coordinator_core.win_portability import no_console_creationflags

    sibling = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        **no_console_creationflags(),
    )
    try:
        assert suite_mutex.acquire("session-a", "pytest", pid=sibling.pid) is True
        assert suite_mutex.acquire("session-a", "pytest") is False
        assert suite_mutex.acquire("session-a", "pytest", timeout=0.3) is False
    finally:
        sibling.kill()
        sibling.wait()


def test_same_owner_holder_that_is_an_ancestor_is_admitted() -> None:
    import psutil

    parent = psutil.Process().parent()
    assert parent is not None
    assert suite_mutex.acquire("session-a", "pytest", pid=parent.pid) is True
    assert suite_mutex.acquire("session-a", "pytest") is True


def test_same_owner_ancestry_unreadable_keeps_owner_only_admission(monkeypatch) -> None:
    import psutil

    def _boom(*_a, **_k):
        raise psutil.Error("ancestry unreadable")

    monkeypatch.setattr(psutil, "Process", _boom)
    assert suite_mutex._holder_is_self_or_ancestor(os.getpid() + 1_000_003) is True


def test_stale_by_dead_pid_is_reclaimed() -> None:
    _write_meta(pid=_dead_pid(), owner="crashed-session", started_at=_iso_ago(60))

    assert suite_mutex.holder() is None
    assert not suite_mutex.lock_path().exists()
    assert suite_mutex.acquire("session-b", "pytest") is True
    assert suite_mutex.holder()["owner"] == "session-b"


def test_live_holder_without_heartbeat_is_never_reclaimed_on_age() -> None:
    assert not hasattr(suite_mutex, "LIVE_HOLDER_TTL_SECS")
    _write_meta(pid=os.getpid(), owner="raw-acquirer", started_at=_iso_ago(7 * 60 * 60))

    current = suite_mutex.holder()
    assert current is not None and current["owner"] == "raw-acquirer"


def test_live_pid_past_stale_ttl_with_fresh_heartbeat_is_kept() -> None:
    _heartbeat_meta(os.getpid(), heartbeat_ago=5, started_ago=suite_mutex.STALE_TTL_SECS + 60)

    current = suite_mutex.holder()
    assert current is not None, "a live holder past STALE_TTL was reclaimed mid-run"
    assert current["owner"] == "hb-session"


def test_live_pid_with_stale_heartbeat_is_reclaimed() -> None:
    """An orphaned child (its wrapper dead) still reads alive but stops heartbeating."""
    _heartbeat_meta(os.getpid(), heartbeat_ago=suite_mutex.HEARTBEAT_STALE_SECS + 60)

    assert suite_mutex.holder() is None
    assert not suite_mutex.lock_path().exists()
    assert suite_mutex.acquire("session-b", "pytest") is True


def test_pid_created_after_the_lock_was_taken_is_reclaimed() -> None:
    import time

    pid = 4_242_424
    _FAKE_STATE[pid] = True
    _FAKE_CREATE[pid] = time.time() - 10
    _write_meta(pid=pid, owner="recycled", started_at=_iso_ago(600))

    assert suite_mutex.holder() is None
    assert suite_mutex.acquire("session-b", "pytest") is True


def test_pid_created_before_the_lock_was_taken_is_kept() -> None:
    import time

    pid = 4_242_425
    _FAKE_STATE[pid] = True
    _FAKE_CREATE[pid] = time.time() - 1200
    _write_meta(pid=pid, owner="original", started_at=_iso_ago(600))

    current = suite_mutex.holder()
    assert current is not None and current["owner"] == "original"


def test_unreadable_create_time_does_not_reclaim_a_live_pid() -> None:
    pid = 4_242_426
    _FAKE_STATE[pid] = True
    _write_meta(pid=pid, owner="unknown-age", started_at=_iso_ago(600))

    assert suite_mutex.holder()["owner"] == "unknown-age"


def test_indeterminate_pid_with_stale_heartbeat_is_reclaimed() -> None:
    pid = 4_242_427
    _FAKE_STATE[pid] = None
    _heartbeat_meta(pid, heartbeat_ago=suite_mutex.HEARTBEAT_STALE_SECS + 60)

    assert suite_mutex.holder() is None


def _fake_kernel32(monkeypatch: pytest.MonkeyPatch, exit_code: int, created_filetime: int):
    import ctypes

    from coordinator_core.ask_spool import record

    class _Kernel32:
        def OpenProcess(self, access, inherit, pid):
            return 77

        def GetExitCodeProcess(self, handle, ref):
            ref._obj.value = exit_code
            return 1

        def GetProcessTimes(self, handle, created, exited, kernel, user):
            created._obj.value = created_filetime
            return 1

        def CloseHandle(self, handle):
            return 1

    monkeypatch.setattr(ctypes, "windll", type("W", (), {"kernel32": _Kernel32()})(), raising=False)
    monkeypatch.setattr(record, "pid_state", record.win_pid_state)
    monkeypatch.setattr(record, "pid_create_time", record.win_pid_create_time)


def test_windows_exit_code_not_still_active_is_reclaimed_by_default_holder(monkeypatch) -> None:
    _fake_kernel32(monkeypatch, exit_code=1, created_filetime=0)
    _write_meta(pid=41712, owner="exited-holder", started_at=_iso_ago(60))

    assert suite_mutex.holder() is None
    assert not suite_mutex.lock_path().exists()


def test_windows_still_active_with_old_create_time_is_kept(monkeypatch) -> None:
    from coordinator_core.ask_spool import record

    long_ago = (1_700_000_000 * 10_000_000) + record._FILETIME_UNIX_EPOCH
    _fake_kernel32(monkeypatch, exit_code=259, created_filetime=long_ago)
    _write_meta(pid=41712, owner="running-holder", started_at=_iso_ago(60))

    assert suite_mutex.holder()["owner"] == "running-holder"


def test_windows_still_active_with_create_time_after_started_at_is_reclaimed(monkeypatch) -> None:
    import time

    from coordinator_core.ask_spool import record

    just_now = int((time.time() - 5) * 10_000_000) + record._FILETIME_UNIX_EPOCH
    _fake_kernel32(monkeypatch, exit_code=259, created_filetime=just_now)
    _write_meta(pid=41712, owner="recycled-holder", started_at=_iso_ago(600))

    assert suite_mutex.holder() is None


def test_touch_heartbeat_stamps_only_the_owners_lock() -> None:
    assert suite_mutex.acquire("session-a", "pytest") is True
    assert "heartbeat_at" not in suite_mutex.holder()

    assert suite_mutex.touch_heartbeat("session-b") is False
    assert suite_mutex.touch_heartbeat("session-a") is True

    current = suite_mutex.holder()
    assert current["heartbeat_at"].endswith("Z")
    assert current["owner"] == "session-a" and current["cmd"] == "pytest"


def test_touch_heartbeat_on_a_freed_lock_does_not_recreate_it() -> None:
    assert suite_mutex.touch_heartbeat("session-a") is False
    assert not suite_mutex.lock_path().exists()


def test_held_refreshes_the_heartbeat_until_release(monkeypatch) -> None:
    import threading

    monkeypatch.setattr(suite_mutex, "HEARTBEAT_INTERVAL_SECS", 0.01)
    beats = threading.Semaphore(0)
    real_touch = suite_mutex.touch_heartbeat

    def counting_touch(owner, *, path=None):
        result = real_touch(owner, path=path)
        beats.release()
        return result

    monkeypatch.setattr(suite_mutex, "touch_heartbeat", counting_touch)
    with suite_mutex.held("session-a", "pytest") as acquired:
        assert acquired is True
        for _ in range(3):
            assert beats.acquire(timeout=5)
        assert "heartbeat_at" in suite_mutex.holder()
    assert suite_mutex.holder() is None
    assert not suite_mutex.lock_path().exists()


def test_dead_pid_past_stale_ttl_still_expires_at_the_short_ceiling() -> None:
    _write_meta(
        pid=_dead_pid(),
        owner="crashed-session",
        started_at=_iso_ago(suite_mutex.STALE_TTL_SECS + 60),
    )

    assert suite_mutex.holder() is None
    assert suite_mutex.acquire("session-b", "pytest") is True


def test_live_pid_within_ttl_is_not_reclaimed() -> None:
    _write_meta(pid=os.getpid(), owner="running-session", started_at=_iso_ago(60))

    current = suite_mutex.holder()
    assert current is not None
    assert current["owner"] == "running-session"


def test_release_by_non_owner_is_a_noop() -> None:
    assert suite_mutex.acquire("session-a", "pytest") is True

    suite_mutex.release("session-b")

    current = suite_mutex.holder()
    assert current is not None
    assert current["owner"] == "session-a"


def test_release_on_free_lock_is_a_noop() -> None:
    suite_mutex.release("session-a")
    assert suite_mutex.holder() is None


@pytest.mark.parametrize(
    "body",
    [
        "",
        "{",
        "not json at all",
        "[]",
        '{"pid": "not-an-int"}',
        '{"owner": 17}',
    ],
)
def test_holder_never_raises_on_corrupt_metadata(body: str) -> None:
    path = suite_mutex.lock_path()
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(body, encoding="utf-8")

    result = suite_mutex.holder()
    assert result is None or isinstance(result, dict)


def test_missing_metadata_inside_grace_window_reports_a_holder() -> None:
    suite_mutex.lock_path().mkdir(parents=True, exist_ok=True)

    current = suite_mutex.holder()
    assert current is not None
    assert current["owner"] == "<unknown>"
    assert current["pid"] == 0


def test_missing_metadata_past_grace_window_is_reclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    suite_mutex.lock_path().mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(suite_mutex, "_META_GRACE_SECS", -1.0)

    assert suite_mutex.holder() is None
    assert not suite_mutex.lock_path().exists()


def test_explicit_pid_is_recorded_instead_of_getpid() -> None:
    runner_pid = os.getpid()
    assert suite_mutex.acquire("session-a", "pytest", pid=runner_pid) is True
    assert suite_mutex.holder()["pid"] == runner_pid


def test_held_context_manager_releases_on_normal_exit() -> None:
    with suite_mutex.held("session-a", "pytest") as acquired:
        assert acquired is True
        assert suite_mutex.holder()["owner"] == "session-a"
    assert suite_mutex.holder() is None


def test_held_context_manager_releases_on_exception() -> None:
    with pytest.raises(RuntimeError):
        with suite_mutex.held("session-a", "pytest") as acquired:
            assert acquired is True
            raise RuntimeError("suite blew up")
    assert suite_mutex.holder() is None


def test_held_yields_false_and_does_not_release_foreign_lock() -> None:
    assert suite_mutex.acquire("session-a", "pytest") is True

    with suite_mutex.held("session-b", "pytest") as acquired:
        assert acquired is False

    current = suite_mutex.holder()
    assert current is not None
    assert current["owner"] == "session-a"


def test_mutex_owner_reads_the_harness_session_var_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # Claude Code sets CLAUDE_CODE_SESSION_ID; CLAUDE_SESSION_ID is unset in practice.
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "harness-sid")
    assert suite_mutex.mutex_owner("suite-mutex") == "harness-sid"
    monkeypatch.setenv("CLAUDE_SESSION_ID", "legacy-sid")
    assert suite_mutex.mutex_owner("suite-mutex") == "harness-sid"
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert suite_mutex.mutex_owner("suite-mutex") == "legacy-sid"
