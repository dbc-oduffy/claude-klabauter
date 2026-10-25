"""Tests for coordinator/bin/coordinator-settings-home.py (door-resolvable entrypoint)."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "coordinator-settings-home.py"


def _load():
    spec = importlib.util.spec_from_file_location("coordinator_settings_home_entry", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def entry():
    return _load()


@pytest.fixture
def homes(tmp_path, monkeypatch):
    claude_home = tmp_path / "home"
    settings = tmp_path / "settings"
    claude_home.mkdir()
    settings.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    legacy = claude_home / ".claude" / "machine-local"
    new = settings / "machine-local"
    return claude_home, settings, legacy, new


def test_no_argument_prints_settings_home(entry, homes, capsys):
    _, settings, _, _ = homes
    from coordinator_core import _settings_home

    assert entry.main([]) == 0
    out = capsys.readouterr().out
    assert out == str(_settings_home.settings_home()) + "\n"
    assert out.strip() == str(settings)


def test_check_ok_when_a_home_is_absent_or_empty(entry, homes):
    _, _, legacy, new = homes
    assert entry.main(["check"]) == 0
    legacy.mkdir(parents=True)
    (legacy / "k").write_text("v")
    assert entry.main(["check"]) == 0
    new.mkdir()
    assert entry.main(["check"]) == 0


def test_divergent_homes_fail_with_message(entry, homes, capsys):
    _, _, legacy, new = homes
    legacy.mkdir(parents=True)
    new.mkdir()
    (legacy / "a").write_text("1")
    (new / "b").write_text("2")
    assert entry.main(["check"]) == 1
    err = capsys.readouterr().err
    assert entry.DIVERGENCE_MSG in err
    assert "Legacy realpath" in err
    assert entry.main([]) == 1
    assert capsys.readouterr().out == ""


def test_symlinked_pair_is_not_divergence(entry, homes):
    _, _, legacy, new = homes
    new.mkdir()
    (new / "a").write_text("1")
    legacy.parent.mkdir(parents=True)
    try:
        legacy.symlink_to(new, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert entry.main(["check"]) == 0


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_returns_zero(entry, flag, capsys):
    assert entry.main([flag]) == 0
    assert "Usage: coordinator-settings-home" in capsys.readouterr().out


def test_unknown_subcommand_exits_2(entry, capsys):
    assert entry.main(["bogus"]) == 2
    err = capsys.readouterr().err
    assert "unknown subcommand: bogus" in err
    assert "Usage:" in err


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_subprocess_runs_with_scrubbed_pythonpath(tmp_path):
    settings = tmp_path / "settings"
    settings.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["COORDINATOR_SETTINGS_HOME"] = str(settings)
    env["CLAUDE_HOME"] = str(tmp_path / "home")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(settings)
