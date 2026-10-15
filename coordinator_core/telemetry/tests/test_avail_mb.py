"""avail_mb() is the one memory reader: it routes each platform to its reader and never raises."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _host_probe as hp
from coordinator_core.telemetry import host_sampler


@pytest.mark.parametrize("plat,attr", [
    ("windows", "_windows_memory_mb"), ("darwin", "_darwin_memory_mb"), ("linux", "_posix_memory_mb")])
def test_avail_mb_routes_to_platform_reader(monkeypatch, plat, attr):
    monkeypatch.setattr(host_sampler, "_IS_WINDOWS", plat == "windows")
    monkeypatch.setattr(host_sampler.sys, "platform", {"windows": "win32"}.get(plat, plat))
    monkeypatch.setattr(host_sampler, attr, lambda: (10, 20, 30))
    assert host_sampler.avail_mb() == (20, 30)


def test_avail_mb_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("x")
    for attr in ("_windows_memory_mb", "_darwin_memory_mb", "_posix_memory_mb"):
        monkeypatch.setattr(host_sampler, attr, boom)
    assert host_sampler.avail_mb() == (None, None)


def test_host_probe_and_read_load_route_through_the_one_reader(monkeypatch):
    monkeypatch.setattr(host_sampler, "_memory_triple", lambda platform=None: (1, 2, 3))
    assert hp._memory_tuple() == (1, 2, 3)
    assert host_sampler.read_load()["mem_avail_mb"] == 2
    assert host_sampler._collect_row()["mem_avail_mb"] == 2
