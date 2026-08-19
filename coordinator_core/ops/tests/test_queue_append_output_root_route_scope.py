"""
coordinator_core.ops.tests.test_queue_append_output_root_route_scope — the
``QUEUE_APPEND_OUTPUT_ROOT`` redirect is honoured on the in-process route only.

Purpose: ``QUEUE_APPEND_OUTPUT_ROOT`` is a property of a CALLING process (a
test redirecting its own writes into a tmpdir). Under the warm engine the op
executes inside a long-lived server whose environment was inherited from
whichever session spawned it, so one session's exported redirect becomes a
standing redirect for every OTHER session's writes that server handles, for
as long as it lives. Observed live: bug-backlog rows landing in
``pytest-of-<user>/pytest-*/…/state/bug-backlog/`` twice, from two different
shells, while the CLI printed a normal repo path and exited 0 — a silently
lost write.

``queue_append._output_root_override`` closes it by refusing the env read on
any route other than ``IN_PROCESS``. These tests pin both directions: the
genuine in-process test caller keeps working, and a served handler ignores
the server's inherited copy.

Bug: state/bug-backlog/2026-08-19-published-caller-imports-a-mirror-only-name-from-the-live-tree.yaml
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops import queue_append
from coordinator_core.telemetry import op_latency


@pytest.fixture
def _redirect(monkeypatch, tmp_path):
    monkeypatch.setenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, str(tmp_path))
    return str(tmp_path)


def test_in_process_route_honours_the_redirect(_redirect, monkeypatch):
    """The case the env var exists for: a test isolating its own writes."""
    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)
    assert op_latency.execution_route() == op_latency.IN_PROCESS
    assert queue_append._output_root_override() == _redirect


@pytest.mark.parametrize("route", [op_latency.WARM_SERVER, op_latency.HTTP_SERVER])
def test_served_routes_ignore_the_servers_inherited_redirect(_redirect, monkeypatch, route):
    """The leak: a server process must never redirect a caller's write to a
    root the CALLER never asked for. Asserted for every non-in-process route,
    not just `warm_server`, so a future transport cannot reopen the hole by
    declaring a new route label."""
    monkeypatch.setenv(op_latency.ROUTE_ENV, route)
    assert op_latency.execution_route() == route
    # The redirect IS set in this process's environment — the point is that a
    # served handler declines to act on it, not that it was never there.
    assert os.environ[queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV] == _redirect
    assert queue_append._output_root_override() is None


def test_absent_env_is_none_on_every_route(monkeypatch):
    """No redirect set is `None`, not `""` — the caller branches on falsiness
    and an empty string would `os.path.join` into a relative path."""
    monkeypatch.delenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, raising=False)
    for route in (op_latency.IN_PROCESS, op_latency.WARM_SERVER):
        monkeypatch.setenv(op_latency.ROUTE_ENV, route)
        assert queue_append._output_root_override() is None


def test_empty_env_value_is_not_treated_as_a_redirect(monkeypatch):
    """An exported-but-empty var is 'unset', not 'redirect to the cwd'."""
    monkeypatch.setenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, "")
    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)
    assert queue_append._output_root_override() is None
