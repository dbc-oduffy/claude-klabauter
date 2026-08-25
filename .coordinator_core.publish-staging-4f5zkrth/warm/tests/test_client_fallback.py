"""Tests for `coordinator_core.warm.client` -- the client-side warm-pipe
preamble and its anti-storm fallback table.

Purpose: C15 of docs/plans/2026-08-16-one-engine-for-the-whole-box.md.
Exercises every row of the anti-storm table directly against
`try_warm_dispatch`, stubbing the transport (`_open_pipe`), the warmth
switch (`is_warm_enabled`), the pipe-name resolver (`election.pipe_name`),
and the spawn call (`spawn_detached`) rather than touching a real named
pipe or process -- this module's own contract is the fallback DECISION
table, not the named-pipe transport mechanics (`election`'s own tests) or
`spawn_detached`'s own never-raise contract (`detached_spawn`'s own
tests).

Runs on any platform: nothing here opens a real Windows named pipe: every
test drives `try_warm_dispatch` through a fake file-like object returned
by a monkeypatched `_open_pipe`.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C15
"""

from __future__ import annotations

import io
import json

import pytest

from coordinator_core.warm import client

#: Captured at collection time, before any test's `_warm_on` fixture stubs
#: `client.engine_token` -- the one test that needs the REAL function
#: (`test_live_tree_cold_names_the_ruling_once_and_never_spawns`) restores
#: it from here rather than from `client.engine_token`, which by the time
#: any test body runs is already the stub.
_REAL_ENGINE_TOKEN = client.engine_token


@pytest.fixture(autouse=True)
def _warm_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module opts warmth on and pins the pipe name and
    engine token, so a test body only has to control `_open_pipe`.

    `engine_token` is stubbed to a fixed value rather than left to compute
    for real: the real path (`skew.compute_client_token`) raises
    `UnstampedEngineRootError` against THIS repo's own live working tree
    (no `_engine_stamp`), which is exactly the condition
    `test_live_tree_cold_*` below exercises deliberately -- every other
    test in this module is about the pipe-open/spawn table, not that path,
    and stubbing it here keeps `_live_tree_cold` false (a "stamped clone")
    for them."""
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    from coordinator_core.warm import breadcrumb

    monkeypatch.setattr(breadcrumb, "should_spawn", lambda engine_root=None, **kw: True)


class _FakePipe:
    """A minimal stand-in for the `open(pipe, "r+b")` handle -- records
    what was written and serves canned bytes (or raises) on `readline`."""

    def __init__(self, read_result=b'{"jsonrpc":"2.0","id":1,"result":{}}\n', raise_on_write=None):
        self.written = []
        self.closed = False
        self._read_result = read_result
        self._raise_on_write = raise_on_write

    def write(self, data: bytes) -> None:
        if self._raise_on_write is not None:
            raise self._raise_on_write
        self.written.append(data)

    def flush(self) -> None:
        pass

    def readline(self):
        if isinstance(self._read_result, BaseException):
            raise self._read_result
        return self._read_result

    def close(self) -> None:
        self.closed = True


_MSG = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}


def test_warmth_disabled_skips_straight_to_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "is_warm_enabled", lambda: False)
    opened = []
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: opened.append(pipe) or _FakePipe())
    assert client.try_warm_dispatch(_MSG) is None
    assert opened == []


def test_file_not_found_spawns_once_and_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_enoent(pipe):
        raise FileNotFoundError(2, "no such pipe")

    monkeypatch.setattr(client, "_open_pipe", _raise_enoent)
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append((repo_root, script)) or True
    )
    assert client.try_warm_dispatch(_MSG) is None
    assert len(spawns) == 1
    assert spawns[0][1] == client.SERVER_ENTRY_SCRIPT


def test_spawn_once_consults_should_spawn_and_skips_when_debounced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W13 (plan 2026-08-20-a-refusal-cannot-exit-zero.md § C29):
    `_spawn_once` is not the only path C18's breadcrumb debounce is meant
    to gate -- `warm_start.py`'s SessionStart trigger already consults it,
    but every concurrent CLI client that hits `FileNotFoundError` on the
    same eviction races through its OWN one-per-process `_spawn_once`.
    Backstop 1 alone does nothing to stop that: it only debounces a single
    process against itself. `should_spawn` returning False (a young, alive
    breadcrumb already vouches for an in-flight spawn) must suppress the
    spawn here too."""
    from coordinator_core.warm import breadcrumb

    calls = []
    monkeypatch.setattr(
        breadcrumb, "should_spawn", lambda engine_root=None, **kw: calls.append(engine_root) or False
    )

    def _raise_enoent(pipe):
        raise FileNotFoundError(2, "no such pipe")

    monkeypatch.setattr(client, "_open_pipe", _raise_enoent)
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append((repo_root, script)) or True
    )
    assert client.try_warm_dispatch(_MSG) is None
    assert spawns == [], "should_spawn=False must suppress the spawn"
    assert len(calls) == 1


