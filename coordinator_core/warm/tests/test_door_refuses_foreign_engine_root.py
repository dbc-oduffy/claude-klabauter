"""A client configured for engine root A never sends a request to a server of root B.

Drives the default `try_warm_dispatch` path against a real unix-socket stub
bound at the import clone's endpoint; the stub records any request line.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, client, election

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX stub")

_MSG = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch):
    base = Path(tempfile.mkdtemp(prefix="wrb-", dir="/tmp"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(base / "settings"))
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(base))
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(breadcrumb, "should_spawn", lambda engine_root=None, **kw: False)
    path = election.socket_path("faketoken")
    path.parent.mkdir(parents=True, exist_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(4)
    srv.settimeout(0.05)
    lines: list = []
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(0.3)
            try:
                data = conn.recv(65536)
                if data:
                    lines.append(data)
                    conn.sendall(b'{"jsonrpc":"2.0","id":1,"result":"foreign"}\n')
            except OSError:
                pass
            finally:
                conn.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    try:
        yield lines
    finally:
        stop.set()
        t.join(1)
        srv.close()
        for p in sorted(base.rglob("*"), reverse=True):
            try:
                p.unlink() if not p.is_dir() else p.rmdir()
            except OSError:
                pass
        try:
            base.rmdir()
        except OSError:
            pass


def test_foreign_configured_root_goes_cold_without_sending(
    stub: list, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(client.CONFIGURED_ROOT_ENV, str(tmp_path))
    assert client.try_warm_dispatch(_MSG) is None
    assert stub == []


def test_matching_configured_root_still_reaches_the_server(
    stub: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(client.CONFIGURED_ROOT_ENV, str(client._engine_clone_root()))
    client.try_warm_dispatch(_MSG)
    assert len(stub) == 1


def test_unset_configured_root_still_reaches_the_server(
    stub: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(client.CONFIGURED_ROOT_ENV, raising=False)
    client.try_warm_dispatch(_MSG)
    assert len(stub) == 1


def _write_settings_root(base: Path, value: str) -> None:
    d = base / "machine-local"
    d.mkdir(parents=True)
    (d / ".claude-klabauter-root").write_text(value + "\n")


def test_foreign_settings_root_goes_cold_without_sending(
    stub: list, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(client.CONFIGURED_ROOT_ENV, raising=False)
    _write_settings_root(Path(os.environ["COORDINATOR_SETTINGS_HOME"]), str(tmp_path))
    assert client.try_warm_dispatch(_MSG) is None
    assert stub == []


def test_matching_settings_root_still_reaches_the_server(
    stub: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(client.CONFIGURED_ROOT_ENV, raising=False)
    _write_settings_root(
        Path(os.environ["COORDINATOR_SETTINGS_HOME"]), str(client._engine_clone_root())
    )
    client.try_warm_dispatch(_MSG)
    assert len(stub) == 1
