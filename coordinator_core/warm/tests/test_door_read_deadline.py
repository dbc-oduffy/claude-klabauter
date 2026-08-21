"""The door's read is BOUNDED, and its bound never re-runs a delivered request.

WHAT REGRESSED, AND WHY THIS FILE EXISTS. `door.c`'s step 8 was a bare
blocking `ReadFile` loop on a synchronous handle until 2026-08-21. Against a
warm server that ACCEPTS a connection and then never answers it, `door.exe`
blocked forever -- observed, not theorised: a resident server on this box sat
with zero live `_worker_loop` threads (accepting and enqueueing, nothing
dequeueing), and a batched K=20 process-time run of `door.exe ping` against it
produced not one result in over seven minutes, twice, both killed by hand. The
Python client this door exists to outrun has always had a deadline
(`warm/client.py :: READ_DEADLINE_SECS`) and gives up at 2s. Nothing asserted
the door had one, so it shipped without one.

THE TWO PROPERTIES, and they pull in opposite directions -- a test that
asserts only the first would happily pass on a "fix" that reintroduces the
2026-08-19 double-commit incident:

  1. TERMINATION. A wedged server must not hold `door.exe` open forever. The
     door stops at `DOOR_READ_DEADLINE_MS`, read here out of `door.c` itself
     rather than pinned as a literal, so the assertion tracks the source it is
     about instead of drifting from it.

  2. NON-RE-EXECUTION. Stopping must NOT become falling through. Once the
     request has been fully written the server may already be executing it,
     and re-running that cold double-executes a mutation
     (`warm/client.py`'s own "THE INVARIANT THIS BLOCK EXISTS TO HOLD"). The
     deadline's expiry is therefore the `-32004` "warm dispatch indeterminate"
     envelope and a nonzero exit -- the door's existing post-delivery
     behaviour, reached by one more route, never a third behaviour.

`test_wedged_server_...` proves BOTH at once, and the second one positively
rather than by omission: the stub engine root it points the door at carries a
fake `coordinator-invoke.py` that prints a marker on stdout. The
pre-delivery test asserts that marker IS present (the fallback really is
reachable from this fixture, so its absence elsewhere means something); the
wedged test asserts it is ABSENT (the door refused instead of re-running).
Without the first assertion the second proves nothing -- an absent marker
would be equally consistent with a fixture that could never have produced one.

NO LIVE WARM SERVER IS INVOLVED, deliberately. The stub is a real named pipe
created through `election.elect()` -- the production call, with production
flags -- against a throwaway engine root, so it keys a DIFFERENT pipe hash
than the fleet server and cannot touch it. The resident server is never
consulted, started, or stopped.

COST, NAMED: `test_wedged_server_...` deliberately waits out the real 40s
deadline instead of a shortened one, because the deadline VALUE is half the
property (a door that gave up in 200ms would pass a termination-only test
while manufacturing `-32004` refusals for every op the server was going to
answer). That is one process, blocked on a kernel wait, burning no CPU -- but
it is 40s of wall clock, which is why this module is `cadence`-tiered and off
the per-commit path.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.warm_tier,
    pytest.mark.skipif(os.name != "nt", reason="door.exe is a Windows binary"),
]

_DOOR_DIR = Path(__file__).resolve().parents[1] / "door"
_DOOR_EXE = _DOOR_DIR / "door.exe"
_DOOR_C = _DOOR_DIR / "door.c"

#: Printed by the stub `coordinator-invoke.py` the door falls through to.
#: Its presence is proof of a fallback spawn; its absence, proof of a refusal.
_FALLBACK_MARKER = "DOOR-TEST-FALLBACK-RAN"
_FALLBACK_EXIT = 42


def _door_deadline_ms(macro: str) -> int:
    """Reads a deadline macro's value out of `door.c`, so this file asserts
    against the number the shipped binary was built from rather than a
    literal that can silently drift away from it."""
    source = _DOOR_C.read_text(encoding="utf-8")
    match = re.search(rf"^#define\s+{macro}\s+(\d+)\s*$", source, re.MULTILINE)
    assert match, f"{macro} not found in {_DOOR_C} -- the deadline was renamed or removed"
    return int(match.group(1))


def _make_stub_engine_root(tmp_path: Path) -> Path:
    """A throwaway directory shaped enough like a published engine for the
    door to accept it: a non-empty `coordinator_core/_engine_stamp` (what
    `is_valid_engine_root_w` checks) plus a `coordinator/bin/
    coordinator-invoke.py` that announces itself instead of running an op.

    The stamp bytes are unique per test run, so `compute_client_token` yields
    a token -- and therefore a pipe name -- no other process on this box is
    using."""
    root = tmp_path / "stub-engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text(
        f"door-read-deadline-test-{os.getpid()}-{time.time_ns()}\n", encoding="utf-8"
    )
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "coordinator-invoke.py").write_text(
        "import sys\n"
        f"print({_FALLBACK_MARKER!r})\n"
        f"raise SystemExit({_FALLBACK_EXIT})\n",
        encoding="utf-8",
    )
    return root.resolve()


def _pipe_name_for(root: Path) -> str:
    from coordinator_core.warm import election, skew

    return election.pipe_name(
        skew.compute_client_token(root), engine_clone=root
    )


class _WedgedServer:
    """A named pipe that accepts exactly one connection and then does
    NOTHING with it -- no read, no reply, no close. This is the shape the
    resident server degrades to when its worker threads are gone: the
    connection is accepted and enqueued, and nothing ever dequeues it.

    The pipe is created by `election.elect()`, the same call and the same
    flags the real server uses, rather than a hand-rolled `CreateNamedPipeW`
    -- a stub that builds its own input tests a door production never opens
    (state/lessons/2026-08-05-coverage-that-builds-its-own-input-never-tests-
    the-door-production-uses.yaml)."""

    def __init__(self, name: str) -> None:
        import _winapi
        from coordinator_core.warm import election

        self._winapi = _winapi
        self._handle = election.elect(name)
        self._connected = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        try:
            self._winapi.ConnectNamedPipe(self._handle, False)
        except OSError:
            pass
        finally:
            self._connected.set()

    def close(self) -> None:
        try:
            self._winapi.CloseHandle(self._handle)
        except OSError:
            pass


class _ReplyingServer:
    """A named pipe that reads one request frame and answers it verbatim
    with `reply`.

    This is the control the deadline tests need to be worth anything: the
    read mechanism changed from a synchronous `ReadFile` to overlapped I/O,
    and a bound on a read that no longer reads correctly is not a fix. These
    exercise the ORDINARY outcomes -- a success envelope and a
    provably-undispatched error -- through the new mechanism, end to end,
    with no live warm server involved."""

    def __init__(self, name: str, reply: bytes) -> None:
        import _winapi
        from coordinator_core.warm import election

        self._winapi = _winapi
        self._handle = election.elect(name)
        self._reply = reply
        self.request = b""
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            self._winapi.ConnectNamedPipe(self._handle, False)
            while b"\n" not in self.request:
                chunk, _ = self._winapi.ReadFile(self._handle, 65536)
                if not chunk:
                    return
                self.request += chunk
            self._winapi.WriteFile(self._handle, self._reply)
        except OSError:
            pass

    def close(self) -> None:
        self._thread.join(timeout=10)
        try:
            self._winapi.CloseHandle(self._handle)
        except OSError:
            pass


def _run_door(engine_root: Path, timeout: float) -> subprocess.CompletedProcess:
    """Runs `door.exe ping` pointed at `engine_root` via the documented
    `COORDINATOR_DOOR_ENGINE_ROOT` override. The override is used rather than
    a sidecar file on purpose: installing a `door.engine-root.txt` anywhere is
    a separate, still-open decision, and a test has no business pre-empting
    it. `timeout` is a harness backstop only -- a door that needs it has
    already failed the property under test."""
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(engine_root)
    return subprocess.run(
        [str(_DOOR_EXE), "ping"],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        cwd=str(engine_root),
    )


def test_pre_delivery_failure_still_falls_through(tmp_path: Path) -> None:
    """Nothing is listening, so the connect fails BEFORE any byte is written.
    That is ordinary doubt, and the door's answer to ordinary doubt is
    unchanged by the deadline: fall through silently to the Python
    entrypoint, propagate its exit code, print no envelope of its own.

    This also establishes that the fallback is genuinely reachable from this
    fixture -- which is what makes the wedged test's assertion that it did NOT
    fire mean something."""
    root = _make_stub_engine_root(tmp_path)
    proc = _run_door(root, timeout=60)

    assert _FALLBACK_MARKER in proc.stdout
    assert proc.returncode == _FALLBACK_EXIT
    assert "-32004" not in proc.stdout
    assert "warm dispatch indeterminate" not in proc.stdout


def test_a_served_reply_still_round_trips(tmp_path: Path) -> None:
    """The fast path, through the overlapped read that replaced the blocking
    one. A bound on a read that stopped reading correctly is not a fix, so
    this asserts the whole exchange: the request the server received is a
    parseable `invoke.from_argv` frame (proving the bounded WRITE delivered
    it intact), and the door relays the reply's three result fields."""
    root = _make_stub_engine_root(tmp_path)
    reply = (
        '{"jsonrpc":"2.0","id":1,"result":'
        '{"stdout":"pong\\n","stderr":"","exit_code":0}}\n'
    ).encode("utf-8")

    server = _ReplyingServer(_pipe_name_for(root), reply)
    try:
        proc = _run_door(root, timeout=60)
    finally:
        server.close()

    import json

    request = json.loads(server.request.decode("utf-8").strip())
    assert request["method"] == "invoke.from_argv"
    assert request["params"]["argv"] == ["ping"]

    assert proc.returncode == 0
    assert proc.stdout == "pong\n"
    assert _FALLBACK_MARKER not in proc.stdout


