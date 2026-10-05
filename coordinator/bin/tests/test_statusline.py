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
