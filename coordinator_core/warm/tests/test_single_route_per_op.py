"""AC6c: one route per logical op, and the executor owns the record.

Spec backlink: docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md
               § Acceptance Criteria (AC6c), § C12.

Negative spec: these tests pin MUTUAL EXCLUSION PER LOGICAL OP, not writer
ownership. Writer ownership alone does not hold the line -- a call routed over
`http` while a shim also calls `ipc.dispatch_from_hook` emits a second
started/complete pair however careful each writer is. The cross-process case is
the whole point; `dispatch_message` already guarantees single-recording WITHIN a
process, so nothing here re-tests that.
"""

import os

import pytest

from coordinator_core.telemetry import op_latency


def test_execution_routes_is_a_closed_set_stated_once():
    assert op_latency.EXECUTION_ROUTES == frozenset(
        {op_latency.IN_PROCESS, op_latency.WARM_SERVER, op_latency.HTTP_SERVER}
    )


def test_route_defaults_to_in_process(monkeypatch):
    monkeypatch.delenv("COORDINATOR_EXECUTION_ROUTE", raising=False)
    assert op_latency.execution_route() == op_latency.IN_PROCESS


@pytest.mark.parametrize(
    "declared",
    [op_latency.IN_PROCESS, op_latency.WARM_SERVER, op_latency.HTTP_SERVER],
)
def test_a_serving_process_can_declare_its_route(monkeypatch, declared):
    monkeypatch.setenv("COORDINATOR_EXECUTION_ROUTE", declared)
    assert op_latency.execution_route() == declared


def test_unrecognised_route_degrades_rather_than_raising(monkeypatch):
    """A wrong label costs one census row; a raise here would cost the op."""
    monkeypatch.setenv("COORDINATOR_EXECUTION_ROUTE", "not-a-route")
    assert op_latency.execution_route() == op_latency.IN_PROCESS


def test_both_row_kinds_carry_the_route(tmp_path, monkeypatch):
    """Both kinds funnel through `_write_entry`, so the stamp lands once."""
    monkeypatch.setenv("COORDINATOR_EXECUTION_ROUTE", op_latency.HTTP_SERVER)
    monkeypatch.delenv("COORDINATOR_OP_LATENCY_DISABLE", raising=False)

    written = []
    monkeypatch.setattr(op_latency, "_append_line", lambda sink, encoded: written.append(encoded))
    monkeypatch.setattr(op_latency, "_sink_path", lambda _p: tmp_path / "sink.ndjson")
    # `_write_entry` resolves the sink through lifecycle.git_common_dir and swallows
    # its RuntimeError on a non-repo path -- unstubbed, this test silently writes
    # nothing and asserts on an empty list.
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda _p: tmp_path)

    op_latency.record_op_started(op="x.y", t_start=1.0, corr_id="c1", repo_root=tmp_path)
    op_latency.record_op_latency(
        op="x.y", t_start=1.0, elapsed_ms=1000.0, outcome="ok", corr_id="c1", repo_root=tmp_path
    )

    import json

    rows = [json.loads(bytes(b).decode("utf-8")) for b in written]
    assert rows, "no rows written -- the stamp cannot be asserted"
    assert {r["kind"] for r in rows} == {"started", "complete"}
    assert all(r["route"] == op_latency.HTTP_SERVER for r in rows)


def test_double_routed_corr_ids_flags_the_ac6c_violation():
    """The http-plus-in-process double pair is what this must catch."""
    entries = [
        {"corr_id": "same-op", "route": op_latency.HTTP_SERVER, "kind": "started"},
        {"corr_id": "same-op", "route": op_latency.IN_PROCESS, "kind": "started"},
        {"corr_id": "clean-op", "route": op_latency.IN_PROCESS, "kind": "started"},
        {"corr_id": "clean-op", "route": op_latency.IN_PROCESS, "kind": "complete"},
    ]
    assert op_latency.double_routed_corr_ids(entries) == {"same-op"}


def test_double_routed_ignores_rows_missing_either_field():
    """Older rows predate the stamp; a census must not read them as violations."""
    entries = [
        {"corr_id": "legacy", "kind": "started"},
        {"route": op_latency.IN_PROCESS, "kind": "started"},
        {"corr_id": "legacy", "route": op_latency.IN_PROCESS, "kind": "complete"},
    ]
    assert op_latency.double_routed_corr_ids(entries) == set()


def test_warm_server_declares_the_warm_server_route(monkeypatch):
    """The stamp was inert until a serving process declared itself.

    Without this, every row in the corpus reads `in_process` -- including the
    server's own -- and `double_routed_corr_ids` cannot fire, because a
    detector for one `corr_id` under two routes needs two route values to
    exist.
    """
    from coordinator_core.warm import server

    # Seed the var so monkeypatch owns its restoration: the production write
    # below is a deliberate process-wide mutation (a serving process declaring
    # itself), and an unseeded `delenv` records nothing to roll back.
    monkeypatch.setenv(op_latency.ROUTE_ENV, op_latency.IN_PROCESS)
    server._declare_execution_route()

    assert os.environ[op_latency.ROUTE_ENV] == op_latency.WARM_SERVER
    assert op_latency.execution_route() == op_latency.WARM_SERVER


def test_route_is_declared_before_the_server_starts_accepting():
    """Pool workers inherit the environment AT SPAWN, so the declaration must
    precede any dispatch -- `_run_guarded` is the only place that ordering
    holds.

    Source-level because the boot path binds a real endpoint and never
    returns; the ordering it asserts is the whole reason the stamp reaches a
    pool worker at all.

    TARGETS `_run_guarded`, NOT `main()`. This parsed `inspect.getsource(
    server.main)` until 2026-08-21 and had been failing since `main()` became
    a thin wrapper around `_run_guarded` (the STEP 0 crash-reporting guard),
    which is where both calls actually live. The ordering itself never
    stopped holding -- only the function this test was reading.
    """
    import ast
    import inspect

    from coordinator_core.warm import server

    tree = ast.parse(inspect.getsource(server._run_guarded))
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "_declare_execution_route" in calls, "the boot path never declares its route"
    assert calls.index("_declare_execution_route") < calls.index("_ServerContext"), (
        "route must be declared before the server context that builds the dispatch pool"
    )
