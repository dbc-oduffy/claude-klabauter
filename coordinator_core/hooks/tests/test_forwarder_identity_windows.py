"""Item 38.3: the Windows forwarder-identity check must match against the
process's ARGV (the same seam the POSIX/`/proc/<pid>/cmdline` branch
matches against), never the executable's image path -- an interpreter's
image path (`.../python.exe`) never carries the trailing script argument,
so a check built on it can never match and is permanently blind.
"""

from __future__ import annotations

import psutil
import pytest

from coordinator_core.hooks import sessionstart_ensure_http_forwarder as mod


class _FakeProcess:
    def __init__(self, cmdline):
        self._cmdline = cmdline

    def cmdline(self):
        return self._cmdline


def test_windows_identity_matches_on_argv_not_image_path(monkeypatch):
    # The image path would be the interpreter itself (python.exe) -- the
    # regex must match against the script argument, not this path.
    monkeypatch.setattr(
        mod,
        "_windows_process_cmdline",
        # abs-path-ok: synthetic Windows argv fixture, not a resolved host path
        lambda pid: ["P:\\fixture-repo\\python.exe", "P:\\fixture-repo\\hooks\\http_hook_forwarder.py"],
    )
    assert mod._pid_is_a_forwarder_windows(4242) is True


def test_windows_identity_false_when_argv_is_unrelated(monkeypatch):
    monkeypatch.setattr(
        mod,
        "_windows_process_cmdline",
        # abs-path-ok: synthetic Windows argv fixture, not a resolved host path
        lambda pid: ["P:\\fixture-repo\\python.exe", "P:\\fixture-repo\\unrelated_script.py"],
    )
    assert mod._pid_is_a_forwarder_windows(4242) is False


def test_windows_identity_false_when_process_gone(monkeypatch):
    def _raise(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(mod, "_windows_process_cmdline", _raise)
    assert mod._pid_is_a_forwarder_windows(4242) is False


def test_windows_identity_none_on_unreadable_process(monkeypatch):
    def _raise(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(mod, "_windows_process_cmdline", _raise)
    assert mod._pid_is_a_forwarder_windows(4242) is None


def test_windows_identity_none_when_psutil_unavailable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert mod._pid_is_a_forwarder_windows(4242) is None


def test_windows_process_cmdline_reads_through_psutil(monkeypatch):
    monkeypatch.setattr(
        psutil, "Process", lambda pid: _FakeProcess(["python.exe", "http_hook_forwarder.py"])
    )
    assert mod._windows_process_cmdline(123) == ["python.exe", "http_hook_forwarder.py"]


def test_unreadable_argv_confirmed_by_port_ownership_and_health(monkeypatch):
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_windows", lambda pid: None)
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_posix", lambda pid: None)
    monkeypatch.setattr(mod, "_port_owner_pid", lambda port=mod._FIXED_PORT: 4242)
    monkeypatch.setattr(mod, "_port_answers_as_forwarder", lambda pid, port=0: True)
    assert mod._pid_is_a_forwarder(4242) is True


def test_unreadable_argv_port_owner_not_answering_as_forwarder_is_not_confirmed(monkeypatch):
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_windows", lambda pid: None)
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_posix", lambda pid: None)
    monkeypatch.setattr(mod, "_port_owner_pid", lambda port=mod._FIXED_PORT: 4242)
    monkeypatch.setattr(mod, "_port_answers_as_forwarder", lambda pid, port=0: False)
    assert mod._pid_is_a_forwarder(4242) is None


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body


class _FakeConn:
    resp = None

    def __init__(self, *a, **kw):
        pass

    def request(self, *a, **kw):
        pass

    def getresponse(self):
        return type(self).resp

    def close(self):
        pass


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, b'{"holder": "doe-http-hook-forwarder", "pid": 4242}', True),
        (200, b'{"holder": "doe-http-hook-forwarder"}', True),
        (200, b'{"holder": "doe-http-hook-forwarder", "pid": 9}', False),
        (200, b'{"holder": "nginx"}', False),
        (200, b"not json", False),
        (500, b'{"holder": "doe-http-hook-forwarder"}', False),
    ],
)
def test_port_answers_as_forwarder_reads_the_health_holder(monkeypatch, status, body, expected):
    import http.client

    _FakeConn.resp = _FakeResp(status, body)
    monkeypatch.setattr(http.client, "HTTPConnection", _FakeConn)
    assert mod._port_answers_as_forwarder(4242) is expected


def test_port_answers_as_forwarder_false_when_nothing_listens(monkeypatch):
    import http.client

    def _refuse(*a, **kw):
        raise ConnectionRefusedError()

    monkeypatch.setattr(http.client, "HTTPConnection", _refuse)
    assert mod._port_answers_as_forwarder(4242) is False


def test_unreadable_argv_with_other_port_owner_stays_unconfirmed(monkeypatch):
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_windows", lambda pid: None)
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_posix", lambda pid: None)
    monkeypatch.setattr(mod, "_port_owner_pid", lambda port=mod._FIXED_PORT: 7)
    assert mod._pid_is_a_forwarder(4242) is None


def test_port_owner_pid_reads_the_listening_pid(monkeypatch):
    from collections import namedtuple

    Addr = namedtuple("Addr", "ip port")
    Conn = namedtuple("Conn", "status laddr pid")
    monkeypatch.setattr(
        psutil,
        "net_connections",
        lambda kind="tcp": [
            Conn(psutil.CONN_ESTABLISHED, Addr("127.0.0.1", mod._FIXED_PORT), 5),
            Conn(psutil.CONN_LISTEN, Addr("127.0.0.1", 9999), 6),
            Conn(psutil.CONN_LISTEN, Addr("127.0.0.1", mod._FIXED_PORT), 4242),
        ],
    )
    assert mod._port_owner_pid() == 4242


def test_port_owner_pid_none_when_socket_table_unreadable(monkeypatch):
    def _deny(kind="tcp"):
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "net_connections", _deny)
    assert mod._port_owner_pid() is None


def test_pid_is_a_forwarder_dispatches_to_windows_branch(monkeypatch):
    monkeypatch.setattr(mod.os, "name", "nt")
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_windows", lambda pid: True)
    assert mod._pid_is_a_forwarder(1) is True
