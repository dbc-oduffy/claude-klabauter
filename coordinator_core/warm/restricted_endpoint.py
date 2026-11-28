"""Host-owned diagnostics for a single Darwin process. No general IPC dispatch.

The host binds a stream listener before launching the native child. Datagram
connect and sendto fail against that listener. The Seatbelt outbound grant
matches the pathname: a datagram receiver substituted at the same pathname
could receive datagrams. Preserve host custody of the private directory.
"""

from contextlib import ExitStack, contextmanager
import errno
import json
import os
import select
from pathlib import Path
import socket
import stat
import struct
import sys
import time

import psutil


class _DiagnosticsBusy(BlockingIOError):
    """Another native launcher holds this directory."""


@contextmanager
def _private_directory(directory):
    import fcntl

    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("private socket directory required")
        # Serialize bind, stale recovery and cleanup across participating launchers.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _DiagnosticsBusy() from exc
        yield
    finally:
        os.close(fd)


def _unlink_same(address, original):
    try:
        if os.path.samestat(original, address.lstat()):
            address.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def _listen(address):
    if sys.platform != "darwin":
        raise OSError("darwin-only endpoint")
    if len(os.fsencode(address)) >= 104:
        raise OSError(errno.ENAMETOOLONG, "Darwin socket pathname exceeds 103 bytes")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(str(address))
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            original = address.lstat()
            if not stat.S_ISSOCK(original.st_mode) or original.st_uid != os.getuid():
                raise
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.setblocking(False)
                if probe.connect_ex(str(address)) != errno.ECONNREFUSED:
                    raise
            _unlink_same(address, original)
            listener.bind(str(address))
        original = address.lstat()
        try:
            address.chmod(0o600)
            listener.listen(8)
            yield listener
        finally:
            _unlink_same(address, original)


@contextmanager
def bound_listener(directory: Path):
    """Hold the directory lock until the private diagnostics listener closes."""
    with _private_directory(directory), _listen(directory / "diagnostic.sock") as listener:
        yield listener


def _answer(payload):
    if isinstance(payload, dict) and payload.keys() == {"op", "params"}:
        op, params = payload["op"], payload["params"]
        if op == "ping" and params == {}:
            from coordinator_core.ops.ping import _ping
            return _ping({})
        if (op == "engine_ask.submit" and isinstance(params, dict)
                and params.get("fire") is False
                and params == {"ask": "probe", "sizing": "also-supplied", "fire": False}):
            from coordinator_core.hooks._engine_ask_gates import submit_ask
            return submit_ask(ask="probe", sizing="also-supplied")
    return {"refused": "operation-denied"}


def _peer_uid(conn):
    # Darwin LOCAL_PEERCRED returns xucred: version then effective uid.
    cred = conn.getsockopt(0, 1, 128)
    version, uid = struct.unpack_from("=II", cred)
    if version != 0:
        raise OSError("unknown Darwin peer credential layout")
    return uid


def _matches(conn, peer):
    return (_peer_uid(conn) == os.getuid() and peer is not None
            and conn.getsockopt(0, 2) == peer.pid and peer.is_running())


def _read_request(conn, *, limit=1024):
    deadline = time.monotonic() + 1
    frame = bytearray()
    while len(frame) < limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        conn.settimeout(remaining)
        data = conn.recv(min(8192, limit - len(frame)))
        if not data:
            break
        frame.extend(data)
        if b"\n" in frame:
            if frame.count(b"\n") == 1 and frame.endswith(b"\n"):
                return json.loads(frame[:-1].decode("utf-8"), object_pairs_hook=_unique_keys)
            break
    raise ValueError("invalid diagnostic frame")


def _unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate request key")
    return result