def test_backstop_one_spawn_attempt_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_enoent(pipe):
        raise FileNotFoundError(2, "no such pipe")

    monkeypatch.setattr(client, "_open_pipe", _raise_enoent)
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append(1) or True
    )
    client.try_warm_dispatch(_MSG)
    client.try_warm_dispatch(_MSG)
    client.try_warm_dispatch(_MSG)
    assert len(spawns) == 1


def test_error_pipe_busy_231_as_plain_oserror_never_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The load-bearing case: ERROR_PIPE_BUSY (231) surfaces as a plain
    `OSError` with `winerror=231, errno=22` on live Windows (verified
    2026-08-15) -- NOT a named subclass. Branching on class alone would
    misclassify this as "no pipe" and spawn; branching on `winerror`
    (this module's actual implementation) must not."""
    exc = OSError("pipe busy")
    exc.winerror = 231
    exc.errno = 22  # CPython's winerror->errno table has no entry for 231

    def _raise_busy(pipe):
        raise exc

    monkeypatch.setattr(client, "_open_pipe", _raise_busy)
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append(1) or True
    )
    assert client.try_warm_dispatch(_MSG) is None
    assert spawns == []


def test_live_tree_cold_names_the_ruling_once_and_never_spawns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """C3 (plan 2026-08-20-a-refusal-cannot-exit-zero): an unstamped clone
    (a live working tree) is not a warm-server host, by DR-315 s2 (PM
    ruling), corroborated by DR-326/DR-331. The failure site must NAME
    those DRs, must say so only ONCE across repeated dispatches (not a
    per-call diagnostic), and must never attempt a spawn for a tree that
    cannot host a server."""
    from coordinator_core.warm import skew

    def _raise_unstamped(repo_root=None):
        raise skew.UnstampedEngineRootError("no stamp")

    monkeypatch.setattr(skew, "compute_client_token", _raise_unstamped)

    def _raise_enoent(pipe):
        raise FileNotFoundError(2, "no such pipe")

    monkeypatch.setattr(client, "_open_pipe", _raise_enoent)
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append(1) or True
    )
    # Undo the module-wide `engine_token` stub for THIS test only, so the
    # real `engine_token()` runs and reaches the (now stubbed)
    # `skew.compute_client_token` above.
    monkeypatch.setattr(client, "engine_token", _REAL_ENGINE_TOKEN)

    assert client.try_warm_dispatch(_MSG) is None
    assert client.try_warm_dispatch(_MSG) is None
    assert client.try_warm_dispatch(_MSG) is None

    assert spawns == [], "an unstamped clone must never spawn a server it cannot host"

    err = capsys.readouterr().err
    assert err.count("DR-315") == 1, "the ruling must be named exactly once, not per dispatch"
    assert "DR-326" in err and "DR-331" in err


