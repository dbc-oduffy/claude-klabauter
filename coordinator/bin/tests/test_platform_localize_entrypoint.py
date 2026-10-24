"""platform-localize.py entrypoint: --help never runs the localizer; argv and exit codes pass through."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "platform-localize.py"


def _load():
    spec = importlib.util.spec_from_file_location("platform_localize_entrypoint", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.spawns_process
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_exits_zero_and_leaves_claude_home_untouched(flag, tmp_path):
    env = {"CLAUDE_HOME": str(tmp_path), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    full_env = {**os.environ, **env}
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), flag],
        capture_output=True, text=True, env=full_env, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout
    assert list(tmp_path.iterdir()) == []


def test_argv_passes_through_verbatim_and_return_code_propagates(monkeypatch):
    mod = _load()
    seen = []

    def fake_main(argv):
        seen.append(argv)
        return 7

    monkeypatch.setattr(mod, "_import_main", lambda: fake_main)
    assert mod.main(["--x", "y"]) == 7
    assert seen == [["--x", "y"]]


def test_import_failure_returns_three(monkeypatch, capsys):
    mod = _load()

    def boom():
        raise ImportError("nope")

    monkeypatch.setattr(mod, "_import_main", boom)
    assert mod.main([]) == 3
    assert "platform_localize: error (see stderr)" in capsys.readouterr().out