def test_provably_undispatched_error_still_falls_through(tmp_path: Path) -> None:
    """The one post-delivery route that is still allowed to fall through:
    an error code `is_provably_undispatched` recognises as proof the server
    never invoked a handler. The deadline work must not have collapsed this
    into the refusal branch."""
    root = _make_stub_engine_root(tmp_path)
    reply = (
        '{"jsonrpc":"2.0","id":1,"error":'
        '{"code":-32601,"message":"Method not found"}}\n'
    ).encode("utf-8")

    server = _ReplyingServer(_pipe_name_for(root), reply)
    try:
        proc = _run_door(root, timeout=60)
    finally:
        server.close()

    assert _FALLBACK_MARKER in proc.stdout
    assert proc.returncode == _FALLBACK_EXIT
    assert "-32004" not in proc.stdout


def test_a_suspended_op_refusal_never_reads_as_a_maybe_completed_mutation(
    tmp_path: Path,
) -> None:
    """-32006 OP_SUSPENDED must fall through, NOT become a -32004.

    The regression this pins was measured on the real binary 2026-08-21,
    before -32006 was on `is_provably_undispatched`'s list: the door
    discarded the server's refusal and emitted its own "the op may have
    COMPLETED, reconcile against real state" envelope. For
    `ceremony.scoped_git_commit` -- one of the 17 ops the 2s-max ruling
    suspended -- that told an operator whose commit was refused BEFORE
    anything ran that it might have landed, while the actual reason and the
    bar to reinstate the op never reached them at all.

    Asserted as an ABSENCE of the -32004 shape rather than a presence of
    the refusal text, because the fall-through hands the request to the
    cold engine, and it is the cold engine's own `_dispatch_message_impl`
    -- the same one that refused here -- that renders the message. What
    this door owes is not swallowing it."""
    root = _make_stub_engine_root(tmp_path)
    reply = (
        '{"jsonrpc":"2.0","id":1,"error":{"code":-32006,'
        '"message":"ceremony.scoped_git_commit is suspended: over the 2s max bar."}}\n'
    ).encode("utf-8")

    server = _ReplyingServer(_pipe_name_for(root), reply)
    try:
        proc = _run_door(root, timeout=60)
    finally:
        server.close()

    assert _FALLBACK_MARKER in proc.stdout
    assert proc.returncode == _FALLBACK_EXIT
    assert "-32004" not in proc.stdout
    assert "may have COMPLETED" not in proc.stdout


