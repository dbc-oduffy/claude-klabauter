"""Per-box peer router: register every Codex thread / agy conversation in peers.json, plus every
live agy leg record, agy presence lock and Codex thread-writer-lock, as its own roster peer and
route messages between any Claude session and any peer, or peer to peer. A discovered peer retires
when its record or lock goes; a peers.json entry wins a name or target clash.

  unimatrix-peer-router.py [--peers FILE] [--sessions-dir DIR] [--state-dir DIR] [--endpoint-dir DIR]
      Serve. Default peers file: <settings home>/unimatrix/peers.json (absent is fine); leg
      records in <settings home>/unimatrix/agy-legs are rescanned every few seconds.
  unimatrix-peer-router.py send [--as NAME] <peer> <text...>
      Ask a peer a question through the running router; prints the labelled reply.

The harness keys a registry entry and its key file by pid, so one process cannot hold M
entries. The router therefore spawns one minimal registration child ("face") per peer: the
child's pid names its entry, it binds the peer's endpoint, and it forwards every inbound frame
to the router's 0600 control endpoint. Turn locks, adapters and replies stay in the router.
Replies are stamped with provenance (peer contract) and written by the router straight to the
sender's session socket; the receiver's crossSessionInbound=accept setting removes the prompt.
A frame whose sender is itself a registered peer is answered to nobody, so two peers cannot
ping-pong; the `send` path returns the reply on its own connection instead.
Windows uses current-user-only named pipes; POSIX uses private Unix sockets.
"""
import argparse
import asyncio
import importlib.util
import json
import os
import re
import secrets
import shlex
import signal
import socket
import sys
import time
import threading
from pathlib import Path

READY_SECONDS = 15
SEND_TIMEOUT = 900
ROUTER_ENDPOINT_SEED = "unimatrix-peer-router"
FRAME_LIMIT = 1 << 22
LEG_SCAN_SECONDS = 2
ACK_BODIES = frozenset({
    "", "ok", "okay", "k", "ack", "acked", "ack received", "acknowledged", "got it",
    "thanks", "thank you", "thx", "ty", "understood", "roger", "will do",
    "sounds good", "ok thanks", "okay thanks", "👍"})


_MEMO = {}


def here():
    """Directory holding this script, resolved on first use so import does no filesystem work."""
    if "here" not in _MEMO:
        _MEMO["here"] = Path(__file__).resolve().parent
    return _MEMO["here"]


def _memo_re(key, pattern, flags=0):
    if key not in _MEMO:
        _MEMO[key] = re.compile(pattern, flags)
    return _MEMO[key]


def label_prefix():
    """Reply-label pattern ("[peer] " / "[reply] "), compiled on first use."""
    return _memo_re("label", r"^(\[[^\]]*\]\s*)+")


def _from_agent_trailer():
    return _memo_re("trailer", r"\s*From agent \S+, not the PM\..*$", re.S)


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, here() / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SIBLINGS = {}


def _sibling(filename, modname):
    if modname not in _SIBLINGS:
        _SIBLINGS[modname] = _load(filename, modname)
    return _SIBLINGS[modname]


def _roster():
    """The unimatrix-roster module, loaded on first use."""
    return _sibling("unimatrix-roster.py", "unimatrix_roster")


def _contract():
    """The peer contract module, loaded on first use."""
    return _sibling("unimatrix-peer-contract.py", "unimatrix_peer_contract")


def default_peers_file():
    """The peers file a default invocation reads."""
    return _roster().settings_home() / "unimatrix" / "peers.json"


def default_legs_dir():
    """Where the agy launch hook writes one leg record per conversation."""
    return _roster().settings_home() / "unimatrix" / "agy-legs"


