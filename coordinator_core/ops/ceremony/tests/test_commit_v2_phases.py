"""PhaseLedger: phase summing, one phase_time row, never-raises, env kill switch."""
import json

import pytest

from coordinator_core.ops.ceremony import commit_v2_phases as cp
from coordinator_core.telemetry import op_latency


@pytest.fixture
def common_dir(tmp_path, monkeypatch):
    d = tmp_path / ".git"
    d.mkdir()
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: d)
    monkeypatch.delenv("COORDINATOR_OP_LATENCY_DISABLE", raising=False)
    return d


def _rows(common_dir):
    sink = common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    if not sink.exists():
        return []
    return [json.loads(x) for x in sink.read_text(encoding="utf-8").splitlines() if x]


def _fake_clock(monkeypatch, ticks):
    it = iter(ticks)
    monkeypatch.setattr(cp.time, "thread_time", lambda: next(it))


def test_repeated_and_nested_phases_sum(monkeypatch):
    # outer: 0..10 ; inner a: 1..3 ; inner a again: 4..5 ; inner b: 6..9
    _fake_clock(monkeypatch, [0, 1, 3, 4, 5, 6, 9, 10])
    led = cp.PhaseLedger()
    with led.phase("outer"):
        with led.phase("a"):
            pass
        with led.phase("a"):
            pass
        with led.phase("b"):
            pass
    ms = led.phases_ms()
    assert ms == {"a": 3000.0, "b": 3000.0, "outer": 10000.0}
    assert list(ms) == ["a", "b", "outer"]


def test_phase_records_on_exception(monkeypatch):
    _fake_clock(monkeypatch, [0, 2])
    led = cp.PhaseLedger()
    with pytest.raises(ValueError):
        with led.phase("x"):
            raise ValueError
    assert led.phases_ms() == {"x": 2000.0}


def test_phase_charges_child_bucket(monkeypatch):
    from coordinator_core import ipc

    _fake_clock(monkeypatch, [0, 0])
    bucket = []
    token = ipc._op_cpu_secs_var.set(bucket)
    try:
        led = cp.PhaseLedger()
        with led.phase("git"):
            ipc.charge_op_cpu_secs(0.25)
    finally:
        ipc._op_cpu_secs_var.reset(token)
    assert led.phases_ms() == {"git": 250.0}


def test_emit_writes_one_phase_time_row(common_dir, tmp_path, monkeypatch):
    _fake_clock(monkeypatch, [0, 1])
    led = cp.PhaseLedger(corr_id="c-1")
    with led.phase("stage"):
        pass
    led.emit(tmp_path, abandoned=True, paths=2, spawns=7)
    rows = _rows(common_dir)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "phase_time"
    assert r["op"] == "ceremony.commit_v2"
    assert r["corr_id"] == "c-1"
    assert r["phases_ms"] == {"stage": 1000.0}
    assert r["total_ms"] == 1000.0
    assert r["abandoned"] is True
    assert r["paths"] == 2
    assert r["spawns"] == 7


def test_emit_not_abandoned_by_default(common_dir, tmp_path):
    cp.PhaseLedger().emit(tmp_path, abandoned=False, paths=0, spawns=None)
    (r,) = _rows(common_dir)
    assert r["abandoned"] is False
    assert r["spawns"] is None


def test_emit_swallows_write_failure(tmp_path, monkeypatch):
    def boom(entry, repo_root):
        raise RuntimeError("x")

    monkeypatch.setattr(op_latency, "_write_entry", boom)
    cp.PhaseLedger().emit(tmp_path, abandoned=False, paths=1, spawns=0)


def test_kill_switch_writes_nothing(common_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_OP_LATENCY_DISABLE", "1")
    cp.PhaseLedger().emit(tmp_path, abandoned=False, paths=1, spawns=0)
    assert _rows(common_dir) == []
