"""coordinator/bin/tests/test_native_route_entry_served_side.py

Subject: `entry_point_shim._native_route_entry`'s served-side short-circuit.

`invoke.from_argv` serves `baton-assemble` / `pickup-assemble` /
`workstream-complete-assemble` by running that entrypoint's `main` in-process,
and that `main` is the shim itself. A shim that routes again from inside the
engine asks the engine to run itself: an unbounded self-recursion through the
door, which every warm-served call of those three ended in (-32004 at the 30s
read deadline, for ~0.5s of real work).

Pinned here:
  - inside a pool worker the implementation runs and nothing is routed;
  - outside one the route is exactly today's;
  - the env spelling cannot drift from the engine's own constant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIB_DIR = _REPO_ROOT / "coordinator" / "bin" / "lib"
for _p in (str(_LIB_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cc_invoke  # noqa: E402
import entry_point_shim  # noqa: E402

_ROUTE_ENV = "COORDINATOR_EXECUTION_ROUTE"


@pytest.fixture
def entry(monkeypatch):
    calls = {"legacy": [], "route": []}

    def _fake_simple_entry(_name, _dotted):
        def _legacy(argv):
            calls["legacy"].append(argv)
            return 0

        return _legacy

    def _fake_route(op, params, _repo_root, _legacy_fn):
        calls["route"].append((op, params["entrypoint"]))
        return {"stdout": "", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(entry_point_shim, "_simple_entry", _fake_simple_entry)
    monkeypatch.setattr(cc_invoke, "route", _fake_route)
    monkeypatch.setattr(entry_point_shim, "_merge_assemble_checked_repo_root", lambda: str(_REPO_ROOT))
    fn = entry_point_shim._native_route_entry("workstream-complete-assemble", "x.y")
    return fn, calls


def test_inside_the_engine_the_implementation_runs_and_nothing_routes(entry, monkeypatch):
    fn, calls = entry
    monkeypatch.setenv(_ROUTE_ENV, "warm_server")

    assert fn(["brief"]) == 0
    assert calls["legacy"] == [["brief"]]
    assert calls["route"] == []


def test_outside_the_engine_the_call_routes_through_invoke_from_argv(entry, monkeypatch):
    fn, calls = entry
    monkeypatch.setenv(_ROUTE_ENV, "in_process")

    assert fn(["brief"]) == 0
    assert calls["route"] == [("invoke.from_argv", "workstream-complete-assemble")]
    assert calls["legacy"] == []


def test_the_route_spelling_matches_the_engine():
    """If the engine's spelling moves, the check reads a variable nobody sets
    and the recursion silently returns."""
    from coordinator_core.telemetry import op_latency

    assert op_latency.ROUTE_ENV == _ROUTE_ENV
    assert op_latency.WARM_SERVER == "warm_server"
