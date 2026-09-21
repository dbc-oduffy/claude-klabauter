"""coordinator/bin/tests/test_cc_invoke_in_process_reentry.py — C2 of
`docs/plans/2026-09-20-stop-the-engine-spawning-to-talk-to-itself.md`.

Subject: `cc_invoke._try_in_engine_dispatch`, which dispatches in-process when
the caller is already inside the warm engine's process tree.

The plan's three required tests, plus the two properties that decide whether
this is safe to ship on the surface every coordinator CLI shares:

  - with the route env set, nothing spawns AND nothing dials the door;
  - without it, the ladder is exactly today's;
  - an import failure falls through rather than raising;
  - a failure AFTER dispatch began never falls through (double execution);
  - the mirrored env spelling cannot drift from the engine's own constant.
"""

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
    """The in-engine path front-inserts the engine root on `sys.path` and imports
    `coordinator_core.*` -- correct in a short-lived CLI process, a leak in a
    test process shared with sibling files that resolve `coordinator_core`
    against FAKE engine roots (`test_cc_invoke_provenance_reporting_seams.py`
    pops only the parent package and would find this file's real submodules
    still cached). Same restore discipline as that file's `clean_sys_path` /
    `clean_sys_modules_coordinator_core`, widened to the submodules this path
    actually imports."""

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
    """Both legs C2 removes. Either one being reached is the failure."""

    def _spawn(*_a, **_k):  # pragma: no cover -- reaching it is the failure
        raise AssertionError("cold spawn reached from inside the engine")

    def _door(*_a, **_k):  # pragma: no cover -- reaching it is the failure
        raise AssertionError("warm door dialled from inside the engine")

    monkeypatch.setattr(cc_invoke.subprocess, "run", _spawn)
    monkeypatch.setattr(cc_invoke, "_capture_warm_reach", _door)


def test_the_mirrored_route_spelling_matches_the_engine():
    """Two planes, no shared importable constant. If either spelling moves, the
    check reads a variable nobody sets and C2 silently stops firing — which
    looks exactly like today's behaviour and would never be noticed."""
    from coordinator_core.telemetry import op_latency

    assert cc_invoke._ROUTE_ENV == op_latency.ROUTE_ENV
    assert cc_invoke._ROUTE_WARM_SERVER == op_latency.WARM_SERVER


def test_the_pool_worker_actually_sets_what_this_reads(monkeypatch):
    """The whole premise: `_worker_process_init` declares the route in the pool
    worker's environment, so a CLI it spawns inherits it."""
    from coordinator_core.warm import server

    # setenv, not delenv: `delenv(raising=False)` on an ABSENT key records
    # nothing to restore, so the direct `os.environ` write below would leak
    # `warm_server` into every later test and route all of them in-engine.
    # setenv records the prior absence, and teardown deletes the key again.
    monkeypatch.setenv(cc_invoke._ROUTE_ENV, "in_process")
    server._declare_execution_route()
    assert os.environ.get(cc_invoke._ROUTE_ENV) == cc_invoke._ROUTE_WARM_SERVER


def test_inside_the_engine_neither_spawns_nor_dials_the_door(
    _in_engine, _no_spawn_no_door, monkeypatch
):
    """`ping` is none-scoped, so this exercises the real dispatch body end to
    end with no worktree resolution in the way.

    The stamp gate is opened for this test only: this source tree carries no
    build stamp, and in production the caller sits under the stamped published
    engine. `coordinator_core`'s own conftest opens it suite-wide; this bin-side
    suite does not, so it is opened here by the monkeypatch revert the gate's
    own docstring prescribes."""
    from coordinator_core import ipc

    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", True)

    out = cc_invoke._try_in_engine_dispatch("ping", {}, _REPO, _REPO)

    assert out is not None
    assert "result" in out, out


def test_outside_the_engine_it_falls_through_untouched(_no_route, monkeypatch):
    """The common case — every caller not inside a pool worker. Nothing may be
    imported or dispatched; the caller keeps today's warm-then-cold ladder."""

    def _must_not_import(*_a, **_k):  # pragma: no cover
        raise AssertionError("engine path mutated for a caller outside the engine")

    monkeypatch.setattr(cc_invoke, "_front_insert_on_path", _must_not_import)

    assert cc_invoke._try_in_engine_dispatch("ping", {}, _REPO, _REPO) is None


def test_an_unrecognised_route_value_falls_through(monkeypatch):
    monkeypatch.setenv(cc_invoke._ROUTE_ENV, "in_process")
    assert cc_invoke._try_in_engine_dispatch("ping", {}, _REPO, _REPO) is None


def test_an_import_failure_falls_through_rather_than_raising(_in_engine, monkeypatch):
    """Pre-dispatch: nothing has run, so today's ladder is still safe."""
    import builtins

    real_import = builtins.__import__

    def _fail(name, *a, **k):
        if name == "coordinator_core.invoke.dispatch":
            raise ImportError("engine not importable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fail)
    assert cc_invoke._try_in_engine_dispatch("ping", {}, _REPO, _REPO) is None


def test_an_unwalkable_worktree_falls_through(_in_engine, monkeypatch, tmp_path):
    """A worktree-scoped op with no worktree to name: the cold path would refuse
    it too, so falling through changes nothing about the outcome."""
    from coordinator_core.op_scopes import WORKTREE_SCOPED_OPS

    scoped = sorted(WORKTREE_SCOPED_OPS)[0]
    import coordinator_core.git.repo_root as rr

    monkeypatch.setattr(rr, "show_toplevel", lambda _p=None: None)
    assert cc_invoke._try_in_engine_dispatch(scoped, {}, str(tmp_path), _REPO) is None


def test_a_failure_after_dispatch_began_raises_and_never_falls_through(_in_engine, monkeypatch):
    """The one branch that must NOT return None. The op may have run; handing it
    back to the warm or cold leg is the double execution this module's
    indeterminate handling exists to prevent."""
    import coordinator_core.invoke.dispatch as d

    async def _boom(*_a, **_k):
        raise RuntimeError("handler exploded mid-write")

    monkeypatch.setattr(d, "dispatch_message", _boom)

    with pytest.raises(RuntimeError, match="may have run"):
        cc_invoke._try_in_engine_dispatch("ping", {}, _REPO, _REPO)


def test_a_scoped_op_carries_the_walked_worktree(_in_engine, monkeypatch):
    """Scoped ops refuse -32602 without `_origin_worktree`. Resolved by walking,
    never by `git rev-parse` — a spawn here would reintroduce the cost this
    function exists to remove."""
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
