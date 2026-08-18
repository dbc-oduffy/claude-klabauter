"""coordinator_core.warm.server — the warm engine's own process entrypoint.

ADDED 2026-08-18 AT EXECUTION, not present in the authored spine of
docs/plans/2026-08-16-one-engine-for-the-whole-box.md — chunk C30. C14 elects
a pipe, C15 connects to one, C16 evicts a stale one, C17 shuts one down, C18
spawns one and writes its breadcrumb, and no chunk built the process those
five describe. This module is that process: the exact script
`coordinator_core.warm.client.SERVER_ENTRY_SCRIPT` names as its spawn target.

WHAT THIS MODULE OWNS, and only this:
  - the process entrypoint C18's spawn targets (`main()`, run via
    `if __name__ == "__main__"`, since `detached_spawn.spawn_detached`
    respawns by resolved interpreter path against this file, never `-m`);
  - the accept loop over the pipe handle `warm.election.elect()` returns,
    including creating and connecting FURTHER (non-first) pipe instances --
    election itself only ever contests the FIRST instance;
  - per-connection framed-NDJSON read, dispatch into the existing engine
    core (`coordinator_core.ipc.dispatch_message`), and framed response
    write;
  - boot identity: `warm.skew.ServerVersionState` constructed ONCE at boot,
    held for the pipe's own generation token and for skew responses.

WHAT THIS MODULE MUST NOT REIMPLEMENT -- every one of these already exists
and a second copy is the failure mode this row is most likely to produce:
  - election -> `warm.election.elect()` / `pipe_name()`. This module only
    creates FOLLOW-ON pipe instances after the first (never re-elects), and
    reuses `election._build_security_attributes` for their ACL rather than
    hand-rolling a second SDDL descriptor -- the one reach into a private
    peer symbol in this module, done because the alternative (duplicating
    the restricted-DACL construction the 2026-08-14 transport spike proved
    out) is the worse of the two costs. Worth promoting to `election.py`'s
    public surface in a future chunk; not done here since `election.py` is
    outside this row's `writes:`.
  - staleness -> `warm.skew`, including the listener-close-before-drain
    order (`evict_on_skew`).
  - shutdown -> `warm.lifecycle`'s single ordered, single-shot sequence;
    this module calls `drain_and_exit` (bound as `evict_on_skew`'s `drain`
    argument -- `evict_on_skew` already closes the listener itself, so
    binding `begin_shutdown` there would double-close it) and never re-rolls
    the close-listener/wait-drain/ctx-shutdown/exit sequence by hand.
  - warm on/off -> `warm.settings` (consulted by the CLIENT before it ever
    spawns this process; this module does not re-check it at boot).
  - breadcrumb -> C18, not yet landed. Step 3 of the lifecycle sequence
    ("flush the log, unlink the breadcrumb, close pipe handles") therefore
    has no breadcrumb to unlink yet; `_ctx_shutdown` below is a documented
    no-op pending that chunk, not a silent gap.

TRANSPORT MODEL -- synchronous, one OS thread per connection, not asyncio
end to end. `warm.lifecycle`'s shutdown sequence is itself synchronous
(`time.sleep`-based polling in `_wait_for_drain`), and the client's own
transport (`warm.client._open_pipe`) is plain blocking `open(pipe, "r+b")`
-- both match the classic non-overlapped Win32 named-pipe server shape the
2026-08-14 transport spike measured, not the asyncio `PipeServer` /
`start_serving_pipe` machinery that spike also exercised (that route
requires monkeypatching a private `asyncio.windows_events` attribute to get
the restricted ACL onto EVERY instance, which is a heavier, more fragile
surface than driving `_winapi.CreateNamedPipe` / `ConnectNamedPipe`
directly). Each pipe instance is wrapped via `msvcrt.open_osfhandle` +
`os.fdopen(fd, "r+b")` into the SAME blocking file-object API the client
already uses -- verified end-to-end on this box before landing (throwaway
probe, discarded). One thread per connection is what gives "a wedged op
does not stall the next" for free: a hung dispatch wedges only its own
thread's `readline`/dispatch/`write`, never the accept loop or any other
connection's thread.

PER-REQUEST STATE IS NOT OPTIONAL. `_run_dispatch` opens
`coordinator_core.warm.entry_seam.per_request_state()` around every call
into `coordinator_core.ipc.dispatch_message` -- explicit, Token/reset-scoped
per dispatch, per the seam's own contract. `dispatch_message` ->
`_dispatch_message_impl` already carries its own independent declared-writes
Token/reset scope internally (entry_seam's own docstring, "path 1 ...
Converged"), so this wrap nests with it rather than duplicating a dialect;
nesting is safe by `session.declared_writes.collecting()`'s own contract.
Binding explicitly here, rather than relying solely on path 1's internal
convergence, is deliberate: it is what makes this module's own per-request
scoping visible and testable at the boundary this row owns, independent of
`ipc.py`'s internals ever changing.

NEVER FAIL A CALLER. Every fault below the frame-parse boundary --
malformed JSON, a non-object frame, a raised handler exception -- returns a
well-formed JSON-RPC error envelope over the SAME connection rather than
closing it uncleanly or leaving the accept loop's other threads affected;
`_serve_line` never lets an exception escape to its caller. A client always
sees either a well-formed response, a closed pipe, or a dead pipe -- never a
hang, never a malformed frame -- so it can always fall back to cold (C15
Backstop 2, mirrored here on the server's own side of the same contract).

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C30;
gap found and named during C15 execution, 2026-08-18.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.ipc import INTERNAL_ERROR, INVALID_REQUEST, PARSE_ERROR
from coordinator_core.warm import election, lifecycle, skew
from coordinator_core.warm.entry_seam import per_request_state

__all__ = ["InFlightCounter", "main"]

# Mirrors `_winapi.ERROR_PIPE_CONNECTED` (535) -- a client already connected
# before this instance's own `ConnectNamedPipe` call was posted, which is a
# WIN for a synchronous server, not a failure (the same "already connected"
# race non-overlapped named-pipe servers always have to tolerate).
_ERROR_PIPE_CONNECTED = 535

_PIPE_BUFFER_BYTES = 65536


class InFlightCounter:
    """Thread-safe in-flight request counter.

    The `in_flight_count` callable `warm.lifecycle.begin_shutdown` /
    `drain_and_exit` poll to learn when a drain has completed -- one
    instance per server process, incremented when a connection's request is
    accepted for processing and decremented as soon as that request's
    response has been written (not when the connection object is closed),
    so a request that itself triggers the drain (a skew-evicting request)
    has already released its own slot before `drain()` starts polling for
    zero -- see `_serve_line`'s `_release` closure, which is the one call
    site that matters for avoiding a self-deadlock here.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def enter(self) -> None:
        with self._lock:
            self._count += 1

    def exit(self) -> None:
        with self._lock:
            self._count -= 1

    def __call__(self) -> int:
        with self._lock:
            return self._count


