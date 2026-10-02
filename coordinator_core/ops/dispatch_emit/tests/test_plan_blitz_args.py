"""Covers plan_blitz_args.resolve: roster detection, key omission, gateReportPath passthrough."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import plan_blitz_args as pba


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setattr(pba.shutil, "which", lambda name: None)
    return home


def _plugin(tmp_path: Path, agents=("plan-author.md", "blitz-em.md"), bins=()) -> Path:
    root = tmp_path / "plugin"
    (root / "agents").mkdir(parents=True)
    (root / "bin").mkdir()
    for a in agents:
        (root / "agents" / a).write_text("x", encoding="utf-8")
    for b in bins:
        (root / "bin" / b).write_text("x", encoding="utf-8")
    return root


def test_full_roster_true_and_gate_path_passthrough(isolated, tmp_path):
    out = pba.resolve(plugin_root=_plugin(tmp_path), engine_root=None, sizing_abs="/abs/s.yaml")
    assert out["pluginAgentsAvailable"] is True
    assert out["gateReportPath"] == "/abs/s.yaml"


def test_missing_agents_dir_is_false_key_present(isolated, tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    out = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    assert out["pluginAgentsAvailable"] is False


def test_no_plugin_root_is_false(isolated):
    out = pba.resolve(plugin_root=None, engine_root=None, sizing_abs="s")
    assert out["pluginAgentsAvailable"] is False


def test_unresolvable_clis_are_omitted_not_null(isolated, tmp_path):
    out = pba.resolve(plugin_root=_plugin(tmp_path), engine_root=None, sizing_abs="s")
    assert set(out) == {"pluginAgentsAvailable", "gateReportPath"}


def test_plugin_bins_resolve_when_present(isolated, tmp_path):
    root = _plugin(tmp_path, bins=("plan-spine-check.py", "instrument-can-report-red.py"))
    out = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    assert "plan-spine-check.py" in out["spineCheckCli"]
    assert "instrument-can-report-red.py" in out["armingCheckCli"]


def test_sidecar_resolves_from_settings_home(isolated, tmp_path):
    (isolated / "bin").mkdir()
    (isolated / "bin" / "provision-sidecar").write_text("x", encoding="utf-8")
    out = pba.resolve(plugin_root=None, engine_root=None, sizing_abs="s")
    assert out["provisionSidecarCli"].endswith("provision-sidecar")


def test_sidecar_resolves_from_engine_bin(isolated, tmp_path):
    eng = tmp_path / "eng"
    (eng / "coordinator" / "bin").mkdir(parents=True)
    (eng / "coordinator" / "bin" / "provision-sidecar.py").write_text("x", encoding="utf-8")
    out = pba.resolve(plugin_root=None, engine_root=eng, sizing_abs="s")
    assert "provision-sidecar.py" in out["provisionSidecarCli"]
