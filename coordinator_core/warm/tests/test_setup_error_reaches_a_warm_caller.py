"""Pins the WARM half of the fleet setup-error diagnostic channel.

Purpose: a fleet setup error returns the frozen exit_code:1 envelope, which
carries no reason field by contract (§2.1), so the reason exists ONLY as a
side-channel write. `ops/fleet/tests/test_setup_error_stderr_channel.py` pins
the COLD half of that channel — the op process's own stderr, which for a cold
spawn is the caller's pipe. Under the warm engine "the op process" is the
SERVER, so that write lands on the server's stderr and the caller reads an
empty stream: same op, same refusal, same caller code, and the reason is gone.

Observed live 2026-08-19 — `cross-repo-memo send` against a running warm
server printed `route_mutation: op='memo.send' refused (exit_code=1, failed=0)`
and nothing else, while `COORDINATOR_WARM=0` on the identical invocation
printed the full `scoped_to.sha does not resolve` reason. Reported by
doe-claude-em as a CLI-forwarder defect; the forwarder reads stderr on the
rc==0 path correctly (`coordinator/bin/lib/cc_invoke.py :: route_mutation`) and
there was simply nothing on the stream.

These tests pin the three links of the warm half — producer emits, server
attaches to the transport frame, client re-emits and pops — plus the negative
spec that the cold path is unchanged and the frozen `result` envelope is never
expanded.

Spec backlink: cross-repo/inbox/2026-08-19-doe-claude-em-warm-engine-seam-async-declined.md
"""

from __future__ import annotations

from coordinator_core.ops.fleet._common import build_setup_error_result
from coordinator_core.warm.entry_seam import (
    collecting_diagnostics,
    emit_diagnostic,
    per_request_state,
)


# ---------------------------------------------------------------------------
# Link 1 — the producer
# ---------------------------------------------------------------------------

def test_emit_diagnostic_is_a_noop_with_no_sink_bound():
    """The cold path binds nothing; emitting must not raise or accumulate."""
    emit_diagnostic("no sink is bound")  # must not raise
    with collecting_diagnostics() as sink:
        pass
    assert sink == []


def test_setup_error_reason_reaches_a_bound_sink():
    with collecting_diagnostics() as sink:
        envelope = build_setup_error_result("send", False, "the receiver is not registered")
    assert len(sink) == 1
    assert "the receiver is not registered" in sink[0]
    # The FROZEN envelope is not expanded — the reason rides the sink, never `result`.
    assert envelope["exit_code"] == 1
    assert "reason" not in envelope
    assert "error" not in envelope
    assert "_stderr" not in envelope


def test_setup_error_still_writes_stderr_when_a_sink_is_bound(capsys):
    """The warm sink is ADDITIVE. A producer that emitted only to the sink
    would go silent on the cold path, which is the majority of invocations."""
    with collecting_diagnostics():
        build_setup_error_result("send", False, "receiver unresolvable")
    captured = capsys.readouterr()
    assert "fleet op setup error: receiver unresolvable" in captured.err


def test_sink_is_token_reset_scoped_and_does_not_leak():
    with collecting_diagnostics() as outer:
        emit_diagnostic("outer-before")
        with collecting_diagnostics() as inner:
            emit_diagnostic("inner-only")
        emit_diagnostic("outer-after")
    assert inner == ["inner-only"]
    assert outer == ["outer-before", "outer-after"]
    # Closed: nothing is bound outside the blocks.
    emit_diagnostic("dropped")
    assert outer == ["outer-before", "outer-after"]


def test_per_request_state_binds_the_sink_only_when_asked():
    collected: list = []
    with per_request_state(diagnostics=collected):
        emit_diagnostic("bound")
    assert collected == ["bound"]

    with per_request_state():
        emit_diagnostic("unbound")  # no sink — must not raise
    assert collected == ["bound"]


# ---------------------------------------------------------------------------
# Link 2 — the server attaches to the TRANSPORT frame, not to `result`
# ---------------------------------------------------------------------------

def _run_dispatch_with(monkeypatch, fake_dispatch):
    """Drive `server._run_dispatch` against a stand-in dispatch core.

    Patches `coordinator_core.ipc.dispatch_message`, which `_run_dispatch`
    imports function-locally, so the real op registry is never touched.
    """
    import coordinator_core.ipc as ipc
    from coordinator_core.warm import server

    monkeypatch.setattr(ipc, "dispatch_message", fake_dispatch)
    return server._run_dispatch({"jsonrpc": "2.0", "id": 1, "method": "memo.send"})


def test_server_attaches_collected_diagnostics_to_the_frame(monkeypatch):
    async def fake_dispatch(msg):
        emit_diagnostic("fleet op setup error: scoped_to.sha does not resolve")
        return {"jsonrpc": "2.0", "id": 1, "result": {"exit_code": 1, "mode": "send"}}

    response = _run_dispatch_with(monkeypatch, fake_dispatch)

    assert "scoped_to.sha does not resolve" in response["_stderr"]
    # Sibling of `result`, never inside it: the wire envelope stays frozen.
    assert "_stderr" not in response["result"]
    assert response["result"] == {"exit_code": 1, "mode": "send"}


def test_server_adds_no_key_when_the_op_emitted_nothing(monkeypatch):
    async def fake_dispatch(msg):
        return {"jsonrpc": "2.0", "id": 1, "result": {"exit_code": 0, "mode": "send"}}

    response = _run_dispatch_with(monkeypatch, fake_dispatch)
    assert "_stderr" not in response


def test_server_does_not_leak_one_requests_diagnostics_into_the_next(monkeypatch):
    """Connections are served on their own threads against one long-lived
    process — the reason this is a per-request ContextVar and not a capture of
    the process-global `sys.stderr`."""
    async def noisy(msg):
        emit_diagnostic("first request's reason")
        return {"jsonrpc": "2.0", "id": 1, "result": {"exit_code": 1}}

    async def quiet(msg):
        return {"jsonrpc": "2.0", "id": 2, "result": {"exit_code": 0}}

    first = _run_dispatch_with(monkeypatch, noisy)
    second = _run_dispatch_with(monkeypatch, quiet)

    assert "first request's reason" in first["_stderr"]
    assert "_stderr" not in second


# ---------------------------------------------------------------------------
# Link 3 — the client re-emits on the caller's own stderr, and POPS
# ---------------------------------------------------------------------------

def test_client_pops_and_reemits_rather_than_passing_the_key_through():
    """The response a warm caller receives must have the same SHAPE as a cold
    one — `_stderr` is a transport detail no consumer may observe.

    Structural, not behavioural: the pop sits inside `_try_warm_dispatch_inner`
    between a real pipe read and the return, so exercising it end-to-end needs
    a live server. What this pins is that the client still POPS (rather than
    passing the key through to `invoke.__main__`) and still writes to its own
    stderr — the two ways this link silently rots. The behavioural half is
    covered by the server tests above plus the live reproduction in the module
    docstring.
    """
    from pathlib import Path

    from coordinator_core.warm import client

    text = Path(client.__file__).read_text(encoding="utf-8")
    assert 'response.pop("_stderr", None)' in text, "client no longer pops the transport key"
    assert "print(op_stderr, file=sys.stderr" in text, "client no longer re-emits on stderr"