class _FrameError(Exception):
    """Internal signal carrying a pre-built JSON-RPC error envelope for a
    frame that failed to parse -- never raised past `_serve_line`."""

    def __init__(self, response: dict):
        self.response = response
        super().__init__(response.get("error", {}).get("message", "frame error"))


def _engine_clone_root() -> Path:
    """This server process's own resolved engine-clone root -- the same
    computation `election._default_engine_clone` / `warm.client.
    _engine_clone_root` / `skew._default_engine_clone` each make for their
    own default, kept as a local copy per this package's convention of not
    reaching into a peer module's private name for a one-line computation.
    """
    return Path(__file__).resolve().parents[2]


def _encode(response: dict) -> bytes:
    return json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"


def _parse_frame(raw_line: bytes) -> dict:
    """Decode one NDJSON frame into a JSON-RPC request dict, or raise
    `_FrameError` carrying a well-formed PARSE_ERROR / INVALID_REQUEST
    envelope -- the malformed-frame half of "never fail a caller"."""
    try:
        text = raw_line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _FrameError(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": PARSE_ERROR, "message": f"Parse error: frame is not valid UTF-8 ({exc})"},
            }
        ) from exc

    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _FrameError(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": PARSE_ERROR, "message": f"Parse error: {exc}"},
            }
        ) from exc

    if not isinstance(msg, dict):
        raise _FrameError(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": INVALID_REQUEST,
                    "message": f"Invalid Request: frame must be a JSON object, got {type(msg).__name__}",
                },
            }
        )
    return msg


