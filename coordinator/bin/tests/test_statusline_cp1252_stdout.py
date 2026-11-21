"""Both statusline scripts exit 0 and emit utf-8 on a cp1252 console (F23); callable as a fresh-box probe."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BIN = Path(__file__).resolve().parents[1]


def _run(script: str, payload: dict, tmp_path: Path, env_extra: dict | None = None):
    env = dict(os.environ)
    env.update(
        PYTHONIOENCODING="cp1252",
        COORDINATOR_SETTINGS_HOME=str(tmp_path),
        NO_COLOR="1",
    )
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(BIN / script)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_statusline_cp1252_emits_utf8(tmp_path):
    payload = {
        "session_id": "cp1252-probe",
        "model": {"display_name": "Opus"},
        "context_window": {"used_percentage": 10},
        "workspace": {"project_dir": str(tmp_path / "proj")},
    }
    result = _run("statusline.py", payload, tmp_path)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"UnicodeEncodeError" not in result.stderr
    out = result.stdout.decode("utf-8")
    assert "▰" in out or "▱" in out  # gauge glyph a cp1252 stdout cannot encode


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_subagent_statusline_cp1252_emits_utf8(tmp_path):
    payload = {
        "tasks": [{"id": "t1", "name": "worker", "description": "résumé — → done", "status": "running"}],
        "columns": 200,
    }
    result = _run("subagent-statusline.py", payload, tmp_path)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"UnicodeEncodeError" not in result.stderr
    row = json.loads(result.stdout.decode("utf-8").splitlines()[0])
    assert row["id"] == "t1"
    assert "→" in row["content"]
