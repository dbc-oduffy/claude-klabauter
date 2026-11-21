"""Behavioural coverage for `claude-klabauter.claude.cross_session_inbound`
(`bin/claude-klabauter-doctor-probe.py::_run_probe_cross_session_inbound`): flagged when the Claude
settings.json lacks `crossSessionInbound=accept`, ok when it is accept, reported not raised
when the file is unparseable, skipped where no Claude host exists.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _require_module() -> ModuleType:
    key = "claude_klabauter_doctor_probe_cross_session_inbound_unit"
    spec = importlib.util.spec_from_file_location(key, _BIN_PROBE)
    if spec is None or spec.loader is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not importable")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:  # pragma: no cover - environment-dependent
        sys.modules.pop(key, None)
        pytest.skip("bin/claude-klabauter-doctor-probe.py not importable")
    return mod


@pytest.fixture
def probe(tmp_path, monkeypatch):
    mod = _require_module()
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return mod, claude_dir / "settings.json"


def test_accept_is_pass(probe):
    mod, settings = probe
    settings.write_text('{"crossSessionInbound": "accept"}', encoding="utf-8")
    r = mod._run_probe_cross_session_inbound()
    assert r.status == mod._PASS and not r.skipped


@pytest.mark.parametrize(
    "raw",
    ['{"other": 1}', '{"crossSessionInbound": "reject"}', '{"crossSessionInbound": null}'],
)
def test_absent_or_non_accept_is_flagged(probe, raw):
    mod, settings = probe
    settings.write_text(raw, encoding="utf-8")
    r = mod._run_probe_cross_session_inbound()
    assert r.status == mod._DEGRADED and not r.skipped
    assert "crossSessionInbound" in r.detail
    assert "scripts/setup.py" in r.remediation


def test_missing_settings_file_is_flagged(probe):
    mod, settings = probe
    assert not settings.exists()
    r = mod._run_probe_cross_session_inbound()
    assert r.status == mod._DEGRADED
    assert "absent" in r.detail


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]"])
def test_unparseable_settings_is_reported_not_raised(probe, raw):
    mod, settings = probe
    settings.write_text(raw, encoding="utf-8")
    r = mod._run_probe_cross_session_inbound()
    assert r.status == mod._DEGRADED
    assert "unreadable" in r.detail


def test_no_claude_host_skips(tmp_path, monkeypatch):
    mod = _require_module()
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "nohome"))
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: None)
    r = mod._run_probe_cross_session_inbound()
    assert r.skipped and not r.required


def test_claude_config_dir_selects_the_settings_file(probe, tmp_path, monkeypatch):
    mod, home_settings = probe
    home_settings.write_text("{}", encoding="utf-8")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settings.json").write_text('{"crossSessionInbound": "accept"}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert mod._run_probe_cross_session_inbound().status == mod._PASS
    (cfg / "settings.json").write_text("{}", encoding="utf-8")
    home_settings.write_text('{"crossSessionInbound": "accept"}', encoding="utf-8")
    assert mod._run_probe_cross_session_inbound().status == mod._DEGRADED


def test_probe_never_gates_step_zero(probe):
    mod, settings = probe
    settings.write_text("{}", encoding="utf-8")
    assert mod._run_probe_cross_session_inbound().required is False


def test_probe_is_in_default_run_set():
    mod = _require_module()
    src = _BIN_PROBE.read_text(encoding="utf-8")
    assert "_add(_CROSS_SESSION_INBOUND_PROBE, _run_probe_cross_session_inbound)" in src
    assert mod._CROSS_SESSION_INBOUND_PROBE == "claude-klabauter.claude.cross_session_inbound"