def _run_dispatch(msg: dict) -> dict:
    """Invoke the existing engine core for one already-parsed JSON-RPC
    request -- the SOLE process-level dispatch chokepoint
    (`coordinator_core.ipc.dispatch_message`'s own docstring), never a
    hand-rolled call into `get_op_handler`.

    Opens `entry_seam.per_request_state()` around the call (module
    docstring's "PER-REQUEST STATE IS NOT OPTIONAL"), and runs the async
    dispatch via a fresh `asyncio.run()` scoped to this one call -- each
    connection already lives on its own OS thread (module docstring's
    "TRANSPORT MODEL"), so a per-call event loop needs no cross-thread
    scheduling and leaves no shared loop state for the next request on this
    thread, or any other connection's thread, to inherit.
    """
    import asyncio

    from coordinator_core.ipc import dispatch_message

    with per_request_state():
        return asyncio.run(dispatch_message(msg))


def _serve_line(
    raw_line: bytes,
    *,
    write: Callable[[bytes], None],
    version_state: "skew.ServerVersionState",
    server_sha: Optional[str],
    close_listener: Callable[[], None],
    drain: Callable[[], None],
    release_in_flight: Callable[[], None],
    dispatch: Callable[[dict], dict] = _run_dispatch,
) -> None:
    """Process one request frame for one connection: write exactly one
    response frame (or delegate that write to `warm.skew.evict_on_skew`'s
    own `respond` callable on a detected skew), then release this request's
    in-flight slot. Never raises -- see module docstring's "NEVER FAIL A
    CALLER".

    `release_in_flight` is idempotency-guarded here (not by the caller) so
    it is safe to call it once inline (right after the response is written,
    which is what lets a skew-triggered `drain()` observe this request as
    already-released rather than deadlocking on its own count) and have the
    connection thread's own cleanup call it again as a no-op safety net.
    """
    released = False

    def _release() -> None:
        nonlocal released
        if not released:
            released = True
            release_in_flight()

    def _write_and_release(response: dict) -> None:
        write(_encode(response))
        _release()

    try:
        msg = _parse_frame(raw_line)
    except _FrameError as exc:
        _write_and_release(exc.response)
        return

    request_id = msg.get("id")
    client_token = msg.pop("_engine_token", None)

    if client_token is not None and version_state.is_skewed(client_token):
        skew.evict_on_skew(
            respond=_write_and_release,
            close_listener=close_listener,
            drain=drain,
            request_id=request_id,
            server_sha=server_sha,
            client_token=client_token,
        )
        return

    try:
        response = dispatch(msg)
    except Exception as exc:  # noqa: BLE001 -- never fail the caller, see module docstring
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": INTERNAL_ERROR, "message": f"internal error: {exc!r}"},
        }
    _write_and_release(response)


def _handle_connection(
    io: Any,
    *,
    version_state: "skew.ServerVersionState",
    server_sha: Optional[str],
    close_listener: Callable[[], None],
    drain: Callable[[], None],
    in_flight: InFlightCounter,
    dispatch: Callable[[dict], dict] = _run_dispatch,
) -> None:
    """One connection's request/response lifecycle, run on its own thread.

    Exactly one request per connection, matching `warm.client.
    _try_warm_dispatch_inner`'s own shape (one write, one read, close) --
    there is no live client that keeps a connection open past its single
    request, so this does not loop reading further lines.
    """
    in_flight.enter()
    exited = False

    def _exit_once() -> None:
        nonlocal exited
        if not exited:
            exited = True
            in_flight.exit()

    def _write_flushed(data: bytes) -> None:
        # A skew-detected write is immediately followed by `close_listener`
        # and (synchronously, same thread) `drain` -> possibly `os._exit(0)`
        # -- an unflushed buffered write would never reach the pipe before
        # the process dies. Every write on this path flushes explicitly
        # rather than relying on `io.close()`'s implicit flush, which may
        # never run for exactly that reason.
        io.write(data)
        io.flush()

    try:
        try:
            line = io.readline()
        except OSError:
            return
        if not line:
            return
        _serve_line(
            line,
            write=_write_flushed,
            version_state=version_state,
            server_sha=server_sha,
            close_listener=close_listener,
            drain=drain,
            release_in_flight=_exit_once,
            dispatch=dispatch,
        )
    finally:
        _exit_once()
        try:
            io.close()
        except OSError:
            pass


