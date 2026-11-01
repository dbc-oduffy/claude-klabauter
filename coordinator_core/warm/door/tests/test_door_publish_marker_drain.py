"""The native door honours the publish marker: a missing pipe under a held marker spawns no warm server.

Mirrors `warm/breadcrumb.py :: wait_for_publish_release`. Drives the committed `door.exe` (installed as
`coordinator-invoke.exe` in a stub engine root) against a controlled `COORDINATOR_WARM_RUNTIME_BASE`, so
`<base>/coordinator/warm/<clone_hash>/publish.marker` is the real marker path. A stub `server.py` under
the stub root writes a sentinel file when the door's respawn reaches it: sentinel present means a warm
server was started, absent means none was.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from coordinator_core.warm import breadcrumb
from coordinator_core.warm.door.tests.test_windows_door_cold_leg_route import (
    _release_unconnected_server,
)
from coordinator_core.warm.tests.door_test_support import (
    DOOR_POSIX_C,
    DOOR_WINDOWS_C,
    FALLBACK_MARKER,
    ReplyingServer,
    WINDOWS_ONLY,
    door_under_default_name,
    make_stub_engine_root,
    pipe_name_for,
    read,
)
from coordinator_core.win_portability import no_console_creationflags

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.warm_tier]

_SENTINEL_ENV = "DOOR_TEST_SPAWN_SENTINEL"
_WARM_REPLY = (
    '{"jsonrpc":"2.0","id":1,"result":'
    '{"stdout":"served-warm\\n","stderr":"","exit_code":0}}\n'
).encode("utf-8")
_NO_SPAWN_SETTLE_SECS = 2.0
_SPAWN_APPEARS_SECS = 15.0


def _arrange(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path, Path]:
    root = make_stub_engine_root(tmp_path)
    warm = root / "coordinator_core" / "warm"
    warm.mkdir(parents=True)
    sentinel = tmp_path / "spawned.sentinel"
    (warm / "server.py").write_text(
        "import os\n"
        f"open(os.environ[{_SENTINEL_ENV!r}], 'w').write('spawned')\n",
        encoding="utf-8",
    )
    base = tmp_path / "runtime-base"
    base.mkdir()
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(base))
    svc = breadcrumb.svc_dir(root)
    svc.mkdir(parents=True)
    return root, door_under_default_name(root), svc, sentinel


def _door_env(root: Path, sentinel: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env[_SENTINEL_ENV] = str(sentinel)
    env.pop("COORDINATOR_DOOR_STDIN_MODE", None)
    env.pop("COORDINATOR_WARM", None)
    return env


def _run(door: Path, root: Path, sentinel: Path) -> tuple[subprocess.CompletedProcess, float]:
    started = time.monotonic()
    proc = subprocess.run(
        [str(door), "ping"],
        input=b"",
        capture_output=True,
        env=_door_env(root, sentinel),
        timeout=60,
        cwd=str(root),
        **no_console_creationflags(),
    )
    return proc, time.monotonic() - started


def _wait_for(path: Path, secs: float) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


def _hold(svc: Path, *, age_secs: float = 0.0) -> Path:
    marker = svc / breadcrumb.PUBLISH_MARKER_FILENAME
    marker.write_text("{}", encoding="utf-8")
    if age_secs:
        stamp = time.time() - age_secs
        os.utime(marker, (stamp, stamp))
    return marker


@WINDOWS_ONLY
def test_held_marker_and_missing_pipe_spawns_no_server_and_goes_cold(tmp_path: Path, monkeypatch) -> None:
    root, door, svc, sentinel = _arrange(tmp_path, monkeypatch)
    _hold(svc)

    proc, elapsed = _run(door, root, sentinel)

    assert FALLBACK_MARKER.encode() in proc.stdout
    assert elapsed >= 0.4, f"door did not queue behind the held marker (returned in {elapsed:.2f}s)"
    assert not _wait_for(sentinel, _NO_SPAWN_SETTLE_SECS), "door spawned a warm server under a held marker"


@WINDOWS_ONLY
def test_no_marker_missing_pipe_still_spawns_the_server(tmp_path: Path, monkeypatch) -> None:
    root, door, svc, sentinel = _arrange(tmp_path, monkeypatch)

    proc, elapsed = _run(door, root, sentinel)

    assert FALLBACK_MARKER.encode() in proc.stdout
    assert _wait_for(sentinel, _SPAWN_APPEARS_SECS), "door no longer respawns the server when no marker is held"


@WINDOWS_ONLY
def test_aged_out_marker_does_not_block_the_spawn(tmp_path: Path, monkeypatch) -> None:
    root, door, svc, sentinel = _arrange(tmp_path, monkeypatch)
    _hold(svc, age_secs=breadcrumb.PUBLISH_MARKER_MAX_AGE_SECS + 600)

    proc, elapsed = _run(door, root, sentinel)

    assert FALLBACK_MARKER.encode() in proc.stdout
    assert _wait_for(sentinel, _SPAWN_APPEARS_SECS), "a marker past PUBLISH_MARKER_MAX_AGE_SECS still blocked the spawn"


@WINDOWS_ONLY
def test_marker_released_during_the_wait_retries_the_pipe_once(tmp_path: Path, monkeypatch) -> None:
    root, door, svc, sentinel = _arrange(tmp_path, monkeypatch)
    marker = _hold(svc)
    holder: dict[str, ReplyingServer] = {}

    def release() -> None:
        holder["server"] = ReplyingServer(pipe_name_for(root), _WARM_REPLY)
        marker.unlink()

    timer = threading.Timer(0.3, release)
    timer.start()
    try:
        proc, elapsed = _run(door, root, sentinel)
    finally:
        timer.join()
        server = holder.get("server")
        if server is not None:
            if not server.request:
                _release_unconnected_server(root)
            server.close()

    assert holder["server"].request, "door did not retry the pipe after the marker was released"
    assert proc.stdout == b"served-warm\n"
    assert elapsed >= 0.2
    assert not sentinel.exists()


def test_both_doors_stat_the_publish_marker_before_spawning() -> None:
    windows = read(DOOR_WINDOWS_C)
    posix = read(DOOR_POSIX_C)
    assert breadcrumb.PUBLISH_MARKER_FILENAME in windows
    assert breadcrumb.PUBLISH_MARKER_FILENAME in posix
    age = float(breadcrumb.PUBLISH_MARKER_MAX_AGE_SECS)
    assert f"DOOR_PUBLISH_MARKER_MAX_AGE_SECS {age}" in windows
    assert f"DOOR_PUBLISH_MARKER_MAX_AGE_SECS {age}" in posix
    assert f"DOOR_DRAIN_WAIT_MS {int(breadcrumb.DRAIN_WAIT_SECS * 1000)}" in windows
    assert f"DOOR_DRAIN_WAIT_MS {int(breadcrumb.DRAIN_WAIT_SECS * 1000)}" in posix
    for source in (windows, posix):
        spawn_fn = source.index("door_maybe_spawn_server(")
        body = source[source.index("static void door_maybe_spawn_server("):]
        assert body.index("marker_held") < body.index("warm.json.boot.lock")
        assert spawn_fn >= 0
