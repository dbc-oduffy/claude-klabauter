"""PROTOTYPE: agent_pipe.codex_app_server -- JSON-RPC client for the codex app-server, over `codex app-server proxy`.

TRAP: the proxy is a byte pipe and codex owns the OS socket (Unix socket or named pipe). The
daemon speaks WebSocket (RFC 6455) over that pipe, so bare newline JSON gets no reply: the client
writes an HTTP upgrade, then exchanges masked client text frames, one JSON-RPC message per frame.
A connection that did not start the thread receives only `thread/status/changed` for it, so a
delivery's replies are read back with `thread/turns/list` once the thread goes idle.
Never `thread/resume`: it fails with "no rollout found" on a loaded thread that has no turn yet.
A thread mid-turn gets `turn/steer` with `expectedTurnId` instead of a second `turn/start`.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import struct
import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CLIENT_INFO = {"name": "coordinator-agent-pipe", "version": "1"}
_DEFAULT_TIMEOUT_S = 600.0
_HANDSHAKE_TIMEOUT_S = 15.0


class AppServerError(Exception):
    """A JSON-RPC error response, or a transport loss after the handshake (code -1)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message


class AppServerUnreachable(AppServerError):
    """The daemon could not be reached: the proxy failed to spawn or closed before `initialize` returned."""