def _create_pipe_instance(name: str, sid: str) -> int:
    """Create one FOLLOW-ON pipe instance (never the first -- that is
    election's own job) carrying the same restricted ACL as the elected
    first instance. See module docstring's negative-spec for why this
    reaches into `election._build_security_attributes` rather than
    duplicating the SDDL descriptor.
    """
    import ctypes
    import _winapi

    from coordinator_core.warm.election import _build_security_attributes

    security_attributes = _build_security_attributes(sid)
    return _winapi.CreateNamedPipe(
        name,
        _winapi.PIPE_ACCESS_DUPLEX,
        _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | _winapi.PIPE_WAIT,
        _winapi.PIPE_UNLIMITED_INSTANCES,
        _PIPE_BUFFER_BYTES,
        _PIPE_BUFFER_BYTES,
        0,
        ctypes.addressof(security_attributes),
    )


def _connect_pipe(handle: int) -> None:
    """Block until a client connects to `handle`. `ERROR_PIPE_CONNECTED`
    (a client already connected before this call was posted) is a win, not
    a failure -- see `_ERROR_PIPE_CONNECTED`'s own comment."""
    import _winapi

    try:
        _winapi.ConnectNamedPipe(handle, _winapi.NULL)
    except OSError as exc:
        if getattr(exc, "winerror", None) != _ERROR_PIPE_CONNECTED:
            raise


def _close_handle(handle: int) -> None:
    import _winapi

    try:
        _winapi.CloseHandle(handle)
    except OSError:
        pass


def _wrap_handle(handle: int) -> Any:
    """Wrap a connected pipe HANDLE into the same blocking file-object API
    the client already reads/writes (`warm.client._open_pipe`), via
    `msvcrt.open_osfhandle` + `os.fdopen` -- verified end-to-end on this
    box before landing (throwaway probe, discarded per this repo's probe
    discipline)."""
    import msvcrt

    fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    return os.fdopen(fd, "r+b")


