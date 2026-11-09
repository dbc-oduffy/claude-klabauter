"""Local JSON-line transport: private Unix sockets and native Windows named pipes.

Windows uses asyncio's Proactor pipe transports, preserving the harness's raw byte protocol.
No TCP listener, shell, authentication framing or extra proxy process is introduced.
"""
import asyncio
import os
import signal
import socket
import stat
import sys


def is_pipe(endpoint):
    return str(endpoint).lower().startswith("\\\\.\\pipe\\")


def endpoint_exists(endpoint):
    """Probe a pipe without opening it or sending a liveness frame; filesystem stat off Windows."""
    if not is_pipe(endpoint):
        return os.path.exists(endpoint)
    try:
        import _winapi
        _winapi.WaitNamedPipe(str(endpoint), 1)
        return True
    except OSError as exc:
        return getattr(exc, "winerror", None) in (121, 231)  # an existing busy pipe
    except ImportError:
        return False


class PipeServers:
    def __init__(self, servers):
        self.servers = servers

    def close(self):
        for server in self.servers:
            server.close()


def _remove_stale_socket(endpoint):
    """Refuse regular files/live listeners; unlink only an unchanged, refused Unix socket."""
    try:
        before = os.lstat(endpoint)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(before.st_mode):
        raise FileExistsError(f"local endpoint is not a socket: {endpoint}")
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(0.1)
        try:
            probe.connect(endpoint)
        except ConnectionRefusedError:
            pass
        except FileNotFoundError:
            return
        else:
            raise FileExistsError(f"local endpoint already has a listener: {endpoint}")
    try:
        after = os.lstat(endpoint)
    except FileNotFoundError:
        return
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise FileExistsError(f"local endpoint changed during stale check: {endpoint}")
    os.unlink(endpoint)


async def start_server(callback, endpoint, limit):
    """Bind a harness-compatible endpoint; socket mode is private from creation."""
    loop = asyncio.get_running_loop()
    if is_pipe(endpoint):
        def protocol():
            reader = asyncio.StreamReader(limit=limit)
            return asyncio.StreamReaderProtocol(reader, callback)

        return PipeServers(await loop.start_serving_pipe(protocol, endpoint))
    os.makedirs(os.path.dirname(endpoint) or ".", mode=0o700, exist_ok=True)
    _remove_stale_socket(endpoint)
    old = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(callback, path=endpoint, limit=limit)
    finally:
        os.umask(old)
    os.chmod(endpoint, 0o600)
    return server


async def _pipe_exchange(endpoint, data, timeout, reply_timeout):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=1 << 22)
    protocol = asyncio.StreamReaderProtocol(reader)
    async with asyncio.timeout(timeout):
        transport, _ = await loop.create_pipe_connection(lambda: protocol, endpoint)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        try:
            writer.write(data)
            await writer.drain()
            if reply_timeout is None:
                return await reader.readline()
            try:
                return await asyncio.wait_for(reader.readline(), reply_timeout)
            except asyncio.TimeoutError:
                return b""  # harness sessions may accept without a response line
        finally:
            writer.close()


def pipe_exchange(endpoint, data, timeout, reply_timeout=None):
    """One raw newline request/reply, called off the router's event-loop thread."""
    return asyncio.run(_pipe_exchange(endpoint, data, timeout, reply_timeout))


def install_stop_handlers(stop):
    """Proactor loops lack add_signal_handler; the main thread's signal API remains usable."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


def lock_file(file, *, windows=None):
    """Take one nonblocking process lock; False means another holder owns it."""
    file.seek(0)
    use_windows = sys.platform == "win32" if windows is None else windows
    try:
        if use_windows:
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def unlock_file(file, *, windows=None):
    file.seek(0)
    use_windows = sys.platform == "win32" if windows is None else windows
    if use_windows:
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