def _frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """One masked client frame (FIN set); servers reject unmasked client frames."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 1 << 16:
        head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
    mask = os.urandom(4)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _read_exact(stream, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise EOFError("proxy closed")
        buf += chunk
    return buf


def _read_upgrade_reply(stream) -> bytes:
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        head += _read_exact(stream, 1)
    return head


def _read_message(stream) -> tuple[int, bytes]:
    """(opcode, payload) of one complete message; continuation frames are joined."""
    data = b""
    first_opcode = 0
    while True:
        b0, b1 = _read_exact(stream, 2)
        opcode = b0 & 0x0F
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", _read_exact(stream, 2))
        elif n == 127:
            (n,) = struct.unpack("!Q", _read_exact(stream, 8))
        mask = _read_exact(stream, 4) if b1 & 0x80 else None
        payload = _read_exact(stream, n)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode >= 0x8:
            return opcode, payload
        if opcode:
            first_opcode = opcode
        data += payload
        if b0 & 0x80:
            return first_opcode, data


class _Conn:
    """An initialized JSON-RPC connection; a reader thread feeds `inbox` so waits can time out."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.inbox: "queue.Queue[Any]" = queue.Queue()
        self._next_id = 0
        self.upgraded = threading.Event()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            reply = _read_upgrade_reply(self.proc.stdout)
            if b" 101 " not in reply.split(b"\r\n", 1)[0]:
                raise EOFError("upgrade refused")
            self.upgraded.set()
            while True:
                opcode, payload = _read_message(self.proc.stdout)
                if opcode == 0x8:
                    raise EOFError("ws close")
                if opcode == 0x9:
                    self.inbox.put(("ping", payload))
                elif opcode == 0x1:
                    self.inbox.put(("msg", json.loads(payload)))
        except (EOFError, OSError, ValueError) as exc:
            self.inbox.put(("eof", str(exc)))

    def send(self, obj: dict) -> None:
        self._write(_frame(json.dumps(obj).encode()))

    def _write(self, data: bytes) -> None:
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise AppServerError(-1, f"proxy write failed: {exc}") from exc

    def upgrade(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        self._write(
            ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
        )

    def next_message(self, deadline: float, unreachable: bool = False) -> dict:
        """The next JSON message; answers pings; raises on EOF or deadline."""
        exc_cls = AppServerUnreachable if unreachable else AppServerError
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise exc_cls(-1, "timed out waiting for the app-server")
            try:
                kind, val = self.inbox.get(timeout=remaining)
            except queue.Empty:
                raise exc_cls(-1, "timed out waiting for the app-server") from None
            if kind == "ping":
                self._write(_frame(val, 0xA))
            elif kind == "eof":
                raise exc_cls(-1, f"app-server connection closed: {val}")
            else:
                return val

    def request(self, method: str, params: dict, deadline: float, unreachable: bool = False) -> Any:
        """Send a request, skip unrelated messages, return its `result`; a JSON-RPC error raises.

        TRAP: notifications arriving before the response are dropped, so `deliver` relies on the
        daemon answering `turn/start` before it emits that turn's notifications.
        """
        self._next_id += 1
        rid = self._next_id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            msg = self.next_message(deadline, unreachable)
            if msg.get("id") == rid and "method" not in msg:
                err = msg.get("error")
                if err:
                    raise AppServerError(int(err.get("code", -1)), str(err.get("message", "")))
                return msg.get("result")


@contextmanager
def _connect(argv: list[str], *, experimental: bool = False, timeout: float = _HANDSHAKE_TIMEOUT_S) -> Iterator[_Conn]:
    """Spawn the proxy, upgrade, `initialize`/`initialized`; always kills the child."""
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        raise AppServerUnreachable(-1, f"cannot spawn proxy: {exc}") from exc
    try:
        conn = _Conn(proc)
        conn.upgrade()
        # TRAP: the daemon drops frames written before it answers 101; wait for the reply.
        deadline = time.monotonic() + timeout
        while not conn.upgraded.wait(0.05):
            if time.monotonic() > deadline or not conn.inbox.empty():
                break
        if not conn.upgraded.is_set():
            raise AppServerUnreachable(-1, "no upgrade reply from the app-server")
        init = {"clientInfo": _CLIENT_INFO}
        if experimental:
            init["capabilities"] = {"experimentalApi": True}
        conn.request("initialize", init, deadline, unreachable=True)
        conn.send({"jsonrpc": "2.0", "method": "initialized"})
        yield conn
    finally:
        proc.kill()
        try:
            proc.wait(timeout=min(5, timeout))
        except subprocess.TimeoutExpired:
            pass


def enqueue(argv: list[str], thread_id: str, text: str, message_id: str) -> str:
    """Persist a completion notice for a live TUI; never resume or start a competing writer.

    The returned submission id confirms queue acceptance, not consumption by the TUI.
    """
    deadline = time.monotonic() + 0.2
    with _connect(argv, experimental=True, timeout=0.2) as conn:
        result = conn.request(
            "thread/queue/add",
            {"threadId": thread_id, "clientUserMessageId": message_id,
             "input": [{"type": "text", "text": text}]},
            deadline,
        ) or {}
        submission = result.get("queuedSubmission") or {}
        if not submission.get("id"):
            raise AppServerError(-1, "thread/queue/add returned no submission id")
        return submission["id"]


def loaded_threads(argv: list[str]) -> list[str]:
    """Ids of every thread loaded in the daemon (all pages), the live TUI's included."""
    ids: list[str] = []
    cursor: Optional[str] = None
    with _connect(argv) as conn:
        deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_S
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = conn.request("thread/loaded/list", params, deadline) or {}
            ids.extend(t for t in result.get("data", []) if isinstance(t, str))
            cursor = result.get("nextCursor")
            if not cursor:
                return ids


def _agent_texts(items: Any) -> list[str]:
    return [
        i["text"] for i in items or []
        if isinstance(i, dict) and i.get("type") == "agentMessage" and i.get("text")
    ]


def _failure(turn: dict) -> Optional[AppServerError]:
    if turn.get("status") in ("failed", "interrupted") or turn.get("error"):
        err = turn.get("error")
        text = err.get("message") if isinstance(err, dict) else err
        return AppServerError(-1, f"turn {turn.get('status')}: {text or 'no detail'}")
    return None


def _in_progress_turn(conn: _Conn, thread_id: str, deadline: float) -> Optional[str]:
    """Id of the thread's running turn, or None; a thread with no rollout lists nothing."""
    try:
        listed = conn.request("thread/turns/list", {"threadId": thread_id}, deadline) or {}
    except AppServerError as exc:
        if exc.code == -1:
            raise
        return None
    for turn in listed.get("data", []):
        if turn.get("status") == "inProgress" and turn.get("id"):
            return turn["id"]
    return None


def deliver(argv: list[str], thread_id: str, text: str, timeout: float = _DEFAULT_TIMEOUT_S) -> Iterator[str]:
    """`turn/start` `text` on a loaded thread; yield that turn's agent-message texts in order.

    A thread already mid-turn is steered, not restarted.
    Completion: `turn/completed` when this connection owns the thread's notifications, else the
    thread's `thread/status/changed` to idle followed by a `thread/turns/list` read-back.
    """
    with _connect(argv) as conn:
        deadline = time.monotonic() + timeout
        items = [{"type": "text", "text": text}]
        turn_id = None
        busy = _in_progress_turn(conn, thread_id, deadline)
        if busy:
            try:
                steered = conn.request(
                    "turn/steer",
                    {"threadId": thread_id, "input": items, "expectedTurnId": busy},
                    deadline,
                )
                turn_id = (steered or {}).get("turnId") or busy
            except AppServerError as exc:
                if exc.code == -1:
                    raise
        if turn_id is None:
            started = conn.request("turn/start", {"threadId": thread_id, "input": items}, deadline)
            turn_id = ((started or {}).get("turn") or {}).get("id")
        saw_active = owner = False
        if busy and turn_id == busy:
            saw_active = True
        streamed: list[str] = []
        while True:
            msg = conn.next_message(deadline)
            method = msg.get("method")
            params = msg.get("params") or {}
            if not method or params.get("threadId") != thread_id:
                continue
            if method == "thread/status/changed":
                kind = (params.get("status") or {}).get("type")
                saw_active = saw_active or kind == "active"
                if kind == "idle" and saw_active and not owner:
                    break
            elif method in ("turn/started", "item/started", "item/completed") and params.get("turnId", turn_id) == turn_id:
                owner = True
                item = params.get("item") or {}
                if method == "item/completed":
                    streamed.extend(_agent_texts([item]))
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if turn_id and turn.get("id") != turn_id:
                    continue
                failure = _failure(turn)
                if failure:
                    raise failure
                yield from (_agent_texts(turn.get("items")) or streamed)
                return
        listed = conn.request("thread/turns/list", {"threadId": thread_id}, deadline) or {}
        for turn in listed.get("data", []):
            if turn.get("id") == turn_id:
                failure = _failure(turn)
                if failure:
                    raise failure
                yield from _agent_texts(turn.get("items"))
                return
        raise AppServerError(-1, f"turn {turn_id} missing from thread/turns/list")
