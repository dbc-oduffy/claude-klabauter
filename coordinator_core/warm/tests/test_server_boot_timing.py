
from __future__ import annotations

import ast
import inspect
import textwrap

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


def test_a_pool_worker_starts_no_registry_or_emit_chain_import_of_its_own() -> None:
    """A worker's boot CPU is its first task's alone: a full registry import costs ~1s warm and
    seconds cold in every worker, so init spawns neither a preload nor an emit-chain prewarm."""
    assert "_preload_op_registry" not in _called_names(server._worker_process_init)
    assert "_prewarm_emit_chain" not in inspect.getsource(server._worker_process_init)
    assert not hasattr(server, "_worker_first_task_done")


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
