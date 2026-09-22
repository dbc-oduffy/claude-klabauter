"""
coordinator_core.ops.workflow_fire.tests.test_fire_env_settings_bin --
acceptance surface for ``build_fire_env``'s settings-home PATH fix
(claude-klabauter#45 class D).

Purpose: an emitted executor invokes settings-home launchers (e.g.
``cross-repo-memo``) by bare name from its row body. The launcher lives at
``$COORDINATOR_SETTINGS_HOME/bin/<name>`` and runs fine by absolute path,
but the fired driver's session environment -- which every ``agent()``
subagent it spawns inherits -- never carried that directory on PATH, so
the bare-name invocation failed with "command not found" (exit 127) before
ever reaching the launcher. This pins ``build_fire_env`` prepending
``<settings-home>/bin`` onto PATH, handling PATH absent/empty, never
duplicating an already-present entry, and never spawning a subprocess to
do it.

Spec backlink: claude-klabauter#45 class D
Spec backlink: docs/plans/2026-08-18-claude-klabauter-fires-the-workflows-it-emits.md
§ C4 (``build_fire_env`` itself)

Negative-spec:
  - Does NOT spawn any subprocess -- ``settings_home()`` is a pure env/home
    read (brightline: no new process spawn on this path).
  - Does NOT re-derive the settings-home path by a hand-rolled probe --
    resolved solely via ``coordinator_core._settings_home.settings_home()``.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.workflow_fire import fire

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _settings_bin(monkeypatch, tmp_path) -> str:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    return str(tmp_path / "settings-home" / "bin")


def test_settings_bin_prepended_onto_existing_path(monkeypatch, tmp_path):
    settings_bin = _settings_bin(monkeypatch, tmp_path)

    env = fire.build_fire_env({"PATH": "/usr/bin"})

    assert env["PATH"] == os.pathsep.join([settings_bin, "/usr/bin"])


def test_settings_bin_added_when_path_absent(monkeypatch, tmp_path):
    settings_bin = _settings_bin(monkeypatch, tmp_path)

    env = fire.build_fire_env({})

    assert env["PATH"] == settings_bin


def test_settings_bin_added_when_path_empty(monkeypatch, tmp_path):
    settings_bin = _settings_bin(monkeypatch, tmp_path)

    env = fire.build_fire_env({"PATH": ""})

    assert env["PATH"] == settings_bin


def test_settings_bin_not_duplicated_when_already_present(monkeypatch, tmp_path):
    settings_bin = _settings_bin(monkeypatch, tmp_path)

    env = fire.build_fire_env({"PATH": os.pathsep.join(["/usr/bin", settings_bin])})

    assert env["PATH"] == os.pathsep.join(["/usr/bin", settings_bin])


def test_settings_bin_does_not_spawn_a_subprocess(monkeypatch, tmp_path):
    _settings_bin(monkeypatch, tmp_path)

    def _boom(*_args, **_kwargs):
        raise AssertionError("build_fire_env must not spawn a subprocess")

    monkeypatch.setattr(fire.subprocess, "run", _boom)
    monkeypatch.setattr(fire.subprocess, "Popen", _boom)

    fire.build_fire_env({"PATH": "/usr/bin"})