def _drive_one_cold_dispatch(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Run a single dispatch whose token computation raises `exc` -- the
    shared body of the cold-reason tests below.

    The preamble past the token is stubbed out wholesale rather than at the
    transport: on POSIX it computes a socket path, and this suite's
    quarantined home makes that path exceed `sun_path` on macOS, which is
    itself a permanent cold condition. Letting it run would mix a second
    reason into a test about the first. `engine_token()` is still the REAL
    function, so the classification under test is genuinely exercised."""
    from coordinator_core.warm import skew

    def _raise(repo_root=None):
        raise exc

    monkeypatch.setattr(skew, "compute_client_token", _raise)
    monkeypatch.setattr(client, "engine_token", _REAL_ENGINE_TOKEN)
    monkeypatch.setattr(client, "_try_warm_dispatch_inner", lambda msg: client.engine_token() and None)
    assert client.try_warm_dispatch(_MSG) is None


def test_absent_engine_root_names_the_path_and_cites_no_ruling(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path
) -> None:
    """THE 2026-08-22 INSTALL-DOGFOOD CASE. A root-resolution channel (a
    pointer file carried in from another machine) named a path that does not
    exist here, and the operator was told their clone carries no build stamp
    -- a ruling, citing DR-315, for a condition DR-315 does not cover, with
    no path anywhere in it. The message must name the path that failed to
    resolve, and must NOT send the reader to a decision record."""
    from coordinator_core.warm import skew

    absent = tmp_path / "not-here"
    _drive_one_cold_dispatch(
        monkeypatch,
        skew.UnstampedEngineRootError("no stamp", root=absent, root_exists=False),
    )

    err = capsys.readouterr().err
    assert str(absent) in err, "the path that failed to resolve must be named"
    assert "does not exist" in err
    assert "DR-315" not in err, "no ruling covers a root that is not on disk"
    assert "root_channel_reconcile" in err, "must route to the reader that shows which channel produced it"

    assert client.last_cold_reason() is not None
    assert str(absent) in client.last_cold_reason()


def test_unstamped_but_present_root_keeps_the_ruling_and_gains_the_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path
) -> None:
    """The other arm, unchanged in substance: a live working tree that IS on
    disk is cold BY RULING, and naming DR-315 there is the point. It gains
    the resolved root so the reader can tell WHICH tree answered."""
    from coordinator_core.warm import skew

    present = tmp_path / "live-tree"
    present.mkdir()
    _drive_one_cold_dispatch(
        monkeypatch,
        skew.UnstampedEngineRootError("no stamp", root=present, root_exists=True),
    )

    err = capsys.readouterr().err
    assert "DR-315" in err
    assert str(present) in err


def test_root_exists_unknown_keeps_the_ruling_not_the_absent_path_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path
) -> None:
    """THE LANDMINE THIS PINS. `root_exists` defaults to `None` (unknown), not
    `True` (present) -- a future call site passing `root=` without
    `root_exists=` must not have presence fabricated on its behalf, but it
    also must not be told the path is absent on no evidence. Unknown falls
    through to the same ruling-shaped message as an explicit `True`."""
    from coordinator_core.warm import skew

    present = tmp_path / "live-tree"
    present.mkdir()
    exc = skew.UnstampedEngineRootError("no stamp", root=present)
    assert exc.root_exists is None

    _drive_one_cold_dispatch(monkeypatch, exc)

    err = capsys.readouterr().err
    assert "DR-315" in err
    assert str(present) in err
    assert "does not exist" not in err


def test_cold_reason_survives_past_the_one_shot_print(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The print is once per process; the REASON is read by a later caller
    turning a miss into a fatal error. A reason that existed only for the
    first dispatch would be missing from exactly the message that needs it."""
    from coordinator_core.warm import skew

    absent = tmp_path / "not-here"
    exc = skew.UnstampedEngineRootError("no stamp", root=absent, root_exists=False)
    _drive_one_cold_dispatch(monkeypatch, exc)
    monkeypatch.setattr(client, "_cold_reason", None)
    assert client.try_warm_dispatch(_MSG) is None

    assert client.last_cold_reason() is not None
    assert str(absent) in client.last_cold_reason()


def test_transient_warm_miss_records_no_cold_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The discriminator the fail-hard caller branches on: an ordinary miss
    (no pipe, server booting) is transient and MUST leave the reason unset,
    so "retry in a moment" stays the advice for the case where retrying
    actually works.

    Stubs the whole preamble rather than just the transport: on POSIX the
    real preamble computes a socket path first, and this suite's quarantined
    home makes that path exceed `sun_path` on macOS -- a genuinely permanent
    condition that would be recorded before any transport stub was reached.
    The subject here is the classification, not the transport."""
    monkeypatch.setattr(client, "_try_warm_dispatch_inner", lambda msg: None)

    assert client.try_warm_dispatch(_MSG) is None
    assert client.last_cold_reason() is None


def test_einval_without_231_never_spawns_and_never_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Problem 4: `OSError(errno=EINVAL)` with a winerror OTHER than 231
    (the distinct contended-pipe outcome measured at ~8% of failed opens
    under 16-way concurrency, docs/research/warm-engine-premise/warm-
    under-concurrency.md) must be classified the same as ERROR_PIPE_BUSY --
    go cold, never spawn -- and must NOT reach Backstop 2's generic
    stderr write, since it is now a named table row rather than an
    unanticipated exception."""
    exc = OSError("invalid argument")
    exc.errno = 22  # EINVAL
    exc.winerror = 1  # explicitly NOT 231, so this exercises the new branch

    def _raise_einval(pipe):
        raise exc

    monkeypatch.setattr(client, "_open_pipe", _raise_einval)
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append(1) or True
    )
    assert client.try_warm_dispatch(_MSG) is None
    assert spawns == []
    assert capsys.readouterr().err == ""


