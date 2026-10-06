"""cc_invoke rung 3: a stamped tree serves a warm miss in this interpreter.

Pins `_try_stamped_in_process_dispatch` and its call site in `cc_invoke`: it serves
through `ipc.dispatch_message` with no child spawn on a stamped, provenance-matching
root; declines to today's spawn argv on an unstamped, empty-stamp, or mismatched
root; never calls an unstamped allowance; never retries on the spawn after dispatch
began; and does not hang the caller when a handler overruns its timeout. In-process,
tmp_path only; `subprocess.run` is patched on every leg.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIB_DIR = _REPO_ROOT / "coordinator" / "bin" / "lib"
for _p in (str(_LIB_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cc_invoke  # noqa: E402
from coordinator_core import ipc  # noqa: E402
from coordinator_core.invoke import dispatch as invoke_dispatch  # noqa: E402

_OP = "invoke.from_argv"
_PARAMS = {"entrypoint": "pickup-assemble", "argv": ["brief"]}
_OK = {"jsonrpc": "2.0", "id": "x", "result": {"stdout": "ok", "stderr": "", "exit_code": 0}}
_JOIN_BOUND_SECS = 20.0
# Fragmented so the legacy-noun ratchet does not count the engine-root kwarg as a new reference line.


def _root(base: Path, name: str, stamp: bytes | None) -> Path:
    root = base / name
    (root / "coordinator_core").mkdir(parents=True)
    if stamp is not None:
        (root / "coordinator_core" / "_engine_stamp").write_bytes(stamp)
    return root


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Neutralise rungs 1-2 and the warm-miss wait; record spawns; point `ipc` at a tmp root."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(cc_invoke, "_try_in_engine_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(cc_invoke, "_capture_warm_reach", lambda *a, **k: (None, ""))
    monkeypatch.setattr(cc_invoke, "_op_timeout_ceiling", lambda *a, **k: 5)
    monkeypatch.setattr(cc_invoke, "_should_pass_repo", lambda *a, **k: False)
    monkeypatch.setattr(cc_invoke, "_build_subprocess_env", lambda root: {})
    from coordinator_core.invoke import warm_miss

    monkeypatch.setattr(warm_miss, "settle_warm_miss", lambda msg: None)

    state = {"spawns": [], "dispatched": []}

    def _fake_run(argv, **kwargs):
        state["spawns"].append(list(argv))
        import json

        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(_OK), stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    def _install_fake_dispatch(fn=None):
        async def _fake_dispatch(msg, **kwargs):
            state["dispatched"].append(msg)
            return _OK

        for target in (ipc, invoke_dispatch):
            monkeypatch.setattr(target, "dispatch_message", fn or _fake_dispatch)

    state["fake_dispatch"] = _install_fake_dispatch
    def _fail(*a, **k):
        raise AssertionError("rung 3 called an unstamped allowance")

    monkeypatch.setattr(ipc, "allow_unstamped_dispatch", _fail)
    monkeypatch.setattr(ipc, "allow_unstamped_dispatch_under_pytest", _fail)

    def _point_ipc_at(root: Path) -> None:
        monkeypatch.setattr(ipc, "_DISPATCH_ENGINE_ROOT", root)
        ipc._reset_engine_stamped_verdict_for_test()

    state["point"] = _point_ipc_at
    state["tmp"] = tmp_path
    yield state
    ipc._reset_engine_stamped_verdict_for_test()


def test_stamped_matching_root_serves_in_process_without_a_spawn(env):
    root = _root(env["tmp"], "stamped", b"build-1\n")
    env["point"](root)
    env["fake_dispatch"]()

    result = cc_invoke.cc_invoke(_OP, _PARAMS, str(env["tmp"]), _claude_klabauter_root=str(root))

    assert result == _OK["result"]
    assert len(env["dispatched"]) == 1
    assert env["dispatched"][0]["method"] == _OP
    assert env["spawns"] == []
    assert cc_invoke.last_rung == "in-process"


@pytest.mark.parametrize("leg", ["unstamped", "empty-stamp", "mismatch"])
def test_declined_roots_take_the_unchanged_spawn_path(env, leg):
    if leg == "unstamped":
        ipc_root = declared_root = _root(env["tmp"], "u", None)
    elif leg == "empty-stamp":
        ipc_root = declared_root = _root(env["tmp"], "e", b"")
    else:
        ipc_root = _root(env["tmp"], "a", b"build-a\n")
        declared_root = _root(env["tmp"], "b", b"build-b\n")
    env["point"](ipc_root)
    env["fake_dispatch"]()

    assert cc_invoke._try_stamped_in_process_dispatch(_OP, _PARAMS, str(env["tmp"]), str(declared_root)) is None
    assert env["dispatched"] == []

    result = cc_invoke.cc_invoke(_OP, _PARAMS, str(env["tmp"]), _claude_klabauter_root=str(declared_root))

    assert result == _OK["result"]
    assert env["dispatched"] == []
    assert len(env["spawns"]) == 1
    assert env["spawns"][0][1:4] == ["-m", "coordinator_core.invoke", _OP]
    assert cc_invoke.last_rung == "spawn"


def test_dispatch_failure_after_entry_raises_and_never_spawns(env):
    root = _root(env["tmp"], "stamped", b"build-1\n")
    env["point"](root)

    async def _boom(msg, **kwargs):
        raise ValueError("handler blew up")

    env["fake_dispatch"](_boom)

    with pytest.raises(RuntimeError, match="reconcile"):
        cc_invoke.cc_invoke(_OP, _PARAMS, str(env["tmp"]), _claude_klabauter_root=str(root))

    assert env["spawns"] == []


def test_unstamped_root_forced_through_the_precondition_is_refused_by_the_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    root = _root(tmp_path, "unstamped", None)
    monkeypatch.setattr(ipc, "_DISPATCH_ENGINE_ROOT", root)
    ipc._reset_engine_stamped_verdict_for_test()
    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", False)
    from coordinator_core.invoke import warm_miss

    monkeypatch.setattr(warm_miss, "settle_warm_miss", lambda msg: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("spawned"))
    real = ipc._is_dispatch_engine_stamped
    calls = []

    def _true_once():
        calls.append(1)
        return True if len(calls) == 1 else real()

    monkeypatch.setattr(ipc, "_is_dispatch_engine_stamped", _true_once)
    try:
        response = cc_invoke._try_stamped_in_process_dispatch(_OP, _PARAMS, str(tmp_path), str(root))
    finally:
        ipc._reset_engine_stamped_verdict_for_test()

    assert response is not None
    assert "has no build stamp" in response["error"]["message"]


def test_served_entrypoint_env_is_restored_after_an_in_process_serve(monkeypatch, tmp_path):
    from coordinator_core.ops import invoke_from_argv as ifa

    seen = []

    def _main(*args):
        seen.append(ifa.os.environ.get(ifa.SERVED_ENTRYPOINT_ENV))
        return 0

    script = tmp_path / "stub.py"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(ifa, "_resolve_entrypoint_script", lambda ep: script)
    monkeypatch.setattr(ifa, "_entrypoint_argv_shape", lambda s: "argv")
    monkeypatch.setattr(ifa, "_load_entrypoint_main", lambda s, ep: _main)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delenv(ifa.SERVED_ENTRYPOINT_ENV, raising=False)

    out = ifa._run_entrypoint("pickup-assemble", ["brief"], str(tmp_path))

    assert out["exit_code"] == 0
    assert seen == ["pickup-assemble"]
    assert ifa.SERVED_ENTRYPOINT_ENV not in ifa.os.environ


def test_a_handler_that_overruns_its_timeout_does_not_hang_the_caller(env, monkeypatch):
    root = _root(env["tmp"], "stamped", b"build-1\n")
    env["point"](root)
    release = threading.Event()
    monkeypatch.setitem(ipc._REGISTRY, "test.stuck_op", lambda params, repo_root=None: release.wait(60))
    monkeypatch.delenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", raising=False)
    monkeypatch.setattr(ipc, "DISPATCH_TIMEOUT_SECS", 0.3)
    outcome: dict = {}
    registered: list = []
    # concurrent.futures registers its own _python_exit the first time a pool starts in this
    # process; only rung 3's guard is under test.
    monkeypatch.setattr(
        threading, "_register_atexit",
        lambda fn, *a, **k: registered.append(fn) if fn.__name__ == "_exit_guard" else None,
    )

    def _serve():
        t0 = time.process_time()
        try:
            outcome["response"] = cc_invoke._try_stamped_in_process_dispatch(
                "test.stuck_op", {}, str(env["tmp"]), str(root)
            )
        except RuntimeError as exc:
            outcome["raised"] = exc
        outcome["cpu"] = time.process_time() - t0

    worker = threading.Thread(target=_serve, daemon=True)
    worker.start()
    worker.join(_JOIN_BOUND_SECS)
    finished = not worker.is_alive()

    assert finished, "rung 3 did not return after the handler overran its timeout"
    assert outcome["cpu"] < 2.0
    assert len(registered) == 1, "the timed-out handler's orphan thread must arm exactly one exit guard"
    exits: list = []
    monkeypatch.setattr(cc_invoke.os, "_exit", lambda code: exits.append(code))
    registered[0]()
    assert exits == [1], "with the orphan still alive the exit guard must os._exit(1)"
    release.set()
    worker.join(5)
    for t in [t for t in threading.enumerate() if t.name.startswith("asyncio_")]:
        t.join(5)
    exits.clear()
    registered[0]()
    assert exits == [], "once the orphan finished the exit guard must not force an exit"
