"""A restart boots one successor: concurrent spawn decisions after the server dies
elect exactly one booter, including when the claimer has already exited and when
the young record's endpoint probe fails.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from coordinator_core.warm import breadcrumb

N_CALLERS = 8

_CLAIM_AND_EXIT = """
import sys
sys.path.insert(0, {repo_root!r})
from pathlib import Path
from coordinator_core.warm import breadcrumb
sys.stdout.write("claimed\\n" if breadcrumb.try_claim_boot(Path({lock_path!r})) else "denied\\n")
"""


def _race(decide) -> list[bool]:
    results: list[bool] = [False] * N_CALLERS
    barrier = threading.Barrier(N_CALLERS)

    def _call(i: int) -> None:
        barrier.wait()
        results[i] = decide()

    threads = [threading.Thread(target=_call, args=(i,)) for i in range(N_CALLERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return results


def test_a_young_record_whose_probe_fails_elects_one_booter(tmp_path: Path, monkeypatch) -> None:
    lock_path = tmp_path / "warm.json.boot.lock"
    now = time.time()
    record = {
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda *a, **k: True)

    results = _race(
        lambda: breadcrumb.should_spawn_decision(
            record, lock_path=lock_path, now=now, extra_liveness=lambda rec: False
        )
    )

    assert sum(1 for r in results if r) == 1, results


def test_a_young_record_whose_probe_passes_never_spawns(tmp_path: Path, monkeypatch) -> None:
    lock_path = tmp_path / "warm.json.boot.lock"
    now = time.time()
    record = {
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda *a, **k: True)

    assert breadcrumb.should_spawn_decision(
        record, lock_path=lock_path, now=now, extra_liveness=lambda rec: True
    ) is False


def test_a_restart_with_no_record_elects_one_booter(tmp_path: Path) -> None:
    lock_path = tmp_path / "warm.json.boot.lock"

    results = _race(lambda: breadcrumb.should_spawn_decision(None, lock_path=lock_path))

    assert sum(1 for r in results if r) == 1, results


class _Pipe:
    def __init__(self, reply: bytes) -> None:
        self._reply = reply

    def write(self, data: bytes) -> None:
        pass

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        return self._reply

    def close(self) -> None:
        pass


_PING = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
_SKEW = (
    b'{"jsonrpc":"2.0","id":1,"error":{"code":%d,"message":"retired"}}\n' % -32002
)


def _client_with_skewing_first_pipe(monkeypatch, *, token_moves: bool):
    from coordinator_core.warm import client

    state = {"token": "oldtoken"}
    opened: list = []

    def _open(pipe):
        opened.append(pipe)
        if len(opened) == 1:
            if token_moves:
                state["token"] = "newtoken"
            return _Pipe(_SKEW)
        return _Pipe(b'{"jsonrpc":"2.0","id":1,"result":{"served":"successor"}}\n')

    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: state["token"])
    monkeypatch.setattr(client.election, "pipe_name", lambda token: "pipe-" + token)
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_open_pipe", _open)
    monkeypatch.setattr(breadcrumb, "should_spawn", lambda engine_root=None, **kw: True)
    return client, opened


def test_skew_from_a_retired_generation_resends_once_to_the_successor_pipe(monkeypatch) -> None:
    client, opened = _client_with_skewing_first_pipe(monkeypatch, token_moves=True)
    response = client.try_warm_dispatch(_PING)
    assert response is not None and response["result"] == {"served": "successor"}
    assert len(opened) == 2 and opened[0] != opened[1]


def test_skew_with_an_unmoved_token_goes_cold_without_a_resend(monkeypatch) -> None:
    client, opened = _client_with_skewing_first_pipe(monkeypatch, token_moves=False)
    assert client.try_warm_dispatch(_PING) is None
    assert len(opened) == 1


def test_windows_drain_refusal_thread_ends_when_the_client_never_writes(monkeypatch) -> None:
    import pytest

    if os.name != "nt":
        pytest.skip("named-pipe drain refusal is Windows-only")
    import _winapi

    from coordinator_core.warm import server

    monkeypatch.setattr(server, "_DRAIN_REFUSAL_READ_SECS", 0.3)
    bs = chr(92)
    name = f"{bs}{bs}.{bs}pipe{bs}drain-refusal-{os.getpid()}-{time.monotonic_ns()}"
    handle = _winapi.CreateNamedPipe(
        name,
        _winapi.PIPE_ACCESS_DUPLEX,
        _winapi.PIPE_WAIT,
        1,
        65536,
        65536,
        0,
        0,
    )
    client_end = _winapi.CreateFile(name, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, 0, _winapi.OPEN_EXISTING, 0, 0)
    try:
        before = {t.ident for t in threading.enumerate()}
        server._refuse_pipe_while_draining(handle, server_sha=None)
        refusal = next(t for t in threading.enumerate() if t.name == "warm-pipe-drain-refusal" and t.ident not in before)
        refusal.join(5.0)
        assert not refusal.is_alive()
    finally:
        _winapi.CloseHandle(client_end)
