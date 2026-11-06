"""Behavioural coverage for `claude-klabauter.publish.mirror_clean`
(`bin/claude-klabauter-doctor-probe.py::_run_probe_publish_mirror_clean`): PASS on a clean
mirror clone, DEGRADED naming files on a dirty one, skipped when unregistered.
Clones are built under tmp_path; the probe never mutates them.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_BIN_PROBE = Path(__file__).parent.parent.parent.resolve() / "bin" / "claude-klabauter-doctor-probe.py"


@pytest.fixture
def mod():
    key = "claude_klabauter_doctor_probe_publish_mirror_clean_unit"
    spec = importlib.util.spec_from_file_location(key, _BIN_PROBE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(key, None)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    repo = tmp_path / "claude-klabauter"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "engine.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "engine.py")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _register(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda key, *a, **k: value)


def test_clean_mirror_is_pass(mod, clone, monkeypatch) -> None:
    _register(monkeypatch, str(clone))
    result = mod._run_probe_publish_mirror_clean(None)
    assert result.probe == mod._PUBLISH_MIRROR_CLEAN_PROBE
    assert result.status == mod._PASS


def test_dirty_mirror_is_degraded_and_names_files(mod, clone, monkeypatch) -> None:
    (clone / "engine.py").write_text("x = 2\n", encoding="utf-8")
    (clone / "stray.py").write_text("y\n", encoding="utf-8")
    _register(monkeypatch, str(clone))
    result = mod._run_probe_publish_mirror_clean(None)
    assert result.status == mod._DEGRADED
    assert "engine.py" in result.detail and "stray.py" in result.detail
    assert (clone / "engine.py").read_text(encoding="utf-8") == "x = 2\n"


def test_unregistered_mirror_is_skipped(mod, monkeypatch) -> None:
    _register(monkeypatch, "")
    result = mod._run_probe_publish_mirror_clean(None)
    assert result.status == mod._INFO
    assert result.skipped is True


def test_missing_directory_is_skipped(mod, tmp_path, monkeypatch) -> None:
    _register(monkeypatch, str(tmp_path / "absent"))
    result = mod._run_probe_publish_mirror_clean(None)
    assert result.status == mod._INFO
    assert result.skipped is True
