"""Spotlight advice: darwin indexed -> advice, darwin excluded -> silent, non-darwin -> no spawn."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from coordinator_core.ops import doctor


def _runner(stdout: str, returncode: int = 0):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    run.calls = calls
    return run


def test_darwin_indexed_root_gets_advice(tmp_path: Path):
    run = _runner(f"{tmp_path}/repo/CLAUDE.md\n")
    advice = doctor.spotlight_advice(tmp_path, platform="darwin", run=run)
    assert advice and str(tmp_path) in advice and "Search Privacy" in advice
    assert run.calls == [["mdfind", "-onlyin", str(tmp_path), "-name", "CLAUDE.md"]]


def test_darwin_excluded_root_is_silent(tmp_path: Path):
    assert doctor.spotlight_advice(tmp_path, platform="darwin", run=_runner("")) is None


def test_darwin_probe_failure_is_silent(tmp_path: Path):
    assert doctor.spotlight_advice(tmp_path, platform="darwin", run=_runner("x", returncode=1)) is None

    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    assert doctor.spotlight_advice(tmp_path, platform="darwin", run=boom) is None


def test_non_darwin_spawns_nothing(tmp_path: Path):
    run = _runner("hit\n")
    for plat in ("win32", "linux"):
        assert doctor.spotlight_advice(tmp_path, platform=plat, run=run) is None
    assert run.calls == []


def test_layer_off_darwin_resolves_no_root(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def boom():
        raise AssertionError("engine root resolved off darwin")

    monkeypatch.setattr("coordinator_core.engine_root.coordinator_engine_root", boom)
    layer = doctor._check_spotlight_exclusion()
    assert layer.status == "ok" and layer.findings == []


def test_run_doctor_sweeps_kept_blobs_only_when_a_root_is_passed(monkeypatch, tmp_path: Path):
    from coordinator_core.ops import doctor_kept_blobs

    seen: list[Path] = []

    def fake(root):
        seen.append(root)
        return doctor.Layer(doctor_kept_blobs.LAYER_NAME, "ok", [])

    monkeypatch.setattr(doctor_kept_blobs, "kept_blobs_layer", fake)
    doctor.run_doctor()
    assert seen == []
    doctor.run_doctor(kept_blobs_root=tmp_path)
    assert seen == [tmp_path]


def test_layer_is_info_only_and_never_broken(monkeypatch, tmp_path: Path):
    # Wiring only: the probe itself is covered by the injected-runner tests above.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("coordinator_core.engine_root.coordinator_engine_root", lambda: str(tmp_path / "eng"))
    monkeypatch.setattr(doctor, "spotlight_advice", lambda root: "exclude it")
    layer = doctor._check_spotlight_exclusion()
    assert layer.status == "ok"
    assert [f.severity for f in layer.findings] == ["info"]
