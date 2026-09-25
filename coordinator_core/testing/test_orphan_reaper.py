"""Tests for coordinator_core.testing.orphan_reaper (R1).

`test_sessionfinish_reaps_a_detached_sleeper_under_basetemp` spawns a real
detached process, hence `spawns_process` (admitted by the spawn ratchet,
`coordinator_core/tests/test_no_new_spawning_tests.py`) and `cadence` (tiered
off the per-commit path — Rule 4). Every other test here exercises pure
helpers over synthetic data and spawns nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import types

import psutil
import pytest

from coordinator_core.testing import orphan_reaper

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Pure-helper cases
# ---------------------------------------------------------------------------


def test_is_worker_true_when_workerinput_present():
    config = types.SimpleNamespace(workerinput={"workerid": "gw0"})
    assert orphan_reaper.is_worker(config) is True


def test_is_worker_false_for_controller():
    config = types.SimpleNamespace()
    assert orphan_reaper.is_worker(config) is False


def test_get_basetemp_returns_none_and_never_creates_when_unset():
    calls = []

    class _Factory:
        _basetemp = None

        def getbasetemp(self):
            calls.append("called")
            return "/should/not/be/created"

    config = types.SimpleNamespace(_tmp_path_factory=_Factory())
    assert orphan_reaper.get_basetemp(config) is None
    assert calls == []


def test_get_basetemp_returns_basetemp_when_set(tmp_path):
    factory = types.SimpleNamespace(_basetemp=tmp_path)
    config = types.SimpleNamespace(_tmp_path_factory=factory)
    assert orphan_reaper.get_basetemp(config) == str(tmp_path)


def test_get_basetemp_none_when_no_factory():
    config = types.SimpleNamespace()
    assert orphan_reaper.get_basetemp(config) is None


@pytest.mark.parametrize(
    "path,basetemp,expected",
    [
        ("/tmp/pytest-2/foo", ("/", "tmp", "pytest-2"), True),
        ("/tmp/pytest-20/foo", ("/", "tmp", "pytest-2"), False),
        ("/tmp/pytest-2", ("/", "tmp", "pytest-2"), True),
        ("/tmp/other", ("/", "tmp", "pytest-2"), False),
        (None, ("/", "tmp", "pytest-2"), False),
        ("", ("/", "tmp", "pytest-2"), False),
    ],
)
def test_is_under_basetemp_path_component_boundary(path, basetemp, expected):
    assert orphan_reaper.is_under_basetemp(path, basetemp) is expected


def test_is_under_basetemp_empty_basetemp_parts_never_matches():
    assert orphan_reaper.is_under_basetemp("/anything", ()) is False


def test_collect_ancestor_pids_includes_parent():
    ancestors = orphan_reaper.collect_ancestor_pids(os.getpid())
    assert os.getppid() in ancestors


def test_collect_ancestor_pids_empty_when_psutil_missing(monkeypatch):
    monkeypatch.setattr(orphan_reaper, "psutil", None)
    assert orphan_reaper.collect_ancestor_pids(os.getpid()) == set()


def test_find_orphans_empty_when_psutil_missing(monkeypatch):
    monkeypatch.setattr(orphan_reaper, "psutil", None)
    assert orphan_reaper.find_orphans("/tmp/pytest-1", os.getpid(), 0.0, None) == []


def test_reap_processes_empty_when_psutil_missing(monkeypatch):
    monkeypatch.setattr(orphan_reaper, "psutil", None)
    assert orphan_reaper.reap_processes([]) == []


def test_find_orphans_excludes_self_and_ancestors(monkeypatch):
    """A process that matches on cwd but is the current process (or an ancestor)
    must never be returned as an orphan."""
    own_pid = os.getpid()
    own_proc = psutil.Process(own_pid)
    basetemp = own_proc.cwd()  # guarantees a cwd match for own_pid

    monkeypatch.setattr(orphan_reaper, "collect_ancestor_pids", lambda pid: {os.getppid()})

    orphans = orphan_reaper.find_orphans(
        basetemp, own_pid, session_start_time=0.0, own_username=own_proc.username()
    )
    orphan_pids = {p.pid for p in orphans}
    assert own_pid not in orphan_pids
    assert os.getppid() not in orphan_pids


def test_find_orphans_prefilters_by_create_time(monkeypatch):
    """A process older than session_start_time is excluded even if its cwd matches,
    because stage one never advances to the cwd/cmdline read for it."""
    own_pid = os.getpid()
    own_proc = psutil.Process(own_pid)
    basetemp = own_proc.cwd()
    far_future = time.time() + 10_000

    orphans = orphan_reaper.find_orphans(
        basetemp, own_pid, session_start_time=far_future, own_username=own_proc.username()
    )
    assert orphans == []


def test_pytest_sessionfinish_skips_workers_without_scanning(monkeypatch):
    calls = []
    monkeypatch.setattr(
        orphan_reaper,
        "get_basetemp",
        lambda config: calls.append("get_basetemp") or "/tmp/pytest-1",
    )
    config = types.SimpleNamespace(workerinput={"workerid": "gw0"})
    session = types.SimpleNamespace(config=config)
    orphan_reaper.pytest_sessionfinish(session, exitstatus=0)
    assert calls == []


def test_pytest_sessionfinish_never_raises_on_broken_config():
    """A broken/missing config must not propagate past this hook."""
    session = types.SimpleNamespace(config=types.SimpleNamespace())
    # Should not raise even though session.config carries none of the expected attrs.
    orphan_reaper.pytest_sessionfinish(session, exitstatus=0)


def test_pytest_sessionfinish_noop_when_basetemp_none():
    factory = types.SimpleNamespace(_basetemp=None)
    config = types.SimpleNamespace(_tmp_path_factory=factory)
    session = types.SimpleNamespace(config=config)
    # Must return cleanly with no basetemp to scan.
    orphan_reaper.pytest_sessionfinish(session, exitstatus=0)


# ---------------------------------------------------------------------------
# Real-process case
# ---------------------------------------------------------------------------


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_sessionfinish_reaps_a_detached_sleeper_under_basetemp(tmp_path, capsys):
    fake_basetemp = tmp_path / "pytest-fakebase"
    fake_basetemp.mkdir()

    popen_kwargs = {"cwd": str(fake_basetemp)}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        ) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        **popen_kwargs,
    )
    try:
        # Let it actually start and settle into its cwd before the scan.
        time.sleep(0.2)

        factory = types.SimpleNamespace(_basetemp=fake_basetemp)
        config = types.SimpleNamespace(_tmp_path_factory=factory)
        session = types.SimpleNamespace(config=config)

        orphan_reaper.pytest_sessionfinish(session, exitstatus=0)

        gone, _alive = psutil.wait_procs([psutil.Process(proc.pid)], timeout=3) if psutil.pid_exists(proc.pid) else ([], [])
        assert not psutil.pid_exists(proc.pid) or proc.poll() is not None

        captured = capsys.readouterr()
        assert f"pid={proc.pid}" in captured.err
        assert "scan_process_ms=" in captured.err
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)

    assert session.config is config  # exitstatus/session object left untouched
