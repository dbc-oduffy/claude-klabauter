"""Tests for `install-substrate --engine-root` (C4,
docs/dispatch-briefs/2026-09-01-the-dogfooded-install-stops-lying-about/C4.md).

Covers: the flag parses, sets COORDINATOR_ENGINE_ROOT ahead of `run()` so it
outranks every discovered rung (registry/sentinel), and the shim's terminal
resolution-failure message leads with BOOTSTRAP remedies (the flag, the env
var, the sentinel file) before POST-BOOTSTRAP ones (`machine-local set`).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from coordinator_core.install import substrate


@pytest.fixture(autouse=True)
def _clean_engine_root_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    yield


def test_engine_root_flag_sets_env_var_before_run(monkeypatch, tmp_path):
    captured = {}

    def _fake_run(setup_only=False, check_only=False, allow_venv_fallback=False):
        captured["engine_root_env"] = os.environ.get("COORDINATOR_ENGINE_ROOT")
        return 0

    monkeypatch.setattr(substrate, "run", _fake_run)

    rc = substrate.main(["--engine-root", str(tmp_path / "engine"), "--check-only"])

    assert rc == 0
    assert captured["engine_root_env"] == str(tmp_path / "engine")


def test_no_engine_root_flag_leaves_env_untouched(monkeypatch):
    captured = {}

    def _fake_run(setup_only=False, check_only=False, allow_venv_fallback=False):
        captured["engine_root_env"] = os.environ.get("COORDINATOR_ENGINE_ROOT")
        return 0

    monkeypatch.setattr(substrate, "run", _fake_run)

    rc = substrate.main(["--check-only"])

    assert rc == 0
    assert captured["engine_root_env"] is None


_SHIM_PATH = (
    Path(__file__).resolve().parents[3]
    / "coordinator"
    / "lib"
    / "resolve-claude-klabauter"
    / "_resolve_claude_klabauter.py"
)


def _load_shim():
    spec = importlib.util.spec_from_file_location("_resolve_claude_klabauter_test_load", _SHIM_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolution_error_leads_with_bootstrap_remedies(tmp_path):
    shim = _load_shim()
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()

    with pytest.raises(shim.ClaudeKlabauterResolutionError) as excinfo:
        shim._resolve_claude_klabauter_root(ml_dir)

    message = str(excinfo.value)

    bootstrap_flag_idx = message.index("--engine-root")
    bootstrap_env_idx = message.index("COORDINATOR_ENGINE_ROOT")
    bootstrap_sentinel_idx = message.index(".claude-klabauter-live-root")
    post_bootstrap_idx = message.index("machine-local set")

    # Bootstrap remedies (reachable before machine-local is configured) all
    # precede the post-bootstrap `machine-local set` remedies.
    assert bootstrap_flag_idx < post_bootstrap_idx
    assert bootstrap_env_idx < post_bootstrap_idx
    assert bootstrap_sentinel_idx < post_bootstrap_idx
