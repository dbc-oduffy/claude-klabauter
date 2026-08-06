"""Resolution-order tests for the standalone `coordinator/bin/machine-local`
bare-name forwarder.

Why this file exists: on 2026-07-28 the deliberate retirement of `~/.claude/bin`
(archived to `bin.retired-<ts>/`, verified, reversible) left this forwarder naming
the retired directory as its ONLY install location. Every `machine-local get` then
exited 127, and callers that treat a non-zero exit as "key unset" — the common
`_ml_get` shape — silently read the whole registry as empty. Consumers each fell
through to their own last-resort rung: the meta-repo's generated git hooks baked a
path that does not exist, disarming auto-push in `~/.claude`.

The sibling resolvers (`bare_forwarder.forward`,
`machine_local_impl_resolve`) were already settings-home-first; this standalone
script was simply missed. These tests hold the ordering so it cannot regress
independently of them.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest

_FORWARDER = Path(__file__).resolve().parents[1] / "machine-local"


def _load():
    """Import the extensionless forwarder script as a module."""
    spec = importlib.util.spec_from_loader(
        "machine_local_bare_forwarder",
        importlib.machinery.SourceFileLoader(
            "machine_local_bare_forwarder", str(_FORWARDER)
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_settings_home_is_tried_before_legacy_claude_bin(monkeypatch, tmp_path):
    """Durable settings-home install wins over the legacy ~/.claude/bin path."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    mod = _load()
    assert len(mod._REAL_BASES) >= 2
    assert mod._REAL_BASES[0].startswith(str(tmp_path / "settings"))
    assert ".claude" in mod._REAL_BASES[1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit probe")
def test_resolves_settings_home_install(monkeypatch, tmp_path):
    settings = tmp_path / "settings" / "bin"
    settings.mkdir(parents=True)
    real = settings / "machine-local"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "nonexistent-home"))
    mod = _load()
    assert mod._resolve_real() == str(real)


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit probe")
def test_falls_back_to_legacy_claude_bin_when_settings_home_absent(monkeypatch, tmp_path):
    """A machine that predates the settings-home move must keep working."""
    legacy = tmp_path / "home" / ".claude" / "bin"
    legacy.mkdir(parents=True)
    real = legacy / "machine-local"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-settings-here"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    mod = _load()
    assert mod._resolve_real() == str(real)


def test_remediation_names_every_probed_location(monkeypatch, tmp_path):
    """A 127 must say where it looked. The failure this file guards against was
    invisible precisely because callers saw only 'unset', never 'unreachable'."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    mod = _load()
    assert mod._resolve_real() is None
    msg = mod._REMEDIATION.format(paths=" or ".join(mod._REAL_BASES))
    for base in mod._REAL_BASES:
        assert base in msg
