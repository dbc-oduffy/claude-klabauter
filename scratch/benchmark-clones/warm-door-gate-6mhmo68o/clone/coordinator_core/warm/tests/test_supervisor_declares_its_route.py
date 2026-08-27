"""Pins that `warm.supervisor.main` declares its execution route before serving,
mirroring `warm.server`'s own invariant for the pipe transport
(`test_single_route_per_op.py :: test_route_is_declared_before_the_server_starts_accepting`).

AC2 (docs/plans/2026-08-23-no-hook-fire-pays-an-interpreter-start.md) requires the warm
route be provable from telemetry `route` reading `warm_server`, never from timing --
`server._declare_execution_route` is the sole primitive that stamp comes from, and
until `supervisor.main` calls it too, every op-latency row a `/hook` fire on this
transport writes carries the `in_process` default, making that read unachievable no
matter what a caller measures.

Source-level, not a live boot: `main()` binds a real socket and calls
`serve_forever()`, which never returns -- there is no live-process way to observe the
env-var side effect from inside a test. The ordering this asserts is what the
request-handling threads this server spawns actually rely on: they inherit
`os.environ` at whatever moment they read it, so the route must be set before the
context that builds their shared handler is constructed, not merely before the
process eventually exits.
"""

import ast
import inspect

from coordinator_core.warm import supervisor


def _calls_in(func) -> list:
    tree = ast.parse(inspect.getsource(func))
    return [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]


def test_supervisor_main_declares_its_execution_route():
    calls = _calls_in(supervisor.main)
    assert "_declare_execution_route" in calls, (
        "supervisor.main never declares its execution route -- every /hook op-latency "
        "row on this transport stamps the in_process default, and AC2 can never read "
        "warm_server off it"
    )


def test_route_is_declared_before_the_server_context_is_built():
    calls = _calls_in(supervisor.main)
    assert calls.index("_declare_execution_route") < calls.index("_ServerContext"), (
        "route must be declared before the context whose handler serves the first "
        "request is built"
    )


def test_supervisor_reuses_servers_declare_function_rather_than_a_second_copy():
    """Module docstring's own negative-spec convention (WHAT THIS MODULE MUST NOT
    REIMPLEMENT): a second `_declare_execution_route` defined in this module would
    drift from `warm.server`'s the moment either one's stamp value changed."""
    from coordinator_core.warm import server

    assert supervisor._declare_execution_route is server._declare_execution_route
