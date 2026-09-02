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

    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    rc = substrate.main(["--engine-root", str(engine_dir), "--check-only"])

    assert rc == 0
    assert captured["engine_root_env"] == str(engine_dir)


def test_engine_root_flag_empty_string_errors_loudly(monkeypatch, capsys):
    """Review: code-reviewer Finding 1 — `--engine-root ""` must not
    silently degrade to whatever rung would otherwise fire; it is an
    explicit, obviously-wrong value and should fail loudly instead."""
    monkeypatch.setattr(
        substrate, "run", lambda **_: pytest.fail("run() must not be reached")
    )

    rc = substrate.main(["--engine-root", "", "--check-only"])

    assert rc == 1
    assert "not an existing directory" in capsys.readouterr().err


def test_engine_root_flag_nonexistent_path_errors_loudly(monkeypatch, tmp_path, capsys):
    """Review: code-reviewer Finding 2 — a typo'd/nonexistent path is
    rejected at parse time, at the flag that caused it, rather than
    degrading into a less legible failure downstream in run()."""
    monkeypatch.setattr(
        substrate, "run", lambda **_: pytest.fail("run() must not be reached")
    )
    bad_path = tmp_path / "does-not-exist"

    rc = substrate.main(["--engine-root", str(bad_path), "--check-only"])

    assert rc == 1
    assert "not an existing directory" in capsys.readouterr().err


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


def test_env_var_bootstrap_remedy_actually_resolves(monkeypatch, tmp_path):
    """Review: code-reviewer Finding 4 — the ordering-only test above never
    proved the printed remedies actually work. Round-trips through the same
    shim: set COORDINATOR_ENGINE_ROOT per the message's own remedy text,
    then re-call `_resolve_claude_klabauter_root` and confirm it now succeeds instead
    of raising, closing the "message names X" / "X actually works" gap.
    """
    shim = _load_shim()
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()

    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(engine_dir))

    resolved = shim._resolve_claude_klabauter_root(ml_dir)

    assert resolved == str(engine_dir)


def test_engine_root_flag_bootstrap_remedy_resolves_via_substrate(
    monkeypatch, tmp_path
):
    """Same round-trip, driven through the actual bootstrap surface
    (`install-substrate --engine-root`) rather than setting the env var by
    hand — confirms the flag's own overlay satisfies the shim's ladder."""
    shim = _load_shim()
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()

    captured = {}

    def _fake_run(setup_only=False, check_only=False, allow_venv_fallback=False):
        captured["resolved"] = shim._resolve_claude_klabauter_root(ml_dir)
        return 0

    monkeypatch.setattr(substrate, "run", _fake_run)

    rc = substrate.main(["--engine-root", str(engine_dir), "--check-only"])

    assert rc == 0
    assert captured["resolved"] == str(engine_dir)
