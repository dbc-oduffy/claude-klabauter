"""stop_warm_pair against a server that is respawned by a peer on every sweep.

Falsifier: a new role=server pid appears on every process enumeration and exits unaided after one
poll. Each is a stale entry, not a survivor of a failed signal, yet the final sweep reports the
latest as a survivor ("respawned or still present after sweep") and the stop is not ok.

Run: python -m pytest coordinator_core/warm/tests/test_pair_stop_respawn_race.py -q
"""

from __future__ import annotations

import sys

import pytest

from coordinator_core.warm import pair_stop

SRV = ("coordinator_core", "warm", "server.py")


class _NoSuchProcess(Exception):
    pass


class _AccessDenied(Exception):
    pass


class _Proc:
    def __init__(self, pid, argv):
        self.pid = pid
        self.info = {"pid": pid, "name": "python.exe"}
        self._argv = argv

    def cmdline(self):
        return list(self._argv)


class _RespawningWorld:
    """Every process_iter call yields one fresh server pid that is already gone by the next call."""

    NoSuchProcess = _NoSuchProcess
    AccessDenied = _AccessDenied

    def __init__(self, script):
        self.script = script
        self.next_pid = 1000
        self.enumerations = 0

    def process_iter(self, attrs=None):
        self.enumerations += 1
        pid = self.next_pid
        self.next_pid += 1
        return [_Proc(pid, [sys.executable, self.script])]

    def Process(self, pid):
        raise _NoSuchProcess(pid)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(pair_stop, "_TERMINATE_GRACE_SECS", 0.02)
    monkeypatch.setattr(pair_stop, "_TERMINATE_POLL_INTERVAL_SECS", 0.005)
    monkeypatch.setattr(pair_stop, "_RESWEEP_DEADLINE_SECS", 0.05)


def test_server_respawned_on_every_poll_does_not_leave_a_survivor(tmp_path):
    root = tmp_path / "mirror"
    world = _RespawningWorld(str(root.joinpath(*SRV)))
    result = pair_stop.stop_warm_pair(root, psutil_module=world)
    assert world.enumerations > 1
    assert result.ok(), result.render_survivors()
