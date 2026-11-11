"""Pins append_reflog's deadline: a hung reflog write never holds the caller past the bound."""
import threading
import time

import pytest

from coordinator_core.git import git_objects

COMMITTER = "c <c@x> 1 +0000"


def _hang_open_for(monkeypatch, release):
    real_open = open

    def _hung_open(path, *a, **k):
        if str(path).endswith("main"):
            release.wait(10)
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _hung_open)


def test_deadline_constant_is_a_sub_second_ish_bound():
    assert 0 < git_objects.REFLOG_APPEND_DEADLINE_SECS <= 2.0


@pytest.mark.deliberate_wall_clock(reason="bounds how long a hung reflog write blocks the caller, a wait CPU time cannot see")
def test_hung_write_returns_false_within_the_deadline_not_the_hang(tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(git_objects, "REFLOG_APPEND_DEADLINE_SECS", 0.2)
    _hang_open_for(monkeypatch, release)
    try:
        started = time.monotonic()
        result = git_objects.append_reflog(tmp_path, "refs/heads/main", None, "b" * 40, COMMITTER, "m")
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert result is False
    assert elapsed < 3.0


def test_abandoned_write_still_lands_after_the_hang_clears(tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(git_objects, "REFLOG_APPEND_DEADLINE_SECS", 0.1)
    _hang_open_for(monkeypatch, release)
    try:
        assert git_objects.append_reflog(tmp_path, "refs/heads/main", None, "b" * 40, COMMITTER, "late") is False
    finally:
        release.set()
    log = tmp_path / "logs" / "refs/heads/main"
    for _ in range(100):
        if log.exists() and log.read_bytes().endswith(b"\tlate\n"):
            break
        time.sleep(0.05)
    assert log.read_bytes().endswith(b"\tlate\n")


def test_write_inside_the_deadline_returns_true_and_appends(tmp_path):
    assert git_objects.append_reflog(tmp_path, "refs/heads/main", "a" * 40, "b" * 40, COMMITTER, "m") is True
    assert (tmp_path / "logs" / "refs/heads/main").read_bytes().endswith(b"\tm\n")


def test_os_error_inside_the_deadline_still_raises(tmp_path):
    (tmp_path / "logs").write_bytes(b"not a directory")
    with pytest.raises(OSError):
        git_objects.append_reflog(tmp_path, "refs/heads/main", None, "b" * 40, COMMITTER, "m")


def test_reflog_disabled_writes_nothing_and_returns_true(tmp_path):
    (tmp_path / "config").write_text("[core]\n\tlogallrefupdates = false\n", encoding="utf-8")
    assert git_objects.append_reflog(tmp_path, "refs/heads/main", None, "b" * 40, COMMITTER, "m") is True
    assert not (tmp_path / "logs").exists()