def load_leg_peers(legs_dir, taken_names=(), taken_targets=()):
    """Peers for every live agy leg record in `legs_dir`: name agy-<conversation>, target the conversation.

    A record whose leg pid is dead, whose name or conversation is already `taken_*` (peers.json
    wins), or that is unreadable is skipped.
    """
    peers = []
    try:
        names = sorted(os.listdir(legs_dir))
    except OSError:
        return peers
    for f in names:
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(legs_dir, f), encoding="utf-8") as fh:
                rec = json.load(fh)
            conv, pid = rec["conversation"], int(rec["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(conv, str) or not conv or conv in taken_targets or not _roster().pid_alive(pid):
            continue
        name = readable_peer_name("agy", conv, rec.get("cwd"))
        if name in taken_names:
            continue
        peer = {"name": name, "vendor": "agy", "target": conv}
        if rec.get("cwd"):
            peer["cwd"] = rec["cwd"]
        peers.append(peer)
    return peers


def default_codex_locks_dir():
    """Where Codex keeps one writer lock per live thread: $CODEX_HOME (else ~/.codex)/thread-writer-locks."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "thread-writer-locks"


def default_presence_dir():
    """Where agy keeps one presence lock per live conversation: next to its brain root (AGY_BRAIN_DIR)."""
    brain = os.environ.get("AGY_BRAIN_DIR")
    root = Path(brain).parent if brain else Path.home() / ".gemini" / "antigravity-cli"
    return root / "presence"


def readable_peer_name(vendor, target, cwd=None):
    """Stable routing name carrying vendor, repository, session identity and box."""
    cwd = os.fsdecode(cwd) if isinstance(cwd, (str, os.PathLike)) else ""
    def clean(value):
        return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-")
    base = str(cwd or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if cwd:
        path = Path(cwd)
        # Match the SessionStart hook: a session in repo/Plugins/Feature belongs to repo.
        # Foreign-platform paths still retain their normalized basename as a fallback.
        if path.is_absolute():
            try:
                root = next((p for p in (path, *path.parents) if (p / ".git").exists()), None)
                if root is not None:
                    base = root.name
            except OSError:
                pass
    import hashlib
    short = hashlib.sha256(target.encode()).hexdigest()[:10]
    return f"{vendor}-{clean(base) or 'session'}-{short}-on-{clean(socket.gethostname()).lower()}"


def codex_thread_metadata(home):
    """Read available identity fields without modifying Codex's database or requiring its newest schema."""
    import sqlite3
    from contextlib import closing
    databases = []
    for db in Path(home).glob("state_*.sqlite"):
        version = re.fullmatch(r"state_(\d+)\.sqlite", db.name)
        if version:
            databases.append((int(version.group(1)), db))
    for _, db in sorted(databases, reverse=True):
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as conn:
                available = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
                columns = [name for name in ("id", "cwd", "source", "thread_source", "agent_path")
                           if name in available]
                if "id" not in columns or "cwd" not in columns:
                    continue
                return {row[0]: dict(zip(columns, row))
                        for row in conn.execute("SELECT " + ", ".join(columns) + " FROM threads")}
        except (sqlite3.Error, ValueError):
            continue
    return {}


def codex_cwds(home):
    """Thread cwd mapping retained for callers that only need repository names."""
    return {target: row["cwd"] for target, row in codex_thread_metadata(home).items()}


def codex_is_subagent(metadata):
    """Spawned child agents belong to their owner and cannot accept independently queued input."""
    if metadata.get("thread_source") == "subagent":
        return True
    source = metadata.get("source")
    if source == "subagent":
        return True
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            source = None
    if isinstance(source, dict) and "subagent" in source:
        return True
    path = metadata.get("agent_path")
    return isinstance(path, str) and path.startswith("/root/")


def codex_is_sendable_root(metadata):
    """Positive local identity evidence only; a held lock/address is not root proof.

    Native source='vscode', thread_source='user', agent_path=NULL is an observed
    root. CLI roots use the same user discriminator. Missing/unknown source,
    child source and contradictory fields are not independently sendable.
    """
    if not isinstance(metadata, dict) or codex_is_subagent(metadata):
        return False
    source = metadata.get('source')
    if isinstance(source, str) and source not in ('cli', 'vscode'):
        try:
            source = json.loads(source)
        except ValueError:
            return False
    if not isinstance(source, str) or source not in ('cli', 'vscode'):
        return False
    if metadata.get('thread_source') not in (None, 'user'):
        return False
    return metadata.get('agent_path') in (None, '/root')


def lock_held(path):
    """True while another process holds any flock on `path`; a free or unreadable file is not live.

    Probes with an exclusive lock so a holder in shared mode still reads as held. Without
    `fcntl` (Windows) nothing is discoverable, so nothing is live.
    """
    try:
        import fcntl
    except ImportError:
        if sys.platform != "win32":
            return False
        import msvcrt
        try:
            with open(path, "r+b") as lock:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    import errno
                    return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                return False
        except OSError:
            return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def load_lock_peers(lock_dir, vendor, taken_names=(), taken_targets=()):
    """Peers for every held `<id>.lock` in `lock_dir`: name <vendor>-<id>, target the id.

    Lock files outlive their holder, so a file nobody holds is stale and skipped; so is a name or
    id already `taken_*` (peers.json wins) and any dotfile.
    """
    peers = []
    try:
        names = sorted(os.listdir(lock_dir))
    except OSError:
        return peers
    metadata = codex_thread_metadata(Path(lock_dir).resolve().parent) if vendor == "codex" else {}
    cwds = {target: row["cwd"] for target, row in metadata.items()}
    for f in names:
        ident = f[:-len(".lock")]
        if f.startswith(".") or not f.endswith(".lock") or not ident or ident in taken_targets:
            continue
        if vendor == "codex" and not codex_is_sendable_root(metadata.get(ident, {})):
            continue
        legacy = readable_peer_name(vendor, ident, cwds.get(ident))
        address = None
        if vendor == 'codex':
            try:
                address = _contract().read_codex_address(
                    _roster().settings_home() / 'host-sessions' / 'codex', ident, socket.gethostname())
            except _contract().PeerConfigError as exc:
                print(f'unimatrix: refusing Codex address: {exc}', file=sys.stderr)
                continue
        name = address['name'] if address else legacy
        if name in taken_names:
            if address:
                print(f'unimatrix: refusing engine alias conflict: {name}', file=sys.stderr)
            continue
        if not lock_held(os.path.join(lock_dir, f)):
            continue
        peer = {"name": name, "vendor": vendor, "target": ident}
        if address:
            peer.update(address)
            peer['aliases'] = [alias for alias in (legacy, f'codex-{ident}') if _contract().safe_alias(alias) and alias != name]
        if cwds.get(ident):
            peer["cwd"] = cwds[ident]
        peers.append(peer)
    return peers


SEND_SCRIPT = "unimatrix-send.py"
NO_REPLY_MARKERS = {"[unimatrix-peer-codex] no reply before timeout"}
NOT_DELIVERED_PREFIXES = ("[unimatrix-peer-agy] not delivered", "[unimatrix-peer-agy] delivery failed",
                          "delivery failed:")


def not_delivered(reply):
    """True when an adapter reply says the message never reached the peer."""
    return str(reply or "").lstrip().startswith(NOT_DELIVERED_PREFIXES)


def wrap_inbound(sender, body):
    """What a peer sees: `[<sender>] <body>` plus a trailer naming the sender and the exact reply command."""
    # Run through the interpreter, never the exec bit: Windows has none (check_posix_exec_assumptions).
    send = f"{shlex.quote(sys.executable)} {shlex.quote(str(here() / SEND_SCRIPT))}"
    return (f"[{sender}] {body}\n\nFrom agent {sender}, not the PM. "
            f"Never answer them in plain text to the human at your keyboard. "
            f"Reply to them with: {send} --to {shlex.quote(str(sender))} '<your reply>'")


def is_ack(text):
    """True for an empty body or a bare acknowledgement ("ok", "got it", "thanks!"), which nobody answers."""
    t = _from_agent_trailer().sub("", str(text))
    t = " ".join(t.lower().split())
    t = label_prefix().sub("", t).strip(" .!?,;:-")
    return t in ACK_BODIES


def default_state_dir():
    return _roster().settings_home() / "unimatrix" / "router"


def endpoint_path(seed, endpoint_dir=None):
    """Endpoint for `seed`: roster.local_endpoint, or <endpoint_dir>/<hash>.sock when overridden."""
    if endpoint_dir and sys.platform == "win32":
        seed = f"{os.path.abspath(endpoint_dir)}:{seed}"
    ep = _roster().local_endpoint(seed)
    if endpoint_dir and sys.platform != "win32":
        return os.path.join(endpoint_dir, os.path.basename(ep))
    return ep


def peer_endpoint(name, endpoint_dir=None):
    return endpoint_path(f"unimatrix-peer-{name}", endpoint_dir)


def _private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)


def bind_private_server(path):
    """Bind a unix socket file that is 0600 from creation."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    _private_dir(os.path.dirname(path))
    old = os.umask(0o177)
    try:
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(path)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    srv.listen(32)
    srv.setblocking(False)
    return srv


def _pipe_call(fn, *args, **kwargs):
    """Give callers the same OSError contract as Unix socket operations."""
    import pywintypes
    try:
        return fn(*args, **kwargs)
    except pywintypes.error as exc:
        raise OSError(exc.winerror, str(exc)) from exc


def request_line(endpoint, request, timeout=SEND_TIMEOUT):
    """Send one JSON line to a local endpoint and return the one-line JSON answer."""
    if endpoint.startswith("\\\\.\\pipe\\"):
        import win32pipe
        import win32file
        deadline = time.monotonic() + timeout
        _pipe_call(win32pipe.WaitNamedPipe, endpoint, max(1, int(timeout * 1000)))
        handle = _pipe_call(win32file.CreateFile, endpoint, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                                      0, None, win32file.OPEN_EXISTING, 0, None)
        try:
            _pipe_call(win32file.WriteFile, handle, (json.dumps(request) + "\n").encode())
            buf = bytearray()
            while b"\n" not in buf:
                if time.monotonic() >= deadline:
                    raise TimeoutError("router response timed out")
                if not _pipe_call(win32pipe.PeekNamedPipe, handle, 0)[1]:
                    time.sleep(0.01)
                    continue
                buf.extend(_pipe_call(win32file.ReadFile, handle, 65536)[1])
                if len(buf) > FRAME_LIMIT:
                    raise ValueError("router response exceeds frame limit")
            return json.loads(buf.split(b"\n", 1)[0])
        finally:
            handle.Close()
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(endpoint)
        s.sendall((json.dumps(request) + "\n").encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.split(b"\n", 1)[0] or b'{"ok":false,"error":"closed"}')


class PipeServer:
    """Byte-mode named pipes with a protected DACL granting only the current user access.

    Blocking Win32 I/O stays on daemon threads; handlers run on the owning asyncio loop.
    """

    def __init__(self, endpoint, handler):
        import pywintypes
        import win32api
        import win32con
        import win32pipe
        import win32security
        self.endpoint, self.handler = endpoint, handler
        self.loop = asyncio.get_running_loop()
        self.closed = False
        self.pending = None
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        acl = win32security.ACL()
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, sid)
        sd = win32security.SECURITY_DESCRIPTOR()
        sd.SetSecurityDescriptorDacl(1, acl, 0)
        sd.SetSecurityDescriptorControl(win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED)
        self.security = pywintypes.SECURITY_ATTRIBUTES()
        self.security.SECURITY_DESCRIPTOR = sd
        # Bind synchronously so a successful constructor means the endpoint exists.
        self.pending = self._new_pipe()
        threading.Thread(target=self._accept, daemon=True).start()

    def _new_pipe(self):
        import win32pipe
        return win32pipe.CreateNamedPipe(
            self.endpoint, win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT
            | getattr(win32pipe, "PIPE_REJECT_REMOTE_CLIENTS", 8),
            win32pipe.PIPE_UNLIMITED_INSTANCES, 65536, 65536, 0, self.security)

    def _accept(self):
        import pywintypes
        import win32pipe
        while True:
            handle = self.pending
            try:
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except pywintypes.error as exc:
                    if exc.winerror != 535:  # client connected before ConnectNamedPipe
                        raise
                if self.closed:
                    handle.Close()
                    return
                self.pending = self._new_pipe()
                threading.Thread(target=self._read, args=(handle,), daemon=True).start()
            except (OSError, pywintypes.error):
                if not self.closed:
                    self.loop.call_exception_handler({"message": "named pipe accept failed"})
                return

    def _read(self, handle):
        import win32file
        import pywintypes
        try:
            raw = bytearray()
            while True:
                raw.extend(win32file.ReadFile(handle, 65536)[1])
                if len(raw) > FRAME_LIMIT:
                    return
                while b"\n" in raw:
                    first, rest = raw.split(b"\n", 1)
                    # Auth headers can be separate writes or share a write with the user frame.
                    parsed = json.loads(first)
                    if isinstance(parsed, dict) and parsed.get("type") == "auth":
                        raw = bytearray(rest)
                        continue
                    break
                else:
                    continue
                break
            reader = asyncio.StreamReader(limit=FRAME_LIMIT, loop=self.loop)
            reader.feed_data(bytes(raw))
            reader.feed_eof()

            class Writer:
                def __init__(self):
                    self.pending = bytearray()

                def write(self, data):
                    self.pending.extend(data)

                async def drain(self):
                    data, self.pending = bytes(self.pending), bytearray()
                    if data:
                        await asyncio.to_thread(_pipe_call, win32file.WriteFile, handle, data)

                def close(self):
                    pass

            asyncio.run_coroutine_threadsafe(self.handler(reader, Writer()), self.loop).result()
        except (OSError, ValueError, pywintypes.error):
            pass
        finally:
            handle.Close()

    def close(self):
        self.closed = True
        # Wake the synchronous accept; its thread owns and closes the pending handle.
        import win32file
        import pywintypes
        try:
            handle = win32file.CreateFile(self.endpoint,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None)
            handle.Close()
        except (OSError, pywintypes.error):
            pass



async def start_private_server(handler, endpoint):
    if endpoint.startswith("\\\\.\\pipe\\"):
        return PipeServer(endpoint, handler)
    srv = bind_private_server(endpoint)
    try:
        return await asyncio.start_unix_server(handler, sock=srv, limit=FRAME_LIMIT)
    except BaseException:
        srv.close()
        raise


def install_stop_signals(stop):
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        if sys.platform == "win32":
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        else:
            loop.add_signal_handler(sig, stop.set)


class Registration:
    """A registry entry plus its 0600 key file for one endpoint, keyed by the writing pid."""

    def __init__(self, name, endpoint, sessions_dir, cwd=None, vendor=None, peer=None):
        native_target = None
        if vendor == 'codex':
            native_target = peer.get('target') if isinstance(peer, dict) else None
            if (not isinstance(peer, dict) or peer.get('vendor') != 'codex' or peer.get('name') != name
                    or not isinstance(native_target, str)
                    or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', native_target)
                    or peer.get('session_id') not in (None, native_target)):
                raise ValueError('Codex face requires its accepted peer native target UUID')
        self.pid = os.getpid()
        self.endpoint = endpoint
        self.sessions_dir = sessions_dir
        self.token = secrets.token_hex(16)
        self.entry = os.path.join(sessions_dir, f"{self.pid}.json")
        self.key = os.path.join(sessions_dir, _roster().key_name(self.pid, endpoint))
        now = int(time.time() * 1000)
        self.data = {
            "pid": self.pid, "sessionId": f"peer-{name}-{self.pid}", "cwd": cwd or os.getcwd(),
            "startedAt": now, "procStart": _roster().proc_start(self.pid), "peerProtocol": 1,
            "peerFeatures": [], "kind": "interactive", "entrypoint": "cli",
            "pidDomain": _roster().pid_domain(), "messagingSocketPath": endpoint, "name": name,
            "nameSource": "derived", "nameSince": now, "status": "idle", "updatedAt": now,
            "statusUpdatedAt": now,
            "vendor": vendor or "unknown", "box": socket.gethostname().lower(),
        }
        if native_target is not None:
            # Explicit router-owned binding, also for legacy display names.
            # Do not invent an engine-owned alias, role or parent identity.
            self.data['target'] = native_target
        if isinstance(peer, dict) and 'address_version' in peer:
            address = _contract().owned_codex_address(peer, peer.get('target'))
            if vendor != 'codex' or address['name'] != name:
                raise _contract().PeerConfigError('face does not match engine-owned Codex address')
            self.data.update(address, target=peer['target'], aliases=list(peer.get('aliases', [])), nameSource='engine')

    def write(self):
        _private_dir(self.sessions_dir)
        fd = os.open(self.key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"peerToken": self.token, ("procStartFt" if sys.platform == "win32" else "procStart"): self.data["procStart"],
                       "pidDomain": self.data["pidDomain"]}, f)
            f.write("\n")
        _roster().write_json_atomic(self.entry, self.data)

    def remove(self):
        for p in (self.entry, self.key):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


def sweep_pid_files(sessions_dir, pid):
    """Delete every `<pid>.*` registry file (the roster.unmirror naming rule), killing nothing."""
    try:
        names = os.listdir(sessions_dir)
    except FileNotFoundError:
        return
    for f in names:
        if f.startswith(f"{pid}."):
            try:
                os.remove(os.path.join(sessions_dir, f))
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------ face (registration child)

async def run_face(name, endpoint, forward, sessions_dir, cwd=None, transport=False, vendor=None, peer=None):
    """Register `name`, take inbound frames on `endpoint`, forward each to the router."""
    reg = Registration(name, endpoint, sessions_dir, cwd=cwd, vendor=vendor, peer=peer)
    if transport:
        reg.data.update(unimatrixTransport=True, vendor="transport")

    async def on_conn(reader, writer):
        try:
            raw = await reader.read()
            if not raw.strip():
                return  # liveness probe
            for line in raw.splitlines():
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("op") == "deliver":
                    # The receiving harness checks the connecting PID against the
                    # registration for frame.from, so the registered face must write.
                    try:
                        outgoing = frame["frame"]
                        if not isinstance(outgoing, dict) or outgoing.get("from") != f"uds:{endpoint}":
                            raise ValueError("outbound frame must originate from this face")
                        await asyncio.to_thread(deliver, frame["endpoint"],
                                                (json.dumps(outgoing) + "\n").encode("utf-8"), sessions_dir)
                        response = {"ok": True}
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        response = {"ok": False, "error": f"face delivery failed: {exc}"}
                    writer.write((json.dumps(response) + "\n").encode("utf-8"))
                    await writer.drain()
                    return
                if frame.get("type") == "user":
                    try:
                        await asyncio.to_thread(request_line, forward,
                                                {"op": "inbound", "peer": name, "frame": frame}, 15)
                    except (OSError, ValueError):
                        pass
        finally:
            writer.close()

    server = await start_private_server(on_conn, endpoint)
    reg.write()
    stop = asyncio.Event()
    install_stop_signals(stop)
    try:
        await stop.wait()
    finally:
        server.close()
        reg.remove()


# ------------------------------------------------------------------ router

def default_adapters():
    """vendor -> async fn(peer, text) -> reply text, loading the adapters on first use."""
    cache = {}

    def mod(filename, key):
        if key not in cache:
            cache[key] = _load(filename, f"unimatrix_peer_{key}")
        return cache[key]

    async def codex(peer, text):
        m = mod("unimatrix-peer-codex.py", "codex")
        return await m.codex_turn(m.socket_path(), peer["target"], text, tui_held=bool(peer.get("tui")))

    async def agy(peer, text):
        m = mod("unimatrix-peer-agy.py", "agy")
        return await m.agy_turn(m.leg_endpoint(peer["target"]), m.transcript_path(peer["target"]), text)

    return {"codex": codex, "agy": agy}


class Router:
    """Owns one face child per peer, the control endpoint and the turn locks."""

    def __init__(self, peers, sessions_dir=None, state_dir=None, endpoint_dir=None,
                 adapters=None, ready_seconds=READY_SECONDS, legs_dir=None,
                 codex_locks_dir=None, presence_dir=None):
        peers = list(peers)
        self.codex_home = Path(codex_locks_dir or default_codex_locks_dir()).resolve().parent
        metadata = codex_thread_metadata(self.codex_home) if any(p.get('vendor') == 'codex' for p in peers) else {}
        self.refused_codex_addresses = set()
        accepted = []
        for peer in peers:
            if peer.get('vendor') == 'codex' and not codex_is_sendable_root(metadata.get(peer.get('target'), {})):
                self.refused_codex_addresses.update([peer['name'], peer['target'], 'codex-' + peer['target'], *peer.get('aliases', [])])
                continue
            accepted.append(peer)
        self.peers = {p["name"]: p for p in accepted}
        self.sessions_dir = str(sessions_dir or _roster().sessions_dir())
        self.state_dir = str(state_dir or default_state_dir())
        self.endpoint_dir = str(endpoint_dir) if endpoint_dir else None
        self.adapters = adapters or default_adapters()
        self.ready_seconds = ready_seconds
        self.locks = {n: asyncio.Lock() for n in self.peers}
        self.ctl = endpoint_path(ROUTER_ENDPOINT_SEED, self.endpoint_dir)
        self.log_path = os.path.join(self.state_dir, "unimatrix-peer-router.log")
        self.faces = {}  # peer name -> Popen
        self.addressed = {}  # peer name -> names it sent to during its open inbound turn
        self.server = None
        self.tasks = set()
        self.legs_dir = str(legs_dir) if legs_dir else None
        self.codex_locks_dir = str(codex_locks_dir) if codex_locks_dir else None
        self.presence_dir = str(presence_dir) if presence_dir else None
        self.discovered = set()  # names found by a scan, not given at construction; only these retire
        self.scanner = None
        self.transport_sources = {}  # infrastructure face name -> registered caller name or None
        self.transport_targets = {}  # each infrastructure face is one source/destination conversation
        self.transport_lock = asyncio.Lock()

    def log(self, *a):
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
        except OSError:
            pass

    def endpoint_of(self, name):
        return peer_endpoint(name, self.endpoint_dir)

    def peer_endpoints(self):
        return {self.endpoint_of(n) for n in self.peers}

    # -- lifecycle

    async def _wait(self, what, ready, proc=None):
        deadline = time.monotonic() + self.ready_seconds
        while not ready():
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(f"{what} exited with {proc.returncode} before it was ready")
            if time.monotonic() > deadline:
                raise RuntimeError(f"{what} not ready within {self.ready_seconds}s")
            await asyncio.sleep(0.05)

    async def start(self):
        try:
            _private_dir(self.state_dir)
            if sys.platform != "win32":
                _private_dir(os.path.dirname(self.ctl))
            self.server = await self._bind_control()
            for name in list(self.peers):
                await self._spawn_face(name)
            if self.legs_dir or self.codex_locks_dir or self.presence_dir:
                await self.refresh_discovered()
                self.scanner = asyncio.create_task(self._scan_discovered())
        except BaseException:
            await self.stop()
            raise

    def _live_discovered(self):
        """name -> peer for every live agy leg, agy presence lock and Codex writer lock.

        Peers given at construction (peers.json) are `taken`: they win a name or target clash.
        """
        fixed = [p for n, p in self.peers.items() if n not in self.discovered]
        names = {p["name"] for p in fixed}
        targets = {p["target"] for p in fixed}
        live = {}
        owned = {p['target']: p for n, p in self.peers.items()
                 if n in self.discovered and p.get('vendor') == 'codex' and p.get('address_version') == 1}
        scans = []
        if self.legs_dir:
            scans.append(lambda tn, tt: load_leg_peers(self.legs_dir, tn, tt))
        if self.presence_dir:
            scans.append(lambda tn, tt: load_lock_peers(self.presence_dir, "agy", tn, tt))
        if self.codex_locks_dir:
            scans.append(lambda tn, tt: load_lock_peers(self.codex_locks_dir, "codex", tn, tt))
        for scan in scans:
            for peer in scan(names | set(live), targets):
                prior = owned.get(peer['target'])
                if prior and peer.get('vendor') == 'codex':
                    if peer.get('address_version') == 1 and peer['name'] != prior['name']:
                        self.log('ADDRESS-CONFLICT', peer['target'], prior['name'], peer['name'])
                        continue
                    if 'address_version' not in peer:
                        peer = {**peer, **prior}  # default refresh cannot replace an owned address
                if peer['name'] in names or peer['name'] in live:
                    self.log('ADDRESS-CONFLICT', peer['target'], peer['name'])
                    continue
                live.setdefault(peer["name"], peer)
                # Rich leg records win over the same conversation's presence lock.
                # Their cwd can produce a different display name for the same target.
                targets.add(peer["target"])
        return live

    async def refresh_discovered(self):
        """Register a face for each newly live discovered peer and retire the ones whose record or lock went."""
        live = await asyncio.to_thread(self._live_discovered)
        for name in sorted(self.discovered - set(live)):
            await self._retire(name)
        for name, peer in live.items():
            if name in self.peers:
                continue
            if peer["vendor"] == "codex":
                peer["tui"] = True  # a held writer lock is a TUI, so the turn goes through `codex queue`
            self.peers[name] = peer
            self.locks[name] = asyncio.Lock()
            self.discovered.add(name)
            try:
                await self._spawn_face(name)
            except Exception as e:
                self.log("DISCOVER-REGISTER-FAILED", name, type(e).__name__, e)
                await self._retire(name)

    async def _retire(self, name):
        """Drop a discovered peer: stop its face, remove its registry files and endpoint."""
        if name in self.faces:
            await self._stop_face(name)
        _unlink(self.endpoint_of(name))
        self.peers.pop(name, None)
        self.locks.pop(name, None)
        self.discovered.discard(name)
        self.log("retired", name)

    async def _scan_discovered(self):
        while True:
            await asyncio.sleep(LEG_SCAN_SECONDS)
            try:
                await self.refresh_discovered()
            except Exception as e:
                self.log("DISCOVER-SCAN-FAILED", type(e).__name__, e)

    async def _spawn_face(self, name):
        ep = self.endpoint_of(name)
        proc = _roster().spawn([sys.executable, str(Path(__file__).resolve()), "face",
                                "--name", name, "--endpoint", ep, "--forward", self.ctl,
                                "--sessions-dir", self.sessions_dir,
                                "--vendor", self.peers.get(name, {}).get("vendor") or "transport",
                                "--peer-json", json.dumps(self.peers.get(name, {})),
                                "--cwd", self.peers.get(name, {}).get("cwd") or os.getcwd()]
                               + (["--transport"] if name in self.transport_sources else []))
        self.faces[name] = proc
        await self._wait(f"face {name}",
                         lambda p=proc: os.path.exists(os.path.join(self.sessions_dir, f"{p.pid}.json")),
                         proc)
        self.log("registered", name, "pid", proc.pid, ep)

    async def restart_face(self, name):
        """Replace `name`'s registration child; the new pid and key sit behind the same name and endpoint.

        Nothing supervises faces: a face that dies on its own stays dead until a caller invokes this.
        """
        await self._stop_face(name)
        await self._spawn_face(name)

    async def _stop_face(self, name):
        old = self.faces.pop(name)
        if old.poll() is None:
            old.terminate()
        try:
            await asyncio.to_thread(old.wait, 3)
        except Exception:
            _roster().tree_kill(old.pid)
            await asyncio.to_thread(old.wait, 3)
        sweep_pid_files(self.sessions_dir, old.pid)

    async def _bind_control(self):
        return await start_private_server(self._on_ctl, self.ctl)

    async def stop(self):
        """Remove every entry, key, endpoint, key file and child this router wrote."""
        if self.scanner is not None:
            self.scanner.cancel()
            self.scanner = None
        if self.server is not None:
            self.server.close()
            self.server = None
        for t in list(self.tasks):
            t.cancel()
        procs = [(n, p) for n, p in self.faces.items()]
        for _, p in procs:
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                await asyncio.to_thread(p.wait, 3)
            except Exception:
                _roster().tree_kill(p.pid)  # stuck: only here is the pid still ours, not recycled
                await asyncio.to_thread(p.wait, 3)
            sweep_pid_files(self.sessions_dir, p.pid)
        for name in self.faces:
            _unlink(self.endpoint_of(name))
        self.faces = {}
        _unlink(self.ctl)

    # -- routing

    async def _on_ctl(self, reader, writer):
        try:
            line = await reader.readline()
            resp = await self._dispatch(json.loads(line)) if line.strip() else {"ok": True}
        except (ValueError, OSError) as e:
            resp = {"ok": False, "error": f"bad request: {e}"}
        try:
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    async def _dispatch(self, req):
        op = req.get("op") if isinstance(req, dict) else None
        if op == "inbound":
            peer = req.get("peer")
            if not isinstance(req.get("frame"), dict):
                return {"ok": False, "error": "unknown peer or frame"}
            if peer in self.transport_sources:
                handler = self.handle_transport_inbound
            elif peer in self.peers:
                handler = self.handle_inbound
            else:
                return {"ok": False, "error": "unknown peer or frame"}
            if peer in self.peers and self.peers[peer].get('vendor') == 'codex':
                metadata = codex_thread_metadata(self.codex_home)
                if not codex_is_sendable_root(metadata.get(self.peers[peer].get('target'), {})):
                    return {'ok': False, 'error': 'Codex target is not an independently sendable root'}
            t = asyncio.create_task(handler(peer, req["frame"]))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)
            return {"ok": True}
        if op == "list":
            details = await asyncio.to_thread(self.session_details)
            sessions = sorted(details)
            details.update({name: {"vendor": peer["vendor"], "box": socket.gethostname().lower(),
                                   "cwd": peer.get("cwd") or ""}
                            for name, peer in self.peers.items()})
            return {"ok": True, "peers": sorted(self.peers), "sessions": sessions,
                    "details": details, "ready": await asyncio.to_thread(self.peer_readiness)}
        if op == "send":
            peer, text = req.get("to"), req.get("text")
            try:
                peer = self.pick_peer(peer) or peer
            except ValueError as exc:
                return {'ok': False, 'error': str(exc)}
            # A peer's own shell knows its thread/conversation id, not the name the router gave it.
            by_target = {p.get("target"): n for n, p in self.peers.items()}
            sender = by_target.get(req.get("as_target")) or str(req.get("as") or "cli")
            if peer not in self.peers and isinstance(peer, str):
                if not isinstance(text, str) or is_ack(text):
                    return {"ok": False, "error": "empty or acknowledgement text is not delivered"}
                if sender in self.peers:
                    return await self._peer_send_to_session(sender, peer, text)
                return await self._transport_send_to_session(sender, peer, text)
            if peer not in self.peers:
                return {"ok": False, "error": f"no peer named {peer}", "peers": sorted(self.peers)}
            if not isinstance(text, str) or is_ack(text):
                return {"ok": False, "error": "empty or acknowledgement text is not delivered"}
            reply = await self.turn(peer, wrap_inbound(sender, text))
            if not_delivered(reply):
                return {"ok": False, "error": str(reply).strip()}
            return {"ok": True, "reply": _contract().label(peer, reply)}
        return {"ok": False, "error": "unknown op"}

    def pick_peer(self, address):
        """Project safe legacy aliases and the native target onto one peer, never retarget."""
        if not isinstance(address, str):
            return None
        if address in self.refused_codex_addresses:
            raise ValueError('Codex target is not an independently sendable root')
        matches = [name for name, peer in self.peers.items() if address == name
                   or address == peer.get('target') or address in peer.get('aliases', [])
                   or (peer.get('vendor') == 'codex' and address == 'codex-' + peer.get('target', ''))]
        owners = {(self.peers[name]['vendor'], self.peers[name]['target']) for name in matches}
        if len(owners) > 1:
            raise ValueError(f'peer alias conflict for {address!r}')
        if address in matches:
            return address
        return next(iter(matches), None)

    def peer_readiness(self):
        """peer name -> True when a send can reach it now; an agy peer is ready only while its leg endpoint exists."""
        ready = {}
        metadata = codex_thread_metadata(self.codex_home) if any(p.get('vendor') == 'codex' for p in self.peers.values()) else {}
        for name, p in self.peers.items():
            ok = p.get('vendor') != 'codex' or codex_is_sendable_root(metadata.get(p.get('target'), {}))
            if p.get("vendor") == "agy" and os.name != "nt":
                m = _sibling("unimatrix-peer-agy.py", "unimatrix_peer_agy_ready")
                ok = os.path.exists(m.leg_endpoint(p["target"]))
            ready[name] = ok
        return ready

    async def turn(self, peer, text, addressed=None):
        """One adapter turn for `peer` under its lock; a failure becomes the reply text.

        `addressed`, when given, collects the names the peer sent to by its own addressed send
        during this turn, so the caller can skip the harvested reply for those.
        """
        p = self.peers[peer]
        async with self.locks[peer]:
            if addressed is not None:
                self.addressed[peer] = addressed
            try:
                if p["vendor"] == "codex":
                    metadata = await asyncio.to_thread(codex_thread_metadata, self.codex_home)
                    if not codex_is_sendable_root(metadata.get(p['target'], {})):
                        return 'delivery failed: Codex target is not an independently sendable root'
                if p["vendor"] == "codex" and not p.get("tui") and self.thread_held(p["target"]):
                    p = {**p, "tui": True}
                return await self.adapters[p["vendor"]](p, text)
            except Exception as e:
                self.log("TURN-FAILED", peer, type(e).__name__, e)
                return f"delivery failed: {e}"
            finally:
                if addressed is not None:
                    self.addressed.pop(peer, None)

    def thread_held(self, thread_id):
        """True while a process holds the Codex writer lock of `thread_id`, so a TUI owns the thread.

        The route follows the holder, never whether the app-server socket connects: that daemon
        is up beside a TUI and rejects a turn on a thread the TUI writes.
        """
        if not self.codex_locks_dir or not thread_id:
            return False
        return lock_held(os.path.join(self.codex_locks_dir, f"{thread_id}.lock"))

    def session_names(self):
        """Names of the live roster sessions that are not the router's own peers."""
        names = set()
        try:
            files = os.listdir(self.sessions_dir)
        except FileNotFoundError:
            return []
        for f in files:
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.sessions_dir, f), encoding="utf-8") as fh:
                    e = json.load(fh)
            except (OSError, ValueError):
                continue
            if (isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"] not in self.peers and e["name"] not in self.transport_sources
                    and isinstance(e.get("pid"), int) and _roster().pid_alive(e["pid"]) and self.external_codex_sendable(e)):
                names.add(e["name"])
        return sorted(names)

    def session_details(self):
        """Identity metadata for external sessions, keyed by their unchanged routable names."""
        details, latest = {}, {}
        try:
            files = os.listdir(self.sessions_dir)
        except FileNotFoundError:
            return details
        for filename in files:
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.sessions_dir, filename), encoding="utf-8") as file:
                    entry = json.load(file)
            except (OSError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            name, pid = entry.get("name"), entry.get("pid")
            if (not isinstance(name, str) or name in self.peers or name in self.transport_sources
                    or entry.get("unimatrixTransport") or not isinstance(pid, int)
                    or not _roster().pid_alive(pid) or not self.external_codex_sendable(entry)):
                continue
            rank = entry.get("startedAt") or 0
            if name in latest and latest[name] > rank:
                continue
            latest[name] = rank
            details[name] = {**_roster().session_identity(entry), "status": entry.get("status") or "unknown"}
        return details

    async def _transport_face(self, source, destination):
        """Lazily register a genuine transport sender for one source/destination conversation."""
        name = readable_peer_name("unimatrix", source + "\0" + destination, "transport")
        async with self.transport_lock:
            self.transport_sources[name] = source if self.resolve_sender(source) else None
            self.transport_targets[name] = destination
            process = self.faces.get(name)
            if process is not None and process.poll() is not None:
                await self._stop_face(name)
                process = None
            if process is None:
                try:
                    await self._spawn_face(name)
                except BaseException:
                    self.transport_sources.pop(name, None)
                    self.transport_targets.pop(name, None)
                    raise
        return name

    async def _transport_send_to_session(self, source, to, text):
        endpoint = self.resolve_sender(to)
        if not endpoint:
            return {"ok": False, "error": f"no unique live session named {to}", "peers": sorted(self.peers)}
        try:
            transport = await self._transport_face(source, to)
            content = f"[{source} via Unimatrix transport] {text}"
            frame = _contract().build_frame(self.endpoint_of(transport), transport, content)
            frame = _contract().stamp(frame, transport, "transport", source, os.getpid())
            await self._deliver_as_peer(transport, endpoint, frame)
        except Exception as exc:
            return {"ok": False, "error": f"transport delivery to {to} failed: {exc}"}
        self.log("SENT-TRANSPORT", source, "->", to, frame["msg_id"])
        return {"ok": True, "msg_id": frame["msg_id"], "sender": transport}

    async def handle_transport_inbound(self, transport, frame):
        """Bridge both reply directions without pretending to be either endpoint's vendor."""
        source = self.transport_sources.get(transport)
        if not source:
            self.log("NO-TRANSPORT-RETURN", transport)
            return
        sender_endpoint, sender, body = _contract().parse_inbound(frame)
        if is_ack(body):
            return
        source_endpoint = self.resolve_sender(source)
        from_source = bool(source_endpoint and sender_endpoint == source_endpoint) if sender_endpoint else sender == source
        destination = self.transport_targets[transport] if from_source else source
        endpoint = self.resolve_sender(destination)
        if not endpoint:
            self.log("NO-TRANSPORT-RETURN", transport)
            return
        outgoing = _contract().build_frame(self.endpoint_of(transport), transport,
                                          f"[forwarded message from {sender}] {body}")
        outgoing = _contract().stamp(outgoing, transport, "transport", source, os.getpid())
        try:
            await self._deliver_as_peer(transport, endpoint, outgoing)
        except Exception as exc:
            self.log("TRANSPORT-RETURN-FAILED", transport, type(exc).__name__)

    async def _deliver_as_peer(self, peer, endpoint, frame):
        """Send through the registered face so the OS client PID matches frame.from."""
        response = await asyncio.to_thread(request_line, self.endpoint_of(peer),
            {"op": "deliver", "endpoint": endpoint, "frame": frame}, 15)
        if not response.get("ok"):
            raise OSError(response.get("error", "face delivery failed"))

    async def _peer_send_to_session(self, peer, to, text):
        """A peer's own addressed send to a Claude session `to`; marks the peer's open turn as answered."""
        ep = self.resolve_sender(to)
        if not ep:
            return {"ok": False, "error": f"no session named {to}", "peers": sorted(self.peers)}
        p = self.peers[peer]
        frame = _contract().build_frame(self.endpoint_of(peer), peer, _contract().label(peer, text))
        frame = _contract().stamp(frame, peer, p["vendor"], p["target"], os.getpid())
        try:
            await self._deliver_as_peer(peer, ep, frame)
        except Exception as e:
            self.log("POST-FAILED", peer, "->", to, type(e).__name__, e)
            return {"ok": False, "error": f"delivery to {to} failed: {e}"}
        if peer in self.addressed:
            self.addressed[peer].add(to)
        self.log("SENT", peer, "->", to, frame["msg_id"])
        return {"ok": True, "msg_id": frame["msg_id"]}

    def external_codex_sendable(self, entry):
        """Gate resolved roster identity, never infer native UUID from a face/name/PID.

        Remote mirrors carry a live local bridge generation plus explicit far
        socket/box fields. Their native source belongs to that box, not our DB.
        """
        roster = _roster()
        if roster.session_identity(entry).get('vendor') != 'codex':
            return True
        remote_fields = ('unimatrixRemote', 'macSocketPath', 'unimatrixMirror')
        if any(entry.get(key) for key in remote_fields):
            remote, far_socket, box = (entry.get('unimatrixRemote'), entry.get('macSocketPath'), entry.get('box'))
            pid, far_pid = entry.get('pid'), entry.get('farPid')
            if not (isinstance(remote, str) and remote.strip() and isinstance(far_socket, str) and far_socket
                    and isinstance(box, str) and box.strip() and box.casefold() != socket.gethostname().casefold()
                    and type(pid) is int and pid > 0 and type(far_pid) is int and far_pid > 0):
                return False
            try:
                return (roster.pid_alive(pid) and entry.get('procStart') is not None
                        and entry['procStart'] == roster.proc_start(pid)
                        and entry.get('pidDomain') == roster.pid_domain())
            except (OSError, ValueError, TypeError):
                return False
        target, session_id = entry.get('target'), entry.get('session_id')
        if target is not None and session_id is not None and target != session_id:
            return False
        target = target if target is not None else session_id
        if not isinstance(target, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', target):
            return False
        return codex_is_sendable_root(codex_thread_metadata(self.codex_home).get(target, {}))

    def resolve_sender(self, name, fallback=""):
        """Current messaging endpoint of the live roster session called `name`, else `fallback`.

        Re-read on every reply, so a sender that restarted under a new pid/endpoint keeps its
        name as its address. The router's own faces never match a Claude sender.
        """
        exact, aliases, owners = set(), set(), set()
        owned_alias = False
        refused_identity = False
        try:
            names = os.listdir(self.sessions_dir)
        except FileNotFoundError:
            return fallback
        for f in names:
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.sessions_dir, f), encoding="utf-8") as fh:
                    e = json.load(fh)
            except (OSError, ValueError):
                continue
            if (not isinstance(e, dict) or name not in [e.get("name"), e.get("unimatrixFarName"), *_contract().projected_addresses(e)]
                    or name in self.peers or name in self.transport_sources):
                continue
            ep, pid = e.get("messagingSocketPath"), e.get("pid")
            if not ep or not isinstance(pid, int) or not _roster().pid_alive(pid):
                continue
            if not self.external_codex_sendable(e):
                refused_identity = True
                continue
            owned_alias = owned_alias or name in _contract().projected_addresses(e)
            owners.add((e.get('vendor'), e.get('target') or ep))
            (exact if e.get("name") == name else aliases).add(ep)
        if refused_identity:
            return ''  # Never retarget a refused local identity to another matching row/fallback.
        if owned_alias and len(owners) > 1:
            return ''  # A projected alias cannot override another target's exact name.
        matches = exact or aliases
        if len(matches) > 1:
            return ""  # Never choose an arbitrary box when roster names collide.
        return next(iter(matches)) if matches else fallback

    async def handle_inbound(self, peer, frame):
        sender_ep, sender_name, body = _contract().parse_inbound(frame)
        if is_ack(body) or body.lstrip().lower().startswith("[reply]"):
            self.log("DROPPED-ACK", sender_name, "->", peer)
            return
        addressed = set()
        reply = await self.turn(peer, wrap_inbound(sender_name, body), addressed)
        if not sender_ep:
            return
        if sender_name in addressed:
            self.log("DEDUP-ADDRESSED", peer, "->", sender_name)
            return
        if not str(reply or "").strip():
            self.log("DROPPED-EMPTY-REPLY", peer, "->", sender_name)
            return
        if str(reply).strip() in NO_REPLY_MARKERS:  # the peer is still busy (an approval prompt, say), not answering
            self.log("NO-REPLY-YET", peer, "->", sender_name)
            return
        if sender_ep in self.peer_endpoints() or sender_name in self.peers:
            self.log("NO-REPLY-TO-PEER", peer, "->", sender_name)
            return
        sender_ep = self.resolve_sender(sender_name, sender_ep)
        p = self.peers[peer]
        frame = _contract().build_frame(self.endpoint_of(peer), peer, f"[reply] {peer}: {str(reply).strip()}")
        frame = _contract().stamp(frame, peer, p["vendor"], p["target"], os.getpid())
        try:
            await self._deliver_as_peer(peer, sender_ep, frame)
        except Exception as e:
            self.log("POST-FAILED", peer, "->", sender_name, type(e).__name__, e)
            return
        resp = {"ok": True, "msg_id": frame["msg_id"]}
        self.log("REPLIED", peer, "->", sender_name, resp)