def test_permission_error_someone_elses_pipe_never_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = PermissionError(5, "access denied")

    def _raise_denied(pipe):
        raise exc

    monkeypatch.setattr(client, "_open_pipe", _raise_denied)
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append(1) or True
    )
    assert client.try_warm_dispatch(_MSG) is None
    assert spawns == []


def test_broken_pipe_mid_request_reopens_once_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    pipes = [
        _FakePipe(raise_on_write=BrokenPipeError()),
        _FakePipe(read_result=b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'),
    ]

    def _open(pipe):
        return pipes.pop(0)

    monkeypatch.setattr(client, "_open_pipe", _open)
    response = client.try_warm_dispatch(_MSG)
    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_broken_pipe_on_both_attempts_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakePipe(raise_on_write=BrokenPipeError()))
    spawns = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda repo_root, script, args=None: spawns.append(1) or True
    )
    assert client.try_warm_dispatch(_MSG) is None
    assert spawns == []  # BrokenPipeError is never the spawn trigger


def test_well_formed_success_response_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        client,
        "_open_pipe",
        lambda pipe: _FakePipe(read_result=b'{"jsonrpc":"2.0","id":1,"result":{"pong":true}}\n'),
    )
    response = client.try_warm_dispatch(_MSG)
    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"pong": True}}


def test_well_formed_error_envelope_is_used_not_treated_as_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live server's generic error response means "server up" per the
    table -- it must be returned and used, never mistaken for a dead
    server (the exact -32603-vs-no-listener confusion the ~49-process
    storm was caused by)."""
    monkeypatch.setattr(
        client,
        "_open_pipe",
        lambda pipe: _FakePipe(
            read_result=b'{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"boom"}}\n'
        ),
    )
    response = client.try_warm_dispatch(_MSG)
    assert response == {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "boom"}}


def test_engine_skew_error_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": client.ENGINE_SKEW, "message": "stale"}}
    ).encode("utf-8") + b"\n"
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakePipe(read_result=payload))
    assert client.try_warm_dispatch(_MSG) is None


def test_read_deadline_expiry_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    class _StuckPipe(_FakePipe):
        def readline(self):
            threading.Event().wait(client.READ_DEADLINE_SECS + 5)  # never returns in time
            return b'{"jsonrpc":"2.0","id":1,"result":{}}\n'

    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.05)
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _StuckPipe())
    assert client.try_warm_dispatch(_MSG) is None


# --- delivered mutations never go cold and never re-send -------------------
# The 2026-08-19 defect: a `git commit` outran the 2s liveness deadline, the
# client went cold, and the cold engine re-ran the op -- committing nothing,
# because the warm server (still finishing) had already committed the paths.
# The operator was told "no commit landed" about a commit that existed under a
# token the second execution had never minted. Evidence: peer session
# 30008a4b, commits e527554b8 / b330d767d. These pin the invariant that closes
# it: once a MUTATING request is DELIVERED, no re-send and no cold fallback.

_MUTATING_MSG = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "ceremony.scoped_git_commit",
    "params": {},
}


def _assert_indeterminate(response) -> None:
    assert response is not None, "a delivered mutation must never go cold"
    assert response["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert "may have COMPLETED" in response["error"]["message"]


def test_delivered_mutation_waits_past_the_liveness_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE FIX, happy half. `READ_DEADLINE_SECS` asks "is the server wedged";
    a mutation that merely takes longer than that is not wedged, it is working.
    The client keeps waiting on the SAME read and returns the real answer, so
    the false "no commit landed" never happens in the first place."""
    import threading

    started = threading.Event()

    class _SlowPipe(_FakePipe):
        def readline(self):
            started.set()
            threading.Event().wait(0.20)
            return b'{"jsonrpc":"2.0","id":1,"result":{"committed":true}}\n'

    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "MUTATION_READ_DEADLINE_SECS", 5.0)
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _SlowPipe())

    response = client.try_warm_dispatch(_MUTATING_MSG)
    assert started.is_set()
    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"committed": True}}


