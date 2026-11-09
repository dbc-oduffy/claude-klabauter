"""Tests for the warm-server boot process-time harness: the reducers, the tmp engine root it builds,
the runtime isolation, and one real boot."""

from __future__ import annotations

import os
import time
from pathlib import Path

import psutil
import pytest

from coordinator_core.benchmarks import warm_pair_boot_process_time as boot
from coordinator_core.warm import breadcrumb, skew


def _snap(server_ms: float, pool_ms: float = 0.0, workers: int = 0) -> boot.CpuSnapshot:
    return boot.CpuSnapshot(wall_secs=0.0, server_ms=server_ms, pool_ms=pool_ms, pool_workers=workers)


def test_phase_deltas_are_tree_cpu_between_consecutive_milestones() -> None:
    marks = {
        "listener": _snap(250.0),
        "accept": _snap(900.0),
        "first": _snap(910.0, 800.0, 1),
        "pool_ready": _snap(910.0, 850.0, 1),
    }
    assert boot.phase_deltas(marks) == {
        "boot_imports_election": 250.0,
        "registry_preload_context": 650.0,
        "pool_build_worker_init": 810.0,
        "emit_prewarm_settle": 50.0,
    }


def test_phase_deltas_skip_a_phase_whose_milestone_never_arrived() -> None:
    marks = {"listener": _snap(250.0), "first": _snap(900.0, 500.0, 1)}
    assert boot.phase_deltas(marks) == {"boot_imports_election": 250.0}


def test_report_budget_is_the_server_process_time_at_first_answer() -> None:
    assert boot.BootReport(server_process_ms_to_first_answer=499.0).within_budget()
    assert not boot.BootReport(server_process_ms_to_first_answer=boot.FIRST_ANSWER_BUDGET_MS + 1).within_budget()
    assert not boot.BootReport().within_budget()


def _fake_source(root: Path) -> Path:
    pkg = root / "coordinator_core"
    (pkg / "warm").mkdir(parents=True)
    (pkg / "tests").mkdir()
    (pkg / "__pycache__").mkdir()
    (pkg / "warm" / "server.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "tests" / "test_x.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"pyc")
    (pkg / "NOTES.md").write_text("notes\n", encoding="utf-8")
    return root


def test_build_engine_root_is_a_stamped_tree_without_tests_or_docs(tmp_path: Path) -> None:
    dest = boot.build_engine_root(tmp_path / "engine", _fake_source(tmp_path / "src"))

    pkg = dest / "coordinator_core"
    assert (pkg / "warm" / "server.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (pkg / "__pycache__" / "mod.cpython-313.pyc").is_file()
    assert not (pkg / "tests").exists()
    assert not (pkg / "NOTES.md").exists()
    assert skew.compute_client_token(dest)


def test_build_engine_root_cold_pyc_leaves_the_bytecode_behind(tmp_path: Path) -> None:
    dest = boot.build_engine_root(tmp_path / "engine", _fake_source(tmp_path / "src"), cold_pyc=True)

    assert not (dest / "coordinator_core" / "__pycache__").exists()
    assert (dest / "coordinator_core" / "warm" / "server.py").is_file()


def test_isolated_runtime_points_the_runtime_at_the_base_and_restores_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, "sentinel")
    monkeypatch.delenv(boot._LISTENER_AUTOSTART_ENV, raising=False)

    with boot.isolated_runtime(tmp_path / "rt"):
        assert os.environ[breadcrumb.RUNTIME_BASE_ENV] == str(tmp_path / "rt")
        assert os.environ[boot._LISTENER_AUTOSTART_ENV] == "0"

    assert os.environ[breadcrumb.RUNTIME_BASE_ENV] == "sentinel"
    assert boot._LISTENER_AUTOSTART_ENV not in os.environ


def test_snapshot_reads_cumulative_cpu_of_a_live_process() -> None:
    proc = psutil.Process()
    before = boot.snapshot(proc, time.monotonic())
    sum(i * i for i in range(300_000))
    after = boot.snapshot(proc, time.monotonic())

    assert after.server_ms >= before.server_ms > 0
    assert after.tree_ms == after.server_ms + after.pool_ms


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_run_measures_a_real_boot_to_first_answer_and_pool_ready(tmp_path: Path) -> None:
    report = boot.run(work_dir=tmp_path)

    assert report.missing == []
    assert report.server_process_ms_to_first_answer is not None and report.server_process_ms_to_first_answer > 0
    assert report.tree_process_ms_to_first_answer >= report.server_process_ms_to_first_answer
    assert report.pool_ready_process_ms >= report.tree_process_ms_to_first_answer
    assert set(report.phase_process_ms) == {
        "boot_imports_election",
        "registry_preload_context",
        "pool_build_worker_init",
        "emit_prewarm_settle",
    }