def test_wedged_server_bounds_the_read_and_refuses_to_re_run(tmp_path: Path) -> None:
    """The server accepts and never answers. The door must stop -- and must
    stop by REFUSING, never by re-running a request it already delivered."""
    root = _make_stub_engine_root(tmp_path)
    deadline_ms = _door_deadline_ms("DOOR_READ_DEADLINE_MS")
    deadline_s = deadline_ms / 1000.0

    server = _WedgedServer(_pipe_name_for(root))
    try:
        started = time.monotonic()
        # The backstop is generously above the deadline: it exists to fail the
        # test rather than hang the suite, and must never be the thing that
        # produces the "returned in time" verdict.
        proc = _run_door(root, timeout=deadline_s * 3)
        elapsed = time.monotonic() - started
    finally:
        server.close()

    # Property 1 -- it terminated, at the deadline it claims.
    assert elapsed < deadline_s * 2, (
        f"door.exe took {elapsed:.1f}s against a {deadline_s:.0f}s read deadline"
    )
    assert elapsed >= deadline_s * 0.8, (
        f"door.exe gave up after {elapsed:.1f}s, well inside its {deadline_s:.0f}s "
        "deadline -- a door that abandons a delivered request early manufactures "
        "-32004 refusals for ops the server was going to answer"
    )

    # Property 2 -- it refused, and refused in the ONE post-delivery shape the
    # door already had. No third behaviour.
    assert proc.returncode != 0
    assert '"code":-32004' in proc.stdout
    assert "warm dispatch indeterminate" in proc.stdout

    # Property 2, the half that actually guards the 2026-08-19 incident: the
    # delivered request was NOT re-run cold.
    assert _FALLBACK_MARKER not in proc.stdout, (
        "the door fell through AFTER delivering the request -- this is the "
        "double-execution the -32004 envelope exists to prevent"
    )