def test_delivered_mutation_that_never_answers_is_indeterminate_not_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE FIX, refusal half. Past even the mutation deadline the answer is an
    honest "it may have happened", never `None` -- `None` is this module's
    go-cold signal, and going cold re-runs the mutation.

    Mechanism check: reverting the mutation branch makes this return None, which
    is precisely the state that re-executed a landed commit."""
    import threading

    class _StuckPipe(_FakePipe):
        def readline(self):
            threading.Event().wait(30)
            return b'{"jsonrpc":"2.0","id":1,"result":{}}\n'

    # 0.02s / 0.5s -- a 480ms margin, not the original 30ms one: at this
    # box's stated 50-70-concurrent-session load norm a thin margin here
    # flakes under contention (reviewer finding, sidecar dcf219af, SLICE 1).
    # The StuckPipe's own 30s wait means the test still resolves in ~0.5s
    # either way -- only the deadline widens, nothing the test asserts does.
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "MUTATION_READ_DEADLINE_SECS", 0.5)
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _StuckPipe())

    _assert_indeterminate(client.try_warm_dispatch(_MUTATING_MSG))


def test_broken_pipe_after_delivery_is_not_resent_for_a_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The table's "one re-open" retry is safe only while the server cannot have
    executed the request. After delivery it is a second execution of a mutation,
    so the retry must not fire -- pinned by counting opens, not just by the
    return value."""
    opens = []

    def _open(pipe):
        fake = _FakePipe(read_result=BrokenPipeError("mid-response"))
        opens.append(fake)
        return fake

    monkeypatch.setattr(client, "_open_pipe", _open)
    _assert_indeterminate(client.try_warm_dispatch(_MUTATING_MSG))
    assert len(opens) == 1, "a delivered mutation must not be re-sent"


def test_broken_pipe_before_delivery_still_re_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same line: a write that never landed leaves a
    partial frame the server cannot parse as a request, so it cannot have
    executed. That re-open stays, mutation or not."""
    opens = []

    def _open(pipe):
        fake = _FakePipe(raise_on_write=BrokenPipeError("before delivery"))
        opens.append(fake)
        return fake

    monkeypatch.setattr(client, "_open_pipe", _open)
    assert client.try_warm_dispatch(_MUTATING_MSG) is None
    assert len(opens) == 2, "an undelivered request keeps the table's re-open"


def test_zero_byte_close_still_goes_cold_for_a_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one post-delivery shape that still goes cold for a mutation, and the
    distinction is evidence rather than convenience: not one byte came back, which
    is what a connection the server never serviced looks like. That is the common
    case here, not a corner -- the warm server keeps a single pending listener, so
    under this box's load norm connections routinely die unserviced, and
    `flush()` returning only ever proved the bytes left THIS process.

    Regression guard: making this indeterminate turned a large-pathspec commit
    into an outright failure instead of the cold path it has always taken
    (`test_large_pathspec_round_trips_end_to_end`)."""
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakePipe(read_result=b""))
    assert client.try_warm_dispatch(_MUTATING_MSG) is None