def deliver(endpoint, data, sessions_dir=None):
    """Write an authenticated frame to a registered session's Unix socket or Windows pipe."""
    data = _roster().authenticated_data(endpoint, data, sessions_dir)
    if endpoint.startswith("\\\\.\\pipe\\"):
        import win32file
        import win32pipe
        _pipe_call(win32pipe.WaitNamedPipe, endpoint, 10000)
        handle = _pipe_call(win32file.CreateFile, endpoint, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                                      0, None, win32file.OPEN_EXISTING, 0, None)
        try:
            _pipe_call(win32file.WriteFile, handle, data)
        finally:
            handle.Close()
        return
    s = socket.socket(socket.AF_UNIX)
    try:
        s.settimeout(10)
        s.connect(endpoint)
        s.sendall(data)
    finally:
        s.close()


def _unlink(path):
    if str(path).startswith("\\\\.\\pipe\\"):
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


async def serve(router):
    await router.start()
    stop = asyncio.Event()
    install_stop_signals(stop)
    try:
        await stop.wait()
    finally:
        await router.stop()


def cmd_send(argv):
    p = argparse.ArgumentParser(prog="unimatrix-peer-router.py send")
    p.add_argument("--as", dest="as_name", default=os.environ.get("UNIMATRIX_PEER_NAME", "cli"))
    p.add_argument("--endpoint-dir", default=None)
    p.add_argument("peer")
    p.add_argument("text", nargs="+")
    a = p.parse_args(argv)
    ctl = endpoint_path(ROUTER_ENDPOINT_SEED, a.endpoint_dir)
    try:
        resp = request_line(ctl, {"op": "send", "as": a.as_name, "to": a.peer, "text": " ".join(a.text)})
    except OSError as e:
        print(f"router not reachable at {ctl}: {e}", file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(resp.get("error", "failed"), file=sys.stderr)
        if resp.get("peers"):
            print("peers: " + ", ".join(resp["peers"]), file=sys.stderr)
        return 1
    print(resp["reply"])
    return 0


def cmd_face(argv):
    p = argparse.ArgumentParser(prog="unimatrix-peer-router.py face")
    p.add_argument("--name", required=True)
    p.add_argument("--endpoint", required=True)
    p.add_argument("--forward", required=True)
    p.add_argument("--sessions-dir", required=True)
    p.add_argument("--cwd", default=None)
    p.add_argument("--transport", action="store_true")
    p.add_argument("--vendor", default=None)
    p.add_argument("--peer-json", default='{}')
    a = p.parse_args(argv)
    asyncio.run(run_face(a.name, a.endpoint, a.forward, a.sessions_dir, a.cwd, a.transport, a.vendor,
                         json.loads(a.peer_json)))
    return 0


def cmd_serve(argv):
    p = argparse.ArgumentParser(prog="unimatrix-peer-router.py")
    p.add_argument("--peers", default=None, help="peers.json (default: <settings home>/unimatrix/peers.json)")
    p.add_argument("--sessions-dir", default=None)
    p.add_argument("--state-dir", default=None)
    p.add_argument("--endpoint-dir", default=None)
    a = p.parse_args(argv)
    peers_file = a.peers or default_peers_file()
    try:
        peers = _contract().load_peers(peers_file)
    except FileNotFoundError:
        peers = []  # discovery alone can supply peers
    except (OSError, ValueError, _contract().PeerConfigError) as e:
        print(f"cannot read peers from {peers_file}: {e}", file=sys.stderr)
        return 2
    asyncio.run(serve(Router(peers, a.sessions_dir, a.state_dir, a.endpoint_dir,
                             legs_dir=default_legs_dir(), codex_locks_dir=default_codex_locks_dir(),
                             presence_dir=default_presence_dir())))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "face":
        return cmd_face(argv[1:])
    if argv and argv[0] == "send":
        return cmd_send(argv[1:])
    return cmd_serve(argv)


if __name__ == "__main__":
    sys.exit(main())
