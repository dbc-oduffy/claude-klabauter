"""resolve_test_runner_python: a pin that cannot import pytest is replaced by one that can, or named plainly."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import interpreter_pin as ip


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ip, "_capability_cache_path", lambda: tmp_path / "cache" / "caps.json")
    ip._imports_pytest.cache_clear()
    yield
    ip._imports_pytest.cache_clear()


def _fake(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_text("", encoding="utf-8")
    return path.as_posix()


def _repo(tmp_path: Path, pin: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "coordinator.local.md").write_text(f"---\ntargeted_test_python: \"{pin}\"\n---\n", encoding="utf-8")
    return repo


def _fakes(monkeypatch, probes: dict, calls: list):
    def probe(command):
        calls.append(command)
        return probes[command]

    monkeypatch.setattr(ip, "_probe_pytest", probe)


def test_a_pin_without_pytest_falls_back_to_a_candidate_that_has_it(tmp_path, monkeypatch):
    py312, py313 = _fake(tmp_path, "py312.exe"), _fake(tmp_path, "py313.exe")
    _fakes(monkeypatch, {py312: False, py313: True}, [])
    monkeypatch.setattr(ip, "_fallback_candidates", lambda: [py313])
    got = ip.resolve_test_runner_python(_repo(tmp_path, py312))
    assert got.command == py313 and got.source == "pytest-fallback" and got.defect is None


def test_a_pin_with_pytest_is_kept_and_only_it_is_probed(tmp_path, monkeypatch):
    pin = _fake(tmp_path, "py.exe")
    calls: list = []
    _fakes(monkeypatch, {pin: True}, calls)
    monkeypatch.setattr(ip, "_fallback_candidates", lambda: pytest.fail("fallbacks must not be listed"))
    got = ip.resolve_test_runner_python(_repo(tmp_path, pin))
    assert got.command == pin and got.source == "targeted_test_python" and calls == [pin]


def test_the_answer_is_cached_per_box_so_a_fresh_process_does_not_spawn(tmp_path, monkeypatch):
    pin = _fake(tmp_path, "py.exe")
    calls: list = []
    _fakes(monkeypatch, {pin: True}, calls)
    repo = _repo(tmp_path, pin)
    ip.resolve_test_runner_python(repo)
    ip._imports_pytest.cache_clear()
    ip.resolve_test_runner_python(repo)
    assert calls == [pin]


def test_no_candidate_with_pytest_is_a_defect_naming_the_pin_and_the_remedy(tmp_path, monkeypatch):
    pin = _fake(tmp_path, "py312.exe")
    other = _fake(tmp_path, "other.exe")
    _fakes(monkeypatch, {pin: False, other: False}, [])
    monkeypatch.setattr(ip, "_fallback_candidates", lambda: [other])
    got = ip.resolve_test_runner_python(_repo(tmp_path, pin))
    assert got.defect and "cannot import pytest" in got.defect and "pip install pytest" in got.defect and other in got.defect


def test_an_unpinned_repo_is_not_probed(tmp_path, monkeypatch):
    repo = tmp_path / "bare"
    repo.mkdir()
    monkeypatch.setattr(ip, "_probe_pytest", lambda c: pytest.fail("no pin, no probe"))
    assert ip.resolve_test_runner_python(repo).command is None


def test_the_engine_interpreter_is_answered_in_process():
    assert ip._probe_pytest(sys.executable) is True