def test_malformed_response_is_indeterminate_for_a_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same reasoning as EOF: an unreadable answer is not evidence of an
    unperformed op."""
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakePipe(read_result=b"not json\n"))
    _assert_indeterminate(client.try_warm_dispatch(_MUTATING_MSG))


def test_engine_skew_still_goes_cold_for_a_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one post-delivery cold fallback that stays safe for a mutation, and
    it is safe by the server's own construction rather than by assumption:
    `server.py::_serve_one` answers ENGINE_SKEW and returns BEFORE it calls
    `dispatch(msg)`, so a skewed server has provably not executed the op."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": client.ENGINE_SKEW, "message": "stale"}}
    ).encode("utf-8") + b"\n"
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakePipe(read_result=payload))
    assert client.try_warm_dispatch(_MUTATING_MSG) is None


def test_mutation_deadline_tracks_ipc_timeout_for_not_a_flat_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer finding (sidecar dcf219af, SLICE 1): a flat
    `MUTATION_READ_DEADLINE_SECS` outlives the CALLER's own kill ceiling for
    every mutating op except `ceremony.scoped_git_commit`, because the
    ceiling is `ipc._timeout_for(op) + MARGIN` and every other op resolves
    to `ipc`'s ~30s default. `_mutation_deadline_for` must derive from that
    same function, not the flat constant, whenever the constant is at its
    untouched default."""
    import coordinator_core.ipc as ipc

    monkeypatch.setattr(ipc, "_timeout_for", lambda method: 7.0)
    assert client._mutation_deadline_for("some.mutating.op") == 7.0

    monkeypatch.setattr(ipc, "_timeout_for", lambda method: 150.0)
    assert client._mutation_deadline_for("ceremony.scoped_git_commit") == 150.0


def test_mutation_deadline_derivation_does_not_outwait_the_ops_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutating op with a short (default-sized) engine budget must not
    wait past it -- the whole point of deriving per-op rather than using a
    flat 120s that outlives the caller's own ~40s kill ceiling for every op
    but the one the original incident concerned."""
    import threading

    import coordinator_core.ipc as ipc

    class _StuckPipe(_FakePipe):
        def readline(self):
            threading.Event().wait(30)
            return b'{"jsonrpc":"2.0","id":1,"result":{}}\n'

    monkeypatch.setattr(ipc, "_timeout_for", lambda method: 0.1)
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _StuckPipe())

    _assert_indeterminate(client.try_warm_dispatch(_MUTATING_MSG))


def test_mutation_deadline_override_wins_over_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit `MUTATION_READ_DEADLINE_SECS` override (test or operator)
    must win verbatim over the per-op derivation -- otherwise a monkeypatch
    on the constant would be silently ignored, which is exactly what would
    have broken `test_delivered_mutation_waits_past_the_liveness_deadline`
    and `test_delivered_mutation_that_never_answers_is_indeterminate_not_cold`
    once derivation was introduced."""
    import coordinator_core.ipc as ipc

    monkeypatch.setattr(ipc, "_timeout_for", lambda method: 150.0)
    monkeypatch.setattr(client, "MUTATION_READ_DEADLINE_SECS", 9.0)
    assert client._mutation_deadline_for("ceremony.scoped_git_commit") == 9.0


def test_unclassified_op_is_treated_as_mutating(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: an op with no classification entry must be treated as a
    mutation. Being wrong this way costs one honest error; being wrong the other
    way executes a mutation twice."""
    monkeypatch.setattr(
        client, "_open_pipe", lambda pipe: _FakePipe(read_result=b"not json\n")
    )
    unknown = {"jsonrpc": "2.0", "id": 1, "method": "op.that.does.not.exist", "params": {}}
    _assert_indeterminate(client.try_warm_dispatch(unknown))


def test_malformed_response_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakePipe(read_result=b"not json\n"))
    assert client.try_warm_dispatch(_MSG) is None


def test_empty_response_goes_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakePipe(read_result=b""))
    assert client.try_warm_dispatch(_MSG) is None


def test_unexpected_exception_never_propagates_backstop_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backstop 2: nothing in the preamble may fail in a way that fails the
    op. Even a wholly unanticipated exception (not one of the table's
    named rows) must resolve to a cold-path signal, never propagate."""

    def _explode(pipe):
        raise RuntimeError("unanticipated transport failure")

    monkeypatch.setattr(client, "_open_pipe", _explode)
    assert client.try_warm_dispatch(_MSG) is None


def test_request_payload_carries_engine_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePipe()
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: fake)
    client.try_warm_dispatch(_MSG)
    assert len(fake.written) == 1
    sent = json.loads(fake.written[0].decode("utf-8"))
    assert sent["_engine_token"] == client.engine_token()
    assert sent["method"] == "ping"


