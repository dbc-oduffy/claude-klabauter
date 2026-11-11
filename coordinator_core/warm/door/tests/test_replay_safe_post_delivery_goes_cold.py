"""A replay-safe invocation whose warm answer never arrives runs cold once; any other still refuses.

Compiles the POSIX door into a scratch directory under the name
`consolidate-assemble`, points it at a stub engine root and a socket server that
reads the request frame then closes without replying (the post-delivery
failure). `brief` is read-only and must fall through to the stub cold leg;
`apply` must keep the indeterminate envelope and never run cold.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.skipif(os.name == "nt", reason="door_posix is a macOS/Linux binary"),
]

_MARKER = "DOOR-TEST-COLD-RAN"
_COLD_EXIT = 42
_NAME = "consolidate-assemble"


class _PeerDeathServer:
    def __init__(self, sock_path: Path) -> None:
        self._path = sock_path
        self.request = b""
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(sock_path))
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn = None
        try:
            conn, _ = self._sock.accept()
            while b"\n" not in self.request:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                self.request += chunk
        except OSError:
            pass
        finally:
            if conn is not None:
                conn.close()

    def close(self) -> None:
        self._thread.join(timeout=10)
        self._sock.close()
        self._path.unlink(missing_ok=True)


@pytest.fixture
def runtime_base():
    from coordinator_core.temp_layout import managed_tempdir
    from coordinator_core.warm.election import SUN_PATH_MAX_BYTES

    roots = [r for r in ("/tmp", str(managed_tempdir())) if os.path.isdir(r)]
    roots.sort(key=lambda r: len(os.fsencode(os.path.realpath(r))))
    base = Path(os.path.realpath(tempfile.mkdtemp(prefix="rs-", dir=roots[0])))
    projected = len(os.fsencode(str(base))) + len("/coordinator/warm/") + 16 + 1 + 16 + 5
    if projected > SUN_PATH_MAX_BYTES:
        rmtree_or_raise(base, label="replay-safe-runtime-base")
        pytest.skip(f"runtime base {str(base)!r} leaves no room under the sun_path budget (projected {projected})")
    try:
        yield base
    finally:
        rmtree_or_raise(base, label="replay-safe-runtime-base")


def _stub_engine_root(tmp_path: Path) -> Path:
    root = tmp_path / "stub-engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text(
        f"replay-safe-test-{os.getpid()}-{time.time_ns()}\n", encoding="utf-8"
    )
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    cold = f"print({_MARKER!r})\nraise SystemExit({_COLD_EXIT})\n"
    (bin_dir / f"{_NAME}.py").write_text(cold, encoding="utf-8")
    (bin_dir / "coordinator-invoke.py").write_text(cold, encoding="utf-8")
    return root.resolve()


@pytest.fixture
def door_named(tmp_path: Path) -> Path:
    from coordinator_core.warm.door import build_posix

    root = _stub_engine_root(tmp_path)
    built = build_posix.build(root, output=tmp_path / "door-build" / "door")
    named = tmp_path / "door-build" / _NAME
    shutil.copy2(built, named)
    return named


def _run(door: Path, root: Path, runtime_base: Path, *args: str):
    from coordinator_core.warm import breadcrumb, election, skew

    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env["COORDINATOR_WARM_RUNTIME_BASE"] = str(runtime_base)
    prior = os.environ.get(breadcrumb.RUNTIME_BASE_ENV)
    os.environ[breadcrumb.RUNTIME_BASE_ENV] = str(runtime_base)
    try:
        sock_path = election.socket_path(skew.compute_client_token(root), engine_clone=root)
    finally:
        if prior is None:
            os.environ.pop(breadcrumb.RUNTIME_BASE_ENV, None)
        else:
            os.environ[breadcrumb.RUNTIME_BASE_ENV] = prior
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(sock_path.parent, 0o700)
    os.chmod(sock_path.parent.parent, 0o755)
    server = _PeerDeathServer(sock_path)
    try:
        proc = subprocess.run(
            [str(door), *args], capture_output=True, text=True, env=env, timeout=60,
            cwd=str(root), **no_console_creationflags(),
        )
    finally:
        server.close()
    return proc, server


def test_replay_safe_subcommand_runs_cold_once_after_delivery(tmp_path, runtime_base, door_named):
    root = _stub_engine_root(tmp_path / "e1")
    proc, server = _run(door_named, root, runtime_base, "brief")
    assert b'"invoke.from_argv"' in server.request, "the request must have been delivered warm first"
    assert _MARKER in proc.stdout
    assert proc.returncode == _COLD_EXIT
    assert "re-running cold once (replay-safe)" in proc.stderr
    assert "-32004" not in proc.stdout


def test_other_subcommand_still_refuses_after_delivery(tmp_path, runtime_base, door_named):
    root = _stub_engine_root(tmp_path / "e2")
    proc, server = _run(door_named, root, runtime_base, "apply")
    assert b'"invoke.from_argv"' in server.request
    assert _MARKER not in proc.stdout
    assert "-32004" in proc.stdout
    assert proc.returncode != _COLD_EXIT
    assert "replay-safe" not in proc.stderr
