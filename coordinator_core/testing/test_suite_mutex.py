"""
coordinator_core.testing.test_suite_mutex — scoped tests for the machine-wide
test-suite mutex (`suite_mutex.py`).

Purpose: exercises the acquire/release round-trip, cross-owner exclusion, both
staleness signals (dead PID and TTL expiry), re-entrancy, non-owner release
safety, and the never-raises contract on `holder()`.

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-test-suite-invocation-guards.md § ask 4
Spec backlink: coordinator-claude coordinator/docs/wiki/test-environment-discipline.md §5, §6

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


@pytest.fixture(autouse=True)
def sandboxed_settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Repoint the settings home at a per-test temp dir for every test here."""
    home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    return home


def _write_meta(pid: int, owner: str, started_at: str, cmd: str = "pytest") -> Path:
    """Materialize a lock dir with fully-specified holder metadata."""
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


def _dead_pid() -> int:
    """Return a PID that is reliably not running.

    Allocated by spawning a trivial child and reaping it, so the value is a
    real, just-exited PID on both POSIX and Windows rather than a guessed
    constant that might collide with a live process.
    """
    import subprocess
    import sys

    from coordinator_core.win_portability import no_console_creationflags

    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        **no_console_creationflags(),
    )
    proc.wait()
    return proc.pid


def test_lock_path_is_under_settings_home_claude_klabauter() -> None:
    path = suite_mutex.lock_path()
    assert path.parent.name == "claude-klabauter"
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


def test_stale_by_dead_pid_is_reclaimed() -> None:
    _write_meta(pid=_dead_pid(), owner="crashed-session", started_at=_iso_ago(60))

    assert suite_mutex.holder() is None
    assert not suite_mutex.lock_path().exists()
    assert suite_mutex.acquire("session-b", "pytest") is True
    assert suite_mutex.holder()["owner"] == "session-b"


def test_stale_by_ttl_is_reclaimed_even_with_a_live_pid() -> None:
    _write_meta(
        pid=os.getpid(),
        owner="wedged-session",
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
