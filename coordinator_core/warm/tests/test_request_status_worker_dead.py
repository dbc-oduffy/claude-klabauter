"""`warm.request_status` answers a dispatch whose pool worker died with the terminal `unknowable/worker_dead`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.warm import dispatch_ack, server

pytestmark = [pytest.mark.cadence]

BOOT_NS = 1_000_000
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _FakeIO:
    def __init__(self, lines) -> None:
        self._lines = list(lines)
        self.written: list = []

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _VersionState:
    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


def _poll(key: str) -> dict:
    """The answer the resident server gives `warm.request_status` for `key`."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "warm.request_status", "params": {"key": key}, "_engine_token": "t"}
    io_obj = _FakeIO([(json.dumps(msg) + "\n").encode("utf-8")])

    def _never(*_a, **_k):  # pragma: no cover
        raise AssertionError("request_status must not reach dispatch")

    server._handle_connection(
        io_obj, version_state=_VersionState(), server_sha="x", close_listener=lambda: None,
        drain=lambda: None, in_flight=server.InFlightCounter(), dispatch=_never,
    )
    [line] = io_obj.written
    result = json.loads(line)["result"]
    return {k: v for k, v in result.items() if k not in ("engine_boot_ns", "engine_pid")}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dispatch_ack.AckStore:
    ack = dispatch_ack.AckStore(boot_ns=BOOT_NS, executing_bound=lambda method: 600.0)
    monkeypatch.setattr(server, "_ack_store", ack)
    return ack


def _admit(store: dispatch_ack.AckStore, n: int) -> str:
    key = f"1-{n}-{BOOT_NS + 100 + n}"
    assert store.admit(key, "ceremony.commit_v2")
    return key


def test_worker_locator_reads_the_claim_pid_and_ignores_the_accept_process(tmp_path: Path) -> None:
    claim = tmp_path / "claim"

    class _Proc:
        is_alive = staticmethod(lambda: True)

    class _Pool:
        _processes = {4242: _Proc}

    locate = server._worker_locator(claim, _Pool())
    assert locate() is None
    claim.write_text("4242", encoding="ascii")
    pid, probe = locate()
    assert (pid, probe()) == (4242, True)
    claim.write_text("999999", encoding="ascii")
    assert locate() == (999999, None)
    import os

    claim.write_text(str(os.getpid()), encoding="ascii")
    assert locate() is None


def test_an_unclaimed_record_with_a_locator_that_finds_nothing_stays_executing(store) -> None:
    key = _admit(store, 8)
    store.bind_worker(key, lambda: None)
    assert _poll(key) == {"state": dispatch_ack.STATE_EXECUTING}
