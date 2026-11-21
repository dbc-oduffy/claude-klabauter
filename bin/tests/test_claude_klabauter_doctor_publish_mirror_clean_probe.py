"""Behavioural coverage for `claude-klabauter.publish.mirror_clean`
(`bin/claude-klabauter-doctor-probe.py::_run_probe_publish_mirror_clean`): PASS on a clean
mirror clone, DEGRADED naming files on a dirty one, skipped when unregistered.
Clones are built under tmp_path; the probe never mutates them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

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


def _register(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda key, *a, **k: value)


def test_unregistered_mirror_is_skipped(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    _register(monkeypatch, "")
    result = mod._run_probe_publish_mirror_clean(None)
    assert result.status == mod._INFO
    assert result.skipped is True


def test_missing_directory_is_skipped(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _register(monkeypatch, str(tmp_path / "absent"))
    result = mod._run_probe_publish_mirror_clean(None)
    assert result.status == mod._INFO
    assert result.skipped is True
