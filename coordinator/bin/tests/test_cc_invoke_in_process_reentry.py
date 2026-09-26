
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import cc_invoke  # noqa: E402

_REPO = str(Path(__file__).resolve().parents[3])


@pytest.fixture(autouse=True)
def _restore_import_state():

    def _cc(mods):
        return {k: v for k, v in mods.items() if k == "coordinator_core" or k.startswith("coordinator_core.")}

    path_before = list(sys.path)
    mods_before = _cc(sys.modules)
    try:
        yield
    finally:
        sys.path[:] = path_before
        for name in set(_cc(sys.modules)) - set(mods_before):
            sys.modules.pop(name, None)
        sys.modules.update(mods_before)


@pytest.fixture
def _in_engine(monkeypatch):
    monkeypatch.setenv(cc_invoke._ROUTE_ENV, cc_invoke._ROUTE_WARM_SERVER)


@pytest.fixture
def _no_route(monkeypatch):
    monkeypatch.delenv(cc_invoke._ROUTE_ENV, raising=False)


@pytest.fixture
def _no_spawn_no_door(monkeypatch):

    def _spawn(*_a, **_k):  # pragma: no cover -- reaching it is the failure
        raise AssertionError("cold spawn reached from inside the engine")

    def _door(*_a, **_k):  # pragma: no cover -- reaching it is the failure
        raise AssertionError("warm door dialled from inside the engine")

    monkeypatch.setattr(cc_invoke.subprocess, "run", _spawn)
    monkeypatch.setattr(cc_invoke, "_capture_warm_reach", _door)


def test_the_mirrored_route_spelling_matches_the_engine():
    from coordinator_core.telemetry import op_latency

    assert cc_invoke._ROUTE_ENV == op_latency.ROUTE_ENV
    assert cc_invoke._ROUTE_WARM_SERVER == op_latency.WARM_SERVER


def test_the_pool_worker_actually_sets_what_this_reads(monkeypatch):
    from coordinator_core.warm import server

    monkeypatch.setenv(cc_invoke._ROUTE_ENV, "in_process")
    server._declare_execution_route()
    assert os.environ.get(cc_invoke._ROUTE_ENV) == cc_invoke._ROUTE_WARM_SERVER


def test_inside_the_engine_neither_spawns_nor_dials_the_door(
    _in_engine, _no_spawn_no_door, monkeypatch
):
    from coordinator_core import ipc

    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", True)

    out = cc_invoke._try_in_engine_dispatch("ping", {}, _REPO, _REPO)

    assert out is not None
    assert "result" in out, out


def test_outside_the_engine_it_falls_through_untouched(_no_route, monkeypatch):

    def _must_not_import(*_a, **_k):  # pragma: no cover
        raise AssertionError("engine path mutated for a caller outside the engine")

    monkeypatch.setattr(cc_invoke, "_front_insert_on_path", _must_not_import)

    assert cc_invoke._try_in_engine_dispatch("ping", {}, _REPO, _REPO) is None


def test_an_unrecognised_route_value_falls_through(monkeypatch):
    monkeypatch.setenv(cc_invoke._ROUTE_ENV, "in_process")
    assert cc_invoke._try_in_engine_dispatch("ping", {}, _REPO, _REPO) is None


def test_an_import_failure_falls_through_rather_than_raising(_in_engine, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fail(name, *a, **k):
        if name == "coordinator_core.invoke.dispatch":
            raise ImportError("engine not importable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fail)
    engine_root = "/nonexistent/engine-root-for-this-test"
    path_before = list(sys.path)
    assert cc_invoke._try_in_engine_dispatch("ping", {}, _REPO, engine_root) is None
    assert sys.path == path_before, "a fall-through must leave the ladder an untouched sys.path"


def test_an_unwalkable_worktree_falls_through(_in_engine, monkeypatch, tmp_path):
    from coordinator_core.op_scopes import WORKTREE_SCOPED_OPS

    scoped = sorted(WORKTREE_SCOPED_OPS)[0]
    import coordinator_core.git.repo_root as rr

    monkeypatch.setattr(rr, "show_toplevel", lambda _p=None: None)
    assert cc_invoke._try_in_engine_dispatch(scoped, {}, str(tmp_path), _REPO) is None


def test_a_failure_after_dispatch_began_raises_and_never_falls_through(_in_engine, monkeypatch):
    import coordinator_core.invoke.dispatch as d

    async def _boom(*_a, **_k):
        raise RuntimeError("handler exploded mid-write")

    monkeypatch.setattr(d, "dispatch_message", _boom)

    with pytest.raises(RuntimeError, match="may have run"):
        cc_invoke._try_in_engine_dispatch("ping", {}, _REPO, _REPO)


def test_a_scoped_op_carries_the_walked_worktree(_in_engine, monkeypatch):
    from coordinator_core.op_scopes import WORKTREE_SCOPED_OPS
    import coordinator_core.invoke.dispatch as d

    seen = {}

    async def _capture(msg, **_k):
        seen.update(msg)
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    monkeypatch.setattr(d, "dispatch_message", _capture)
    scoped = sorted(WORKTREE_SCOPED_OPS)[0]

    cc_invoke._try_in_engine_dispatch(scoped, {"k": 1}, _REPO, _REPO)

    assert seen["method"] == scoped
    assert seen["params"] == {"k": 1}
    assert seen["_origin_worktree"]
    assert "_caller_cwd" in seen
