"""Push-sweep row shape: `ahead` and `elapsed_secs` are written only when given."""

from __future__ import annotations

import json
import subprocess

from coordinator_core.warm import telemetry


def _rows(tmp_path):
    p = telemetry.push_sweep_path(tmp_path)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def test_new_keys_absent_when_not_passed(tmp_path):
    telemetry.record_sweep(["a"], ["b"], [], engine_root=tmp_path)
    row = _rows(tmp_path)[0]
    assert "ahead" not in row and "elapsed_secs" not in row
    assert row["not_reached"] == []


def test_new_keys_present_and_serialisable(tmp_path):
    telemetry.record_sweep(
        [], [], [], not_reached=["c"], ahead=["a", "c"], elapsed_secs=1.23456,
        engine_root=tmp_path,
    )
    row = _rows(tmp_path)[0]
    assert row["ahead"] == ["a", "c"]
    assert row["elapsed_secs"] == 1.235
    assert row["not_reached"] == ["c"]


def test_empty_ahead_is_recorded(tmp_path):
    telemetry.record_sweep([], [], [], ahead=[], elapsed_secs=0, engine_root=tmp_path)
    row = _rows(tmp_path)[0]
    assert row["ahead"] == [] and row["elapsed_secs"] == 0.0


def test_never_raises_on_unwritable_svc_dir(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(telemetry, "svc_dir", lambda engine_root=None: blocker / "sub")
    telemetry.record_sweep([], [], [], ahead=["a"], elapsed_secs=1.0)


def test_spawns_nothing(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    telemetry.record_sweep([], [], [], ahead=["a"], elapsed_secs=1.0, engine_root=tmp_path)
    assert _rows(tmp_path)
