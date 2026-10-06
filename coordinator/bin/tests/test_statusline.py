"""Smoke tests for this tree's copy of `statusline.py`, run as a real process.

The behavioural suite lives beside the source in coordinator-content-repo
(`coordinator/tests/test_statusline_*.py`); this copy is gated byte-identical to it at publish.
These pin what a consumer install depends on: the inner statusline is read from the durable
settings home, never from inside the plugin root, and the visible line carries effort and the
context gauge.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SCRIPT = Path(__file__).resolve().parents[1] / "statusline.py"


def _run(stdin: bytes, settings_home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COORDINATOR_SETTINGS_HOME"] = str(settings_home)
    env["NO_COLOR"] = "1"
    env["CLAUDE_CONFIG_DIR"] = str(settings_home / "no-claude-config")
    env.pop("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", None)
    env.pop("COORDINATOR_STATUSLINE_GAUGE", None)
    return subprocess.run([sys.executable, str(_SCRIPT), *args], input=stdin,
                          capture_output=True, env=env, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _payload(session_id: str = "sl-smoke") -> bytes:
    return json.dumps({
        "session_id": session_id,
        "model": {"id": "claude-opus-5", "display_name": "Opus 5"},
        "effort": {"level": "high"},
        "context_window": {"used_percentage": 10, "context_window_size": 200000},
    }).encode("utf-8")


def test_own_line_renders_model_effort_and_gauge(tmp_path):
    result = _run(_payload(), tmp_path)
    assert result.returncode == 0
    line = result.stdout.decode("utf-8")
    assert "Opus 5" in line and "high" in line and "%" in line


def test_sidecar_lands_under_the_settings_home(tmp_path):
    _run(_payload("sl-sidecar"), tmp_path)
    record = json.loads((tmp_path / "state" / "context-window" / "sl-sidecar.json").read_text())
    assert record["context_window"]["used_percentage"] == 10


def test_inner_command_is_read_from_the_settings_home(tmp_path):
    (tmp_path / "statusline-inner.json").write_text(json.dumps(
        {"type": "command", "command": f'"{sys.executable}" -c "print(\'inner-ran\')"'}))
    result = _run(_payload(), tmp_path)
    assert result.stdout.decode("utf-8").strip() == "inner-ran"


def test_selftest_reports_the_sidecar_path(tmp_path):
    result = _run(b"", tmp_path, "--selftest")
    assert result.returncode == 0
    assert str(tmp_path / "state" / "context-window") in result.stdout.decode("utf-8")


def _load_statusline():
    import importlib.util
    spec = importlib.util.spec_from_file_location("statusline_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("command", [
    "mystatus | head -1", "a && b", "a; b", "a > out.txt", "echo `id`",
])
def test_a_command_needing_a_shell_is_not_delegated(command):
    statusline = _load_statusline()
    with pytest.raises(OSError):
        statusline._argv_for(command)


def test_shell_characters_inside_quotes_stay_an_argument():
    statusline = _load_statusline()
    argv = statusline._argv_for(f'"{sys.executable}" -c "print(\'a|b\')"')
    assert argv[1:] == ["-c", "print('a|b')"]
    assert os.path.samefile(argv[0], sys.executable)


def test_an_unresolvable_executable_is_not_delegated():
    statusline = _load_statusline()
    with pytest.raises(OSError):
        statusline._argv_for("no-such-statusline-binary-xyz --flag")


def test_an_embedded_quoted_value_stays_one_argument():
    statusline = _load_statusline()
    path = r"C:\Users\o'neil\x.py" if os.name == "nt" else "o'neil/x.py"
    argv = statusline._argv_for(f'"{sys.executable}" --arg="a b" "{path}"')
    assert argv[1:] == ["--arg=a b", path]


@pytest.mark.skipif(os.name != "nt", reason="batch files are a Windows CreateProcess shape")
def test_a_batch_file_executable_is_not_delegated(tmp_path):
    shim = tmp_path / "status.cmd"
    shim.write_text("@echo off\r\necho hi\r\n")
    statusline = _load_statusline()
    with pytest.raises(OSError):
        statusline._argv_for(f'"{shim}"')
