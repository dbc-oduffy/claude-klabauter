"""Lease: single holder, second acquirer refused, kernel release on holder death."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.resource_lifecycle.errors import LeaseHeld
from coordinator_core.resource_lifecycle.lease import Lease

_HOLDER = (
    "import sys\n"
    "from pathlib import Path\n"
    "from coordinator_core.resource_lifecycle.lease import Lease\n"
    "d = sys.argv[2] if len(sys.argv) > 2 else None\n"
    "l = Lease(sys.argv[1], d).acquire()\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


def _name() -> str:
    return "t-" + uuid.uuid4().hex[:12]


@pytest.fixture
def lease_dir():
    # pytest's tmp_path pushes the socket path past the 100-byte sun_path budget on macOS.
    base = "/tmp" if os.name == "posix" and len(tempfile.gettempdir()) > 40 else None
    root = tempfile.mkdtemp(prefix="ls", dir=base)
    try:
        yield Path(root) / "l"
    finally:
        rmtree_or_raise(Path(root), label="lease-test")


def _spawn_holder(name, directory):
    args = [sys.executable, "-c", _HOLDER, name]
    if directory is not None:
        args.append(str(directory))
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.stdout.readline().strip() == "ready"
    return proc


def test_acquire_once_then_same_process_second_refused(lease_dir):
    name = _name()
    first = Lease(name, lease_dir).acquire()
    try:
        with pytest.raises(LeaseHeld):
            Lease(name, lease_dir).acquire()
    finally:
        first.release()


def test_release_frees_the_lease(lease_dir):
    name = _name()
    Lease(name, lease_dir).acquire().release()
    with Lease(name, lease_dir) as again:
        assert again.held


def test_context_manager_releases(lease_dir):
    name = _name()
    with Lease(name, lease_dir):
        pass
    Lease(name, lease_dir).acquire().release()


@pytest.mark.spawns_process
def test_other_process_refused_then_acquires_after_holder_killed(lease_dir):
    name = _name()
    holder = _spawn_holder(name, lease_dir)
    try:
        with pytest.raises(LeaseHeld):
            Lease(name, lease_dir).acquire()
        holder.kill()
        holder.wait(timeout=10)
        got = Lease(name, lease_dir).acquire()
        got.release()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)
        holder.stdin.close()
        holder.stdout.close()


def test_bad_name_rejected(lease_dir):
    with pytest.raises(ValueError):
        Lease("a/b", lease_dir)


def test_module_reads_and_writes_no_pid():
    import inspect

    import coordinator_core.resource_lifecycle.lease as mod

    src = inspect.getsource(mod)
    assert "getpid" not in src
    assert "os.kill" not in src


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX AF_UNIX socket branch; not exercisable on Windows")
def test_posix_stale_socket_is_reclaimed(lease_dir):
    import socket

    name = _name()
    lease_dir.mkdir(mode=0o700)
    path = lease_dir / f"{name}.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))
    dead.close()
    assert path.exists()
    lease = Lease(name, lease_dir).acquire()
    lease.release()
    assert not path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX AF_UNIX socket branch; not exercisable on Windows")
def test_posix_release_unlinks_only_its_socket(lease_dir):
    name = _name()
    lease = Lease(name, lease_dir).acquire()
    path = lease_dir / f"{name}.sock"
    assert path.exists()
    lease.release()
    assert not path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named-pipe branch; not exercisable off Windows")
def test_windows_lease_is_a_first_instance_pipe():
    import _winapi

    name = _name()
    lease = Lease(name).acquire()
    try:
        with pytest.raises(OSError) as ei:
            _winapi.CreateNamedPipe(
                "\\\\.\\pipe\\coordinator-lease." + name,
                _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE,
                _winapi.PIPE_WAIT,
                _winapi.PIPE_UNLIMITED_INSTANCES, 512, 512, 0, 0,
            )
        assert ei.value.winerror in (5, 231)
    finally:
        lease.release()
