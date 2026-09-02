"""Tests for `coordinator_core.warm.client`'s POSIX (`AF_UNIX`) arm.

TWO HALVES, AND ONLY ONE OF THEM RUNS ON WINDOWS.

The *classification* half -- which connect failure spawns a server, which
goes cold, and which must never do either -- is platform-independent: it
lives in `_try_warm_dispatch_inner`'s except chain and is reached through
the same monkeypatched `_open_pipe` seam `test_client_fallback.py` already
drives. It is tested here unconditionally, because a POSIX-only test of the
POSIX error table would never have run anywhere before the first Mac
touched it, and the table is exactly where a mistake is expensive: getting
ECONNREFUSED wrong means either a spawn storm or a client that never
recovers from a corpse socket.

The *transport* half -- a real `socket()`/`connect()`/`makefile()` round
trip -- needs `AF_UNIX` and is skipped off POSIX.

Separate module rather than more cases in `test_client_fallback.py`: that
module's `_warm_on` fixture pins `election.pipe_name` and is written around
the Windows named-pipe table, and this file needs the endpoint derivation
itself under test rather than stubbed.
"""

from __future__ import annotations

import errno
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from coordinator_core.warm import client

_MSG = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}

#: Captured at import time, before any test's `os.unlink`/`os.remove`
#: monkeypatch (`test_the_client_never_unlinks_a_refusing_socket` below
#: patches both module-wide) -- `_force_rmtree`'s own teardown must not go
#: through the patched attributes, or it either records a spurious
#: "unlink" the test never asked for, or (since the fake in that test takes
#: one positional arg, not `shutil.rmtree`'s own `dir_fd=`-qualified call)
#: raises a `TypeError` out of fixture teardown.
_REAL_UNLINK = os.unlink
_REAL_RMDIR = os.rmdir


def _force_rmtree(path: Path) -> None:
    try:
        entries = list(path.iterdir())
    except OSError:
        return
    for child in entries:
        if child.is_dir() and not child.is_symlink():
            _force_rmtree(child)
        else:
            try:
                _REAL_UNLINK(str(child))
            except OSError:
                pass
    try:
        _REAL_RMDIR(str(path))
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _short_warm_runtime_base(monkeypatch: pytest.MonkeyPatch):
    """Overrides the suite-wide HOME quarantine's `warm-runtime-base`
    (`coordinator_core/conftest.py::_quarantine_real_home`) with a short,
    real on-disk root under `/tmp`.

    The quarantine's own path is already 90+ bytes deep on macOS before
    `election.socket_path` appends `coordinator/warm/<16-hex-hash>/
    <token>.sock`, tripping `election.SUN_PATH_MAX_BYTES` (100) before
    `_endpoint_name` -- under test in this module -- ever computes the
    real endpoint. Same fix as `test_election_posix.py::short_runtime_base`
    (committed b4e300c8f1); duplicated here rather than lifted into a
    shared `conftest.py` because this dispatch's scope is this file only.

    Teardown uses `_force_rmtree`, not `shutil.rmtree`, for the reason
    `_REAL_UNLINK`'s own docstring gives.
    """
    from coordinator_core.warm import breadcrumb

    base = Path(tempfile.mkdtemp(prefix="wrb-", dir="/tmp"))
    try:
        monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(base))
        yield base
    finally:
        _force_rmtree(base)


@pytest.fixture(autouse=True)
def _warm_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warmth on, token pinned, spawn debounce open -- so a test body
    controls only the connect outcome. Mirrors `test_client_fallback.py`'s
    own fixture; `election.pipe_name` is deliberately NOT stubbed, because
    `_endpoint_name` is under test in this module rather than assumed."""
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    from coordinator_core.warm import breadcrumb

    monkeypatch.setattr(breadcrumb, "should_spawn", lambda engine_root=None, **kw: True)


def _spawn_recorder(monkeypatch: pytest.MonkeyPatch) -> list:
    spawns: list = []
    monkeypatch.setattr(
        client,
        "spawn_detached",
        lambda repo_root, script, args=None, **kwargs: spawns.append(script) or True,
    )
    return spawns


def test_connection_refused_spawns_exactly_like_a_missing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A unix socket's path outlives the process that bound it, so a
    hard-killed server leaves a corpse file that EXISTS and refuses. That is
    the same condition Windows reports as a missing pipe, and it must reach
    the same disposition -- spawn once, go cold. Treating it as an
    unclassified OSError instead would re-raise into the caller; treating it
    as contention would leave a box with a corpse socket permanently unable
    to start a server."""

    def _raise_refused(endpoint):
        raise ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")

    monkeypatch.setattr(client, "_open_pipe", _raise_refused)
    spawns = _spawn_recorder(monkeypatch)

    assert client.try_warm_dispatch(_MSG) is None
    assert spawns == [client.SERVER_ENTRY_SCRIPT]


def test_the_client_never_unlinks_a_refusing_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """NEGATIVE SPEC. Reclaiming a corpse socket belongs to the election,
    under the flock that makes probe->unlink->rebind atomic
    (`election.reclaim_stale_socket`). A client that unlinked on refusal
    would have no such lock and would race a peer's LIVE socket -- the
    exact failure the election's own lock exists to prevent, reintroduced
    from the other side. Pinned because "the path is stale, remove it" is
    the obvious wrong fix for the branch above."""

    def _raise_refused(endpoint):
        raise ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")

    monkeypatch.setattr(client, "_open_pipe", _raise_refused)
    _spawn_recorder(monkeypatch)

    unlinked: list = []
    import os as _os

    monkeypatch.setattr(_os, "unlink", lambda p: unlinked.append(p))
    monkeypatch.setattr(_os, "remove", lambda p: unlinked.append(p))

    assert client.try_warm_dispatch(_MSG) is None
    assert unlinked == []


