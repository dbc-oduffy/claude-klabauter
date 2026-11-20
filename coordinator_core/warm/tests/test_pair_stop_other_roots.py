"""`find_servers_under_other_roots` names warm servers outside the stop's own root."""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.warm import pair_stop


class _Psutil:
    NoSuchProcess = AccessDenied = RuntimeError

    def __init__(self, procs):
        self._procs = procs

    def process_iter(self, attrs):
        return iter(self._procs)


def _proc(pid, name, argv):
    return SimpleNamespace(info={"pid": pid, "name": name}, cmdline=lambda: argv)


def test_lists_servers_from_other_roots_only(tmp_path):
    mine, old = tmp_path / "new", tmp_path / "old"
    procs = [
        _proc(1, "python3", ["python3", str(mine / "coordinator_core" / "warm" / "server.py")]),
        _proc(2, "python3", ["python3", str(old / "coordinator_core" / "warm" / "server.py")]),
        _proc(3, "python3", ["python3", str(old / "coordinator_core" / "warm" / "supervisor.py")]),
        _proc(4, "bash", ["bash", str(old / "coordinator_core" / "warm" / "server.py")]),
    ]

    got = pair_stop.find_servers_under_other_roots(mine, psutil_module=_Psutil(procs))

    assert [(pid, pair_stop._norm(root)) for pid, root in got] == [(2, pair_stop._norm(old))]
