
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from coordinator_core.warm import server, telemetry


def _called_names(fn) -> set:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_the_serving_process_imports_no_op_registry_before_its_first_answer():
    """The server process runs no op, so a registry import on its boot path puts 0.6-2.8s of CPU
    ahead of the first answer (budget: 500ms)."""
    assert "_preload_op_registry" not in _called_names(server._run_guarded)


def test_unstamped_spawn_reads_as_unmeasurable(monkeypatch):
    monkeypatch.delenv(telemetry.SPAWN_EPOCH_ENV, raising=False)
    assert server._spawn_epoch_from_env() is None


def test_malformed_stamp_reads_as_unmeasurable(monkeypatch):
    for bad in ("", "   ", "soon", "not-a-float"):
        monkeypatch.setenv(telemetry.SPAWN_EPOCH_ENV, bad)
        assert server._spawn_epoch_from_env() is None


def test_stamp_round_trips(monkeypatch):
    monkeypatch.setenv(telemetry.SPAWN_EPOCH_ENV, repr(1756230000.5))
    assert server._spawn_epoch_from_env() == 1756230000.5


def test_no_row_is_written_without_a_stamp(tmp_path):
    """AN INVENTED t0 WOULD BE INDISTINGUISHABLE FROM A MEASURED ONE in the
    file that exists to settle how long boot takes, so an unstamped boot
    contributes nothing rather than a fabricated zero."""
    server._record_own_boot(None, 123.0, tmp_path)

    assert telemetry.server_boot_samples(tmp_path) == []


def test_row_is_written_with_both_instants(tmp_path, monkeypatch):
    monkeypatch.setattr(server.time, "time", lambda: 1000.0)
    server._record_own_boot(990.0, 995.5, tmp_path)

    rows = telemetry.server_boot_samples(tmp_path)
    assert len(rows) == 1
    assert rows[0]["listener_secs"] == 5.5
    assert rows[0]["ready_secs"] == 10.0


def test_a_failing_instrument_never_stops_a_boot(tmp_path, monkeypatch):
    def _boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry, "record_server_boot", _boom)
    server._record_own_boot(1.0, 2.0, tmp_path)


_PROBE = (
    "import sys\n"
    "from coordinator_core.warm import server\n"
    "before = 'coordinator_core.warm.push_cadence' in sys.modules\n"
    "mod = server.push_cadence\n"
    "after = 'coordinator_core.warm.push_cadence' in sys.modules\n"
    "print(before, after, mod.__name__)\n"
)


