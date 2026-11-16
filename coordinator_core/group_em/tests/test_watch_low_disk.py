"""LOW-DISK line in the group-em watch poll: threshold, capped top-level count, rate limit."""

from __future__ import annotations

import getpass
import os
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from coordinator_core.group_em import watch

pytestmark = pytest.mark.complete

_Usage = namedtuple("_Usage", "total used free")
_GB = 1024**3
NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def disk(tmp_path, monkeypatch):
    """A fake temp root holding `pytest-of-<user>` with three entries, and a settings home under tmp_path."""
    temp_root = tmp_path / "tmproot"
    pytest_root = temp_root / f"pytest-of-{getpass.getuser()}"
    pytest_root.mkdir(parents=True)
    for i in range(3):
        (pytest_root / f"pytest-{i}").mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(watch.tempfile, "gettempdir", lambda: str(temp_root))
    state = {"free": 50 * _GB}
    monkeypatch.setattr(
        watch.shutil, "disk_usage", lambda p: _Usage(500 * _GB, 0, state["free"])
    )
    return state, pytest_root


def _poll(emitted, now=NOW):
    with mock.patch.object(watch.read_pass, "fetch_live_agents", return_value=[]), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda agents, sid: agents
    ), mock.patch.object(watch.read_pass, "read_remote_snapshot", return_value={}), mock.patch.object(
        watch, "_goal_board_flags", return_value=None
    ):
        watch.poll_once("/repo/root", "caller-1", {}, now=now, emit=emitted.append)


def test_low_free_emits_one_line_then_rate_limit_holds(disk):
    emitted = []
    _poll(emitted)
    assert len(emitted) == 1
    assert emitted[0].startswith("LOW-DISK free=50GB")
    assert "pytest-entries=3" in emitted[0]
    _poll(emitted, NOW + timedelta(seconds=60))
    assert len(emitted) == 1


def test_line_returns_after_the_window(disk):
    emitted = []
    _poll(emitted)
    _poll(emitted, NOW + timedelta(seconds=watch._LOW_DISK_WINDOW_SECONDS + 1))
    assert len(emitted) == 2


def test_enough_free_is_silent(disk):
    state, _ = disk
    state["free"] = 100 * _GB
    emitted = []
    _poll(emitted)
    assert emitted == []


def test_entry_count_stops_at_cap_and_reports_lower_bound(disk, monkeypatch):
    _, pytest_root = disk
    monkeypatch.setattr(watch, "_LOW_DISK_ENTRY_CAP", 2)
    emitted = []
    watch._low_disk_tick(NOW.timestamp(), emitted.append)
    assert "pytest-entries=>=2" in emitted[0]


def test_count_never_descends_into_the_tree(disk, monkeypatch):
    _, pytest_root = disk
    (pytest_root / "pytest-0" / "deep").mkdir()
    scanned = []
    real_scandir = os.scandir

    def _spy(path):
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(watch.os, "scandir", _spy)
    emitted = []
    watch._low_disk_tick(NOW.timestamp(), emitted.append)
    assert len(emitted) == 1
    assert scanned == [str(pytest_root)]


def test_missing_pytest_root_still_alerts_with_zero_entries(disk):
    _, pytest_root = disk
    for child in pytest_root.iterdir():
        child.rmdir()
    pytest_root.rmdir()
    emitted = []
    watch._low_disk_tick(NOW.timestamp(), emitted.append)
    assert "pytest-entries=0" in emitted[0]


def _registry(monkeypatch, values):
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "merged_flat_registry", lambda: values)


def test_free_bytes_setting_raises_and_lowers_the_threshold(disk, monkeypatch):
    state, _ = disk
    state["free"] = 150 * _GB
    _registry(monkeypatch, {watch.LOW_DISK_FREE_BYTES_KEY: str(200 * _GB)})
    emitted = []
    watch._low_disk_tick(NOW.timestamp(), emitted.append)
    assert len(emitted) == 1 and emitted[0].startswith("LOW-DISK free=150GB")
    state["free"] = 50 * _GB
    _registry(monkeypatch, {watch.LOW_DISK_FREE_BYTES_KEY: str(10 * _GB)})
    emitted = []
    watch._low_disk_tick(NOW.timestamp() + 7200, emitted.append)
    assert emitted == []


def test_entry_cap_setting_bounds_the_count(disk, monkeypatch):
    _registry(monkeypatch, {watch.LOW_DISK_ENTRY_CAP_KEY: "2"})
    emitted = []
    watch._low_disk_tick(NOW.timestamp(), emitted.append)
    assert "pytest-entries=>=2" in emitted[0]


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5"])
def test_unusable_settings_fall_back_to_defaults(disk, monkeypatch, bad):
    _registry(
        monkeypatch,
        {watch.LOW_DISK_FREE_BYTES_KEY: bad, watch.LOW_DISK_ENTRY_CAP_KEY: bad},
    )
    emitted = []
    watch._low_disk_tick(NOW.timestamp(), emitted.append)
    assert len(emitted) == 1 and "pytest-entries=3" in emitted[0]


def test_disk_usage_oserror_yields_no_line(disk, monkeypatch):
    def _fail(p):
        raise OSError("gone")

    monkeypatch.setattr(watch.shutil, "disk_usage", _fail)
    emitted = []
    watch._low_disk_tick(NOW.timestamp(), emitted.append)
    assert emitted == []