def test_pipe_handle_always_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePipe()
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: fake)
    client.try_warm_dispatch(_MSG)
    assert fake.closed is True


def test_request_payload_carries_the_callers_resolved_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """C-warm-identity: the client resolves ITS OWN (true-caller)
    environment, cold, and attaches it beside `_engine_token` -- this is
    the sole place identity crosses the wire; the server never re-resolves
    it from its own environment (state/bug-backlog/2026-08-18-a-warm-
    server-stamps-every-op-it-serves-eeb801fc6bee.yaml)."""
    monkeypatch.setattr(client, "_caller_session_id", lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    fake = _FakePipe()
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: fake)
    client.try_warm_dispatch(_MSG)
    sent = json.loads(fake.written[0].decode("utf-8"))
    assert sent["_session_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_request_payload_omits_session_id_when_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unresolvable identity (`_caller_session_id()` returns "") must be
    OMITTED from the request, never sent as an empty string or fabricated
    -- the server-side no-op fallback keys on the field's absence."""
    monkeypatch.setattr(client, "_caller_session_id", lambda: "")
    fake = _FakePipe()
    monkeypatch.setattr(client, "_open_pipe", lambda pipe: fake)
    client.try_warm_dispatch(_MSG)
    sent = json.loads(fake.written[0].decode("utf-8"))
    assert "_session_id" not in sent


def test_caller_session_id_resolves_via_session_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert client._caller_session_id() == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_engine_skew_distinct_from_structural_pin_error() -> None:
    """`client.ENGINE_SKEW` and `ipc.STRUCTURAL_PIN_ERROR` collided at
    -32001 (both are `-32001` by hand-coincidence, never a shared
    definition) -- the client's skew-first check masked it, at the cost of
    a wasted cold spawn per structural-pin op and `diagnostics_probes`'s
    `always_structural_pin` silently measuring the cold path instead of the
    warm one. `ENGINE_SKEW` is the one that moves: it crosses only this
    repo's own pipe, while `STRUCTURAL_PIN_ERROR` is a cross-repo surface
    (cc_invoke pins it; DoE reads rc=2 off it) that must never renumber.
    """
    from coordinator_core import ipc

    assert client.ENGINE_SKEW != ipc.STRUCTURAL_PIN_ERROR
    assert ipc.STRUCTURAL_PIN_ERROR == -32001


def test_socket_path_too_long_is_a_permanent_reason_not_a_transient_miss(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """THE SECOND ROUTE to the same operator-facing contradiction. A runtime
    base long enough to blow `sun_path` makes warm permanently unavailable
    for every call from that base, and cold fallback is disabled -- so the
    caller must not advise a retry that changes nothing. The reason must
    carry the path and the byte budget the exception already measured."""
    from coordinator_core.warm.election import SocketPathTooLongError

    exc = SocketPathTooLongError(
        "socket path is 168 bytes, over the 100-byte sun_path budget: '/very/long/faketoken.sock'"
    )
    monkeypatch.setattr(
        client, "_try_warm_dispatch_inner", lambda msg: (_ for _ in ()).throw(exc)
    )

    assert client.try_warm_dispatch(_MSG) is None

    reason = client.last_cold_reason()
    assert reason is not None
    assert "168 bytes" in reason and "sun_path" in reason
    assert "/very/long/faketoken.sock" in reason
    assert "COORDINATOR_WARM_RUNTIME_BASE" in reason


def test_other_preamble_failures_stay_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """The narrowness is the point: an unanticipated preamble failure is
    exactly where "this recurs forever" is a guess, and a wrong permanent
    verdict removes the retry advice that would have worked."""
    monkeypatch.setattr(
        client,
        "_try_warm_dispatch_inner",
        lambda msg: (_ for _ in ()).throw(RuntimeError("something unforeseen")),
    )

    assert client.try_warm_dispatch(_MSG) is None
    assert client.last_cold_reason() is None
