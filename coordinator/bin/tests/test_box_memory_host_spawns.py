"""Tests for coordinator/bin/lib/box_memory_host.py (no process spawn, no real governor)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

_HOST = Path(__file__).resolve().parents[1] / "lib" / "box_memory_host.py"
_spec = importlib.util.spec_from_file_location("box_memory_host_under_test", _HOST)
host = importlib.util.module_from_spec(_spec)
# dataclass resolves its module through sys.modules, so register before exec.
sys.modules[_spec.name] = host
_spec.loader.exec_module(host)

GIB = host.GIB


@pytest.fixture(autouse=True)
def _roomy_cpu_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep memory-axis tests independent of the runner's core count."""
    monkeypatch.setattr(host, "default_cpu_budget", lambda: 64)


_CPU_KW = dict(rss_fn=lambda p: 0.0, alive_fn=lambda p: True, image_fn=lambda p: "python.exe")
_ROOMY = host.MemoryStatus(100 * GIB, 128 * GIB, 0, 200 * GIB)
_LEGACY_KW = dict(alive_fn=lambda p: True, create_time_fn=lambda p: 500.0, image_fn=lambda p: "python.exe",
                  now_fn=lambda: 1000.0)


@pytest.fixture
def live_child():
    import subprocess

    code = "b = bytearray(40 * 1024 * 1024)\nfor i in range(0, len(b), 4096): b[i] = 1\nprint('up', flush=True)\ninput()\n"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, creationflags=flags,
    )
    try:
        assert proc.stdout.readline().strip() == b"up"
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _peaks(home: Path) -> dict:
    import json

    return json.loads((home / "state" / "group-em" / "box" / "job-peaks.json").read_text(encoding="utf-8"))


def test_release_records_the_childs_measured_peak_not_the_declared_one(tmp_path: Path, live_child) -> None:
    adm, home = tmp_path / "admission.jsonl", tmp_path / "home"
    host.register_command(adm, "k", live_child.pid, 8.0)
    host.release_command(adm, "k", live_child.pid, job_type="T", peaks_home=home)
    entry = _peaks(home)["T"]
    assert 30 < entry["ram_mb"] < 8.0 * 1024
    assert entry["samples"] == 1
    assert host.reserved_bytes(adm, rss_fn=lambda p: 0.0, alive_fn=lambda p: True, image_fn=lambda p: "python.exe") == 0


def test_release_job_type_defaults_to_the_command_key(tmp_path: Path, live_child) -> None:
    adm, home = tmp_path / "admission.jsonl", tmp_path / "home"
    host.release_command(adm, "the-key", live_child.pid, peaks_home=home)
    assert list(_peaks(home)) == ["the-key"]


def test_release_survives_a_failed_job_peaks_import(tmp_path: Path, live_child, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(name, repo_root=None):
        raise OSError(f"cannot import {name}")

    monkeypatch.setattr(host, "_engine_module", refuse)
    adm = tmp_path / "admission.jsonl"
    host.register_command(adm, "k", live_child.pid, 4.0)
    host.release_command(adm, "k", live_child.pid, job_type="T", peaks_home=tmp_path / "home")
    assert not (tmp_path / "home").exists()
    assert host.reserved_bytes(adm, rss_fn=lambda p: 0.0, alive_fn=lambda p: True, image_fn=lambda p: "python.exe") == 0