def test_the_idle_sweep_import_closure_stays_off_the_boot_path():
    """`push_cadence` pulls git, ceremony and push ops (~60 modules): a cold-bytecode successor
    compiles all of them before its first answer. Loading `server` must not import it; the
    attribute stays reachable and imports on first use."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(server.__file__).resolve().parents[2]
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=root, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["False", "True", "coordinator_core.warm.push_cadence"]


def test_a_pool_worker_runs_no_registry_or_emit_chain_import_inline() -> None:
    """A worker's boot CPU is its first task's alone: a full registry import costs ~1s warm and
    seconds cold in every worker, so init runs neither inline; the emit chain may only start on a
    background thread."""
    called = _called_names(server._worker_process_init)
    assert "_preload_op_registry" not in called
    assert "_prewarm_emit_chain" not in called
    assert "_prewarm_emit_chain" not in inspect.getsource(server._worker_process_init)
    assert not hasattr(server, "_worker_first_task_done")
    assert "join" not in _called_attrs(server._start_emit_prewarm)
    assert "daemon=True" in inspect.getsource(server._start_emit_prewarm)


def test_worker_init_starts_the_emit_prewarm_in_the_background_only_when_asked(monkeypatch) -> None:
    import threading

    started: list = []
    monkeypatch.setattr(server, "_start_emit_prewarm", lambda: started.append(1))
    for name in ("_bind_null_std_streams", "_exit_with_parent"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_bind_worker_stderr_log", lambda d: None)
    monkeypatch.setattr(server, "_log_priority_boost", lambda *a, **k: None)
    monkeypatch.setattr(server, "_raise_worker_priority", lambda: False)
    monkeypatch.setattr(server, "_prime_posix_worker", lambda: True)
    monkeypatch.setattr(server, "_worker_pristine_environ", None)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", None)

    server._worker_process_init()
    assert started == []
    server._worker_process_init(prewarm_emit=True)
    assert started == [1]
    assert threading.current_thread().name != "warm-worker-emit-prewarm"


def test_the_emit_prewarm_runs_off_the_calling_thread_and_returns_at_once(monkeypatch) -> None:
    import threading

    release = threading.Event()
    ran_on: list = []

    def _slow() -> None:
        ran_on.append(threading.current_thread())
        release.wait(5)

    monkeypatch.setattr(server, "_prewarm_emit_chain", _slow)
    thread = server._start_emit_prewarm()
    try:
        assert thread.daemon and thread is not threading.current_thread()
        assert thread.is_alive(), "init returned while the import was still in flight"
    finally:
        release.set()
        thread.join(5)
    assert ran_on == [thread]


def test_only_the_dispatch_pool_asks_for_the_emit_prewarm() -> None:
    assert "forward_queue, True)" in inspect.getsource(server._ServerContext._ensure_dispatch_pool)
    assert "True)" not in inspect.getsource(server._ServerContext._ensure_guard_pool).split("initargs")[1]


def test_a_dispatch_on_a_module_mid_import_still_opens_the_import_phase(monkeypatch, tmp_path) -> None:
    import sys
    import types

    from coordinator_core.ops import _registry_map

    mod = types.ModuleType("fake_mid_import_op")
    mod.__spec__ = types.SimpleNamespace(_initializing=True)
    monkeypatch.setitem(sys.modules, "fake_mid_import_op", mod)
    monkeypatch.setitem(_registry_map.OP_MODULE_MAP, "fake.op", "fake_mid_import_op")
    began: list = []
    monkeypatch.setattr(server.dispatch_ack, "begin_import_phase", lambda j, k: began.append(k) or "marker")
    monkeypatch.setattr(server.dispatch_ack, "end_import_phase", lambda m: True)
    monkeypatch.setattr(server, "_effective_method", lambda msg: "fake.op")
    monkeypatch.setattr("importlib.import_module", lambda name: mod)

    assert server._import_op_module({}, str(tmp_path), "k1") is True
    assert began == ["k1"]

    mod.__spec__ = types.SimpleNamespace(_initializing=False)
    assert server._import_op_module({}, str(tmp_path), "k2") is True
    assert began == ["k1"], "a fully loaded module must not write a marker"


def test_a_worker_resolves_a_first_op_through_the_targeted_lookup_not_a_full_registry() -> None:
    import subprocess
    import sys

    probe = (
        "import sys;from coordinator_core import ipc;"
        "ipc.get_op_handler('ceremony.commit_v2');"
        "print(len(ipc._REGISTRY), len([m for m in sys.modules if m.startswith('coordinator_core.ops.')]))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    registered, ops_modules = (int(x) for x in out.stdout.split())
    assert registered >= 1
    assert ops_modules < 150, "a first-op lookup must not import the whole op tree"


def _stub_version_inputs(monkeypatch, tmp_path, stamps, digests):
    """Pin the stat stamp and content hash `_BootVersionState` reads; `digests` records hash calls."""
    from coordinator_core import engine_version, lifecycle
    from coordinator_core.warm import skew

    stamp_iter = iter(stamps)
    monkeypatch.setattr(
        engine_version,
        "resolve_engine_source_head",
        lambda *a, **k: engine_version.SourceHead("sha", "source-tree", "sha"),
    )
    monkeypatch.setattr(skew, "_source_pkg_dir", lambda: tmp_path)
    monkeypatch.setattr(skew, "_source_stamp", lambda _dir: next(stamp_iter))
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: digests.pop(0))


def test_boot_version_state_reads_no_source_content_at_construction(monkeypatch, tmp_path) -> None:
    from coordinator_core import lifecycle

    _stub_version_inputs(monkeypatch, tmp_path, ["s0"], [])
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: (_ for _ in ()).throw(AssertionError))

    state = server._BootVersionState(tmp_path)

    assert state.server_sha == "sha"
    assert state._boot_hash is None


def test_boot_version_state_has_every_attribute_the_base_state_sets(monkeypatch, tmp_path) -> None:
    from coordinator_core.warm import skew

    _stub_version_inputs(monkeypatch, tmp_path, ["s0", "s0"], ["h", "h"])
    lean = server._BootVersionState(tmp_path)
    base = skew.ServerVersionState(tmp_path)

    assert set(vars(base)) <= set(vars(lean))


def test_a_stamp_change_reads_as_stale_and_stays_stale(monkeypatch, tmp_path) -> None:
    _stub_version_inputs(monkeypatch, tmp_path, ["s0", "s1", "s0"], [])
    state = server._BootVersionState(tmp_path)

    state.refresh(force=True)
    assert state._source_stale is True

    state.refresh(force=True)  # the stamp is back at boot's; the flag is sticky
    assert state._source_stale is True


def test_an_unchanged_stamp_reads_as_current_and_never_hashes_content(monkeypatch, tmp_path) -> None:
    from coordinator_core import lifecycle

    _stub_version_inputs(monkeypatch, tmp_path, ["s0", "s0", "s0"], [])
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: (_ for _ in ()).throw(AssertionError))
    state = server._BootVersionState(tmp_path)

    state.refresh(force=True)
    state.refresh(force=True)

    assert state._source_stale is False
    assert not hasattr(state, "settle_boot_hash")


def test_boot_housekeeping_waits_for_the_first_pool_answer_or_the_cap(monkeypatch) -> None:
    import threading

    ran = threading.Event()
    monkeypatch.setattr(server, "_first_pool_answer", threading.Event())
    monkeypatch.setattr(server, "_BOOT_HOUSEKEEPING_MAX_WAIT_SECS", 30.0)
    server._after_first_pool_answer(ran.set, "test-housekeeping")
    assert not ran.wait(0.2)
    server._first_pool_answer.set()
    assert ran.wait(5)

    ran.clear()
    monkeypatch.setattr(server, "_first_pool_answer", threading.Event())
    monkeypatch.setattr(server, "_BOOT_HOUSEKEEPING_MAX_WAIT_SECS", 0.0)
    server._after_first_pool_answer(ran.set, "test-housekeeping-cap")
    assert ran.wait(5)


def test_an_answered_pool_future_releases_boot_housekeeping(monkeypatch) -> None:
    import concurrent.futures
    import threading

    for repo_root in (None, "repo"):
        monkeypatch.setattr(server, "_first_pool_answer", threading.Event())
        future: concurrent.futures.Future = concurrent.futures.Future()
        future.set_result({"result": 1})

        assert server._await_pool_result(future, 1.0, repo_root) == {"result": 1}
        assert server._first_pool_answer.is_set()


def _called_attrs(fn) -> set:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_boot_version_state_spawns_no_git_process_before_the_first_answer() -> None:
    """`resolve_engine_sha` runs `git rev-parse` (64-260ms, the dominant boot phase under load);
    the pre-ready constructor reads HEAD from the git dir in-process."""
    called = _called_attrs(server._BootVersionState.__init__)
    assert "resolve_engine_sha" not in called
    assert "resolve_engine_source_head" in called
    assert "resolve_engine_sha" not in _called_attrs(server._run_guarded)


def test_the_pre_ready_disk_writes_keep_their_stall_bounds() -> None:
    assert server._BOOT_BREADCRUMB_DEADLINE_SECS <= 0.2
    assert server._BOOT_TIMING_DEADLINE_SECS <= 0.25
    assert "_BOOT_BREADCRUMB_DEADLINE_SECS" in inspect.getsource(server._run_guarded)
    assert "_BOOT_TIMING_DEADLINE_SECS" in inspect.getsource(server._record_own_boot)


def test_boot_defers_the_http_listener_behind_the_first_answer_and_hashes_no_source() -> None:
    boot = inspect.getsource(server._run_guarded)
    assert "_BootVersionState(repo_root)" in boot
    assert boot.count("_after_first_pool_answer(") == 1
    assert "settle_boot_hash" not in boot
    assert "threading.Thread(target=_ensure_http_listener" not in boot


@pytest.mark.spawns_process
def test_a_cold_guard_op_resolves_within_the_process_time_budget_and_imports_no_whole_op_tree() -> None:
    """A fresh process answers its first guard op without the whole-package import that stalled a
    fresh engine for 27s; budget is process time, never wall clock (500ms)."""
    import subprocess
    import sys

    probe = (
        "import sys,time;from coordinator_core import ipc;"
        "h=ipc.get_op_handler('hooks.guard_host_subagent_bash_ban');"
        "print(h is not None, time.process_time(),"
        " 'coordinator_core.ops.post_run_finish' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    resolved, process_s, whole_tree = out.stdout.split()
    assert resolved == "True"
    assert whole_tree == "False", "a guard op lookup must not import the whole op tree"
    assert float(process_s) < 0.5


def test_warm_server_boot_never_calls_the_whole_op_tree_preload() -> None:
    """The server serves guard ops before (and without) a whole-registry import: no boot path may
    call `_preload_op_registry` or `_eager_import_all` on the ops package."""
    import re

    src = inspect.getsource(server)
    assert len(re.findall(r"_preload_op_registry\(", src)) == 1, "only its definition"
    assert len(re.findall(r"_ops_pkg\._eager_import_all\(", src)) == 1, "only inside the preload def"
