"""A door whose sidecar-landed engine root differs from the server's goes cold,
and the endpoint is keyed on that root (no settings root, no env root)."""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, client, election

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX stub")

_MSG = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch):
    base = Path(tempfile.mkdtemp(prefix="wsc-", dir="/tmp"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(base / "settings"))
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(base))
    monkeypatch.delenv(client.CONFIGURED_ROOT_ENV, raising=False)
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


def _land_sidecar(value: str) -> None:
    import os

    d = Path(os.environ["COORDINATOR_SETTINGS_HOME"]) / "bin"
    d.mkdir(parents=True)
    (d / "door.engine-root.txt").write_text(value + "\n")


@posix_only
def test_foreign_sidecar_root_is_foreign_and_goes_cold(
    stub: list, tmp_path: Path
) -> None:
    _land_sidecar(str(tmp_path))
    assert client._configured_root_is_foreign() is True
    assert client.try_warm_dispatch(_MSG) is None
    assert stub == []


@posix_only
def test_matching_sidecar_root_still_reaches_the_server(stub: list) -> None:
    _land_sidecar(str(client._engine_clone_root()))
    assert client._configured_root_is_foreign() is False
    client.try_warm_dispatch(_MSG)
    assert len(stub) == 1


@posix_only
def test_endpoint_is_keyed_on_the_sidecar_root(stub: list, tmp_path: Path) -> None:
    baseline = client._endpoint_name("faketoken")
    _land_sidecar(str(tmp_path))
    keyed = client._endpoint_name("faketoken")
    assert keyed != baseline
    assert keyed == str(election.socket_path("faketoken", engine_clone=tmp_path))


@pytest.fixture
def plain_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "settings"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setattr(client, "_scratch_sidecar_warned", False)
    monkeypatch.setattr(client, "_scratch_sidecar_checked", set())
    return home


def _land_in(home: Path, value: str) -> None:
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "door.engine-root.txt").write_text(value + "\n")


def _scratch_tree(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    arm = tmp_path / "scratch" / "arm"
    arm.mkdir(parents=True)
    return arm


def test_scratch_sidecar_warns_once_and_still_returns_the_root(
    plain_settings: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    arm = _scratch_tree(tmp_path)
    _land_in(plain_settings, str(arm))
    assert client._door_sidecar_root() == str(arm)
    assert client._door_sidecar_root() == str(arm)
    err = capsys.readouterr().err
    assert err.count("names a scratch tree") == 1


def test_non_scratch_sidecar_does_not_warn(
    plain_settings: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _land_in(plain_settings, str(clone))
    assert client._door_sidecar_root() == str(clone)
    assert capsys.readouterr().err == ""
