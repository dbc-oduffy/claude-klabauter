
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from coordinator_core.warm import election, skew, supervisor

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX election arm")


class _BlockingHttpd:

    def __init__(self, addr, handler_cls) -> None:
        self.server_address = ("127.0.0.1", 54331)
        self.RequestHandlerClass = handler_cls
        self.booted = threading.Event()
        self.release = threading.Event()

    def serve_forever(self) -> None:
        self.booted.set()
        self.release.wait(timeout=10)

    def shutdown(self) -> None:
        self.release.set()


def _install_doubles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp: str) -> list:
    import http.server

    skew.write_engine_stamp(tmp_path, stamp)
    monkeypatch.setattr(supervisor, "_default_engine_clone", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_declare_execution_route", lambda: None)

    servers: list = []
    created = threading.Event()

    def _make_httpd(addr, handler_cls):
        httpd = _BlockingHttpd(addr, handler_cls)
        servers.append(httpd)
        created.set()
        return httpd

    monkeypatch.setattr(http.server, "ThreadingHTTPServer", _make_httpd)

    write_calls: list = []
    real_write_discovery = supervisor.write_discovery

    def _tracking_write_discovery(**kwargs):
        write_calls.append(kwargs)
        real_write_discovery(**kwargs)

    monkeypatch.setattr(supervisor, "write_discovery", _tracking_write_discovery)
    return servers, created, write_calls


def test_main_elects_and_publishes_a_discovery_record_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers, created, write_calls = _install_doubles(tmp_path, monkeypatch, "sha-posix-election")

    result: dict = {}

    def _run() -> None:
        try:
            result["rc"] = supervisor.main()
        except BaseException as exc:  # noqa: BLE001 -- the failure IS the finding
            result["exc"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    booted = created.wait(timeout=10) and servers[0].booted.wait(timeout=10)

    try:
        assert "exc" not in result, f"main() raised on POSIX instead of electing: {result.get('exc')!r}"
        assert booted, "main() never reached serve_forever on POSIX"
        assert len(write_calls) == 1, "a booted supervisor must publish exactly one discovery record"
        assert supervisor.read_discovery(tmp_path) is not None
    finally:
        for server in servers:
            server.shutdown()
        thread.join(timeout=10)


def test_second_main_loses_the_election_while_the_first_still_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers, created, write_calls = _install_doubles(
        tmp_path, monkeypatch, "sha-posix-election-second"
    )

    thread = threading.Thread(target=supervisor.main, daemon=True)
    thread.start()
    booted = created.wait(timeout=10) and servers[0].booted.wait(timeout=10)

    try:
        assert booted, "first main() never reached serve_forever"
        assert supervisor.main() == 0, "a lost election must exit 0, not deny or raise"
        assert len(write_calls) == 1, "the losing invocation must never write a discovery record"
    finally:
        for server in servers:
            server.shutdown()
        thread.join(timeout=10)

    handle = election.elect_exclusive_lock(supervisor.supervisor_lock_path(tmp_path))
    election.release_exclusive_lock(handle)