def _serve_connection(conn, peer):
    accepted = False
    reply = {"refused": "peer-denied"}
    try:
        conn.settimeout(1)
        if _matches(conn, peer):
            accepted = True
            try:
                payload = _read_request(conn)
            except (ValueError, OSError, RecursionError):
                reply = {"refused": "invalid-frame"}
            else:
                try:
                    reply = _answer(payload)
                except Exception:
                    reply = {"refused": "handler-error"}
    except (OSError, psutil.Error, struct.error):
        pass
    try:
        try:
            wire = json.dumps(reply).encode() + b"\n"
        except (ValueError, TypeError, RecursionError):
            wire = b'{"refused":"handler-error"}\n'
        conn.sendall(wire)
    except OSError:
        pass
    return accepted


def serve_one(listener: socket.socket, peer: psutil.Process):
    """Serve one bound request. A foreign connection cannot consume that request."""
    while peer.is_running():
        if not select.select([listener], [], [], 0.1)[0]:
            continue
        conn, _ = listener.accept()
        with conn:
            if _serve_connection(conn, peer):
                return


def _spawn_notice(control, parent, current, stopped):
    with control.accept()[0] as conn:
        try:
            if not _matches(conn, parent) or (current is not None and current.is_running()):
                return current
            conn.settimeout(0.4)
            if conn.recv(1) != b"B":
                return current
            # Acknowledge before the native parent spawns. Early data connections
            # remain queued until this same credentialed channel identifies its child.
            conn.sendall(b"Y")
            # A slow spawn delays the first PID byte. Once started, a partial
            # frame has only the short handshake window to finish.
            deadline = time.monotonic() + 2.0
            frame = bytearray()
            while len(frame) < 4 and not stopped.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return current
                if not select.select([conn], [], [], min(0.1, remaining))[0]:
                    continue
                chunk = conn.recv(4 - len(frame))
                if not chunk:
                    return current
                if not frame:
                    deadline = time.monotonic() + 0.4
                frame.extend(chunk)
            if len(frame) == 4 and _matches(conn, parent):
                pid, = struct.unpack("!I", frame)
                child = psutil.Process(pid)
                if child.ppid() == parent.pid:
                    conn.sendall(b"Y")
                    return child
        except (OSError, psutil.Error, struct.error):
            pass
    return current


def _serve_spawned(listener, control, parent, stopped, serve=None):
    peer = None
    try:
        while not stopped.is_set():
            ready, _, _ = select.select([control, listener], [], [], 0.1)
            if control in ready:
                peer = _spawn_notice(control, parent, peer, stopped)
            if listener in ready:
                with listener.accept()[0] as conn:
                    if serve is None:
                        _serve_connection(conn, peer)
                    else:
                        serve(conn, peer, parent)
    except (OSError, ValueError) as exc:
        if not stopped.is_set():
            print(f"headless-em-launch: diagnostic listener stopped: {exc}", file=sys.stderr)


@contextmanager
def diagnostic_spawn(directory, argv, env, *, serve_submit=None):
    """Host side of native exec --json. Only the spawned native parent can nominate its pipe child."""
    import subprocess
    import threading

    with ExitStack() as stack:
        try:
            if serve_submit is None:
                data = stack.enter_context(bound_listener(directory))
            else:
                stack.enter_context(_private_directory(directory))
                data = stack.enter_context(_listen(directory / "submit.sock"))
        except _DiagnosticsBusy:
            if serve_submit is not None:
                raise RuntimeError("issued submit endpoint already in use")
            print("headless-em-launch: diagnostic endpoint busy; starting without diagnostics", file=sys.stderr)
            with subprocess.Popen(argv, env=env, stdout=subprocess.PIPE) as child:
                yield child
            return
        control = stack.enter_context(_listen(directory / "spawn.sock"))
        with subprocess.Popen(argv, env=env, stdout=subprocess.PIPE) as child:
            stopped = threading.Event()
            thread = threading.Thread(target=_serve_spawned, args=(data, control, psutil.Process(child.pid), stopped, serve_submit))
            thread.start()
            try:
                yield child
            finally:
                stopped.set()
                thread.join()