def test_a_connect_timeout_goes_cold_and_never_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`CONNECT_DEADLINE_SECS` expiring means a FULL BACKLOG -- server up,
    contended -- the POSIX counterpart of ERROR_PIPE_BUSY. Spawning here is
    the storm the anti-storm table forbids: every queued caller would start
    a second server against a first one that is merely busy.

    `TimeoutError` carries NO errno (`settimeout` does not set one), so this
    also pins that the branch cannot be folded into the errno tests."""

    def _raise_timeout(endpoint):
        raise TimeoutError("timed out")

    monkeypatch.setattr(client, "_open_pipe", _raise_timeout)
    spawns = _spawn_recorder(monkeypatch)

    assert client.try_warm_dispatch(_MSG) is None
    assert spawns == []


@pytest.mark.parametrize("code", sorted(client._CONTENDED_ERRNOS))
def test_contended_errnos_go_cold_and_never_spawn(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Same contended-server row reached without the deadline firing.
    Parametrized off the frozenset itself so a errno added there without a
    disposition cannot slip in untested."""

    def _raise(endpoint):
        raise OSError(code, "contended")

    monkeypatch.setattr(client, "_open_pipe", _raise)
    spawns = _spawn_recorder(monkeypatch)

    assert client.try_warm_dispatch(_MSG) is None
    assert spawns == []


def test_an_unclassified_connect_error_goes_cold_LOUDLY(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Anti-scope pin. The POSIX branches ADD rows to the table; they must
    not turn it into a catch-all.

    Every outcome here ends up cold -- `try_warm_dispatch`'s outer fail-open
    backstop guarantees that much, and it is why this cannot be pinned as a
    raise. The difference that matters is VOLUME: a classified outcome
    returns cold silently, an unclassified one reaches the backstop's
    generic stderr diagnostic. That line is the only way an unknown
    transport failure is ever discovered rather than absorbed as "the warm
    path just isn't hitting today", so it is the thing worth pinning."""

    def _raise(endpoint):
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(client, "_open_pipe", _raise)
    _spawn_recorder(monkeypatch)

    assert client.try_warm_dispatch(_MSG) is None
    noisy = capsys.readouterr().err
    assert "too many open files" in noisy

    # The contrast, in the same test so neither half can rot alone: a
    # CLASSIFIED outcome takes the same cold exit without the diagnostic.
    def _raise_refused(endpoint):
        raise ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")

    monkeypatch.setattr(client, "_open_pipe", _raise_refused)
    assert client.try_warm_dispatch(_MSG) is None
    assert "connection refused" not in capsys.readouterr().err


def test_endpoint_name_is_the_production_derivation_for_this_platform() -> None:
    """CONTRACT PIN, the client half of the one this module's peers already
    carry (`test_server_posix.py`'s socket-path pin, and the POSIX door
    test's). `_endpoint_name` must ASK for the endpoint, never spell it: the
    C door derives the same socket path independently, so a second Python
    recipe would let door and client connect to different paths while every
    surface stayed green."""
    import inspect

    source = inspect.getsource(client._endpoint_name)
    assert "election.pipe_name(token)" in source
    assert "election.socket_path(token)" in source
    # No hand-spelled shape on either arm.
    assert '".sock"' not in source and "'.sock'" not in source
    assert "pipe\\\\" not in source


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX round trip needs a POSIX kernel")
def test_open_pipe_round_trips_over_a_real_unix_socket(tmp_path) -> None:
    """The transport half. `makefile("rwb")` must give the same blocking
    `write`/`flush`/`readline`/`close` surface the named-pipe arm returns,
    and the connection must survive `_open_pipe`'s own `sock.close()` --
    the CPython idiom this seam relies on, and the one thing about it that
    would fail loudly rather than subtly if it were wrong.

    NEVER EXECUTED ON THE PLATFORM THIS PROJECT DEVELOPS ON. Stated so a
    green Windows run is not read as evidence about this test.

    Binds its OWN literal `s.sock`, not one derived through
    `election.socket_path` -- so unlike the rest of this module, `tmp_path`
    itself (not the suite-wide `warm-runtime-base` quarantine) is the thing
    that can blow `sun_path` here. It routinely does on macOS, where
    `tmp_path` is already ~90 bytes deep, so this test binds under a short
    `tempfile.mkdtemp(dir="/tmp")` root instead -- the shortest real POSIX
    temp root available, same reasoning as `test_election_posix.py::
    short_runtime_base`."""
    import shutil
    import tempfile
    import threading
    from pathlib import Path

    short_dir = Path(tempfile.mkdtemp(prefix="ops-", dir="/tmp"))
    try:
        path = str(short_dir / "s.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)

        received: list = []

        def _serve() -> None:
            conn, _ = listener.accept()
            io = conn.makefile("rwb")
            conn.close()
            received.append(io.readline())
            io.write(b'{"jsonrpc":"2.0","id":1,"result":"pong"}\n')
            io.flush()
            io.close()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        try:
            fh = client._open_pipe(path)
            fh.write(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
            fh.flush()
            line = fh.readline()
            fh.close()
        finally:
            listener.close()
        t.join(timeout=5)

        assert received and b'"ping"' in received[0]
        assert b'"pong"' in line
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)