class _ServerContext:
    """Boot-scoped server state: the pipe name, the ACL identity, the
    version state constructed once, and the in-flight counter every
    connection thread shares.

    `close_listener` / `_drain` are the two callables `_serve_line` binds
    into `warm.skew.evict_on_skew` for the skew-eviction path; both are
    also the natural hooks a FUTURE trigger (idle demotion C24, operator
    request) would bind to `warm.lifecycle.begin_shutdown` with, but this
    row does not wire any trigger beyond skew -- out of scope, see module
    docstring's ownership list.
    """

    def __init__(self, *, name: str, sid: str, version_state: "skew.ServerVersionState"):
        self.name = name
        self.sid = sid
        self.version_state = version_state
        self.in_flight = InFlightCounter()
        self._listening_lock = threading.Lock()
        self._listening = True
        self._stopped = threading.Event()

    def close_listener(self) -> None:
        with self._listening_lock:
            self._listening = False

    def _is_listening(self) -> bool:
        with self._listening_lock:
            return self._listening

    def _ctx_shutdown(self) -> None:
        """Step 3 of `warm.lifecycle`'s sequence: flush the log, unlink the
        breadcrumb, close pipe handles. No breadcrumb exists yet (C18 not
        landed -- module docstring), and `os._exit(0)` (step 4, run
        immediately after this by `lifecycle._run_tail`) reclaims every
        open handle at the OS level regardless, so there is nothing this
        step needs to do today. Documented no-op, not a silent gap.
        """
        return None

    def _drain(self) -> None:
        lifecycle.drain_and_exit(in_flight_count=self.in_flight, ctx_shutdown=self._ctx_shutdown)

    def serve_forever(self, first_handle: int) -> None:
        """Kick off the self-replenishing accept chain on its own thread
        and block for the life of the process.

        A drain triggered from any connection thread ends the whole
        process via `os._exit(0)` (`warm.lifecycle`), which is the only
        thing that ever wakes this wait -- no separate interrupt mechanism
        is needed (module docstring's "TRANSPORT MODEL").
        """
        threading.Thread(target=self._accept_and_replenish, args=(first_handle,), daemon=True).start()
        self._stopped.wait()

    def _accept_and_replenish(self, handle: int) -> None:
        """Block for a connection on `handle`, then -- BEFORE handling that
        connection -- post a fresh instance's connect-wait on ITS OWN
        thread, and only then hand `handle` off to `_handle_connection` on
        THIS thread.

        Ordering is the whole point: posting the replacement first is what
        keeps a pending instance listening continuously rather than only
        after a connection has already been wrapped and dispatched. A
        fully-serial create -> connect -> handle -> repeat loop leaves a
        real (if narrow -- CreateNamedPipe is a single syscall) window
        between one instance being claimed and the next being posted,
        during which a simultaneous client sees FileNotFoundError instead
        of ERROR_PIPE_BUSY -- still a handled outcome on the client's own
        anti-storm table (`warm.client`'s own docstring: FileNotFoundError
        is "THE ONLY SPAWN TRIGGER, then cold"), but narrowing it costs
        nothing here since replenishment is a single fast syscall dispatched
        to its own thread rather than serialized after this connection's
        full read/dispatch/write lifecycle.
        """
        try:
            _connect_pipe(handle)
        except OSError as exc:
            print(f"[warm-server] connect failed on pipe instance: {exc!r}", file=sys.stderr)
            _close_handle(handle)
            return

        if not self._is_listening():
            _close_handle(handle)
            return

        if self._is_listening():
            try:
                next_handle = _create_pipe_instance(self.name, self.sid)
            except OSError as exc:
                print(f"[warm-server] failed to create pipe instance: {exc!r}", file=sys.stderr)
            else:
                threading.Thread(
                    target=self._accept_and_replenish, args=(next_handle,), daemon=True
                ).start()

        io = _wrap_handle(handle)
        _handle_connection(
            io,
            version_state=self.version_state,
            server_sha=self.version_state.server_sha,
            close_listener=self.close_listener,
            drain=self._drain,
            in_flight=self.in_flight,
        )


def main() -> int:
    """`SERVER_ENTRY_SCRIPT`'s process entrypoint. Boot sequence:

    1. Resolve this engine clone's pipe name using the SAME primary
       skew token the client computes (`skew.compute_client_token`) as the
       pipe's generation stamp -- a successor bound to a new commit
       computes a different token, hence a different pipe name, and binds
       immediately without contesting the predecessor's still-draining
       pipe (election.py's own docstring).
    2. `election.elect()` the first instance. `ElectionLost` means another
       process already won this exact generation's pipe -- not an error,
       just nothing for this process to do; exits 0.
    3. Construct `skew.ServerVersionState` ONCE (boot identity).
    4. Run the accept loop until a drain-triggered `os._exit(0)` ends the
       process.
    """
    if sys.platform != "win32":
        print("[warm-server] this module is Windows-only", file=sys.stderr)
        return 1

    repo_root = _engine_clone_root()
    sid = election.current_user_sid()
    token = skew.compute_client_token(repo_root)
    name = election.pipe_name(token, engine_clone=repo_root, user_sid=sid)

    try:
        first_handle = election.elect(name, user_sid=sid)
    except election.ElectionLost:
        print(
            f"[warm-server] election lost for {name!r}; another server already "
            "owns this generation's pipe, exiting",
            file=sys.stderr,
        )
        return 0

    version_state = skew.ServerVersionState(repo_root)
    ctx = _ServerContext(name=name, sid=sid, version_state=version_state)
    ctx.serve_forever(first_handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
