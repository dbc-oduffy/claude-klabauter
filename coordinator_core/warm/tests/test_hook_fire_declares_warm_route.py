"""AC2 (docs/plans/2026-08-23-no-hook-fire-pays-an-interpreter-start.md).

AC2's own wording is two clauses joined by "and both must hold":

    (a) A hook fire against a resident warm server dispatches through it,
        proven by telemetry `route` reading `warm_server`, not by timing
        alone.
    (b) DR-347 Ruling 3's stamp gate (`ipc.dispatch_message`'s
        `UNSTAMPED_ENGINE_ROOT_ERROR`, -32005) is what forbids an
        unstamped fallback.

H8 (2b926e09b) made clause (a) READABLE for the first time: before it,
`supervisor.main` never called `server._declare_execution_route`, so every
`/hook` op-latency row stamped the `in_process` default and no telemetry
read could ever return `warm_server` -- the route bit existed
(`telemetry.op_latency.execution_route`) but nothing serving `/hook` ever
set it. `main()` now calls `_declare_execution_route()` at boot (see that
function's step 3) -- but `main()` itself binds a live socket and blocks
in `serve_forever()`, so it cannot be driven from a test. This file calls
`supervisor._declare_execution_route()` directly, exactly what `main()`'s
boot sequence does, then fires a REAL event through the REAL handler.

Clause (b) already has full, dedicated coverage --
`coordinator_core/tests/test_dispatch_message.py ::
test_stamp_gate_refuses_when_unstamped_and_opt_in_off` drives the refusing
branch end-to-end (unstamped root, opt-in off, handler never runs,
response carries `UNSTAMPED_ENGINE_ROOT_ERROR`). Duplicating that here
would be a second copy of the same assertion to drift from the first; this
file instead pins the constant's identity and that the refusal builder
actually uses it, so AC2's "both must hold" is verifiable from one place
per clause without re-deriving either.
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core import ipc
from coordinator_core.telemetry import op_latency
from coordinator_core.warm import supervisor
from coordinator_core.warm.tests.test_supervisor_hook_serves_real_guard import (
    _bind_handler,
    _post,
)


def test_a_hook_fire_through_a_declared_server_stamps_warm_server_route(tmp_path: Path):
    """Clause (a), end to end. `_bind_handler` (reused from
    `test_supervisor_hook_serves_real_guard.py` rather than a second harness) binds
    the real `_Handler` around a real loopback socket; leaving `dispatch=None` means
    `_serve_line` falls through to its own default, `_run_dispatch`, which is the
    real `coordinator_core.ipc.dispatch_message` chokepoint -- not a fake standing in
    for it. The fired event resolves to the REAL registered `warm_guard.evaluate`
    op (`coordinator_core/ops/warm_guard_evaluate.py`), so this exercises the exact
    path a resident server serves a hook through, not a stub of it.

    `supervisor._declare_execution_route()` is called directly, mirroring `main()`'s
    boot-time call -- `main()` cannot be driven here (it blocks in `serve_forever`
    over a real bound port). The prior env value is restored in `finally` so this
    mutation of process-wide `os.environ` never leaks into a later test.

    The assertion reads the LIVE op-latency sink rather than trusting a captured
    return value: `route` is stamped by `op_latency._write_entry` at the moment the
    completion row is appended (`ipc.dispatch_message`'s `finally` block), which is
    the actual telemetry surface AC2 requires proof from -- timing alone (how long
    the call took) proves nothing about which process executed it.
    """
    prior_route = os.environ.get(op_latency.ROUTE_ENV)
    supervisor._declare_execution_route()
    own_pid = os.getpid()
    try:
        httpd, port = _bind_handler(tmp_path, dispatch=None)
        try:
            status, body = _post(
                port,
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "s-ac2-warm-route",
                    "cwd": str(tmp_path),
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hi"},
                },
            )
        finally:
            httpd.shutdown()
    finally:
        if prior_route is None:
            os.environ.pop(op_latency.ROUTE_ENV, None)
        else:
            os.environ[op_latency.ROUTE_ENV] = prior_route

    assert status == 200
    assert "permissionDecision" not in body.get("hookSpecificOutput", {})

    sinks = op_latency.sink_generations(Path.cwd())
    assert sinks, "no op-latency sink resolvable for this repo -- cannot verify telemetry"
    entries, _head_truncated = op_latency.tail_entries(sinks[0], tail_bytes=2_000_000, max_rows=20_000)

    # Filtered by THIS process's pid: the sink is shared with every other concurrent
    # session on the box (module docstring's "no read-modify-write, no lock file"
    # append discipline), and `_run_dispatch` executes synchronously on this same
    # request-handling thread -- no ProcessPoolExecutor is in play for the http
    # transport's default dispatch -- so a completed `warm_guard.evaluate` row
    # carrying our own pid unambiguously belongs to the fire above.
    own_rows = [
        e
        for e in entries
        if e.get("op") == supervisor.GUARD_OP_NAME
        and e.get("pid") == own_pid
        and e.get("kind") != "started"
    ]
    assert own_rows, "no completed warm_guard.evaluate op-latency row for this process"
    assert own_rows[-1]["route"] == op_latency.WARM_SERVER


def test_stamp_gate_constant_is_wired_into_the_refusal_it_names():
    """Clause (b), pinned by identity/wiring rather than re-run. The behavioural
    test for this refusal already lives at
    `coordinator_core/tests/test_dispatch_message.py ::
    test_stamp_gate_refuses_when_unstamped_and_opt_in_off` (drives an unstamped
    root through `dispatch_message`, asserts the handler never ran and the
    response carries this exact code) -- this test does not re-drive that path.
    It pins the two facts AC2's clause (b) actually names: the code value itself
    (-32005, DR-347 Ruling 3's app-code slot) never drifts, and the refusal
    envelope `dispatch_message` answers with is built from that SAME named
    constant, not a second literal `-32005` that could silently diverge from it.
    """
    assert ipc.UNSTAMPED_ENGINE_ROOT_ERROR == -32005

    envelope = ipc._unstamped_dispatch_refusal(request_id=7)
    assert envelope["error"]["code"] is ipc.UNSTAMPED_ENGINE_ROOT_ERROR
    assert envelope["id"] == 7
