"""Tests for `coordinator_core.warm.client` -- the client-side warm-pipe
preamble and its anti-storm fallback table.

Purpose: C15 of docs/plans/2026-08-16-one-engine-for-the-whole-box.md.
Exercises every row of the anti-storm table directly against
`try_warm_dispatch`, stubbing the transport (`_open_pipe`), the warmth
switch (`is_warm_enabled`), the pipe-name resolver (`election.pipe_name`),
and the spawn call (`spawn_detached`) rather than touching a real named
pipe or process -- this module's own contract is the fallback DECISION
table, not the named-pipe transport mechanics (`election`'s own tests) or
`spawn_detached`'s own never-raise contract (`detached_spawn`'s own
tests).

Runs on any platform: nothing here opens a real Windows named pipe: every
test drives `try_warm_dispatch` through a fake file-like object returned
by a monkeypatched `_open_pipe`.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C15
"""

from __future__ import annotations

import io
import json

import pytest

from coordinator_core.warm import client


@pytest.fixture(autouse=True)
def _warm_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module opts warmth on and pins the pipe name and
    engine token, so a test body only has to control `_open_pipe`."""
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(client, "_spawned_this_process", False)


class _FakePipe:
    """A minimal stand-in for the `open(pipe, "r+b")` handle -- records
    what was written and serves canned bytes (or raises) on `readline`."""

    def __init__(self, read_result=b'{"jsonrpc":"2.0","id":1,"result":{}}\n', raise_on_write=None):
        self.written = []
        self.closed = False
        self._read_result = read_result
        self._raise_on_write = raise_on_write

    def write(self, data: bytes) -> None:
        if self._raise_on_write is not None:
            raise self._raise_on_write
        self.written.append(data)

    def flush(self) -> None:
        pass

    def readline(self):
        if isinstance(self._read_result, BaseException):
            raise self._read_result
        return self._read_result

    def close(self) -> None:
        self.closed = True


_MSG = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}


def test_warmth_disabled_skips_straight_to_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "is_warm_enabled", lambda: False)
    opened = []
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: opened.append(pipe) or _FakePipe())
    assert client.try_warm_dispatch(_MSG) is None
    assert opened == []


def test_file_not_found_spawns_once_and_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_enoent(pipe):
        raise FileNotFoundError(2, "no such pipe")

    monkeypatch.setattr(client, "_open_pipe", _raise_enoent)
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append((repo_root, script)) or True
    )
    assert client.try_warm_dispatch(_MSG) is None
    assert len(spawns) == 1
    assert spawns[0][1] == client.SERVER_ENTRY_SCRIPT


def test_backstop_one_spawn_attempt_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_enoent(pipe):
        raise FileNotFoundError(2, "no such pipe")

    monkeypatch.setattr(client, "_open_pipe", _raise_enoent)
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append(1) or True
    )
    client.try_warm_dispatch(_MSG)
    client.try_warm_dispatch(_MSG)
    client.try_warm_dispatch(_MSG)
    assert len(spawns) == 1


def test_error_pipe_busy_231_as_plain_oserror_never_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The load-bearing case: ERROR_PIPE_BUSY (231) surfaces as a plain
    `OSError` with `winerror=231, errno=22` on live Windows (verified
    2026-08-15) -- NOT a named subclass. Branching on class alone would
    misclassify this as "no pipe" and spawn; branching on `winerror`
    (this module's actual implementation) must not."""
    exc = OSError("pipe busy")
    exc.winerror = 231
    exc.errno = 22  # CPython's winerror->errno table has no entry for 231

    def _raise_busy(pipe):
        raise exc

    monkeypatch.setattr(client, "_open_pipe", _raise_busy)
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append(1) or True
    )
    assert client.try_warm_dispatch(_MSG) is None
    assert spawns == []


def test_permission_error_someone_elses_pipe_never_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = PermissionError(5, "access denied")

    def _raise_denied(pipe):
        raise exc

    monkeypatch.setattr(client, "_open_pipe", _raise_denied)
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append(1) or True
    )
    assert client.try_warm_dispatch(_MSG) is None
    assert spawns == []


def test_broken_pipe_mid_request_reopens_once_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    pipes = [
        _FakePipe(raise_on_write=BrokenPipeError()),
        _FakePipe(read_result=b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'),
    ]

    def _open(pipe):
        return pipes.pop(0)

    monkeypatch.setattr(client, "_open_pipe", _open)
    response = client.try_warm_dispatch(_MSG)
    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_broken_pipe_on_both_attempts_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakePipe(raise_on_write=BrokenPipeError()))
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append(1) or True
    )
    assert client.try_warm_dispatch(_MSG) is None
    assert spawns == []  # BrokenPipeError is never the spawn trigger


def test_well_formed_success_response_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        client,
        "_open_pipe",
        lambda pipe: _FakePipe(read_result=b'{"jsonrpc":"2.0","id":1,"result":{"pong":true}}\n'),
    )
    response = client.try_warm_dispatch(_MSG)
    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"pong": True}}


def test_well_formed_error_envelope_is_used_not_treated_as_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live server's generic error response means "server up" per the
    table -- it must be returned and used, never mistaken for a dead
    server (the exact -32603-vs-no-listener confusion the ~49-process
    storm was caused by)."""
    monkeypatch.setattr(
        client,
        "_open_pipe",
        lambda pipe: _FakePipe(
            read_result=b'{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"boom"}}\n'
        ),
    )
    response = client.try_warm_dispatch(_MSG)
    assert response == {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "boom"}}


def test_engine_skew_error_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": client.ENGINE_SKEW, "message": "stale"}}
    ).encode("utf-8") + b"\n"
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakePipe(read_result=payload))
    assert client.try_warm_dispatch(_MSG) is None


def test_read_deadline_expiry_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    class _StuckPipe(_FakePipe):
        def readline(self):
            threading.Event().wait(client.READ_DEADLINE_SECS + 5)  # never returns in time
            return b'{"jsonrpc":"2.0","id":1,"result":{}}\n'

    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.05)
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _StuckPipe())
    assert client.try_warm_dispatch(_MSG) is None


def test_malformed_response_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakePipe(read_result=b"not json\n"))
    assert client.try_warm_dispatch(_MSG) is None


def test_empty_response_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakePipe(read_result=b""))
    assert client.try_warm_dispatch(_MSG) is None


def test_unexpected_exception_never_propagates_backstop_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backstop 2: nothing in the preamble may fail in a way that fails the
    op. Even a wholly unanticipated exception (not one of the table's
    named rows) must resolve to a cold-path signal, never propagate."""

    def _explode(pipe):
        raise RuntimeError("unanticipated transport failure")

    monkeypatch.setattr(client, "_open_pipe", _explode)
    assert client.try_warm_dispatch(_MSG) is None


def test_request_payload_carries_engine_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePipe()
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: fake)
    client.try_warm_dispatch(_MSG)
    assert len(fake.written) == 1
    sent = json.loads(fake.written[0].decode("utf-8"))
    assert sent["_engine_token"] == client.engine_token()
    assert sent["method"] == "ping"


def test_pipe_handle_always_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePipe()
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: fake)
    client.try_warm_dispatch(_MSG)
    assert fake.closed is True
