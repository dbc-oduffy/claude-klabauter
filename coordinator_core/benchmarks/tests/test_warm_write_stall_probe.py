"""warm_write_stall_probe: the shim blocks only under its root, the dump classifier splits caller from
worker, and a tiny-argument `measure` returns the documented shape."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import warm_write_stall_probe as probe

_CALLER_DUMP = """engine-stamp: sha:x
box-msmpeng-cpu-pct: 42.5 (100 = one core; 24 logical cpus)
legs-ms: [[10003, 3033, "coordinator_core/ops/dispatch_emit/land_leg_record.py::write > _abc.py::write_text"], [11000, 500, "coordinator_core/git/commit.py::checkin_blob_sha > _abc.py::read_bytes"]]
--- threads ---
Thread 0x0000000000000001 (most recent call first):
  File "/py/Lib/tempfile.py", line 256 in _mkstemp_inner
  File "/py/Lib/tempfile.py", line 357 in mkstemp
  File "/x/coordinator_core/atomic_replace.py", line 235 in atomic_write_bytes
  File "/x/coordinator_core/ipc.py", line 2433 in _run_handler_recording

Thread 0x0000000000000002 (most recent call first):
  File "/py/Lib/pathlib/_local.py", line 515 in stat
  File "/x/coordinator_core/telemetry/log_rotation.py", line 176 in rotate_if_needed
  File "/py/Lib/threading.py", line 992 in run
"""


@pytest.mark.deliberate_wall_clock(reason="the shim's block is a wall-clock wait by construction")
def test_slow_fs_blocks_under_root_only_and_restores(tmp_path: Path) -> None:
    root = tmp_path / "fx"
    root.mkdir()
    outside = Path(tempfile.mkdtemp(prefix="slowfs-outside-", dir=str(tmp_path)))
    orig = (tempfile.mkstemp, os.replace, os.makedirs)
    with probe.SlowFs(root, 0.4) as shim:
        t0 = time.perf_counter()
        fd, name = tempfile.mkstemp(dir=str(root))
        inside = time.perf_counter() - t0
        os.close(fd)
        os.remove(name)
        fd2, name2 = tempfile.mkstemp(dir=str(outside))
        os.close(fd2)
        assert shim.hits["mkstemp"] == 1
    assert inside >= 0.35
    assert (tempfile.mkstemp, os.replace, os.makedirs) == orig


def test_slow_fs_release_frees_a_blocked_call(tmp_path: Path) -> None:
    import threading

    with probe.SlowFs(tmp_path, 30.0) as shim:
        done = threading.Event()

        def call() -> None:
            os.makedirs(str(tmp_path / "a" / "b"), exist_ok=True)
            done.set()

        threading.Thread(target=call, daemon=True).start()
        assert not done.wait(0.1)
        shim.release()
        assert done.wait(5.0)


def test_classify_dumps_splits_caller_from_worker(tmp_path: Path) -> None:
    (tmp_path / "20261118T210337Z-1-2-3-4.txt").write_text(_CALLER_DUMP, encoding="utf-8")
    (tmp_path / "20261117T010101Z-1-2-3-4.txt").write_text(_CALLER_DUMP, encoding="utf-8")
    today = probe.classify_dumps(tmp_path, "20261118")
    assert today["dumps"] == 1
    frames = today["frames"]
    atomic = frames["atomic_replace.atomic_write_bytes"]
    assert (atomic["caller"], atomic["worker"], atomic["innermost_fs"]) == (1, 0, 1)
    assert frames["tempfile._mkstemp_inner"]["caller"] == 1
    rotate = frames["log_rotation.rotate_if_needed"]
    assert (rotate["caller"], rotate["worker"], rotate["innermost_fs"]) == (0, 1, 1)
    assert frames["land_leg_record.write"]["legs_ge_1s"] == 1
    assert frames["land_leg_record.write"]["leg_max_ms"] == 3033
    assert frames["pathlib.read_bytes"]["legs_ge_1s"] == 0
    assert frames["pathlib.stat"]["worker"] == 1
    assert today["msmpeng_cpu_pct"]["max"] == 42.5
    assert probe.classify_dumps(tmp_path, "")["dumps"] == 2


def test_measure_returns_the_documented_shape(tmp_path: Path) -> None:
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    (dumps / "20261118T210337Z-1-2-3-4.txt").write_text(_CALLER_DUMP, encoding="utf-8")
    result = probe.measure(
        writers=2, rounds=1, stall_secs=0.3, entries=12, samples=4, root=tmp_path, dumps=dumps, day="20261118",
    )
    for op in ("admits", "claim_lookup"):
        for variant in ("plain", "slow_fs"):
            phase = result[op][variant]
            assert phase["ops"] == 2 and phase["errors"] == []
            assert set(phase["caller_wait_ms"]) == {"p50", "p95", "max"}
            assert phase["spawns"] == 0
        assert sum(result[op]["slow_fs"]["shim_hits"].values()) >= 1
        assert "shim_hits" not in result[op]["plain"]
    assert result["admits"]["plain"]["write_landed"] is True
    for variant in ("plain", "slow_fs"):
        for label, phase in result["unbounded_writes"][variant].items():
            assert phase["ops"] == 2 and phase["errors"] == [] and phase["write_landed"] is True, (variant, label)
    for variant in ("plain", "slow_fs"):
        frames = result["halted_landing"][variant]
        assert set(frames) == {"run_token.note_halt_landing", "halted_withheld_index._persist", "op._write_staged_prompt"}
        for label, phase in frames.items():
            assert phase["ops"] == 2 and phase["errors"] == [] and phase["write_landed"] is True, (variant, label)
    for label, phase in result["halted_landing"]["slow_fs"].items():
        assert sum(phase["shim_hits"].values()) >= 1, label
    cost = result["atomic_write_cost"]
    for label in ("fresh_dir", "large_dir"):
        assert {"is_file", "os_stat", "sweep_dead_temps", "mkstemp", "replace", "total_cold_sweep"} <= set(cost[label]["steps_wall_ms"])
    calls = cost["fresh_dir"]["fs_calls_per_write"]
    assert calls["cold_sweep"]["scandir"] >= 1 and calls["warm_sweep"]["scandir"] == 0
    raw = result["raw_latency"]
    assert raw["large_dir"]["entries"] >= 12 and raw["fresh_dir"]["entries"] <= 8
    assert result["dump_classification"]["dumps"] == 1
    assert "caller-wait-ms" in probe.render(result)
    assert not list(tmp_path.glob("warm-write-stall-probe-*"))


@pytest.mark.deliberate_wall_clock(reason="a call slept past the bar must read as outside it")
def test_drive_reports_a_call_slept_past_the_bar_as_outside_it() -> None:
    slow = probe._drive(2, 1, lambda i: None, lambda i: time.sleep((probe.CALLER_WAIT_BAR_MS + 150) / 1000.0))
    assert slow["within_bar"] is False
