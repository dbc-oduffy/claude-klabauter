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


def test_handover_report_budget_is_the_first_attempt_answering() -> None:
    slow = boot.FIRST_ANSWER_BUDGET_MS + 900
    assert boot.BootReport(handover=True, server_process_ms_to_first_answer=slow).within_budget()
    assert not boot.BootReport(handover=False, server_process_ms_to_first_answer=1.0).within_budget()
    assert not boot.BootReport(handover=True, missing=["accept"]).within_budget()


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


def test_handover_report_defaults_to_not_a_handover_run() -> None:
    report = boot.BootReport()
    assert report.handover is None and report.handover_wall_ms is None


def test_accept_milestone_retries_a_pipe_that_refused_the_first_open(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list = []

    class _FlakyProbe:
        def __init__(self, endpoint: str, method: str, token: str) -> None:
            attempts.append(method)
            self.error = "OSError(22)" if len(attempts) == 1 and method == boot.REQUEST_STATUS_METHOD else None

        def start(self) -> None: ...

    def _fake_answered(probe, proc, t0, deadline):  # noqa: ANN001
        return None if probe.error else _snap(100.0)

    monkeypatch.setattr(boot, "Probe", _FlakyProbe)
    monkeypatch.setattr(boot, "_answered", _fake_answered)
    monkeypatch.setattr(boot, "snapshot", lambda proc, t0: _snap(10.0))
    monkeypatch.setattr(boot, "_settle", lambda proc, t0: _snap(200.0, 0.0, 1))
    report = boot.BootReport()

    boot._sample_milestones(object(), "pipe", "tok", time.monotonic(), report)

    assert attempts.count(boot.REQUEST_STATUS_METHOD) == 2
    assert report.accept_answer_process_ms == 100.0 and report.missing == []


def test_main_rejects_old_pair_without_handover() -> None:
    with pytest.raises(SystemExit):
        boot.main(["--old-pair"])


def test_main_handover_flags_reach_run_and_print_the_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict = {}

    def _fake_run(work_dir, source_root=None, **kw):  # noqa: ANN001
        seen.update(kw)
        return boot.BootReport(
            handover=False, handover_wall_ms=10_050, handover_budget_ms=10_000, phase_process_ms={"a": 1.0}
        )

    monkeypatch.setattr(boot, "run", _fake_run)

    assert boot.main(["--handover", "--old-pair", "--cold-pyc"]) == 0
    out = capsys.readouterr().out
    assert seen == {"cold_pyc": True, "handover": True, "old_pair": True}
    assert "handover=False" in out and "handover_wall_ms=10050 budget_ms=10000" in out


def test_main_without_handover_prints_no_handover_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(boot, "run", lambda *a, **kw: boot.BootReport())

    boot.main([])

    assert "handover=" not in capsys.readouterr().out


def _trace(election_end: float = 100.5) -> dict:
    return {
        "pid": 7,
        "entry": [100.1, 0.05],
        "imported": [100.3, 0.20, 400],
        "stamps": [
            ["not_ready_answerer_start", 100.8, 100.85, 0.30, 0.31],
            ["election", 100.4, election_end, 0.21, 0.22],
            ["record_own_boot", 100.9, 100.95, 0.4, 0.4],
            ["compute_client_token", 100.3, 100.4, 0.20, 0.21],
        ],
        "missing_steps": [],
    }


def test_attribute_boot_splits_each_clock_at_the_election_and_names_the_remainder() -> None:
    row = {"listener_secs": 0.6, "ready_secs": 0.9, "pid": 7}

    out = boot.attribute_boot(_trace(), row, spawn_epoch=100.0)

    first, second = out["spawn_to_listener"], out["listener_to_ready"]
    assert list(first["phases"]) == ["interpreter_startup", "server_import", "compute_client_token", "election"]
    assert first["phases"]["interpreter_startup"] == {"wall_ms": 100.0, "process_ms": 50.0}
    assert first["phases"]["server_import"] == {"wall_ms": 200.0, "process_ms": 150.0}
    assert list(second["phases"]) == ["not_ready_answerer_start"]
    assert first["unattributed_wall_ms"] == pytest.approx(600.0 - (100 + 200 + 100 + 100))
    assert second["unattributed_wall_ms"] == pytest.approx(300.0 - 50.0)
    assert out["top_phase"] == "unattributed_listener_to_ready"
    assert set(boot.BOOT_PHASE_KEYS) <= set(out)
    assert "record_own_boot" not in first["phases"] and "record_own_boot" not in second["phases"]


def test_attribute_boot_without_a_telemetry_row_reports_it_missing_not_a_number() -> None:
    out = boot.attribute_boot(_trace(), None, spawn_epoch=100.0)

    assert out["listener_secs"] is None and "server_boot_row" in out["missing"]
    assert out["spawn_to_listener"]["unattributed_wall_ms"] is None


def test_attribute_boot_without_an_election_stamp_flags_it_and_keeps_every_step_before_ready() -> None:
    trace = _trace()
    trace["stamps"] = [s for s in trace["stamps"] if s[0] != "election"]

    out = boot.attribute_boot(trace, {"listener_secs": 0.6, "ready_secs": 0.9}, spawn_epoch=100.0)

    assert "election" in out["missing"]
    assert "compute_client_token" in out["listener_to_ready"]["phases"]


def test_median_attribution_takes_the_median_of_runs_that_have_both_clocks() -> None:
    def run_(listener: float, token_ms: float) -> dict:
        return {
            "listener_secs": listener,
            "ready_secs": listener + 0.1,
            "spawn_to_listener": {
                "phases": {"compute_client_token": {"wall_ms": token_ms, "process_ms": 1.0}},
                "unattributed_wall_ms": 5.0,
                "unattributed_name": "unattributed_spawn_to_listener",
            },
            "listener_to_ready": {"phases": {}, "unattributed_wall_ms": 1.0, "unattributed_name": "u2"},
            "missing": [],
        }

    out = boot.median_attribution([run_(1.0, 10.0), run_(3.0, 30.0), run_(2.0, 20.0), {"missing": ["trace"]}])

    assert out["runs_used"] == 3
    assert out["listener_secs"] == 2.0 and out["ready_secs"] == 2.1
    assert out["spawn_to_listener"]["phases"]["compute_client_token"]["wall_ms"] == 20.0
    assert out["top_phase"] == "compute_client_token"
    assert out["missing"] == ["trace"]


def test_median_attribution_with_no_usable_run_says_so() -> None:
    out = boot.median_attribution([{"missing": ["spawn"]}])

    assert out["runs_used"] == 0 and out["missing"]


def test_trace_entry_is_valid_python_and_boots_only_under_main() -> None:
    compile(boot._TRACE_ENTRY_SOURCE, boot.TRACE_ENTRY_NAME, "exec")

    namespace: dict = {"__name__": "__mp_main__"}
    exec(compile(boot._TRACE_ENTRY_SOURCE, boot.TRACE_ENTRY_NAME, "exec"), namespace)

    assert callable(namespace["_main"])


def test_main_boot_phases_prints_run_objects_and_the_median(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict = {}
    good = {
        "listener_secs": 1.0,
        "ready_secs": 1.2,
        "spawn_to_listener": {"phases": {"election": {"wall_ms": 5.0, "process_ms": 1.0}}, "unattributed_wall_ms": 0.0, "unattributed_name": "u1"},
        "listener_to_ready": {"phases": {}, "unattributed_wall_ms": 0.0, "unattributed_name": "u2"},
        "missing": [],
    }

    def _fake(work_dir, source_root=None, **kw):  # noqa: ANN001
        seen.update(kw)
        return [good, good]

    monkeypatch.setattr(boot, "run_boot_phases", _fake)

    assert boot.main(["--boot-phases", "--runs", "2"]) == 0
    out = capsys.readouterr().out
    assert seen == {"cold_pyc": False, "runs": 2}
    assert "listener_secs=1.0 ready_secs=1.2" in out and "median=" in out


def test_main_boot_phases_exits_nonzero_when_a_run_produced_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(boot, "run_boot_phases", lambda *a, **kw: [{"missing": ["trace"]}])

    assert boot.main(["--boot-phases"]) == 1


def test_main_rejects_runs_without_boot_phases_and_boot_phases_with_handover() -> None:
    with pytest.raises(SystemExit):
        boot.main(["--runs", "3"])
    with pytest.raises(SystemExit):
        boot.main(["--boot-phases", "--handover"])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_run_boot_phases_attributes_a_real_boot(tmp_path: Path) -> None:
    (result,) = boot.run_boot_phases(work_dir=tmp_path)

    assert result["missing"] == []
    assert result["listener_secs"] > 0 and result["ready_secs"] >= result["listener_secs"]
    first = result["spawn_to_listener"]["phases"]
    assert {"interpreter_startup", "server_import", "compute_client_token", "election"} <= set(first)
    assert {"breadcrumb_write", "register_stack_dump_signal"} <= set(result["listener_to_ready"]["phases"])
    assert result["top_phase"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_run_handover_reports_the_single_attempt_verdict_and_phases_for_the_copied_root(
    tmp_path: Path,
) -> None:
    """Pins the mode, not the pass: a successor that misses the window is the baseline this
    benchmark exists to expose, so a False verdict must come back as a report, never an error."""
    report = boot.run(work_dir=tmp_path, handover=True)

    assert isinstance(report.handover, bool)
    assert report.handover_budget_ms == 10_000
    assert report.handover_wall_ms is not None and report.handover_wall_ms > 0
    if report.handover:
        assert report.missing == []
        assert report.handover_wall_ms <= report.handover_budget_ms + 1_000
        assert "boot_imports_election" in report.phase_process_ms
    else:
        assert "unanswered successor" in report.handover_message or report.missing


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
