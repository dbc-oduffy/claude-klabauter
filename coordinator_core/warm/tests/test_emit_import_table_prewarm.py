"""A warm pool worker marks residency unconditionally and builds the emit test-import table off its caller's thread."""

import threading

from coordinator_core.warm import residency, server


def _neutralise_init(monkeypatch):
    monkeypatch.setattr(server, "_prime_posix_worker", lambda: True)
    monkeypatch.setattr(server, "_raise_worker_priority", lambda: False)
    monkeypatch.setattr(server, "_bind_null_std_streams", lambda: None)
    monkeypatch.setattr(server, "_bind_worker_stderr_log", lambda _d: None)
    monkeypatch.setattr(server, "_log_priority_boost", lambda *_a: None)
    monkeypatch.setattr(server, "_exit_with_parent", lambda _pid: None)
    monkeypatch.setattr(server, "_worker_pristine_environ", None)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", None)
    monkeypatch.setattr(
        "coordinator_core.win_portability.install_console_less_spawn_default", lambda: None
    )


def test_worker_init_marks_residency_without_prewarm(monkeypatch):
    _neutralise_init(monkeypatch)
    monkeypatch.setattr(residency, "_resident", False)
    started = []
    monkeypatch.setattr(server, "_start_emit_prewarm", lambda: started.append(1))

    server._worker_process_init(prewarm_emit=False)

    assert residency.is_resident() is True
    assert started == []


def test_worker_init_with_prewarm_starts_prewarm_after_marking(monkeypatch):
    _neutralise_init(monkeypatch)
    monkeypatch.setattr(residency, "_resident", False)
    seen = []
    monkeypatch.setattr(server, "_start_emit_prewarm", lambda: seen.append(residency.is_resident()))

    server._worker_process_init(prewarm_emit=True)

    assert seen == [True]


def test_prewarm_schedules_table_for_engine_root_without_building_inline(monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import test_import_table

    gate = threading.Event()
    built = []
    caller = threading.get_ident()

    def slow_rebuild(root):
        built.append((str(root), threading.get_ident()))
        gate.wait(5)

    monkeypatch.setattr(server, "_engine_clone_root", lambda: tmp_path)
    monkeypatch.setattr(test_import_table, "rebuild", slow_rebuild)
    monkeypatch.setattr(test_import_table, "key", lambda _r: ("k", 0, 0))
    monkeypatch.setattr(test_import_table, "_TABLES", {})
    monkeypatch.setattr(test_import_table, "_THREADS", {})
    monkeypatch.setattr("coordinator_core.ops.review_tripwires.warm_recent_indexes", lambda _r: None)
    monkeypatch.setattr("pkgutil.iter_modules", lambda _p: [])
    monkeypatch.setattr(server, "_PREWARM_BUG_BLITZ_MODULES", ())

    try:
        server._prewarm_emit_chain()  # returns while the rebuild is still blocked
        thread = next(iter(test_import_table._THREADS.values()))
        assert thread.daemon is True
        thread.join(0.05)
        assert built and built[0][1] != caller
    finally:
        gate.set()
