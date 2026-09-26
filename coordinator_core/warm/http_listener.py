"""Loopback HTTP transport for the warm engine, so a hook fire pays no interpreter start.

WHY THIS EXISTS. Every hook registration today launches `python3 -c <LOADER>`, and the
interpreter start plus the engine import graph is a floor no amount of work inside that
process can get under -- measured on this box at 53.7ms at its very best sample and ~294ms
typically (wall), carrying no op work at all. Claude Code supports a native `type: "http"`
hook that POSTs the complete event and reads the result from the response body. On that
transport there is no child process, so the floor is not reduced, it is DELETED.

THIS IS AN ADAPTER, NOT A SECOND SERVER. `warm.server._serve_line` is already
transport-agnostic: it takes one request frame as bytes plus a `write` callable, and writes
exactly one response frame. Everything that makes the warm engine safe to talk to lives
INSIDE it -- op dispatch, version-skew eviction, in-flight accounting, and the fail-CLOSED
refusal of a frame carrying no `_engine_token`. This module reads a POST body, hands it to
that function with a collecting `write`, and returns what was collected. It re-implements
none of those four, and a future edit that starts re-deriving any of them has taken a wrong
turn: the named-pipe path and this path must not be able to disagree about whether a caller
is trusted.

NEGATIVE SPEC -- things that are deliberately NOT here:

- **No `localhost`.** The listener binds the `127.0.0.1` LITERAL and nothing else. Dialling
  the NAME costs ~2s per call on this box: the resolver returns both an IPv6 and an IPv4
  address, the client tries `::1` first against an IPv4-bound listener, and pays a full SYN
  retry before falling back. A ~0.2ms transport with a 2s name lookup in front of it is a
  catastrophic regression rather than a win, which is why `bind_host()` is asserted by test
  rather than left to a reviewer noticing a string.
- **No fallback to the cold path from in here.** Whether a hook may ride a transport that
  fails open when the listener is down is a live cross-repo shape question and is not
  settled by this module. What this module owes is a truthful answer about whether it
  served the request; the caller decides what an unserved request means.
- **No auth of its own.** The `_engine_token` check `_serve_line` already performs is the
  authorisation, and re-deriving a second scheme beside it is the failure this module's own
  header handling exists to avoid. The token travels in a header, is placed into the frame,
  and is then judged by the same code that judges a named-pipe caller. A request without
  one is refused by `_serve_line`, not by us.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional, Tuple

from coordinator_core.warm import telemetry

HOOK_BUDGET_SECS = 2.0

_LOOPBACK_V4 = "127.0.0.1"

ENGINE_TOKEN_HEADER = "X-Coordinator-Engine-Token"

MAX_BODY_BYTES = 1 << 20


def bind_host() -> str:
    return _LOOPBACK_V4


def _collect_response(
    raw_frame: bytes,
    serve_line: Callable[..., None],
    serve_kwargs: dict,
) -> bytes:
    chunks: list = []

    def _write(data: bytes) -> None:
        chunks.append(data)

    serve_line(raw_frame, write=_write, **serve_kwargs)
    return b"".join(chunks)


def _frame_from_request(body: bytes, token: Optional[str]) -> bytes:
    if token is None:
        return body
    try:
        obj = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(obj, dict):
        return body
    obj["_engine_token"] = token
    return json.dumps(obj).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):

    server_version = "coordinator-warm-http"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- base's spelling
        return

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's spelling
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, b'{"error":"bad content-length"}')
            return
        if length < 0 or length > MAX_BODY_BYTES:
            # DRAIN BEFORE REFUSING. Writing a response while the client is still
            remaining = max(0, length)
            while remaining:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._respond(413, b'{"error":"body too large"}')
            return

        body = self.rfile.read(length) if length else b""
        token = self.headers.get(ENGINE_TOKEN_HEADER)
        frame = _frame_from_request(body, token)

        serve_line, serve_kwargs = self.server.serve_binding()  # type: ignore[attr-defined]
        started = time.monotonic()
        try:
            response = _collect_response(frame, serve_line, serve_kwargs)
        except Exception:  # noqa: BLE001 -- see NEVER FAIL A CALLER in server.py
            telemetry.record_degrade(
                kind=telemetry.KIND_COLD_RUN,
                cause="http_listener.py :: _Handler.do_POST -- _serve_line raised "
                "while dispatching a delivered request; answering 500 rather than "
                "the served response",
            )
            self._respond(500, b'{"error":"dispatch failed"}')
            return
        elapsed = time.monotonic() - started
        if elapsed > HOOK_BUDGET_SECS:
            telemetry.record_degrade(
                kind=telemetry.KIND_HOOK_TIMEOUT,
                cause=(
                    "http_listener.py :: _Handler.do_POST -- dispatch took "
                    f"{elapsed:.3f}s, exceeding the {HOOK_BUDGET_SECS}s internal "
                    "budget (the harness's own UserPromptSubmit hook budget is "
                    "reported at 5s; this fires first so the box can say why)"
                ),
            )
        self._respond(200, response)


class _Server(ThreadingHTTPServer):

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, serve_binding):
        super().__init__(addr, handler)
        self.serve_binding = serve_binding


def start(
    serve_binding: Callable[[], Tuple[Callable[..., None], dict]],
    *,
    port: int = 0,
) -> Tuple[_Server, int, threading.Thread]:
    srv = _Server((bind_host(), port), _Handler, serve_binding)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, port, thread
